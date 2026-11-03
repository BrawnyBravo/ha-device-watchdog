# Device watchdog - power cycle an unresponsive device

A Home Assistant automation blueprint. It watches an entity, and if that entity stays
`unavailable` (or `unknown`) longer than you allow, it cuts power at a smart plug, waits,
and turns the power back on.

It is meant for the kind of device that works fine for weeks and then wedges itself until
somebody unplugs it: a wall display, a media player, a camera, a small sensor hub.

## What it does

1. The watched entity goes `unavailable` or `unknown` and stays there for the grace period.
2. The automation checks the entity is still down, and that it has not cycled too recently.
3. **Busy guard:** if the device was last seen mid-cycle (its last state before dropping out,
   or an optional busy entity, is in the busy list - `cleaning`, `returning`, `cycling`,
   `washing`, `drying`, `emptying`, `mowing` by default), power is left on and the run ends.
4. **Gentler first step (optional):** your own actions run first - reload the device's
   integration, refresh the entity, send a robot to its base. If the device comes back within
   the wait, power is never cut. The busy guard is checked again afterwards.
5. The power switch is turned off, held off, then turned back on.
6. It waits out a recovery period, then runs your optional notification action.

## Inputs

| Input | What it is |
| --- | --- |
| Entity to watch | The entity that tells you the device is alive. |
| Power switch | The `switch` entity that supplies its power. Must not be the same device. |
| Unavailable for | How long it must stay down before acting. Default 10 minutes. |
| Power off for | How long power stays cut. Default 15 seconds. |
| Recovery grace period | How long to wait after power returns before calling it recovered. Default 3 minutes. |
| Maximum power cycles per window | Cap on cycles inside the cooldown window. Default 3. |
| Cooldown window | The rolling window the cap applies to. Default 1 hour. |
| Never cut power if last seen in these states | The busy list. Matching ignores case. The defaults are not states a display or media player uses. |
| Busy entities (optional) | Extra status entities checked against the busy list. One that is itself unavailable counts as not busy. |
| Gentler first step (optional) | Actions tried before the power cut. If one fails, the run stops without cutting power. |
| Wait after the gentler step | How long the gentler step gets to work. Default 2 minutes. |
| Notification when power was NOT cut (optional) | Runs when the device was busy or the gentler step fixed it; `watchdog_outcome` is `skipped_busy` or `recovered_by_gentle_step`. |
| Notification action (optional) | Anything you want run after the cycle. Empty by default. |

### Robots and appliances

Cutting power to a vacuum, litter box or washer mid-cycle can leave it jammed, wet or
half-emptied. Point the blueprint at the device's own entity (its last state before it
dropped out is what the busy guard reads), add a status sensor as a busy entity if the
integration has one, and use a gentler first step such as:

```yaml
- action: homeassistant.reload_config_entry
  target:
    entity_id: vacuum.example_robot
```

A device that hangs while busy is never power-cycled by this blueprint; use the "power was
NOT cut" notification so you hear about it.

## Installing a blueprint from a URL

In Home Assistant, go to **Settings > Automations & scenes > Blueprints**, choose
**Import blueprint**, and paste the URL of the blueprint file (the GitHub page for the
`.yaml` file works). Confirm the preview, then use **Create automation** to fill in the
inputs above.

You can also drop the `.yaml` file into `config/blueprints/automation/<your folder>/` and
reload automations.

## Limitations

- **The cooldown is approximate.** A blueprint has no storage of its own, so it cannot
  keep a counter between runs. The only history Home Assistant gives an automation about
  itself is the timestamp of its previous run. This blueprint therefore enforces a minimum
  gap between cycles equal to `cooldown window / maximum cycles`, which caps the long-run
  rate rather than the exact count in any one window. Reloading automations, renaming the
  automation or restarting Home Assistant clears that timestamp, after which one extra
  cycle can slip through. If you need a hard count, replace the rate-limit condition with
  a `counter` helper and a scheduled reset.
- **A hung device that still reports as available will not be caught.** Plenty of devices
  keep their connection alive while the thing you actually care about has frozen. The
  trigger only fires on `unavailable`/`unknown`.

  A common fix is to watch a health signal instead of the device's own entity: add a
  `ping` binary sensor pointed at the device's address, or a template binary sensor that
  goes off when a key value stops updating, for example:

  ```yaml
  template:
    - binary_sensor:
        - name: Example device alive
          device_class: connectivity
          state: >-
            {{ now() - states.sensor.example_heartbeat.last_changed
               < timedelta(minutes = 15) }}
  ```

  Then point the blueprint at that sensor - but note that the trigger looks for
  `unavailable`/`unknown`, not `off`, so use an `availability_template` (or a ping sensor,
  which reports unavailable when the host is unreachable) rather than a plain on/off state.
- **The busy guard only knows what Home Assistant last saw.** If the device was mid-cycle but
  had not reported it before it dropped out, or Home Assistant restarted while it was down
  (no previous state), the guard cannot tell. Use a busy entity for anything where a
  mid-cycle power cut would do damage.
- **After a restart** a device that is already down gets the same grace period, then the
  same checks. An entity that no longer exists is never treated as down. Litter boxes and
  some robots report short status codes rather than words; add your device's busy states.
- **No verification that the cycle worked.** The automation restores power and waits; it
  does not confirm recovery. The notification action is the place to report, and the cycle
  limit is what stops an unfixable device from being cycled all day.
- **The switch must be independently reachable.** If the plug shares the fate of the device
  (same failed hub, same dead network), nothing here can help.
