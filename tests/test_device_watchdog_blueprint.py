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
from homeassistant.const import STATE_UNAVAILABLE
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


async def _setup(hass: HomeAssistant):
    """Install the blueprint and create one automation from it."""
    dest = Path(hass.config.path("blueprints/automation/brawnybravo"))
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(BLUEPRINT, dest / BLUEPRINT.name)

    hass.states.async_set(WATCHED, "playing")
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
