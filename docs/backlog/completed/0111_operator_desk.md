# agora-0111 — operator desk (everything blocked on the human)

- **Origin**: 10h audit rec 1 (both passes' top item), operator ruling
  dm:agora--laurent#42: "discuss and implement with @continuum."
- **Owners**: agora (hub data contract), continuum (the rendered
  surface).

## Problem

The operator is the busiest seat and the only one with no queue. Three
independent multi-hour stalls last night were all "waiting on laurent"
with no surface showing it: a 30-second PyPI click buried in a DM
paragraph (7h), entity frozen awaiting a ruling that never came
(cognition#187), a night-voice question dead inside a 4-question wall
(8h+).

## Design (hub-data half — agora), sharpened by the c3860 staleness review

The desk is a DERIVATION, like the board (0070) — STATE not log, no
cursor, computed at read time (M1). `GET /desk` (operator or
reporting-delegate viewer) returns `{computed_at, rows, satisfied}`:
- open/blocked messages addressed to an operator (same predicates
  `owed()` runs: `to`, assignee, pending per-ask `to`), with age;
- `queue:<operator>:*` rows, each optionally carrying a machine-checkable
  `done_when` predicate (M3) from a CLOSED vocabulary —
  `{kind: retired|decision|work_status|delegation|closed, ...}` —
  validated at store_set write time and EVALUATED at desk read time:
  satisfied rows move to `satisfied` ("this wait is over — close the
  row"), never auto-deleted (the author closes; history stays);
- watchdog escalations needing a human (routed in by 0107).
Each row: what, who waits, age, one-line what-is-needed.

The 0109 hourly digest becomes a REPLY to the hub's own desk facts post
(hub posts `data.desk` + plain-register render; the delegate annotates in
prose) — a stale prose line then sits NEXT TO its own refutation, and the
missed-report check counts only replies to the current desk post.

Trigger-incident proof: `{kind: "retired", agent: "agency"}` would have
self-cleared at 18:28; the digest could not have carried the row at
23:37/00:45.

## Coordination (continuum half — the surface)

continuum renders it in the console (a right-edge Desk, like the
Leaderboard) and folds it into every operator digest. Contract to agree
with continuum: endpoint shape (`GET /desk` vs extending `/board` with a
`blocked_on_operator` section), and the `blocked_on=operator` marker
seats set. Discuss in dm:agora--continuum before either side builds.

## Note

This is the surface half of the same problem 0109 (mandatory digest)
and 0107 (alert routing) attack from the timer and routing sides — all
three converge on "the human must never have to poll to find what waits
on them."
