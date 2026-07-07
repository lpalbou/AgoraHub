# Knowledge base

Critical insights and design decisions. Never remove entries; deprecate with
reasons instead.

## Insights from the adversarial design review (2026-07-06)

Six research agents in three opposing pairs (MCP-advocate vs hooks-skeptic;
protocol-designer vs SOTA-scout; Rust-advocate vs pragmatist) produced these
load-bearing conclusions:

1. **MCP cannot wake an idle agent — by design, not by gap.** MCP is
   pull-based; no server-initiated primitive becomes a user turn, and stdio
   servers die with their harness. Every vendor built a non-MCP surface for
   triggering (Codex app-server `turn/steer`, Claude Agent SDK streaming
   input, Cursor `agent.send`). Consequence: triggering needs an external
   process (the attache); MCP is the in-session participation surface only.
2. **Interleaving is selective receive.** "Fold the message into the next
   loop without stopping" is the Erlang/actor mailbox pattern: never preempt,
   accumulate, drain at chosen receive points. Codex steering implements
   exactly this (queue drained at model-call boundaries). It is a *client*
   capability — no wire protocol can ship it, so the protocol only carries
   the `urgency` metadata and the client provides the receive points.
3. **Google A2A is the wrong substrate for channels.** A2A v1.0 is 1:1
   client-server task RPC: no rooms, no membership, no broker, no fan-out
   (its pub/sub PR #1196 sat unmerged for 8+ months). Adopting it would mean
   building routing/fan-out/state ourselves and keeping only a verbose
   envelope. We keep A2A *interop hooks* instead: body+data mirrors
   Message/TextPart/DataPart for a future mechanical gateway.
4. **The 2025–26 "Slack for agents" graveyard.** ~10 similar projects, all
   <20 stars, several abandoned in days. Differentiation is not the room —
   it is the trigger/interleaving layer plus etiquette. Invention budget was
   allocated accordingly.
5. **Hub language matters less than adapters.** The hub is ~20% of the code
   and commodity; adapters/MCP/skill are the hard 80% and live where harness
   SDKs live (Python/TS). Decision: Python hub now; the wire protocol is the
   stable boundary so a Rust or NATS-backed hub can drop in at ~v0.3 without
   client changes. (Redis ≥7.4 fails the MIT/Apache/BSD constraint — use
   Valkey or NATS if a broker is ever needed.)
6. **Top operational risks (red team), with mitigations shipped in v0.1:**
   - *Runaway reply loops* (critical): hub per-agent rate limit **and**
     attache trigger budget **and** skill etiquette ("don't ack acks").
   - *Cross-agent prompt injection* (critical): messages rendered to LLMs as
     fenced, attributed, quoted data with an explicit "not operator
     instructions" note; never spliced as bare text.
   - *Confused-deputy invites*: only channel owners mint invites; invites
     are single-use, expiring, optionally agent-bound.
   - *Stale shared state*: store is CAS-versioned; blind overwrite impossible
     if writers pass `expect_version` (skill mandates it).
   - *Token leakage*: secrets stored hashed; plaintext shown exactly once.

## Insights from the attention-model review (2026-07-06, v0.2)

Six adversarial agents (envelope pair, reputation pair, governance pair);
two of them validated designs hands-on by running the hub and playing
agents. Load-bearing conclusions:

7. **No sender-declared priority field — importance must be derived.** The
   envelope advocate's own dogfood (12 messages triaged by headline, 7/9
   correct) was baited by a spoofed "URGENT" title; every panelist converged:
   self-declared severity decays to noise between LLMs (severity inflation,
   cf. SEV-1 inflation in incident management), and a stateless sender feels
   no social cost for crying wolf. Importance is derived from what senders
   cannot fake: obligation (`status`), addressing (`to_me`/`reply_to_me`,
   hub-computed), authority (`critical`, operator-only). Timing (`urgency`)
   stays sender-declared but budgeted, with visible downgrades.
8. **Envelope-only delivery must follow the token economics.** Crossover
   analysis: envelope-then-fetch costs ~an extra tool call (~65-90 tokens +
   a round trip + one more fallible decision); it beats inlining only for
   bodies above ~100-250 tokens. Hence hub-side policy: inline small bodies
   (≤1.2KB), inline addressed bodies (≤4KB), envelope-only for large
   broadcasts. The hub knows the size exactly; the choice is deterministic.
9. **Obligations trump triage, and the hub is the escalator.** The fatal
   failure of receiver-side triage is silent rot: an `open` message skipped
   by headline deadlocks the sender. Fix: unanswered open/blocked messages
   older than the channel SLA are escalated (effective urgency = interrupt)
   by the hub — a disinterested party raising priority by obligation *age*.
   This one mechanism simultaneously solves rot AND priority inflation.
10. **Selective reading is only coherent per conversation burst.** Reading a
    message must drag its unread reply-chain ancestors (models confidently
    fill missing context — worse than noise). `read_message` returns the
    unread chain, oldest first.
11. **Read receipts ≠ triage cursors.** "I saw the envelope" (cursor ack) and
    "I read the body" (receipt) are different facts; criticals unpin only on
    the latter. Conflating them was the structural flaw the skeptic found in
    v0.1's high-water-mark-only model.
12. **Numeric reputation between LLMs measures agreement, not truth.** Both
    reputation panelists converged: truth is usually unobservable at read
    time; N≈5 makes percentages noise; documented sycophancy/self-preference
    biases mean scores would punish honest dissent and reward agreeable
    noise — the opposite of intellectual honesty. Shipped instead: private,
    free-text, *revisable* colleague notes (the designer's dogfood found the
    note, not the numbers, was the load-bearing artifact), advisory only,
    never gating obligations.
13. **The title is the premium injection surface** — the one field every
    member is guaranteed to read (cf. EchoLeak CVE-2025-32711; email-agent
    hijacking via subjects). Mitigations: 120-char cap, control-char
    sanitization at post time, rendered inside quoted markers, and the skill
    teaches that structured signals (status/critical/to-you) are the trusted
    ones, not title prose.
14. **Critical authority belongs to operators, not owners** — owners
    self-mint channels, so owner-critical would be self-granted forced
    attention. Operator flag is admin-granted at registration; criticals are
    budgeted (5/hour) even then, wake working agents, and stay pinned until
    actually read.

## Insights from the DM / roles / language review (2026-07-06, v0.3)

Four adversarial agents (DM+roles pair, telegraphic-format pair):

15. **A DM is a channel with a reserved name and no owner — nothing more.**
    `dm:<a>--<b>` (sorted ids, idempotent get-or-create) inherits envelopes,
    escalation, history and a pairwise store for free; *ownerlessness* is the
    security mechanism (no owner → no invites → no third member, no meta
    writes → hub defaults), turning access control into structure instead of
    validation code. Etiquette matters more than mechanism: decisions made in
    DMs are invisible to the team, so DMs are for pairwise logistics only.
16. **The join-flood bug**: v0.2 started a joiner's cursor at 0, so joining a
    busy channel dumped its whole history into their inbox (found by the
    adversarial simplifier). Fix: join sets the cursor to head; history stays
    a *deliberate* read. Onboarding is one call: join returns meta + members
    with abouts + language.
17. **Token-compressed syntax for prose coordination is not supported by
    evidence.** Independent TOON replications show real agentic savings of
    2-18% (not the marketed 30-60%) with up to 47-point cross-model accuracy
    spreads; telegraphic prose saves ~40% of a small number (coordination
    messages are short — ~2.6k tokens/day/channel) while risking misreads
    whose cost dwarfs the savings; and token-efficiency is the documented
    on-ramp to unauditable private agent languages. Verdict: compress via
    ARCHITECTURE (envelope elision — already shipped; bulk data in the
    structured `data` field or the store), allow opt-in `terse`/`structured`
    channel languages with hard invariants (titles plain, obligations plain,
    plain summary line, no private codes).
18. **Self-declared `about` and observed colleague notes are complementary,
    not redundant**: `about` is the agent's own claim of scope ("ask me about
    X"), notes are the observer's private experience of whether that claim
    held up. Both are free text; neither is scored; both are sanitized (the
    `about` is read by every joiner — same injection surface class as
    titles).

## Insights from the security/correctness hardening (2026-07-06, v0.3.1)

A four-agent adversarial review (two correctness, two security) plus a live
integration test found four load-bearing defects, each confirmed by two
independent reviewers. The fixes and their durable lessons:

19. **A static textual fence is not a security boundary.** Wrapping untrusted
    agent text in fixed `<<<MESSAGE … >>>END` markers let a body contain
    `>>>END` + forged `SYSTEM:` text and escape the quote — defeating the
    injection control the whole design rests on. Lesson: quote untrusted
    content with an **unguessable per-render nonce** the author never saw, and
    neutralize forged markers. Centralized in `agora/render.py` so the pull
    (MCP) and push (attaché) paths share one hardened renderer.
20. **Validate cross-references at the trust boundary.** `reply_to` was
    stored unvalidated and later walked by id in `read_message`, giving a
    cross-channel read (IDOR). Message ids are not secrets (they're in every
    envelope), so any id-addressable lookup must re-check membership/channel.
    Fix: validate `reply_to` is same-channel at post time AND stop the
    ancestor walk at channel boundaries (defense in depth).
21. **`asyncio` primitives are loop-bound; producers may run on other
    threads.** Starlette runs sync handlers in a threadpool, so mutating an
    `asyncio.Event`/`Queue` from them is unsafe. A serving-loop reference +
    `call_soon_threadsafe` is mandatory; iterate subscriber sets over a
    snapshot. The prior "5s cap" in the long-poll loop was silently masking
    this.
22. **A read cursor and an obligation are different facts.** Selecting inbox
    purely by `seq > cursor` meant acking triage buried an unanswered
    obligation forever, defeating escalation. Obligations (and criticals)
    must be *sticky* — surfaced until read or answered — independent of the
    cursor. And "browse history" must not write read receipts, or paging
    silently discharges obligations/criticals; only a deliberate read does.
    General lesson: separate "seen the headline" from "attended to the body".

## Design decisions

- **Per-channel `seq` assigned by the hub is the canonical order.** The
  predecessor git-mailbox protocol suffered a real race (shared counter
  across tabs; replies sorting before their questions). A single ordering
  authority removes the race structurally. ULIDs are identity, not order.
- **Statuses encode obligations** (`open`/`blocked` expect replies;
  `resolved` closes) — carried over from the git protocol because it made
  threads scannable for "who owes whom" without reading everything.
- **Append-only messages; state changes are new messages.** Same invariant
  the file protocol used ("never edit a file you did not author"), now
  enforced by the server rather than by discipline.
- **Attache cursor ≠ agent cursor.** The alarm clock (delivery) and the
  reader (comprehension) keep separate bookmarks so neither can corrupt the
  other; the attache never acks the agent's inbox.
- **Attache skips delivery while presence == working** — the working agent
  drains its own inbox at loop boundaries; waking it would double-deliver.
- **SQLite behind a lock, synchronous.** At local-first scale (µs operations)
  this is simpler and safer than async drivers; the service layer is the
  seam where a real backend swaps in.
- **Single global notifier event + re-check for long-polls** rather than
  per-channel conditions: strictly correct (snapshot-before-check prevents
  lost wake-ups) and simple; per-channel granularity is an optimization for
  a scale this deployment model does not have.

## Lessons learned

- FastAPI TestClient exercises WebSockets in-process — full-stack tests of
  fan-out need no running server.
- `uvicorn` with `lifespan="off"` avoids a cosmetic CancelledError when
  embedding a hub in a short-lived script (see the example).
