"""Functional tests for the device watchdog blueprint.

These run the blueprint inside a real (test) Home Assistant instance: the
blueprint file is copied into the test config directory, an automation is
created from it, and the watched entity is driven through the states that
should — and should not — cause a power cycle.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.components import automation
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

BLUEPRINT = Path(__file__).parent.parent / "blueprints" / "automation" / "brawnybravo" / "device_watchdog_power_cycle.yaml"

WATCHED = "media_player.test_display"
PLUG = "switch.test_plug"

UNAVAILABLE_FOR = {"minutes": 5}
POWER_OFF_FOR = {"seconds": 15}
RECOVERY_GRACE = {"minutes": 2}


async def _setup(hass: HomeAssistant, watched_state: str | None = "playing", **extra_inputs):
    """Install the blueprint and create one automation from it."""
    dest = Path(hass.config.path("blueprints/automation/brawnybravo"))
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(BLUEPRINT, dest / BLUEPRINT.name)

    if watched_state is not None:
        hass.states.async_set(WATCHED, watched_state)
    hass.states.async_set(PLUG, "on")

    turn_off = async_mock_service(hass, "switch", "turn_off")
    turn_on = async_mock_service(hass, "switch", "turn_on")

    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: {
                "alias": "watchdog under test",
                "use_blueprint": {
                    "path": "brawnybravo/device_watchdog_power_cycle.yaml",
                    "input": {
                        "watched_entity": WATCHED,
                        "power_switch": PLUG,
                        "unavailable_for": UNAVAILABLE_FOR,
                        "power_off_duration": POWER_OFF_FOR,
                        "recovery_grace": RECOVERY_GRACE,
                        "max_cycles": 2,
                        "cooldown_window": {"hours": 1},
                        **extra_inputs,
                    },
                },
            }
        },
    )
    await hass.async_block_till_done()
    return turn_off, turn_on


async def _advance(hass: HomeAssistant, freezer, **kwargs):
    """Move the clock forward without waiting for real time.

    The blueprint's grace period, power-off hold and recovery wait are all
    real durations; without a frozen clock the tests would sit through them.
    """
    freezer.move_to(dt_util.utcnow() + timedelta(**kwargs))
    async_fire_time_changed(hass)
    # Deliberately not async_block_till_done(): while the blueprint is inside
    # its power-off delay the automation task is still pending, and waiting for
    # it to finish would mean waiting out the delay in real time. Yielding to
    # the loop lets the timers that just fired run, and the next _advance()
    # expires the delay.
    for _ in range(10):
        await asyncio.sleep(0)


async def test_power_is_cycled_after_the_grace_period(hass: HomeAssistant, freezer):
    """The plug is switched off and back on once the device stays unavailable."""
    turn_off, turn_on = await _setup(hass)

    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert turn_off == [], "must not act before the grace period elapses"

    await _advance(hass, freezer, minutes=6)
    assert len(turn_off) == 1
    assert turn_off[0].data["entity_id"] == [PLUG]

    # Power is held off, then restored.
    await _advance(hass, freezer, seconds=20)
    assert len(turn_on) == 1
    assert turn_on[0].data["entity_id"] == [PLUG]


async def test_a_device_that_recovers_in_time_is_left_alone(hass: HomeAssistant, freezer):
    """No power cycle if the entity comes back before the grace period ends."""
    turn_off, _turn_on = await _setup(hass)

    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=2)
    hass.states.async_set(WATCHED, "playing")
    await _advance(hass, freezer, minutes=10)

    assert turn_off == []


@pytest.mark.parametrize("recovered_state", ["playing", "idle"])
async def test_no_cycle_when_the_device_recovers_between_trigger_and_action(
    hass: HomeAssistant, freezer, recovered_state: str
):
    """The live re-check must stop a cycle for a device that just came back.

    The trigger fires on the "unavailable for 5 minutes" edge; if the device
    recovers in the moment before the automation runs, cutting its power would
    be exactly wrong.
    """
    turn_off, _turn_on = await _setup(hass)

    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=6)
    assert len(turn_off) == 1  # first cycle happens

    # Device recovers, drops out again, and recovers once more before the next
    # grace period would expire.
    hass.states.async_set(WATCHED, recovered_state)
    await _advance(hass, freezer, minutes=1)
    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=4)
    hass.states.async_set(WATCHED, recovered_state)
    await _advance(hass, freezer, minutes=5)

    assert len(turn_off) == 1, "second cycle must not fire for a recovered device"


async def test_rate_limit_blocks_a_rapid_second_cycle(hass: HomeAssistant, freezer):
    """With 2 cycles per hour configured, cycles must be at least 30 min apart."""
    turn_off, _turn_on = await _setup(hass)

    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=6)
    assert len(turn_off) == 1

    # Still dead: flap it so the trigger can fire again, well inside the gap.
    hass.states.async_set(WATCHED, "playing")
    await _advance(hass, freezer, seconds=30)
    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=6)

    assert len(turn_off) == 1, "a second cycle inside the minimum gap must be suppressed"


# --------------------------------------------------------------------------- busy guard

ROBOT = "vacuum.test_robot"
STATUS = "sensor.test_robot_status"
OTHER_NOTIFY = [{"action": "test.other_notify", "data": {"outcome": "{{ watchdog_outcome }}"}}]


async def test_no_power_cut_when_last_seen_mid_cycle(hass: HomeAssistant, freezer):
    """A robot that dropped out while cleaning must never have its power cut."""
    other = async_mock_service(hass, "test", "other_notify")
    turn_off, _turn_on = await _setup(hass, watched_state="cleaning", other_notify_action=OTHER_NOTIFY)

    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=6)
    await _advance(hass, freezer, minutes=1)

    assert turn_off == []
    assert [c.data["outcome"] for c in other] == ["skipped_busy"]


async def test_busy_state_survives_an_unavailable_to_unknown_flip(hass: HomeAssistant, freezer):
    """cleaning -> unavailable -> unknown must still be treated as mid-cycle."""
    turn_off, _turn_on = await _setup(hass, watched_state="cleaning")

    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=2)
    hass.states.async_set(WATCHED, "unknown")
    await _advance(hass, freezer, minutes=6)
    await _advance(hass, freezer, minutes=6)

    assert turn_off == []


async def test_down_state_flip_does_not_restart_the_grace_period(hass: HomeAssistant, freezer):
    """A display that flips unavailable -> unknown is still cycled on the first timer."""
    turn_off, _turn_on = await _setup(hass)

    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=3)
    hass.states.async_set(WATCHED, "unknown")
    await _advance(hass, freezer, minutes=3)

    assert len(turn_off) == 1


async def test_busy_match_ignores_case(hass: HomeAssistant, freezer):
    turn_off, _turn_on = await _setup(hass, watched_state="Cleaning")
    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=6)
    assert turn_off == []


async def test_busy_entity_blocks_the_cycle(hass: HomeAssistant, freezer):
    """A separate status sensor saying "cycling" also stops the power cut."""
    hass.states.async_set(STATUS, "cycling")
    turn_off, _turn_on = await _setup(hass, busy_entities=[STATUS])

    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=6)
    assert turn_off == []


async def test_unavailable_busy_entity_does_not_block(hass: HomeAssistant, freezer):
    """A status sensor that dropped out with the device counts as not busy."""
    hass.states.async_set(STATUS, STATE_UNAVAILABLE)
    turn_off, _turn_on = await _setup(hass, busy_entities=[STATUS])

    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=6)
    assert len(turn_off) == 1


async def test_custom_busy_states(hass: HomeAssistant, freezer):
    """The busy list is the user's: a state they add blocks, a default they drop does not."""
    turn_off, _turn_on = await _setup(hass, watched_state="cleaning", busy_states=["printing"])
    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=6)
    assert len(turn_off) == 1


# --------------------------------------------------------------------------- gentler step

GENTLE = [{"action": "test.gentle_step", "data": {}}]


async def test_gentle_step_that_works_means_no_power_cut(hass: HomeAssistant, freezer):
    gentle = async_mock_service(hass, "test", "gentle_step")
    other = async_mock_service(hass, "test", "other_notify")
    turn_off, _turn_on = await _setup(
        hass,
        watched_state="docked",
        gentle_action=GENTLE,
        gentle_wait={"minutes": 2},
        other_notify_action=OTHER_NOTIFY,
    )

    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=6)
    assert len(gentle) == 1
    assert turn_off == [], "power must not be cut while the gentler step is being given time"

    hass.states.async_set(WATCHED, "docked")
    await _advance(hass, freezer, seconds=5)
    await _advance(hass, freezer, minutes=5)

    assert turn_off == []
    assert [c.data["outcome"] for c in other] == ["recovered_by_gentle_step"]


async def test_gentle_step_that_fails_falls_through_to_power_cycle(hass: HomeAssistant, freezer):
    gentle = async_mock_service(hass, "test", "gentle_step")
    turn_off, turn_on = await _setup(
        hass, watched_state="docked", gentle_action=GENTLE, gentle_wait={"minutes": 2}
    )

    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=6)
    assert len(gentle) == 1
    assert turn_off == []

    await _advance(hass, freezer, minutes=3)
    assert len(turn_off) == 1
    await _advance(hass, freezer, seconds=20)
    assert len(turn_on) == 1


async def test_busy_after_gentle_step_still_blocks(hass: HomeAssistant, freezer):
    """If the gentler step wakes a status sensor that now says busy, leave the power on."""
    async_mock_service(hass, "test", "gentle_step")
    hass.states.async_set(STATUS, "docked")
    turn_off, _turn_on = await _setup(
        hass,
        watched_state="docked",
        busy_entities=[STATUS],
        gentle_action=GENTLE,
        gentle_wait={"minutes": 2},
    )

    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)
    await _advance(hass, freezer, minutes=6)
    hass.states.async_set(STATUS, "emptying")
    await _advance(hass, freezer, minutes=3)
    await _advance(hass, freezer, minutes=1)

    assert turn_off == []


# --------------------------------------------------------------------------- restarts


async def test_device_already_down_at_startup_is_cycled(hass: HomeAssistant, freezer):
    """A device that comes up dead after a restart still gets its power cycle."""
    turn_off, _turn_on = await _setup(hass, watched_state=None)
    hass.states.async_set(WATCHED, STATE_UNAVAILABLE)  # late integration, already dead
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await _advance(hass, freezer, minutes=2)
    assert turn_off == [], "must wait out the grace period after a restart"

    await _advance(hass, freezer, minutes=4)
    await _advance(hass, freezer, seconds=5)
    assert len(turn_off) == 1


async def test_device_that_recovers_after_startup_is_left_alone(hass: HomeAssistant, freezer):
    turn_off, _turn_on = await _setup(hass, watched_state=STATE_UNAVAILABLE)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await _advance(hass, freezer, minutes=2)
    hass.states.async_set(WATCHED, "playing")
    await _advance(hass, freezer, minutes=10)
    assert turn_off == []


async def test_missing_entity_at_startup_never_cuts_power(hass: HomeAssistant, freezer):
    """A removed entity reads as unknown; that must not power-cycle anything."""
    turn_off, _turn_on = await _setup(hass, watched_state=None)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await _advance(hass, freezer, minutes=6)
    await _advance(hass, freezer, minutes=6)
    assert turn_off == []
