# Device watchdog - power cycle an unresponsive device

A Home Assistant automation blueprint. It watches an entity, and if that entity stays
`unavailable` (or `unknown`) longer than you allow, it cuts power at a smart plug, waits,
and turns the power back on.

It is meant for the kind of device that works fine for weeks and then wedges itself until
somebody unplugs it: a wall display, a media player, a camera, a small sensor hub.

## What it does

1. The watched entity goes `unavailable` or `unknown` and stays there for the grace period.
2. The automation checks the entity is still down, and that it has not cycled too recently.
3. The power switch is turned off, held off, then turned back on.
4. It waits out a recovery period, then runs your optional notification action.

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
| Notification action (optional) | Anything you want run after the cycle. Empty by default. |

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
- **No verification that the cycle worked.** The automation restores power and waits; it
  does not confirm recovery. The notification action is the place to report, and the cycle
  limit is what stops an unfixable device from being cycled all day.
- **The switch must be independently reachable.** If the plug shares the fate of the device
  (same failed hub, same dead network), nothing here can help.
