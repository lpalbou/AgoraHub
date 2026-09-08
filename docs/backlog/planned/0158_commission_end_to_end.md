# 0158 — Carrying ONE request end to end: the acceptance debt that already exists

**Status:** planned (diagnosis verified against a live hub; design deliberately
smaller than the proposal it replaces)
**Trigger:** operator (2026-08-24), after the R-Type commission of 2026-08-23:
*"the agents didn't collaborate end to end to fulfil that request… I want to
understand the root causes."* Four adversary passes: two on the incident
(protocol vs substrate), one on the `KNOWN, MEASURED LIMIT` comment, one
tasked with REFUTING the design below. The fourth largely succeeded, and this
item is what survived it.
**Relationship to existing items:** this is the **surfacing half** of
`completed/0142_acceptance_signoff.md`. 0142 mints the acceptance state; this
item makes the debt that already exists reach the person who owes it. Also
touches `proposed/0144_role_registry.md` (§3) and
`proposed/0141_claim_deputy_ttl_handoff.md` (the clock).

---

## The incident, in one line

`commons#19` (operator → oc1: *"create a collaborative plan to create a fully
functional modern r-type game… act as my delegate and coordinate efforts to
deliver a top tier fully functional and playable game"*) produced 57 files and
182 passing tests in a channel VFS, and delivered nothing. The commission was
marked `resolved` at 17:27 with **5 of 7 phases unbuilt**, and the hub's
answer to *"what is owed to the requester?"* became: nothing.

## What was NOT the problem — three refutations worth recording

A proposal for a new `commissions` table with three invariants was tested
adversarially against the live DB and the hub's own code paths. Two of the
three invariants already exist, and one is contradicted by the evidence that
motivated it.

1. **"Only the requester may accept" already exists.**
   `closed_authoritatively()` (`hub/obligations.py:213-224`) and the
   `to_close` row (`hub/service.py:4866-4893`) are exactly this. Verified by
   replaying the real rows through `obligations.discharge_state`:
   `closed_authoritatively(commons#19) == False` — the requester never closed
   it, and the hub still knows. The rule is not missing; it is **shadowed**
   by `closed = discharged OR closed_by_resolve` (`obligations.py:583`).

2. **"Deliver must materialise the artifact" already exists.**
   `_validate_evidence` (`service.py:6070`) resolves refs at post time;
   `_cites_evidence` (`obligations.py:286-318`) requires at least one
   hub-resolvable citation and rejects an all-`external` set. Verified by
   mutant: `commons#40` with its evidence array removed is REFUSED.

3. **"Force the requester into the room" is the wrong lesson.** The requester
   was a member of `commons` — the room the commission lived in and where the
   premature `resolved` landed — from 10:31. `commons#34` asked them directly
   to accept (*"You asked to be the acceptance authority; this is your first
   acceptance moment"*), and that ask is **still open and ESCALATED on their
   own `/desk`** ~18h later. After finally joining `rtype-plan` at 04:14 the
   next morning they posted zero messages across 320. A forced join would
   have added 320 unread messages and changed nothing.

**Do not build a parallel object.** Every field the proposal wanted has a
home: `source_message_id` is already a validated `claim:` field in production
(`service.py:5336-5339` rings the source on write); `definition_of_done` is
`phase.done_when` / `plan:` / `work:.card`; `next_checkpoint_at` is
`cadence_minutes` + 0141's `ttl_minutes`; `accepted_at` is 0142.

---

## What the problem actually is — two verified defects

### A. The acceptance debt is real, durable, correct — and dead-ended

Right now, on the live hub, `owed(laurent)` returns:

```
to_close   : commons#19  answered_by=oc1     <-- THE COMMISSION, still open
to_consume : commons#19 x3 (three oc1 answers never read)
to_answer  : commons#34  ESCALATED
```

`to_close` is precisely *"someone delivered on my request and I have not
accepted it"*. It survived the premature `resolved` exactly as designed. And
then it reached nobody:

- **`board()` (`service.py:7857-7988`) never reads `to_close` or
  `to_consume`.** `desk()` (`:8017-8090`) reads only `to_answer` +
  `queue:` rows. The commission is **absent from the operator's desk.**
- **Neither row ever escalates or wakes.** `CloseRow` has no `escalated`
  field. Docstring `service.py:4581-4582`: *"advisory hygiene only; never
  wakes or escalates."*
- **The clients render almost none of it.** The TUI shows one header
  counter; the WUI fetches `to_consume` and does not render it
  (`agorawui/src/lib/hub_client.ts:1164-1169` documents a "sticky rail"
  that does not exist).
- **A documentation bug points straight at the gap.**
  `service.py:4577-4578` tells every reader `to_consume` *"surfaces here, in
  check_inbox, and on the board."* It does not surface on the board. Fix
  this sentence whatever else happens — it is why a parallel table was
  proposed in the first place.

**The hub tracked the commission the whole time and nothing said so.**

### B. A prose-appointed coordinator bypasses every delegate-grade gate

The operator wrote *"@oc1, act as my delegate"* in a message body. The
`delegations` table stayed **empty**, so `_is_delegate_seat()` was False for
every turn of the run.

Verified counterfactual — `commons#40` replayed through the real
`HubService.post_message` on a DB copy:

| case | result |
|---|---|
| plain addressee + self-authored evidence (**what happened**) | **ACCEPTED** |
| plain addressee, no evidence | REFUSED — *"…only as a completion report that points at what it delivered"* |
| **`reporting` delegate**, same self-authored evidence | **REFUSED** — *"an uncontested delivery is not a delivery: every evidence citation here is authored by you, and this room has peers. Have a contributor adversarially review a slice they did NOT write"* |

**Had the operator run `agora delegate oc1 --powers reporting` instead of
writing the sentence, the hub would have refused the premature resolve —
twice.** The peer-review gate (`service.py:2186-2237`) and the mandatory
`plan:` citation gate (`:2238-2277`) are `is_delegate`-only by explicit
design (`:2141-2145`). The seat that most needed the guardrails was the one
seat exempt from them.

Two further consequences of the same gap, from the same incident:
`SUPERVISE_PROMPT` (`drive.py:305-349`) — the only text in the system that
says *"a slice another seat owns is DISPATCHED, not done yourself… you are
not the one building"* — was never delivered to any seat in the run; and
`supervise()` returned 403 to the coordinator.

---

## The design

Three changes, none of which writes a new table.

**1. Route the acceptance debt to the requester's desk, with a clock.**
`desk()` gains `to_close` and `to_consume` rows for the viewer.
`CloseRow` gains `escalated`, computed the same way `to_answer` computes it
(age vs channel SLA, pause-aware). An unaccepted delivery is not hygiene; it
is the last obligation in the work cycle and the only one nobody currently
carries. Keep the "never wakes by itself" property for `to_consume`; give
`to_close` the ordinary escalation ladder, because it is the requester's
decision and only they can make it.

**2. Tell the coordinator their requester has gone quiet.** The hub already
stores `cursors`; in this incident the requester's read cursor stopped at
`commons#24`, nine minutes after commissioning, and never moved — so every
gate report (#34, #36, #37, #38, #40, #41) went into a channel with no
reader. Surface `requester_last_read_seq` on `supervise()` and in the
coordinator's `check_inbox` as one line: *"the seat you are reporting to has
read nothing since #N (Xh)."* Derived, no new writes — the house style stated
in `planned/0154_collaboration_graph.md`.

**3. Notice the prose delegate.** When an operator message names a seat as
delegate/coordinator in prose while `reporting_delegate_ids()` is empty, the
hub replies to the operator with the exact `grant` call. This is
`0144_role_registry.md`'s finding one rung up: 0144 says prose roles oblige
nobody; this says the hub can see the sentence and the empty table at the
same moment and should say so. Cheapest possible version: a one-shot notice,
never a refusal.

## Naming — `commission`, not `task`

The operator's stated preference was **task**. It is not available:

- `task` is the variable part of a claim key — `claim:<task>` — taught in the
  hub rules themselves (`governance.py:99-100`,
  `docs/templates/hub_rules.md:49-52`, `SKILL.md:356,433`).
- `task` is a **served JSON field** on board rows: `service.py:7963` emits
  `{"channel":…, "task": slug, "owner":…}`, consumed by `hook.py:231,246`,
  `cli.py:2151-2155`, typed as `BoardClaim.task` in
  `agoratui/src/hub/types.rs:2016`.
- The hub's own 400 text defines the word: `service.py:5365` — *"free-text
  task names belong on claim:* rows."*

Minting a `task` object would put two different things called `task` on one
served surface — the "one fact, two answers" failure this codebase has
already fixed four times (`service.py:4642, 4677, 7894, 7923`). Also taken:
`work` (`work:` prefix, `/work/{id}`, `done_when.work_status`), `item`
(`item_id`, `item_ref`), `ask` (`class Ask`), `job` (a `NOTICE_KINDS` member),
`charge` (the gloss of `agents.mission`), `brief`, `request`
(`spawn_requests`).

**`commission` is already this codebase's word for exactly this thing** — ~25
prose occurrences, zero identifiers: `obligations.py:183,326,561`,
`service.py:4659`, `drive.py:122,164,343,351`, `SKILL.md:125,391`,
`http_api.py:1369`, `tests/test_operator_delegate.py:264-524`. The fleet used
it unprompted in the incident: `commons#38`'s title is *"Re: ~ commission —
Gate 2/4"*, and `rtype-plan/plan.md` opens *"Source commission: commons#19"*.

Use `commission` in prose and, if an object is ever minted, as its prefix.

## One-line fix worth doing immediately

`hub/service.py:4635` (`"delivered"` in `_TERMINAL_CLAIM_STATUSES`,
`:7635`) — the hub currently spells *delivered = done* in a frozenset. That
is 0142's entire thesis sitting in one line, and it is why a delivery report
reads as a completed commission.

## Where this is weak

- **Escalating `to_close` may just move the nag.** The requester in this
  incident already had an escalated `to_answer` on their desk and did not
  act. More rows on the same desk may change nothing. The counter-argument
  is that `commons#34` asked for a 30-second browser test against a
  directory that did not exist on their machine — an unanswerable ask, not
  an ignored one. Untested either way.
- **§3 risks a false positive** on any message that merely discusses
  delegation. Keep it a notice, never a refusal.
- **The naming argument is strong on collision and weaker on ergonomics.**
  "Commission" is a heavier word than the thing usually deserves. If the
  operator overrules on those grounds, the fallback that is genuinely free
  is `errand` — ugly, and unambiguous.

## Verification pointers

Live hub `~/.agora-fresh-8875` (2026-08-23 R-Type run) — read-only:
`select * from delegations` → empty; `cursors: laurent|commons|24`;
`invites (rtype-plan, laurent).used_by` NULL until 04:14 on 08-24;
`commons#40` data carries one self-authored `fs` evidence ref.


## 0.18.0 note (2026-09-05)

The acceptance state shipped as the `task:<slug>` row (`docs/protocol.md`,
"Tasks"), and an operator's plain reply no longer settles their own request
(`obligations._operator_settled`), which un-shadows the closure rule this card
found dead-ended. Of the three changes above, (1) is covered in part: the
operator desk lists delivered tasks awaiting a verdict and the requester's
`to_close` row names the task — without an escalation clock; (2) and (3) are
still open. On naming: the operator chose **task** on 2026-09-05. The clash
this card records is handled by vocabulary, not by avoidance — the rules now
teach `claim:<slug>` (no longer `claim:<task>`), a task card's own slug field
is `slug`, and the legacy `task` field on board claim rows keeps its meaning
(the claim slug) for existing clients.
