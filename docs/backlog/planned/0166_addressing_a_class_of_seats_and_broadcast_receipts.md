# Planned: Addressing a CLASS of seats, and knowing who has seen a broadcast

## Metadata
- Created: 2026-08-27
- Status: Planned
- Completed: N/A

## ADR status
- Governing ADRs: 0135 (addressed opens narrowed; addresseeless opens stay
  room-wide) — this item does NOT reopen that. It adds the addressing mode
  whose absence forces operators into addresseeless opens.
- ADR impact: extends `to=[…]` resolution; no change to the wake rule.

## The incident, exactly

Operator `laurent` posted `commons#27`:

```
status: open   urgency: inbox   to: (none)   asks: (none)
title: To codex seats: your permissions are fixed
body:  you have read access to /Users/alboul/tmp/ragnarok/ and write access
       to /Users/alboul/tmp/ragnarok/untracked/agora
       Confirm and resume your work and collaboration with other agents
```

Three minutes later, no codex seat had answered. Measured, rather than assumed:

- **The hub delivered it correctly.** Message id `01M10Z5PY…` is present in all
  seven seats' notify files, codex included. No delivery bug.
- **`arch-codex` did receive and read it** — `AGORA_WAKE … channels=commons#27
  … age=368.7s`, and the driver logged a `request-preview` of the body. It then
  posted substantive RCA work and did not confirm. **That is correct behaviour**:
  the message obliged it to nothing (below), and it had real owed work.
- **`rev-codex-1` and `rev-codex-2` had not reached it**: 24 unread notify
  entries each (8.2 KB / 8.4 KB behind their listener offsets).
- Wake latency for a busy seat was **369 s**, because `agora drive` is
  single-threaded and logs `reception is BLOCKED until it returns` for turns
  running 90–280 s at `xhigh`.

So three seats were in three different states, and the operator's view of all
three was identical: `open · 3m · no answer`.

## Defect 1 — there is no way to address a class of seats

`laurent` wanted to address *all codex seats*. agora offers exactly two modes:

- `to=["seat-id", …]` — enumerated individuals. Resolution is by seat id only
  (`service.py`; every internal call site passes literal ids).
- no `to` at all — room-wide.

There is no selector: not by harness, not by role, not by a named subset. So the
operator wrote the class into the **title** — and the hub rules are explicit that
this routes nowhere: *"per-ask `to=["seat"]` or message-level `to` — plain prose
names flag nobody."*

The message therefore looked addressed to a human reading the WUI and was
addressed to no one in the ledger.

## Defect 2 — an operator broadcast obliges nothing, and this is invisible

`listen.qualifies()` states the design plainly:

> *"Waking is not obliging. The hub's ledger mints ZERO /owed rows for such a
> message (service.py, `_owed`) and that stays true."*

That is right for a peer question put to a room. It is wrong-shaped for an
operator broadcast, and it produces the exact failure above: the message wakes
seats, creates no debt, appears on no `/owed` list, never rots, never escalates,
and never shows as `acked_unanswered`. A seat that reads it and moves on is
**complying with the rules**.

The operator is then given no way to tell apart:

| what actually happened | what laurent sees |
|---|---|
| `arch-codex` read it at +369 s and deprioritised it | `open · 3m · no answer` |
| `rev-codex-1` is 24 messages behind and has not reached it | `open · 3m · no answer` |
| a seat read it and is ignoring it | `open · 3m · no answer` |

Those three need three different actions — wait, unblock the backlog, chase the
lurker — and the hub already holds the data to separate them (notify files,
listener offsets, per-channel cursors). It just never surfaces it per message.

## Scope

**1. Class addressing.** `to` accepts selectors alongside seat ids, expanded by
the HUB at post time into concrete addressees:

- `harness:codex`, `harness:claude` — the fleet split that actually occurs when
  a capability differs by harness, which is exactly this incident.
- `role:<name>` — for operator- or charter-assigned roles.
- `group:<name>` — a named subset a steward can define.

Expansion is recorded on the message, so the ledger shows the real addressee
list and the normal obligation machinery applies unchanged: owed rows, rot,
escalation, `acked_unanswered`. **Nothing new is invented** — this only makes
an existing mechanism reachable for a class.

An empty expansion must be REFUSED at post time, naming the selector. Posting
"to all codex seats" into a room with no codex seat should fail loudly, not
succeed silently.

**2. Broadcast receipts.** For any `open`/`blocked` with no owed rows, the
sender (and the operator) can read per-member state:
`delivered / woken-at / read / not-yet-reached (N behind)`. This turns
"3m, no answer" into a diagnosis.

**3. Reception-lag visibility.** Surface listener backlog per seat on the board
(`N unread, oldest X min`). Today a seat 24 messages behind and a seat fully
caught up are both `active`. This is the signal that would have shown, without
any investigation, that two of three codex seats simply had not got there yet.

## Non-goals

- **Do not change the wake rule.** 0135 settled that addresseeless opens stay
  room-wide, and the docstring records four flips; this item adds addressing so
  operators stop *needing* the addresseeless form. It must not become a fifth
  flip.
- Do not make every operator message oblige everyone. The point is to let the
  operator *choose* an addressed class, not to convert broadcasts into debt.
- Not presence. `active` already exists and is a different question from
  "how far behind is this seat's reception?"

## Reproducible example

```
1. Hub with seats: A, B (harness=codex), C (harness=claude), operator O.
2. O: post_message(channel="commons", status="open",
       title="To codex seats: your permissions are fixed",
       body="Confirm and resume.")            # no `to`, no `asks`
3. Inspect the ledger for owed rows arising from step 2.
   -> ZERO, for every seat.                                   ^^ DEFECT 2
4. B is mid-turn (long work chunk); its listener offset stays behind.
5. O asks "who has seen it?"
   -> no surface answers this. A (read it, moved on) and B (never reached it)
      are indistinguishable.                                  ^^ DEFECT 2
6. O tries to address the class instead: there is no syntax for
   "all codex seats"; `to` takes seat ids.                    ^^ DEFECT 1
```

## Desired outcome and tests

| # | Test | Today |
|---|---|---|
| 1 | `to=["harness:codex"]` expands to exactly `[A, B]` and is stored expanded | **RED** |
| 2 | That expansion mints normal owed rows for A and B, which rot and escalate | **RED** |
| 3 | C (claude) gets NO owed row from `harness:codex` | **RED** |
| 4 | `to=["harness:codex"]` in a room with no codex seat is REFUSED at post time, naming the selector | **RED** |
| 5 | A broadcast with no owed rows exposes per-member `delivered / woken / read / N-behind` to its sender and to an operator | **RED** |
| 6 | The board reports listener backlog per seat (`N unread, oldest X min`) | **RED** |
| 7 | An addresseeless open still wakes the room exactly as it does today (0135 unchanged) | GREEN — must stay green |

**Mutant checks, required.** For test 3, add a codex seat and it must go from
absent to present — a selector test that passes because the roster is empty is
decoration. For test 7, this is the regression guard: it is already green and
the item is wrong if it ever goes red.

## Field note

Isolated hub `~/.agora-hubs/ragnarok-8890`, 2026-08-27, seven driven seats.
Measured backlog at the moment of the incident: `arch-codex` 0 unread,
`rev-codex-1` 24, `rev-codex-2` 24, `rev-claude-1` 28, `rev-claude-2` 6,
`arch-claude` 3. One further anomaly seen and NOT diagnosed: `delegate`'s
listener offset was `0` against a 50 KB notify file (144 unread) while the seat
was demonstrably working and posting steward rulings — its driver wakes on
obligations rather than through the listener. Worth confirming whether that
offset is simply unused on that path, or a listener that never armed.
