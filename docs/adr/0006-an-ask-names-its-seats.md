# ADR-0006 — An ask names its seats; an ask that names nobody obliges nobody

**Status:** accepted 2026-09-09 (operator fix commission, criteria (a)/(b)).
**Supersedes** the reading "an ask addressed to nobody is everyone's
obligation" (`hub/service.py`, the owed loop and `mine_pending`; `drive.py`
`_message_pending_asks`), documented there as the anti-partial-answer-rot
guard.

## Context

Two measured facts from the 2026-09-07 fleet run and the 2026-09-08/09 lab:

- A delegate's `core: …` ×18 fan-out flagged nobody (`@` was the only
  sigil), every ask fell to the message-global pool, and one seat's answer
  discharged the others' — 216 of 453 member messages rebuilt rounds the
  hub had silently discharged.
- **22 of 22 owed-only wakes across two lab runs were one message**: the
  commission, whose single ask had `to: None`. Under "everyone's" it was
  due for every seat, undischargeable by any of them (no seat can answer for
  the others), re-rung on every escalation band, and it barred every seat
  from the initiative lane. It cost the clean Cycle 1 run 3.6× the
  baseline's tokens per minute.

## Decision

1. **An ask is addressed by exactly one of three routes**, materialised at
   post time into an explicit `to` list every consumer reads:
   - given (`asks[].to`, or the CLI's `1@beta:text`);
   - derived from a leading member name in its text (`beta: …`), recorded in
     `to_from_text` — the form a human writes for a fan-out; only the leading
     token counts, and a non-member token (`note:`, `claim:`) derives nobody;
   - **inherited** from the message-level `to` when the ask names nobody
     (`to_inherited: true`). Not capped: the author already addressed the
     message to those seats. Each inherited addressee owes the ask until it
     answers; one seat's answer no longer releases the others.
2. **An ask that still names nobody — on a room-wide message — obliges
   nobody.** It stays in the message's `pending_asks`, any member may answer
   it, and the message itself obliges only whom the hub already obliges (the
   reporting delegate on an operator line). `/owed.pending_asks` is
   reader-scoped: a row lists the asks that name the reader, never another
   seat's.
3. **The explicit per-ask cap is 8** (was 3). Inherited lists are uncapped.
4. **A `critical` broadcast is every member's debt**, so a seat mid-turn that
   misses the notify line still meets it in the owed signature.

## Consequences

- The author who means everyone writes everyone; since 0.18 the CLI can
  (`--ask '1@alpha,beta,gamma:…'`), and the natural shape (`--to a --to b` +
  a bare ask) now means what it says.
- The anti-partial-rot guard defended the wrong thing: a bare "noted" cannot
  clear an inherited ask because discharge still requires `answers=[id]`
  from each named seat.
- Messages posted before this rule keep the reading in force when they were
  written (`pre_canvass_epoch`), as every earlier narrowing did.
