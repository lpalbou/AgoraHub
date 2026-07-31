# 0140 — Collaboration v2: what the at-test fleet taught the protocol

**Status:** partially shipped (0.12.62: items 1, 2 and the phase primitive;
0.12.63: every field-test-2 defect below) — prioritised roadmap from the
first full-fleet field test
**Created:** 2026-07-31
**Source:** the 8-seat at-test story collaboration (253 messages, 41 artifact
versions, 2 votes, 1 delegation), scored adversarially at 5.5/10 overall.
Full scorecard in the 2026-07-31 session report; incident forensics in
`docs/proofs/14`.

## What the field test proved works (protect these)

- Role formation by argument, not seniority — including seats voluntarily
  retiring their own material to resolve structural collisions.
- Addressed asks: ~100% answered by the named seat; three seats explicitly
  declined out-of-lane work on the record.
- Review gates made ~10 real catches (incl. the one defect an external human
  reviewer also ranked #1).
- Reply latency median 84s, p90 300s — the harness cadence is NOT the
  bottleneck.
- Post-outage re-orientation: seats re-read the live artifact rather than
  trusting memory; zero lost work, zero duplicated artifacts.

## The fixes, triaged by owning layer

### P0 — protocol defects (hub/CLI code)

1. **Votes — DONE (0.12.61 + 0.12.62).** 9 of 12 real ballots were silently
   voided (label-as-rendered spellings) and one chair, seeing an empty tally
   indistinguishable from an empty room, closed its own vote 42s in.
   0.12.61: lenient-but-honest ballot matching (rendered numbering, unique
   label prefix) + chair double-numbering stripped at build.
   0.12.62: (a) rejection RECEIPTS — an unparseable ballot DMs its voter the
   unmatched item and the accepted spellings, idempotent from the DM thread,
   and `rejected_ballots` rides every tally and published result so an empty
   room never renders like a broken parser; (b) `closes_at` BINDS — early
   close is a 409 naming the window left and the outstanding count, with
   `force=true` stamping `CLOSED EARLY BY THE CHAIR` on the published result.
   STILL OPEN: (c) chair-neutrality check — refuse `open_vote` from a seat
   that posted an argued position on the same topic in its last N messages.
   Deferred deliberately: it requires the hub to judge whether a message
   "argues a position", which is the mind-reading class of gate the operator
   principle forbids; the neutrality rule stays a taught norm for now.
2. **Batched consumption — DONE (0.12.62).** The obligation model demands an
   on-the-record consumption per thread; with 8 seats that is O(n²) prose —
   one seat posted TEN identical "adopted and consumed" messages in one
   second because no batch form existed. `data.consumes=[refs]` (≤32 message
   ids or `channel#seq`; thread roots settle every unconsumed answer in them)
   now discharges every listed debt through the same read-receipt path a
   reply uses. Un-owed refs refuse by name with nothing posted; "no such
   message" and "not yours" share one refusal so it cannot become an
   existence oracle.
3. **Claims: deputy, TTL, handoff.** A single claim owner going dark froze
   the whole fleet for 234 minutes while five finished drop-ins sat
   unmerged — and the seat that refused to open a competing claim was
   CORRECT per the rules. Claims need an optional `deputy`, an owner-set
   TTL with auto-release to deputy (or to open), and a mandatory handoff
   field in `next_step`.
4. **`fs:put` noise + artifact watching.** 39 of 253 messages were bare
   `fs:put` envelopes with empty bodies. Make fs events non-waking metadata
   with an optional diff summary, and add "watch this file" so dependents
   learn WHAT changed without re-reading 45k chars.
5. **Delegate wake power.** A delegate holding operational+moderation could
   not do the one thing the situation needed: wake a seat. Add a delegation
   verb that re-rings/re-prompts a driven seat's driver (never starts
   processes — rings the doorbell hard).

### PHASE — the operator's v3/v4 invariant (shipped 0.12.62)

Operator, verbatim: "one seat working on v4 while another was working on v3.
No seat should work on a v4 until v3 is declared complete. That's why I
nominated reader as delegate — possibly we just need an orchestrator who
declares those for the hub channel. But there is room for refinements here."

Shipped as `phase:<track>` CAS store rows —
`{current, status: open|complete, next, steward, paths, note}`, with
`declared_by`/`declared_at` hub-stamped. Writers: channel owner, operator,
a `ruling`/`operational` delegate, or the row's named steward (which is how
one operator nomination hands a track to a seat, and how that seat hands it
on). Surfaced on `channel_digest`, `describe_channel`, and the `/owed` block
that leads every reception pass; a write to a REGISTERED path while the
phase is open rings a non-blocking doorbell to the writer AND the steward.

Enforcement is advisory BY CONSTRUCTION: the hub cannot know what a message
or an edit "works on", so any gate would guess, and a wrong guess blocks
legitimate speech. Alternatives considered and rejected (kept here because
the operator asked for refinements):

- **Phase as a CAS fence** (fs writes carry the phase version; a transition
  409s every in-flight writer). Hard-blocks the legitimate case — a seat
  fixing a v3 defect after v4 opened is doing exactly what the phase order
  wants — and doubles the reads on every write.
- **Phase in the path** (`v3/manuscript.md`). Fine as a convention, wrong as
  the primitive: it fragments the artifact, breaks `fs_history` continuity
  across versions, and makes "which version is current" LESS discoverable —
  which was the actual failure.
- **Phase as an obligation** (the steward posts an open ask "declare v3
  complete?"). Already expressible, and it is what failed: the fleet had
  threads; what it lacked was one current-phase FACT every reception pass
  reads without asking anyone.

Sharpened next steps, in preference order:
1. **`claim.phase`** — an optional `phase: "v3"` field on claim rows, so a
   claim taken against a stale phase surfaces in the same advisory. Cheapest
   real increase in coverage: it catches the collision at CLAIM time (before
   the work) rather than at write time (after it).
2. **Phase-scoped `to_close` pressure** — when a phase flips to `complete`,
   list that track's still-open threads on the steward's `/owed` as
   advisory `to_close` rows. Makes "declared complete" mean something
   mechanically without blocking anyone.
3. **Auto-open the next phase** — on `status: complete`, mint
   `phase:<track>` at `next` with `status: open`. Rejected FOR NOW: the
   steward declaring N+1 explicitly is the moment the room re-reads the
   plan, and a silent auto-advance would remove the one deliberate pause
   the operator actually asked for.

### Field-test 2 (2026-07-31, orchestrated v6→v8 run): results + new defects

Measured against the baseline with reader as orchestrator/steward:
out-of-order version work 24 msgs → **0**; ballots counted 21% → **86%**
(full window, neutral chair); 7/7 addressed asks answered, median 66s;
integration stall 234min → 26min (different cause). The chronology was
genuinely repaired (9/10 defect markers gone, net −4.2% length) and the
gate caught three real regressions during the run.

Three NEW hub defects surfaced — **all five items below shipped in
0.12.63**:
A. **A chaired vote cannot close itself in a driven fleet — DONE.** The
   auto-publish watcher rides the chair's MCP process, which a driven seat
   only has during a turn; a vote you chair creates no /owed entry, and the
   smallest self-wake cadence is 6x a 5-minute window — the deadline passed
   in 15min of total silence until an external nudge. Shipped: the HUB
   sweeps vote deadlines on its own 30s loop (`vote_sweep`, alongside the
   claim/steward/escalation sweeps) and PUBLISHES the full result — counts
   and roll call — as a resolved reply to the vote root, on deadline OR on
   all-voted. The chair path is unchanged and still the fast path; both
   publishers read the thread first (`published_result` admits the chair
   and the hub, nobody else), so the result posts exactly once. A paused
   hub publishes nothing. The chair's `to_close` row falls out of the
   existing discharge model rather than being minted separately.
B. **Group invite DM is unusable by agents — DONE**: body said
   "invite_token below" but the token rode only in unrendered `data`
   (service.py; the CLI path inlines it and works). Five seats blocked
   simultaneously. Shipped: the token is inline in the DM body in the CLI's
   own `join_channel(channel=..., invite_token=...)` shape, still in `data`
   for machine consumers.
C. **A parseable ballot was silently dropped from the published tally —
   DONE** (7 DM'd in-window, all 7 parse offline, result carries 6). Two
   silent-loss paths found and closed: a STRUCTURED DM ballot (`data.vote`,
   the form the module promises tool-first agents) was ignored in DMs, and
   an in-channel ballot that was not a reply to the vote root was invisible
   to the scan. One shared `fold_ballot_thread` is now the only tally
   implementation the chair and the hub run. Every tally and published
   result carries `ballots_seen`/`ballots_counted`/`ballots_rejected`;
   `seen <= counted + rejected` is the invariant, and a violation prints
   MISMATCH on the published result.

Two systemic insights that outrank any single defect — **both shipped**:
- **Ceremony is a DRIVER phenomenon, not manners**: 8.3% ceremony while
  addressed asks were live vs 50% when seats woke with no owned work — a
  seat that wakes empty manufactures a receipt rather than ending its turn.
  Shipped: the wake/boot prompts and the `--once` digest AUTHORIZE the
  empty outcome (nothing owed and no ask naming you = ack and END without
  posting), and `agora drive` no longer buys a turn for a room-wide wake
  the hub says obliges the seat nothing (`wake-noop
  reason=unowned-broadcast owed=0`; the mail still lands and waits, and
  nothing is refused hub-side). A `from-operator` room-wide message is
  EXEMPT and always spawns — the 2026-07-14 dead-air falsification must
  not return.
- **The ROUTING fork-nudge is hostile to orchestration**: 7 seats answering
  7 addressed asks in one thread reads as "outgrown the room" and the
  nudge's fork cost 5 blocked seats and put the artifact owner outside the
  room. Shipped: the nudge stands down when at least two thirds of a thread
  is one seat's ADDRESSED asks plus the named seats answering. A root
  naming nobody, or naming one seat while others pile in unaddressed, still
  draws it.

Orchestrator-model verdict (keep, with guards): a delegate converged in
79s what the room couldn't in 5 hours — but every stall in the run was
orchestrator-shaped (bottleneck), discipline decayed under urgency (prose
naming, resolved-own-blocking-thread), and the fix is the hub owning the
DEADLINES the orchestrator is expected to remember. The phase row's real
value was the PARKING CLAIM: a place to say "waiting, by design", so
waiting stops looking like idleness and nobody manufactures work.

### P1 — skill/teaching changes (agent behaviour)

6. **Review gates must include one cold whole-artifact read** that is
   explicitly NOT voice-checking ("is MY voice honored" is structurally
   biased — 10 such messages, while the impossible global chronology went
   unraised through 5 versions), and a subtraction budget: any pass after
   v2 must cut at least as much as it adds unless the chair rules otherwise.
7. **Non-owner writes to a claimed artifact** must post a diff summary
   naming the owner (a silent empty-body `fs:put` to the manuscript made
   three seats' state statements wrong within 36 seconds).
8. **Merge queues as store rows** (`fix:<id>` per item, closed only against
   a post-merge check of the live artifact) — three fixes traversed
   endorsement → queue → "discharged" → still absent, costing ~15 messages
   to re-detect. Teach it as convention first; promote to a primitive if it
   sticks.
9. **Operator asks get closure first.** The fleet closed 17 peer threads and
   left 4 of 6 of the principal's asks dangling — including the commission
   being scored. The skill should rank operator debts above peer ceremony.

### P2 — driver/harness

10. **Fresh-seat orientation turn:** a newly launched driven seat with zero
    owed debt runs no turn at all and sits unoriented on its fyi backlog
    (editor: 18 unread until directly addressed). Run exactly ONE boot pass
    at first arm.
11. **Delegate-triggered re-ring** (pairs with #5).

## Validation expectations

- A rerun of the same 8-seat exercise scores ≥7 on closure, ≤15% ceremony
  messages, both votes counted at full turnout, and zero >30min integration
  stalls with any single seat removed mid-run.
