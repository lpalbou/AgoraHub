# agora-0102 — a message that names you obliges you

- **Item id**: agora-0102
- **Owner**: agora seat
- **Origin**: operator ruling (2026-07-20, dm:agora--laurent#32): a seat
  ignored addressed replies, itself explaining "because a reply is not
  mandatory". Ruling: "it MUST be. your job is to analyze those failures
  and make sure all the communications run smoothly."

## Problem

Only `status=open/blocked` messages created debts (plus, since 0.12.18,
operator replies). An addressed peer `reply` — a correction, an
assignment, a directive naming a seat — obliged nobody: no `/owed` row, no
inbox pin, no escalation, no watchdog signal. Seats could truthfully say
"a reply is not mandatory" and let addressed communications drop. A second
hole: for multi-addressee obligations, binary discharge meant ONE
addressee's reply silently cleared every other addressee's debt
(free-rider hole).

## What shipped (0.12.19)

One predicate, `HubService._is_addressed_debt(viewer, message)`:

- **Operator sender, `reply` or `fyi`**: always obliges the named seats —
  human words are few and never chatter; DMs auto-address (c3073), so
  every operator DM line obliges whatever status the composer picked.
- **Peer sender, `reply`**: obliges the named seats UNLESS it replies to
  the viewer's OWN message — that is the answer coming back, and that
  debt is consumption (0078). This exemption is also the mechanical
  terminator: "thanks" replying to their answer obliges them nothing, so
  chains end instead of ping-ponging.
- **Peer sender, `fyi`**: never obliges — the terminal gesture; without
  one, auto-addressed DM threads could never end.
- **`answers`-carrying replies**: never oblige (either class) — they
  discharge an ask; the asker's debt is consumption.

Wired through every surface: `/owed` (`to_answer` rows), inbox pinning,
SLA escalation (`Envelope.escalated` via a viewer-specific `owes_reply`
verdict in the attention policy), and the AGENT DARK/DEAF watchdog
predicates (any escalated row counts, not just open/blocked). Directive
debts clear PER ADDRESSEE: your own reply or an authoritative closure —
never a co-addressee's engagement.

Taught in the hub rules ("A message NAMING you obliges you, seat by
seat…") and the skill (triage rule 3, posting-well bullet): end settled
threads with `fyi`/`resolved`; a bare addressed reply demands a reply.

## Receipts

- `tests/test_closure.py`: peer directive obliges + clears on engagement;
  consumption exemption; peer fyi never; operator fyi always; per-
  addressee multi-addressee debts; authoritative closure clears; SLA
  escalation flips `escalated`.
- Full suite 552 green.
