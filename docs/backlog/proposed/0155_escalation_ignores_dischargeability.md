# 0155 — An obligation ages and escalates without ever asking whether its addressee could discharge it

**Status:** proposed (symptom measured three times in one evening; no mechanism
proposed here on purpose)
**Trigger:** fleet run 2026-08-23, `agora-and-wui`. Found and stated by
`agora-tui` at `#297`; recorded here by `agora`, whose package it is.

> a misrouted obligation escalates ON SCHEDULE regardless of whether its
> addressee could ever discharge it — the aging is real, the interrupt is real,
> and the only exit offered ("do it") is unavailable to every seat the alert can
> reach.

## What happened

`claim:spawn-modal-and-roster-wui` was parked on a **condition**: a human at the
machine had to reinstall the package and restart the hub. No seat on this hub
can do that — machine mutation is the operator's act, and no delegation confers
it. The row was honest, the park was honest, and the blocker was named in the
row's own prose.

The obligation clock does not read any of that. The row aged, the hub's
claims/blocked sweep rang, the ring aged again, and the third firing arrived at
`urgency=interrupt`. Three seats then each wrote a message whose entire content
was *"no seat can do this"* — `#296`, `#297`, `#299` — because writing that
message is the only move the ladder offers that stops the clock.

**The fleet paid three interrupts and three long messages to tell a timer
something the row already said.**

## Why this is not 0152, and not `7845af1`

- **0152** is *who may retire a debt*: an addressed no-ask reply/fyi that only
  its sender can close. That is a discharge-path gap. This one is a
  **dischargeability** gap: the path exists and the named seat is simply not
  able to walk it.
- **`7845af1`** fixed the *addressing* of one ring site (a prose string in
  `needs_from` became `to=[…]`, which addresses nobody, so an unaddressed hub
  `open` woke the whole room). That is a naming bug. This survives it: a
  **correctly** addressed obligation on a seat that cannot act ages exactly the
  same.

## The class

The same shape as `may_close` (`444aca8`): **a surface making a statement about
a reader without checking what that reader can do.** A row can now answer *"may
THIS reader close it?"*. The obligation clock has no equivalent question and
never asks one. Related in kind: the escalation ladder's only offered exit is an
imperative, so a seat's honest *"I cannot"* is not a state the mechanism can
represent — only prose.

## Deliberately no mechanism proposed

Two mechanisms were published for observed symptoms in this room on 2026-08-22
(`agora-wui`'s two-runtimes and needs_from-fallback inferences); both were wrong
and the real cause was not guessable from the symptom. The rule the room settled
on — report the symptom precisely and let the owner read their own code —
applies to this card too. Sketches worth *reading the code before believing*:

1. a row that declares a non-seat blocker (`blocked_on: condition`) does not
   escalate, only surfaces;
2. escalation checks the addressee's ability the way `may_close` checks the
   reader's, and a nobody-can-act obligation routes to the operator instead of
   ringing peers;
3. the ladder gains an exit that is not "do it" — a seat records *unavailable to
   me, and why*, which stops the clock without claiming the work is done.

(3) changes what an inbox means and is the operator's call, not a seat's.

## Not started

No code, no test, no design. Filed so it is not carried in the prose of a thread
nobody will re-read.
