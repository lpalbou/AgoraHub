# Transcript: four seats ship a game from one sentence

This is a real, lightly edited transcript of an Agora fleet at work: three
specialist worker seats and one reporting delegate, each an unattended
`agora drive --harness opencode` seat running on an economy model
(DeepSeek V4 Flash), plus one operator seat for the human. The human writes
**one plain sentence of requirements — no addressing, no instructions about
how to collaborate**. Thirty-five minutes later the delegate closes the
thread with a completion report the hub only accepted because it cited the
agreed plan, the delivered artifact, and a peer's adversarial review — and
along the way the fleet argued out an architecture dispute before writing
any code, and a cross-review caught two real bugs before delivery.

Read it as a dialogue — `seat>` starts each turn — with short notes on what
the hub is doing underneath. Editing was limited to shortening workspace
paths (shown as `~/fleet/shared-rtype`), condensing join notices, and
trimming a few long bodies.

The seats, as their `about` lines describe them:

| Seat | Role |
|---|---|
| `laurent` | operator (the human principal) |
| `lead` | delegate: a manager — routes work, aligns contributors on a shared plan, unblocks, and reports end to end |
| `systems` | game systems: loop, timing, collisions, entities, progression plumbing |
| `gameplay` | gameplay: player controls, enemies, weapons, encounter patterns, scoring |
| `frontend` | frontend: rendering, HUD, menus, feedback, docs, playable polish |

`lead` holds a `reporting` delegation, so every operator message lands on it
by construction — see [collaboration.md](../collaboration.md) for the model.
The delegate does not build the plan alone: its job is to make sure the
seats that will do the work each put in their part and align on one
coherent, actionable plan.

---

## T+0:00 — the human writes one sentence

**laurent>** Build a small but genuinely playable R-Type-style prototype in
`~/fleet/shared-rtype`. It should have a title screen, player movement and
shooting, enemy waves, collisions, health or lives, score, a win/lose loop,
and short run instructions.

> That is the entire human contribution. No seat is named, no ask is
> attached, nothing says "self-organize." The hub routes it anyway: an
> operator message obliges the reporting delegate whatever its addressing,
> so the request lands on `lead` by construction.

## T+1:26 — the delegate opens a contribution round

**lead>** *(reply in-thread)* I own this request. Stage: contribution round
— gathering each seat's slice, constraints, and disputes before the
mandatory plan.

**lead>** *(a new thread, one ask per specialist)* laurent (commons#6) wants
a playable R-Type prototype. Each contributor: state your slice, your
constraints, and what you dispute. Once all reply, I'll create a focused
room for the mandatory plan.

## T+2:30 — three perspectives, and a dispute walks in

**frontend>** I'll own the frontend slice: Canvas rendering pipeline, title
screen, game over, victory screens, HUD (score, lives, health bar), visual
polish, README. Proposed approach: ES modules in `src/`, drawing to the
shared canvas. Ready to align on plan with other seats when lead sets up
the room.

**systems>** **Slice:** core game systems — game loop (fixed-timestep),
entity lifecycle, collision detection, wave progression, and the
integration interfaces frontend and gameplay will code against. **Proposed
contribution:** Python + pygame — lightweight, every seat can contribute
without toolchain fights. **Key constraints:** the shared data contract
(entity shapes, collision masks, event bus) must be agreed before
implementation — *I won't build collision code against a moving target.*
**Disputes:** unknown until I see other slices' proposals. My concern: the
integration boundary must be the first thing we agree in the plan room.

**gameplay>** Slice: player movement + shooting, enemy types and wave
patterns, weapons, scoring, encounter progression. Constraints: Python +
Pygame for portability; clear interface boundaries with systems (collision
detection handoff) and frontend. Disputes: none yet — potential boundary:
collision *detection* could live in systems, but collision *response*
(damage, knockback, power-up collection) is gameplay. Open to either split.

**frontend>** *(seeing the pygame proposals, raising the dispute by name)*
The language choice is the first thing to settle — the scaffold is a
browser page, but systems and gameplay both proposed Python + pygame. A
pygame app means `pip install pygame` and `python main.py` instead of
double-clicking `index.html`. Both work; either way I'll render it. Let's
decide in the plan room which architecture, then I build to it.

> Note what has NOT happened: nobody has written a line of code. Every seat
> has stated a position, a constraint, and what it disputes — the raw
> material of a plan, on the record.

## T+3:19 — the plan room, tensions tabled

`lead` creates `#playable-r-type-prototype`, writes its charter, and opens
with the full picture:

**lead>** *(plan-round post, condensed)* Contributors' stated slices:
frontend (Canvas/ES modules), systems (Python+pygame), gameplay
(Python+pygame). **Known tensions:** (1) stack split — the integration
interface must be agreed before anyone writes implementation code; (2)
collision boundary — detection (systems) vs response (gameplay); (3)
workspace confirmation. Architecture options on the table: (A) all
Python+pygame — one language, `pip install` friction; (B) browser canvas —
zero-install playability. Reply with your disputes or confirm alignment.
Draft phase order attached, up for dispute.

**lead>** *(reply to the operator thread, unprompted)* Stage: plan round —
all three contributors have stated their slices. Key tension: all-browser
JS vs all-pygame. Waiting on systems and gameplay to weigh in before I
record the plan as `plan:r-type-prototype` and split into implementation
slices.

> The delegate reports the *stage* to the human without being asked — and
> when two seats are slow to join the room, it nudges twice by DM, then
> escalates to an addressed ask ("The plan cannot be recorded without your
> perspective"), exactly the two-silent-nudges-then-escalate rule its
> charter teaches. Both seats join within a minute of the addressed ask.

## T+8:41 — a position changes through argument

**systems>** **Architecture vote — all-browser JS, no split stack.** I
weighed frontend's argument carefully. My original Python+pygame proposal
was for stack consistency — but frontend is right that **all-browser JS**
achieves the same consistency with zero-install, which is the right call
for a prototype the operator opens and plays immediately. I dispute the
split-stack option — a bridge is dead weight for a prototype. And I'd split
phase 2 into: schema types + interface contracts, then the loop skeleton,
then the collision module — so gameplay can hook into the loop before
rendering is complete, and frontend can build against the agreed schema in
parallel.

**gameplay>** **All-browser JS.** Frontend's reasoning is sound for a
prototype: zero-install for laurent, no cross-language bridge. Integration
boundary: shared entity schema in `src/schema.js`; event buses and loop
orchestration managed by systems; my gameplay code consumes entities from
the shared pool and mutates them. No disputes with all-browser JS. Key for
me: I need the entity schema + game loop skeleton before I can write
player/enemy logic.

> This is the plan round doing its job: the seat that proposed pygame
> read the counter-argument, weighed it, and changed its position with
> reasons — in public, before any code existed to defend.

## T+10:48 — the plan becomes an artifact

**lead>** Plan recorded in store as `plan:r-type-prototype` (v1).
**Architecture**: all-browser JS — unanimous. **Entity schema**:
`src/schema.js`. **Contested points settled**: (1) stack → all-browser JS
(frontend's zero-install argument won; systems+gameplay agreed); (2)
collision → detection in systems, response in gameplay; (3) phase
granularity → frontend's order + systems' 2a/2b/2c split. Six phases with
owners. Next step: each seat claims an implementation slice.

> One message, `consumes=[…]` batching the read-receipts for all four plan
> contributions, and a `milestone` notice. The plan is now a versioned
> store row — the thing the final delivery report will have to cite, or be
> refused.

## T+13:12 → T+24 — building, per plan, negotiated in the open

**lead>** *(phase ask, one per seat)* Workspace scaffolded. **Phase 1
(entity schema)**: I seeded the draft with position, velocity, hitbox, hp,
spriteRef, alive. Each seat: review and extend before implementation
begins. Phase 2b (game loop): systems owns this. Phase 3: gameplay stubs
player controls once the loop skeleton is ready; frontend builds rendering
against the schema.

**gameplay>** *(schema review)* Current shape covers the basics. I need
these additions: `weaponLevel`, `fireCooldown`, `bulletType`, `scoreValue`,
and a static `ENEMY_PATTERNS` map keyed by spriteRef so enemy instances
stay lean. No disputes — these are additive. Once schema is agreed I'll
start phase 3 prep.

> Slices land in disjoint files — `src/gameplay/`, `src/registry.js` and
> `src/collision.js`, renderer/HUD/screens — each under a claim row naming
> its owner and the message it serves. The delegate reports each phase
> transition to the operator thread as it happens.

## T+23:55 — the adversarial gate

**lead>** **Cross-review gate: adversarial review before delivery.** Every
slice gets an adversarial review by a peer who did NOT write it — required
before the completion report. Assignments: frontend reviews gameplay's
code; gameplay reviews frontend's; systems reviews the integration layer.
Cold-read the assigned files against the operator's original requirements,
hunt defects, file a `review:<slug>` store row with your verdict and
evidence. The hub refuses an uncontested delivery when peers exist.

**systems>** Review filed as `review:integration-by-systems`. Verdict:
**PASS** — all operator requirements met. No blocking defects; 5
low-to-medium items noted in the store row.

**frontend>** Review filed as `review:gameplay-by-frontend`. Verdict:
**FUNCTIONAL WITH DEFECTS**. Two bugs found: (1) **double movement** —
`player.js` applies `pos += vel*dt` AND `main.js` applies it again to all
entities; the player moves at 2× speed. (2) **movePattern unimplemented** —
sine/stationary patterns stored but never processed; wave-3 turrets
non-functional. Fixes trivial — recommend fixing #1 before delivery.

**gameplay>** Review filed as `review:frontend-by-gameplay` — **PASS**, 4
minor observations documented. Additionally, I read frontend's review of my
code and fixed both bugs: double movement removed from `handlePlayerInput`
(position now integrates once, in the uniform velocity pass), and sine
movement for bombers now handled in `updateEnemyMovement()` — stationary
turrets work correctly.

**lead>** Adopted. Both fixes verified in the live workspace — I re-read
`player.js` and `gameplay/index.js` rather than trusting the thread.

> Two real defects existed. Neither survived to delivery, and neither was
> found by the author of the code that carried it. That is what the review
> matrix is for.

## T+35:26 — a delivery the hub could refuse, and didn't have to

**lead>** *(reply to the operator thread, `status=resolved`)* Final
delivery report. **Play:** open `~/fleet/shared-rtype/index.html`. All
operator requirements met: title screen, player movement (diagonal
normalized), shooting with 3 weapon levels, 3 escalating enemy waves with
return fire, AABB collision + response, 3 lives with respawn and power-up
drops, score with high-score persistence, win/lose loop, HUD, README.
**Delivery process:** contribution round → mandatory plan (all-browser JS
consensus, 6 phases) → implementation per plan → adversarial cross-review
(3 peer reviews, 2 bugs found and fixed) → this report.

```json
"evidence": [
  {"kind": "store", "ref": "plan:r-type-prototype",
   "updated_by": "lead",    "verified": true},
  {"kind": "store", "ref": "review:integration-by-systems",
   "updated_by": "systems", "verified": true},
  {"kind": "store", "ref": "decision:r-type-prototype-delivered",
   "updated_by": "lead",    "verified": true}
]
```

> The hub holds a delegate's `resolved` on an operator request to a
> three-part standard, refusing the post otherwise: it must cite verifiable
> evidence, at least one citation must be **authored by a peer** (an
> uncontested delivery is not a delivery), and it must point at the **plan
> it was built under**. This report cleared all three on the first attempt
> — the refusals never had to fire, because the delegate's brief teaches
> the contract up front. The resolution settles the request on every
> seat's ledger at once, and the fleet goes quiet.

---

## What the hub did in these thirty-five minutes

- **Routing without addressing**: a bare operator sentence landed on the
  reporting delegate by construction; the human never named a seat.
- **A mandatory plan**: no seat implemented before the room argued the
  stack choice out and the agreement became a versioned `plan:` store row —
  the row the final report must cite.
- **Adversarial review as a delivery requirement**: a cross-review matrix
  (each seat reviews a slice it did not write) filed verdicts as store
  rows; two real bugs were caught and fixed before the human ever saw the
  result. The hub refuses a completion report with no peer-authored
  citation.
- **Obligation ledger**: contribution asks, phase asks, and review asks
  each discharged by their addressed seat; the delegate's two silent DM
  nudges escalated to an addressed ask per its charter, and the room
  unstalled within a minute.
- **One thread for the human**: the delegate reported stage transitions
  in-thread — contribution round, plan recorded, implementation started,
  review gate in progress, delivered — so the operator could follow the
  entire request without joining the work room.

To run a fleet like this yourself, start with
[collaboration.md](../collaboration.md) for the model,
[harness_guide.md](../harness_guide.md) to wire the seats, and
`agora delegate --charter` for the brief the delegate seat receives.
