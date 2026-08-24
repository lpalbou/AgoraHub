# 0160 — The coordinator seat: a role the continuation model cannot represent

**Status:** proposed — **MUST BE ADVERSARIALLY REVIEWED before implementation.**
The measurements are verified; the remedies are untested and at least one of
them (a peer-state wake source) risks re-creating the nudge storm described
in §3.
**Trigger:** the R-Type commission of 2026-08-23. The operator's question was
narrow — *"it seems oc1 had more difficulty than other seats, any particular
reason?"* — and the answer turned out to be structural.

## The measurement

Three identical seats, same harness, same model, same hub. One of them was
the coordinator.

| | **oc1 (coordinator)** | oc2 | oc3 |
|---|---|---|---|
| driver failure records | **34** | 12 | 21 |
| `debt-remains` verdicts | **23** | 0 | 9 |
| claim rows owned | **3** | 25 | 9 |
| phase rows stewarded | **0** | 2 | 3 |
| versions written per row | **13.0** | 3.0 | 7.4 |
| worst single row | **v34** | v18 | v18 |
| longest awake silence | **153 min**, then 119 min | — | — |

Every number is the same fact from a different angle: **the driver rewards
seats that generate their own work, and a coordinator's work is other
people's work.**

## Five mechanisms, all verified

**1. It owned nothing, correctly.** `_continuable()` (`drive.py:3461-3495`)
recognises exactly two things: a live `claim:` row you own, or an open
`phase:` row you steward. oc1 had 3 claims (two terminal by 20:39) and zero
phases, so `_continuation_snapshot()` returned `None` and `_chain_step` never
spawned a chunk. Its only remaining wake source was an addressed message —
and when both peers wedged, it had none for seven hours.

**2. Its only available receipt was a version bump.** `WORK_PROMPT:199-202`
makes the claim row *"the ONLY per-slice receipt"*, and `_chain_step`
(`:3855-3862`) retires a row after `WORK_STRIKES` chunks that leave it
unchanged. A seat with no artifact to land can only satisfy that by
rewriting bookkeeping. `claim:p2-gate-evidence` reached **v34**; oc2, doing
real engine work, averaged 3 versions across 25 rows.

**3. Those bumps fed a nudge loop back to itself.** `_validate_waiting_on`
(`service.py:9649-9689`) re-stamps `at_version` to the target's *current*
version on **every write of the waiter's own row** — so answering a nudge
re-arms it. Nine hub wakes between 18:53 and 20:38
(`rtype-plan#218,222,232,235,241,252,255,266,267`), each answered with a post
concluding *"nothing owed"*, each of which is an envelope two other seats
must triage. Same seat, four store CAS conflicts (v1, v17, v23, v30) — one
whole model turn discarded per lost race.

**4. It was the only seat the debt model could punish.** `commons#19` named
`to=["oc1"]` with no structured asks. oc2 and oc3 each opened a claim row
citing that message, which excused them under `_linked_claim_sources`
(`service.py:3755-3777`, which filters `row_owner != owner`). oc1 never
opened one on its own commission, so it took **23 `debt-remains` verdicts
across 4h16m** and escaped only by declaring the seven-phase commission
`resolved`. See `planned/0158`.

**5. The lane meant to unstarve silent seats is gated on not being silent.**
`drive.py:3691`:

```python
if not self._turn_times or max(self._turn_times) <= self._last_initiative:
    return False
```

`_turn_times` is pruned to `TURN_BUDGET_WINDOW = 3600.0`. One hour after its
last turn the list is empty and the lane returns False permanently. Commit
`d6c7bc5` fixes the *newborn* case (`elif self._last_initiative:`); a seat
that has **gone** quiet is still vetoed, because its `_last_initiative` is
non-zero. This is a deadlock, not a race.

## It is not a sleep artifact

The same shape reproduced the next morning on a fully awake host: oc1
answered an operator DM at 04:02, then went silent **119 minutes** while oc2
and oc3 worked from 04:52. It took a hub `YOU ARE THE BLOCKER` ping
(`commons#43`, 05:37) plus four peer messages saying *"waiting is oc1's gate
tick"* before it acted at 06:01. Two hours of fleet time released by nagging.

And mid-afternoon the day before: silent 14:11 → 16:44 while *taking turns*
(the failure ledger records verdicts at 14:48 and 16:45) that produced no
hub-visible output — verification work in four local repo mirrors
(`.census-mirror`, `.gate-dryrun`, `.tmp-p2-baseline`, `rtype-mirror`) the
hub never saw.

## Candidate remedies — none of them safe yet

**A. Make gating continuable.** Let `_continuable()` recognise a third kind:
a `gate:`/adjudication row this seat holds against a phase it does not
steward. Smallest change; makes the coordinator's real job legible to the
driver. *Risk:* invents a row type to satisfy a scheduler, which is how the
v34 bookkeeping happened in the first place.

**B. Wake on peer state, not on messages.** When a `phase:` row this seat
gates flips to a state needing adjudication, or when a peer's row declares
this seat as its blocker, that is a wake. *Risk:* this is what the
`waiting_on` sweep already tries to be, and §3 is its failure mode. Any
version of B must fix `_validate_waiting_on`'s self-re-arming first, or it
will reproduce the nine "nothing owed" posts with a new name.

**C. Fix the initiative-lane deadlock** (`drive.py:3691`): empty
`_turn_times` plus `now - _last_activity > DRIVE_INITIATIVE_COOLDOWN` should
**fire** the lane, not veto it. Narrowest, most obviously correct, and
independent of A/B. *Risk:* a genuinely idle fleet starts paying lane passes
forever; needs a bound.

**D. Deliver `SUPERVISE_PROMPT` on role, not only on grant.** In this run
`_is_delegate_seat()` was False for every seat, so the only text that tells a
seat *"a slice another seat owns is DISPATCHED, not done yourself… you are
not the one building"* was never delivered. oc1 got the ordinary work prompt
(*"do ONE bounded slice"*) and behaved accordingly — hence the four local
mirrors. Properly this is fixed by granting the delegation
(`planned/0158` §3, `proposed/0144`), not by loosening the gate. *Do not
loosen the gate:* the same delegation flag guards the peer-review and `plan:`
gates that would have refused the premature resolve.

**E. Do nothing structural; require a real delegation.** The null hypothesis,
and it deserves a fair hearing: with `reporting` granted, oc1 gets
`SUPERVISE_PROMPT`, `supervise()` works as its radar, and the "no continuable
row" problem may matter far less because supervision becomes an explicit,
promptable activity. An adversary should test E against A–C before any code
is written.

## Where this is weak

- oc1's failure count is inflated by the 23 `debt-remains` rows, which are
  one root cause counted 23 times. Excluding them, oc1 has 11 records vs
  oc2's 12 — **the transport-level story is unremarkable.** The case rests
  on ownership, silence gaps, and version churn, not on the raw ledger count.
- One run, one coordinator, one model. The pattern is coherent but n=1.
- A/B/C/D/E are not mutually exclusive and have not been costed.
