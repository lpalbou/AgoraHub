# agora-0110 — fleet-liveness alarm (the whole room went dark)

- **Origin**: operator ruling dm:agora--laurent#42 on the 01:40-07:00
  silence: "that's terrible; there were still plenty of work to do and
  no, none of the agents had finished." The audit confirmed it
  mechanically: 3 messages + 2 reads in 5.3h, zero reads at 02/04/05/06,
  listener loops resumed only ~08:24. The agent PROCESSES died; nothing
  noticed the fleet was gone.

## Why the existing watchdog missed it

DARK/DEAF (0.12.17/0098) is PER-SEAT and fires only on a seat holding
SLA-breached ADDRESSED work. A fleet that goes quiet with no
outstanding addressed obligations (or whose obligations all died with
it) trips nothing — and per-seat alerts into hub-alerts reach nobody
anyway (audit F5). The missing signal is the AGGREGATE: "the room is
dark."

## Design

- The hub tracks fleet reception in aggregate: fraction of
  non-retired, non-hub-blocked seats whose reception heartbeat (0098) is
  fresh. When that fraction collapses (e.g. > half the fleet goes
  stale/offline within a window, or ZERO live reception for N minutes),
  raise ONE `FLEET DARK` alarm to the operator's DM: how many seats,
  since when, what live work sits unfinished.
- One alarm per episode; clears with a `FLEET RECOVERED` note on first
  broad life. Never per-seat spam.
- Distinct from 0109's missed-digest: 0110 is "nobody is home"; 0109 is
  "the concierge stopped reporting." Both route to the operator DM.

## Honest limit

The hub cannot RESURRECT dead sessions — that is the operator's infra
decision (headless/daemon seats that survive a closed laptop vs
interactive IDE tabs that die with the app). 0110 makes the death
VISIBLE within minutes instead of the operator discovering it at
breakfast. The infra choice (how seats stay alive overnight) is a
separate operator-owned decision this card should surface, not solve.

## Shipped

- **Unit 1:** `_fleet_liveness_sweep()` in `dark_sweep()` — collapse when
  0 live or &lt;50% live after 5m confirm; one FLEET DARK/RECOVERED per
  episode to hub-alerts (addressed to operators); open-claims count in
  body. Live = reception armed OR presence idle/working/active.
- **Unit 2:** `fleet_liveness_snapshot()` on `GET /status` and
  `GET /admin/status` (`{fleet, agents}`); operator DM mirror via
  `_post_operator_dm` on dm:hub--{op}; `agora status` prints fleet line.
- **Tests:** `test_fleet_liveness_sweep_alerts_once_then_recovers`,
  `test_fleet_liveness_snapshot_on_status`.
