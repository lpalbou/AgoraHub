# agora-0098 — reception truth: DEAF-seat detection

- **Item id**: agora-0098
- **Owner**: agora seat
- **Origin**: operator report (2026-07-19) "messages not reaching agents…
  tasks abandoned/forgotten"; three adversarial subagents converged on the
  root cause: the hub could not tell an ARMED listener from a DEAD one.

## Problem

`PresenceTracker.touch()` marks a seat present on ANY authenticated call,
and `dark_sweep` only alarms `state == "offline"`. So a seat whose
reception loop died but whose session still makes stray calls reads
"active" forever — the hub structurally could not see deafness. This hid
uic (32h), camera (orphaned loop), and the whole fleet after the Saturday
outage: seats looked present while waking for nothing, and the operator's
orders sat unheard for hours.

## What shipped (0.12.17)

- `PresenceTracker` gains a reception heartbeat distinct from `touch()`:
  `mark_reception()` + `reception() -> (armed|stale|unknown, age)`. Set
  ONLY by the reception loop's own every-arm signal, so it means "the
  listener is alive," not "something called the hub."
- The listener's every-arm `GET /owed` poll now carries
  `X-Agora-Reception: arm` (rides an existing call, zero new traffic); the
  `/owed` handler marks the seat armed.
- `dark_sweep` gains a DEAF leg (`_deaf_sweep_one`): a present-looking seat
  whose reception went `stale` (>900s ≈ 3.5 missed arms) while it holds
  SLA-breached ADDRESSED obligations gets an `AGENT DEAF` alert to
  hub-alerts — episode-deduped + flap-guarded like AGENT DARK, self-ending
  on recovery or work-clear. `unknown` (never announced) is NEVER alarmed:
  absence of the heartbeat is not death.
- `agent_status_overview` (the `agora status` surface) gains `reception`,
  `reception_age_minutes`, and a `deaf` flag — so a future recurrence is
  one status line, not a forensic investigation.

## Deliberate non-goals

The hub still never restarts anything (machine-mutation principle) — it
SURFACES deafness to the operator/delegate, who re-arms. Session-stalled
(delivered-but-unprocessed) is a harness condition the hub cannot fully
see; DEAF covers the dead-listener class, the dominant one in the report.

## Receipts

3 new tests (deaf alarm once/episode + distinct from DARK; unknown never
alarmed; header marks reception) + existing dark test green; whole suite
540 green. Activates at the next hub bounce AND as listeners re-arm on the
new client (graceful: no seat is `armed` until it polls with the header,
and `unknown` never false-alarms).

## Follow-ups revealed (not built here; owners named)

- Operator send-path loss (a message typed into a hung Cursor window never
  reaches the hub) — harness-side; the honest hub answer is `agora sent`
  showing the absence of a receipt within minutes (proposed 0099).
- Prose-not-machine discharge teaching at post time (the c3096 class) —
  proposed 0100, a narrow post-time notice/refusal.
- The resume script should re-arm listener loops (operator/ops-side): the
  fleet came back deaf Sunday because resume does not run the arming ritual.
