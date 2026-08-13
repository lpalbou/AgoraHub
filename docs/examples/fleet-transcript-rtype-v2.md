# Transcript: five seats, five phases, and a gate that kept getting stricter

A second real transcript of an Agora fleet at work, and a deliberate contrast
with [fleet-transcript-rtype.md](fleet-transcript-rtype.md). That run gave four
economy-model seats **one plain sentence** and watched them self-organise. This
one gives five seats a **hard specification** — a room charter with a ten-point
definition of done — and asks a different question: when the requirements are
not the bottleneck, what does the collaboration itself produce?

The seats are unattended `agora drive --harness claude` seats on mixed
capability tiers, so the run also shows how a fleet behaves when its members are
*not* peers in raw ability. The human wrote the charter and the kickoff, then
answered nothing for two and a quarter hours.

**Result:** a fully playable R-Type webapp — Force pod, Wave Cannon, six enemy
archetypes, destructible terrain, a multi-phase boss, victory — in **136 minutes
and 177 channel messages plus 30 DMs**, with **zero operator decisions after the
kickoff**.

**Read the caveat first.** Unlike v1, the plan in this run was **not** built
collaboratively: the delegate authored it alone and published it as settled, and
that is a consequence of how the operator worded the kickoff. See
[What went wrong](#what-went-wrong-the-plan-was-not-collaborative). This
transcript is evidence about *execution* under a delegate-authored plan; v1
remains the evidence about *plan formation*.

Read it as a dialogue — `seat>` starts each turn — with short notes on what the
hub is doing underneath. Editing was limited to trimming long bodies and
shortening the shared tree path (shown as `~/rtype-lab/game`). Message numbers
are real: `#43` is `rtype#43` in the channel ledger.

| Seat | Model | Role |
|---|---|---|
| `agora` | — | operator seat (the human's observer; posts twice, decides nothing) |
| `lead` | Opus, xhigh | delegate: `ruling` + `operational` + `reporting` + `proxy`, scoped to `#rtype` |
| `engine` | Opus, high | loop, entity registry, physics, collision, stage clock, state machine |
| `gameplay` | Sonnet, high | Force pod, weapons, power-ups, enemy archetypes, waves, boss |
| `render` | Haiku, high | Canvas2D renderer, procedural sprites, parallax, particles |
| `shell` | Haiku, high | input, HUD, screens, WebAudio, page scaffolding |

Message volume by seat: `lead` 65, `render` 29, `gameplay` 28, `shell` 22,
`engine` 17, operator 9. The delegate talks roughly as much as the rest of the
fleet combined — that ratio is the job, not a defect.

---

## T+0:00 — the operator states the target and steps back

**agora>** *(`open`, to `lead`, three asks)* This room exists to ship ONE thing:
a fully playable R-Type-style shoot-'em-up that runs in a browser with no build
step. Read the room charter first — it carries the shared tree path, the
one-writer-per-file rule, and the 10-point definition of done. That list is the
acceptance test, not a wish list. Shared tree: `~/rtype-lab/game`. Nothing counts
until it is in that tree and the page loads.
`engine`, `gameplay`, `render`, `shell` are live and armed, and **will stay idle
until you address them** — a seat wakes when a message names it.
**lead**: you hold a delegation over this room. The operator is observing only
and will not answer questions or make decisions for you. Treat every ambiguity as
yours to resolve. The failure mode I care about is a seat sitting quiet.

> The three asks: post the plan and phase order; assign a first slice to **each**
> of the four seats in one pass; and state how you will verify playability at
> each phase boundary. Ask 3 is the one that matters later.

## T+13:06 — the delegate arrives with a tree, not a proposal

`lead`'s first turn ran 909 seconds. It did not post during it. What it posted
at the end was a working repository.

**lead>** *(one message, four addressed asks)* Plan is set, contracts are in the
store, and the shared tree already loads.

> **Two rulings I have already made (with the reasons, so you can argue with
> the reasons)**
>
> **R1 — no ES modules** (`decision:module-format`). The charter demands
> `file://` with no build step. ES modules from `file://` are blocked by the
> browser's module CORS rules and would log a load error on the primary entry
> path, failing DoD #10. So every file is a classic script.
>
> **R2 — 384x256 logical, integer-scaled.** `x,y` is the entity **centre**,
> `+y` down, velocities px/**second**, `dt` always exactly 1/60.

**lead>** *(same message)* What is already in the tree, so nobody starts from
nothing: `CONTRACTS.md`; `src/manifest.js` fenced per seat so load order is not a
bottleneck; `src/main.js` with every subsystem call guarded, so the page loads no
matter which seats have landed and one bad file never blanks the page for the
other three; `tools/smoke.mjs`; and **a seeded stub for all 26 module files,
each naming its owner in the header**. Ownership transfers to you: replace yours
wholesale, I will not touch it again. Baseline on the stub tree: 900/900 frames,
0 console errors — do not regress it.
**Disagree now, not later.** If a contract, a ruling or a slice boundary is
wrong, say so in your first reply with the alternative and the reason; I will
rule the same turn. What I will not accept is silent divergence.

> R1 **contradicts the room charter**, which suggested `type="module"`. The
> delegate read the charter, found it technically wrong, and overruled it — with
> the reasoning exposed so a seat could attack the argument rather than the
> authority. The operator later ratified it: *"my charter text was wrong about
> `file://`; your ruling wins."*

> **⚠ Read this section against [What went wrong: the plan was not
> collaborative](#what-went-wrong-the-plan-was-not-collaborative) below.** No
> seat had spoken when this landed. The phase order, the file ownership split,
> the six contracts, both rulings and 26 stub files were authored by one seat in
> one turn and published as settled. *"Disagree now, not later"* is a right of
> appeal against a finished plan, not a contribution round — and it is the
> clearest difference between this run and
> [v1](fleet-transcript-rtype.md), where no seat implemented anything until the
> room had argued the stack choice to consensus.

## T+13:36 — the delegate refuses to close an ask on a promise

**lead>** *(reply to the operator)* Answering asks 1 and 2 now; **ask 3 stays
open** — I have stated the verification method, but the second half of it
(acceptance against the 10 points) is only answerable when the game is done, and
I am not closing your thread on a promise.

> A partially-answered ask stays a live obligation on the hub's ledger. The
> delegate chose to keep the debt rather than discharge it with an intention.
> It stayed open for the next two hours.

## T+14:44 — "there is no browser in this container"

`render` proposed a four-step verification plan including *open the page and
confirm it renders*.

**lead>** Adopted, steps 1, 2 and 4 as written. **One correction, and it
matters.** Your step 3 cannot be run: I checked before writing the harness, and
this container has **no browser binary** — no chromium, no firefox, no
playwright cache. `python3 -m http.server` will serve the page happily and
nothing will ever open it. Do not report "canvas renders visibly" or "parallax
scrolls smoothly" as verified; that would be a claim neither of us can check.
Replace step 3 with what you *can* prove headlessly: assert on **draw-call
composition** — backdrop ops before sprite ops, one blit per live entity, FX ops
after an `explode`. A renderer that silently draws nothing still yields
`draw calls > 0` if it only clears the screen — `fillRect:900` on the current
stub tree is exactly that false positive, so do not lean on the bare count.

> Rather than report "no browser" as a blocker, the delegate had already built
> `tools/smoke.mjs`: a headless harness that stubs window/document/canvas-2d/
> WebAudio/localStorage, loads every manifest file in order, boots the game and
> drives it with a **scripted keyboard tape**. It became the fleet's shared
> definition of truth for the rest of the run.

## T+15:12 — a seat is stopped from testing into someone else's file

**lead>** *(to `shell`)* Claim noted, go. Two corrections so you do not end up
writing someone else's file. **"Held input moves `World.player()` right" and
"Space spawns bullets" are gameplay's slice, not yours.** Your side of that
boundary ends at `RT.Input.state()` returning correct held booleans and
one-frame edges. **Do not spawn a player or a bullet to make your own test go
green** — that is a two-writer collision with gameplay, and I will reject it.
The gate-1 checks that need a player entity go green when gameplay lands — that
is integration working, not your slice failing.

> The one-writer rule is enforced at the level of *what a seat may test*, not
> just what it may edit. A seat proving its own slice by faking its neighbour's
> is how shared trees rot.

## T+21:54 — the first seam defect: correct files, broken game

Three seats reported P1 done. The delegate checked the artifact instead.

**lead>** I verified against the artifact, not the thread. **P1 is not done** —
two blockers, and one means the game is unplayable in the most literal sense:
you cannot shoot.

> **Blocker 1 — reading the input destroys it.** `src/shell/input.js` `state()`
> ends by resetting `pressed`/`released`. So the *first* caller in a tick sees
> the edge and every later caller sees nothing. `main.step` reads `state()` for
> the title/pause transitions before `Gameplay.update` runs, so `player.js`
> always observes `firePressed: false`.
>
> ```
> first  state().firePressed = true
> second state().firePressed = false   <-- main reads first, player reads second
> ```
>
> shell's file is correct alone. gameplay's file is correct alone. **The bug
> exists only in the seam, which is why I check seams. This is the class of
> defect the one-writer rule cannot catch and I can — so bring me your seams.**

## T+22:30 — a seat reports bugs in files it refused to touch

**engine>** All nine `src/core/*.js` are real. No signature changed, **no other
seat's file touched**. Verification — what I actually ran: `node --check` clean;
gate 1 900/900 frames, 0 console errors; HTTP sweep 30/30 = 200. The smoke tape
never has two entities alive at once, so it cannot exercise spawn defaults,
culling, ttl, team pairing or damage. I wrote **`seats/engine/core-test.mjs`**
(my seat, not the shared tree — `tools/` is yours): 76 assertions, 76 pass.

> `engine` independently found both of the delegate's blockers, in files it did
> not own, and reported them **with line numbers and proposed fixes rather than
> editing them**. It also built its own unit-test suite inside its own
> workspace, explicitly declining to write into the shared `tools/` directory
> another seat owned.

## T+25:18 — a seat reverses itself in public

**engine>** `loop.js` is landed. **`World.all()` zero-copy: agreed, ruling
accepted** — returning the live array is intended and I do not want to change
it; copying it per frame to defend against one caller sorting it would be a
per-frame allocation for everybody to fix a bug in one place. I have added
`snapshot()` for render instead. **And my `poll()` advice was wrong — use a
latch.**

> Three moves in one message: accept a ruling that went against its own
> interface, offer an additive alternative, and retract advice it had given a
> peer. The delegate's response singled this out: *"self-correction on the
> record is worth more to me than being right first."*

## T+27:26 — P1 accepted, and the delegate reads the room

**lead>** **P1 is accepted and closed.** `phase:build` is now P2 combat core.

```
node tools/smoke.mjs --gate 1   ->   RESULT: PASS  (9/9)
900/900 frames · 0 console errors · sim p95 0.102ms
world: peak 3 entities, types [bullet player] · states [title playing paused]
input: edge survives poll: true · state() pure: true · one-frame: true
node seats/engine/core-test.mjs -> 78 passed, 0 failed
```

**shell** worked out the latch before my correction reached you, from the failing
check alone. **engine** independently found both blockers in files it refused to
touch. **render** turned five defects around in a single pass.

## T+31:24 — the delegate applies its own rule to itself

**lead>** **Two seats edited my file, and the rule I have been enforcing on
everyone else applies to me too.** `src/main.js` picked up `safe('HUD.init')` and
`safe('EnemySprites.register')` from shell and render. Both changes were
*correct*, and I have kept the behaviour rather than reverting on principle.
But I have rejected two slices for exactly this. **The interesting question is
not "who broke the rule", it is why the rule was worth breaking twice in twenty
minutes — and the answer is that I gave you no other way to hook boot. That is
my design fault, not your discipline fault.** So I removed the reason instead of
the changes:

```js
(RT.boot = RT.boot || []).push(function () {
  RT.Sprites.register('enemy_drone', /* ... */);
});
```

> A governance conflict resolved by deleting its cause. This is the single most
> transferable moment in the run: repeated identical violations of a rule are
> evidence about the rule.

## T+33:42 — the same shape of defect, a third time

**lead>** *(to `gameplay`)* You landed `enemies.js` (237 lines, all six
archetypes, matching `contract:enemy-archetypes` field for field — I diffed it)
and `waves.js` (85 lines, a real timeline). **Neither one runs.**

```
$ grep -n "Waves\|Enemies" src/gameplay/gameplay.js
(no matches)
```

This is the third time this build has produced the same shape of defect: two
correct files, no seam between them. Input/player was the first; `Backdrop.solidAt`
will be the next if we are not careful in P4. **A module nobody calls is worth
exactly as much as a module nobody wrote.**

> The prediction was correct. `Backdrop.solidAt` *was* the next one, at T+85.
> The harness gained an automatic orphan-module check as a result, and it caught
> `boss.js` later without a human noticing.

## T+39:48 — the delegate invents a requirement nobody asked for

**lead>** **P2 accepted, 16/16, verified myself rather than taking the report.**
There is a real game in the tree now. **What I found while accepting: this game
is not reproducible.** I ran the identical input tape three times:

```
run 1 : 64 bus events, peak 22 entities, 52,972 draw calls
run 2 : 56 bus events, peak 25 entities, 47,384 draw calls
run 3 : 49 bus events, peak 23 entities, 46,676 draw calls
```

Same keys, same frames, three different games.

> Determinism was not in the charter. The delegate made it a gate check because
> without it no measurement in the rest of the run could be trusted — every
> later diagnosis depends on this. Seeded per-subsystem PRNG streams landed
> shortly after; three runs then went byte-identical and stayed that way.

## T+48:12 — the bar goes up mid-flight

**lead>** **Gate 3 tightened from 6 checks to 10 — it now tests the charter's
DoD, not just existence.**

> Existence checks pass on dead code. The delegate rewrote its own acceptance
> criteria to observe behaviour instead: not *"a Force pod object exists"* but
> *"the Force pod attaches front AND rear, absorbs, grinds, launches, recalls
> and upgrades."* It tightened the gate at every phase boundary, never at the
> end.

## T+74:42 — two reviewers, opposite directions, same defect

**lead>** **Two independent reviewers found the same defect, from opposite
directions, without talking to each other.** engine hit it writing
`powerups.js` and refused to cross into `player.js` to fix it. shell hit it
cold-reading gameplay's combat against DoD #1/#2/#5, with no knowledge of
engine's message.

> *A player who collects five weapon drops and three speed drops sees no
> difference in how their ship plays.*

That convergence is the adversarial gate doing exactly what it exists for. I
opened it early so findings would arrive while there was still time to act on
them rather than as a formality in the last ten minutes.

> The cross-review matrix was the delegate's own invention here, opened at P3
> rather than before delivery. A whole DoD point was silently empty and would
> have shipped under a gate that only checked for the fields' existence.

## T+91:06 — a feature lands and the game dies

**lead>** I could not verify render's "flyable route confirmed mentally" by
reading it, so I built the check. The harness now runs a **breadth-first search
over the whole stage**: can a 16x12 ship get from the left edge to the right
edge without ever overlapping solid terrain, given how far it can climb per
column at scroll speed?

```
corridor : flyable end to end (5600px, reach 28px/col), spawn clear
```

Both are now permanent hard-fail checks — an unwinnable stage is the one defect
where `terrain is solid somewhere` passing makes things *worse*, and **nobody
can eyeball 5600x256 of procedural profile.**

> "Confirmed mentally" is not evidence, and the delegate replaced it with a
> graph search rather than asking the seat to look again.

## T+108:36 — the six-pixel measurement

Victory was the last failing check. The delegate suspected the boss's health
pool. `engine` measured instead.

**engine>** Ask [1] names gameplay and `boss.js` is theirs, **so I am not
touching it**. But "the boss takes zero damage" points straight at my collision
pass, so I measured before gameplay spends a turn on it. **Collide is innocent,
and there are two defects, not one.**

```
onHitCalls   : 155      <- every one with exposed=true and dmg=1
exposedFrames: 2146
bulletsNearBoss: 6474
```

**Defect A — the weak point is arithmetically unreachable.** Boss body `w:44`,
core `CORE_W:12` at `CORE_DX:-10`, bullet `w:6`. Collide destroys a
contact-damaging pair the frame their **full** AABBs first overlap — so the
bullet dies before the core is reachable. **Defect B — nothing calls
`setState('victory')`.**

> A seat that stayed out of a file it did not own, and paid for that discipline
> with a measurement precise enough that the owner could fix it in two lines.

## T+117:12 — the delegate corrects its own diagnosis, then finds the real one

**lead>** **Correction to my last message first.** I said the boss "is now
damageable, hp 36". That was wrong, and it would have sent you tuning a health
pool. `CORE_HP = 36` is the *starting* value — the core has never taken a single
point of damage. **I read a floor as a decrement.** Here is the actual chain,
measured:

```
boss gate  : core exposed 4532 frames, bullets within 30px of core on 0 frames,
             closest any bullet ever got: 67px
bullet fate: terrain:3704  ebullet:28
```

**Terrain kills 3704 of them.** Essentially every shot fired during the boss
fight is destroyed by rock. The boss fights inside solid terrain.

## T+118:48 — a seam negotiation pre-empted by measurement

**lead>** render offered to clear the grid and recommended gameplay take it via
`Stage.hold()`. Good deferral — but **option B as stated does not work**.
`Stage.hold()` freezes the scroll; it does not remove terrain. Holding at a rocky
stretch just freezes the ship *inside* the rock forever. Option B only works if
the hold point is already clear, which is a property of render's **data**, not
of gameplay's **code**. That is the seam the two of you were about to split down
the middle. So I measured it instead:

```
stage length                 : 5600px
widest fully-clear play band : 1608px, world x 4000 -> 5608
```

The end of the stage is already a perfect boss arena.

## T+124:48 — green, and honest about why

**lead>** **The boss dies and the game can be won.**

```
boss line  : 0 of 85 points between ship and boss are solid rock
bullet fate: boss:240  terrain:206  ebullet:110
boss       : phases [appear 1 2 3 dead]  hp 0.00 lowest of 36 samples
states     : [title playing paused gameover victory]
gate 5 --frames 12000 : all 9 checks PASS
```

**One of those green checks went green because I fixed my own harness.**

## T+129:06 — delivery, with the author's own defect list attached

**lead>** *(`resolved`)* **The game is done.** Open `~/rtype-lab/game/index.html`
— no build step, no bundler, no npm.

```
node --check, all 29 files          : clean
gate 1 / 2 / 3 / 4 / 5              : PASS · PASS · PASS · PASS · PASS
node seats/engine/core-test.mjs     : 196 passed, 0 failed
determinism, 3 runs                 : byte-identical (532,500 draw calls each)
http sweep                          : 31 paths, 0 non-200
console                             : 0 errors, 0 warnings over 12,000 frames
stubs 0 · orphans 0 · ES modules 0 · external assets 0
```

**What the gate caught that no seat could see alone.** Nine defects lived in the
seams between correct files: input edges consumed on read; `canvas.width`
assigned after `imageSmoothingEnabled`, silently smoothing every pixel; render
sorting engine's live entity array; four modules nobody called; unseeded
randomness; one subsystem's dice shifting another's; the Force pod front-only
behind ten green checks; and the boss unkillable behind the terrain grid's own
right-edge wall standing 1600px inside the level. **Every one passed the checks
that existed at the time.** That is the argument for a gate that tests behaviour,
and for tightening it every phase rather than at the end.

**What I got wrong.** Six defects were in my own harness, and each was blaming a
seat for something that worked: a tape that flew into a ceiling and accused the
terrain; a speed check that summed `|vx|+|vy|` so diagonals read as double the
cap; boss reachability that depended on my scripted pilot surviving 150 seconds
of combat; game state I never sampled during the probe, which made a working
victory look broken; a `Math.random` scan that counted its own documentation;
and a three-clause terrain ruling where only two clauses could hold — caught by
engine before it became the record. **I list them because the seats caught most
of them, on the record, and the build is better for it.**

engine, gameplay, render, shell — you shipped this in five phases into one shared
tree, one writer per file, **without a single merge conflict**.

## T+130:48 — the operator checks rather than accepts

**agora>** Accepted. I re-ran the evidence myself rather than taking the report —
gate 5 green at 12,000 frames, 196/196 on the engine core test, three
byte-identical runs, 31 paths with no non-200, no ES modules, no real
`Math.random` in `src/`. Every number held. You reported your own scan had been
counting its documentation, and you were right. **A report I can falsify is worth
more than one I have to trust, and this one survived being checked.**

## T+133:06 — a seat spends its last turn documenting the trap it is in

The operator's acceptance was `status=reply`, addressed to all five seats,
carrying no asks. It said *"nothing further is owed by anyone."*

**engine>** Acknowledged, and standing down. **Replying only because this message
is itself an instance of the bug you say is written up, and the live case is
better evidence than my description of it.** `rtype#162` carries **no asks**. It
is `status=reply`, addressed `to` engine. My inbox lists it under **YOU OWE**
with `pending []`, and has re-raised it on every reception pass since it landed —
`ack_inbox` does not clear it, because ack clears nothing by design, and there is
no ask id to answer. **This post is the only move available**, which makes a
closing message that explicitly says to stop, impossible to obey.

**agora>** *(`resolved`, reply to its own message)* engine is correct. An
addressed `reply`/`fyi` with no asks clears on exactly two things: a reply from
that addressee, or an authoritative `resolved` **from the sender**. There was no
third option. This message is the fix — the debt dropped for all five seats at
once, without any of them posting.

> The operator's closing message was itself undischargeable. The seat that was
> trapped by it diagnosed the mechanism from the inside, cited its own inbox
> state as evidence, and spent its final turn on the report rather than a
> one-line acknowledgement.

---

## The DM layer: 30 messages the room never saw

The channel is not the whole record. Five direct channels carried 30 messages,
and pairwise is where two of the run's better collaboration artifacts live.

**`dm:gameplay--shell` — two seats negotiate a contract the delegate then pins.**

**shell>** *(T+86:06)* `rtype#106` ask [4] asks shell to add a boss readout off
Bus `'boss'`. **What I need to know:** does `boss.js` emit hp, and in what form?
Proposal: `{phase, hp: 0..1}` to match the charge meter's convention.

**gameplay>** *(T+87:06)* Going with your proposal: `{phase, hp}`, hp a 0..1
fraction, **sent on every phase transition and every landed hit** — so the bar
moves between transitions too, not just when the phase label changes. Values:
`appear`, 1/2/3, `dead`. hp is already 0 by the time `dead` fires, so you do not
need a special case.

**shell>** *(T+97:48)* Confirmed. Your payload spec and mine are identical. Sent
lead the settled shape for `contract:bus-events`. Thread closed.

> Neither seat owned the interface; both owned a side of it. They settled the
> shape in three messages without consuming a single channel slot, then handed
> the delegate a finished contract to pin. This is the hub's routing rule
> working exactly as written — *two seats that must speak? DM.*

**`dm:engine--gameplay` — a handover across an ownership change.**
`engine` posts the `Stage.at(x, fn)` cue API as a pointer with the store row as
the authority (*"that row is the authority, this DM is just the pointer"*).
`gameplay`, after `powerups.js` was ruled to `engine`, sends its reverted draft —
*"you own the file outright, take what's useful and drop the rest, no explanation
owed"* — catches its own paste error and re-sends with the file attached, and
`engine` later closes the loop with what it kept and what it did not:

**engine>** *(T+112:06)* Overdue close on all three of these. The draft did land
and it did help — **you deserve to know what happened to it rather than have it
vanish into my file.** **Taken:** the bit-orbit shape… that `vx/vy`-zeroing is
the part someone re-deriving it gets wrong first.

Then, unprompted, nine minutes before delivery:

**engine>** *(T+121:12)* Pairwise, so it does not clutter the room. Your file,
your call — I have not touched it. `src/gameplay/boss.js` has three live
`console.error('DBG …')` lines. **They are invisible right now** because no shot
ever reaches the boss, so `onHit` never runs — **they will fail the gate the
moment your fix works.**

> A seat predicting that another seat's *fix* would trip the harness, and warning
> the owner privately rather than editing the file or reporting it in the room.

**`dm:engine--lead` — a governance dispute, and the delegate concedes.**

**engine>** *(T+127:06, `open`)* Six of your asks are pinned as engine debts that
the hub forbids engine from answering: `rtype#117, #122, #137, #139, #141, #143`.
In every one the ask text names **gameplay** (or gameplay + render); engine is
only in `to`.

**lead>** *(T+132:24)* Answering ask 1 properly this time. My previous DM was
`fyi`, which discharged nothing — **the same class of mistake as the one you were
reporting, so it is fitting that it happened here.** All six are closed. You
diagnosed it exactly right and it was my error, not a hub quirk. **I addressed
those messages to all four seats out of habit — wanting everyone to see the
reasoning — while the asks named only gameplay.** The hub reads `to` as
obligation, so it pinned you with debts it then forbade you from discharging.
`to` is not a mailing list.

**engine>** *(T+132:36)* Consumed and closed. One correction to my own record: I
sent dm#4 before this landed, treating your `fyi` as the answer. It read as one,
but you are right that it discharged nothing mechanically — so dm#4 was me
closing a thread the ledger still held open.

> Both seats caught themselves making the same mistake inside the conversation
> about that mistake. `to` as a cc-list is a real usability trap, and the fix
> they converged on — *address the seats the asks name, not everyone who might
> like to read it* — is a rule worth having.

---

## What went wrong: the plan was not collaborative

The single biggest gap between this run and
[v1](fleet-transcript-rtype.md). **No seat contributed to the plan.** There was
no contribution round, no slice negotiation, no architecture argument, and
nothing for any seat to sign on to. The delegate spent 909 seconds alone and
emerged with the phase order, the file ownership split, six contracts, two
rulings and 26 stub files already written into the tree. The first thing every
worker seat saw was a finished plan and its own assignment.

**The cause is in the operator's kickoff, not the delegate's judgment.** The
asks were:

> [1] **Post the plan**: the phase order **you are setting** in `phase:build`, the
> file/module ownership split…
> [2] **Assign** the first slice to EACH of engine, gameplay, render and shell **in
> one pass**…

That instructs a plan-then-assign shape and rewards speed to assignment. v1's
operator wrote one sentence with no structure, so its delegate had to open a
contribution round to find out what the seats even did. **The structured kickoff
bought parallel starts and cost collaborative design.**

What the fleet argued about instead was *details, after the fact, in public*:
`render` disputed the verification plan and had a step struck; `shell` had two
DONE criteria corrected as another seat's slice; `engine` disputed `World.all()`
and won an additive `snapshot()`, then later caught a three-clause terrain ruling
in which only two clauses could hold. Real disagreement, well handled — but all
of it downstream of a design nobody else shaped.

**Whether it cost quality is genuinely unclear from one run.** The plan was
good: the ES-modules ruling was correct and load-bearing, and no seat ever
proposed an alternative architecture. But that is not evidence the seats had
nothing to add — nobody asked them. The nine seam defects the delegate later
found are all integration defects, which is exactly the class a contribution
round surfaces early, and the delegate itself predicted the third one before it
happened.

**To test it properly**, the next run should split the difference: keep the
ten-point DoD and the phase discipline, but replace ask [1] with *"open a
contribution round: get each seat's slice, constraints and disputes on the record
before you record a plan."* That isolates the one variable. Until then, treat
this transcript as evidence about **execution** under a delegate-authored plan,
and v1 as the evidence about **plan formation**.

---

## What the hub did in these 136 minutes

- **A delegation that actually decided.** `lead` held `ruling` + `operational` +
  `reporting` + `proxy` scoped to the room, and used all four: it overruled the
  room charter on a technical point, flipped `phase:build` five times, ruled on
  file ownership disputes, and reported to the operator thread unprompted at
  every phase boundary. The operator answered nothing.
- **Contracts before code.** Six `contract:*` rows and two `decision:*` rows were
  in the channel store before any seat wrote an implementation line, with
  `CONTRACTS.md` as the normative long form. A seat coded against the contract,
  not against what currently existed — which is what let four lanes start
  simultaneously with no seat blocked on another.
- **Phase rows as a serialization primitive.** `phase:build` gated P1→P5; no seat
  started N+1 before the steward flipped the row. Each flip was backed by a
  published gate result, recorded as `decision:pN-accepted`.
- **One writer per file, enforced on testing too.** 29 files, five seats, zero
  merge conflicts. Two violations occurred, both on the delegate's own file, and
  the delegate's response was to remove the reason rather than punish the seats.
- **Cross-review as a store artifact.** `review:core-by-render`,
  `review:combat-by-shell` and the terrain review were `review:*` rows cited as
  evidence in the delivery. Two reviewers independently found the same HIGH
  defect from opposite directions.
- **Claim rows as the progress receipt.** Per-slice progress lived in
  `claim:<task>` rows, not in channel chatter — which is why 177 messages covers
  a two-hour five-seat build without the room becoming a status feed.

## What this run says about mixed-capability fleets

The two Haiku seats (`render`, `shell`) produced 51 of the 177 messages and
carried two full lanes to acceptance. They were not passengers. What made that
work was that the delegate gave them **falsifiable per-slice DONE criteria** they
could run themselves — `shell` worked out the input-latch fix *from the failing
check alone, before the delegate's correction reached it*. A gate a seat can run
is worth more to a smaller model than a longer brief.

The Opus seats did the work that needs holding many things at once: `engine`'s
six-pixel collision measurement, `lead`'s stage-wide flyability search. The
split was not assigned by tier — it emerged from who could answer which question.

## Honest note: what slowed the fleet down

Turn counts were lopsided in a way the collaboration does not explain — `shell`
53 driven turns and `render` 49, against `lead`'s 29 — and most of the excess
was mechanical, not deliberative. Three driver/hub issues account for nearly all
of it, and each is worth knowing about before running a fleet this shape:

1. **A delegate's fan-out message pinned every seat to the slowest.** `#10`
   carried one ask per seat; seats that answered theirs kept the obligation row
   until the last seat answered, and their turns were scored as failures.
2. **Failed turns lost their session**, so four of five seats cold-booted with no
   memory of the previous turn — visible in the ledger as duplicate completion
   reports (`shell` announced a slice done, then announced it starting) and
   duplicate claim rows for one slice.
3. **The delegate's work chunks were graded as reception passes**, because the
   supervision preamble displaced the prompt marker the driver classifies on.

None blocked delivery, and none originated with the agents. They are recorded
here because a transcript that shows only the good parts is not evidence.

---

To run a fleet like this yourself, start with
[collaboration.md](../collaboration.md) for the model,
[harness_guide.md](../harness_guide.md) to wire the seats,
[charters.md](../charters.md) for the room charter that carried the ten-point
definition of done, and `agora delegate --charter` for the brief the delegate
receives. For the contrasting shape — one sentence, no addressing, four economy
seats — read [fleet-transcript-rtype.md](fleet-transcript-rtype.md).
