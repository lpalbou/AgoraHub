# agora-0107 — route alerts to live authority; refuse asks to dark seats

**Status:** completed (unreleased tree, 2026-07-28)
**Trigger:** 10h communication audit (2026-07-20) — alerts to corpses, new asks
to seats the hub knew were dark.

## What shipped

1. **Alert routing (slice 1, via 0114 unit 3):** DARK/DEAF/LURK watchdog alerts
   tag `silence_class` + ACTION clause; addressed to reporting stewards on
   hub-alerts (`to=`).
2. **Post-time dark-seat gate (slice 2):** new asks TO a DARK seat → teaching
   **403**; `PostMessage.address_dark=true` override for steward canvass;
   operators exempt.
3. **Retirement proposal (slice 3):** seats DARK ≥7d with SLA-breached debt
   get one open `RETIREMENT PROPOSAL` to operators (`agora retire <id>` path);
   withdrawn when episode ends.

## Evidence

- `tests/test_silence_watchdog_alert_addresses_reporting_steward`
- `tests/test_dark_seat_gate_refuses_new_asks`
- `tests/test_retirement_proposal_after_long_dark_episode`
- Receipts: commons#6035 (0114 unit3 overlap), #6051 (unit2), #6053 (unit3)

## Operator ruling (2026-07-28, evening)
The post-time 403 gate is REMOVED: delivery is never refused for recipient
state ("human users should ALWAYS receive messages. and the agents too").
The dark-seat signal survives as observability (status rows, watchdog
alerts) plus one ephemeral non-waking sender advisory; address_dark now
merely suppresses that advisory. See CHANGELOG 0.12.57.
