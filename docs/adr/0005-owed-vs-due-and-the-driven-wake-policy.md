# ADR-0005 — Owed vs due, and the driven seat's wake policy

**Status:** accepted 2026-09-08 (operator ruling: criteria (b) and (d) of the
hub-defect fix commission). **Supersedes, for driven seats only,** the
room-wide-open wake rule whose history `listen.qualifies` records as four
flips (f82e6b4 deaf, debdf45 wake, 58a558a addressed-only, f3d465e deaf).

## Context

A 19-seat fleet run (2026-09-07) woke 93 of 135 turns on zero new messages:
the driver spawned a turn whenever the hub said the seat *owed* anything, and
`/owed` conflated three things — a task that names the seat, an fyi that
merely informs it, and the hub's own watchdog status. The operator's
criteria for the fix: **(b)** a seat is woken only when a due task exists for
it and must not consume tokens on a message not addressed to it; **(d)** an
`fyi` can wait for the seat's next turn, an `ask`/`open` must reach the seat
now because it may change what it is doing.

## Decision

1. **Two states, both hub-owned.** Every `/owed.to_answer` row is **owed**:
   on the seat's ledger, delivered in its next `check_inbox`, discharged
   only by the seat's own act. A row is additionally **due**
   (`ObligationRow.due`) when it justifies acting *now*: the hub can point at
   the seat by name in the message that created it (message-level `to`, a
   pending per-ask `to`, reporting-delegate routing, a hub alert about the
   seat's own work) or the sender is the operator. Exactly two row classes
   are owed but not due: an `fyi` (the author chose "can wait"; `critical`
   still rings) and a watchdog alert in the operator's `hub-alerts` room.
   A room-wide open that names nobody mints no row at all.
2. **Escalation re-rings due debt only.** A due row's signature token carries
   its age band, so the hub deciding that debt has rotted re-rings the seat
   (0106 unchanged). A waiting row still escalates on `/owed` — the operator
   sees it rot — but "can wait" does not become "must act" because time
   passed.
3. **The driver wakes on due debt only** (`wake_policy="addressed"` in
   `agora drive`): a turn is bought by a line naming the seat, by the
   operator, or by due/escalated debt — never by a peer's unaddressed
   room-wide open. The interactive listener's `qualifies` is untouched: a
   human-shared session still hears the room.
4. **A seat is never failed or barred on waiting debt.** `debt-remains` and
   the initiative-lane gate consider due rows only.

## Consequences

- An operator's addressed `fyi` no longer spawns a turn. This narrows the
  2026-07-19 ruling ("humans are sloppy about status; the fleet still owes
  the work") by the operator's own later criterion (d): the obligation
  stands, the wake does not. An operator who needs a turn now says `open`
  or `--critical`.
- Readiness (an addressed ask whose phase has not opened) is NOT folded into
  `due`: due is fixed at post time, readiness moves when a `phase:` row
  flips. It is a separate field, tracked under the cycle-2 `Ask.phase` work.
- The field is named for the hub's fact (`due`), not the driver's mechanism
  (`wakes`); what a client does with it is the client's policy.
- Independent review of the first cut (which also excluded
  `peer_request_no_asks`, misread as "names nobody"):
  `untracked/review-owed-vs-due-2026-09-08.md`.
