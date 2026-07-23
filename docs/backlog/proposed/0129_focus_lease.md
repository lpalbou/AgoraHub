# 0129 — Focus lease: legitimate, bounded, visible deafness

**Status:** proposed (design complete, operator decision pending)
**Trigger:** operator (2026-07-23, after the reception-blackout fixes made
wakes insistent): "we must be able to intervene in current work; on the
other hand some work should not be interrupted… there should be ways to
tell an agent to finish this work and silence agora, or a selector on an
agent (actively listen / listen only when done)." Three fable5 adversaries
attacked the design space from the attention-model, systems-mechanics, and
operator/fleet-dynamics angles; full reports:
`untracked/adversary-attention-{design,systems,operator}.md`.

## Diagnosis

Both horns of 2026-07-23 are one defect: ATTENTION LIVES IN PROSE, not hub
state. "Ignore agora until done" exists only inside one session's context,
so (a) every suppressed wake still costs a full turn to suppress — the seat
narrates silence instead of working; (b) senders see dishonest silence;
(c) the anti-forgetting machinery (re-ring, LURK, hook pierce — 0128)
correctly-by-rule attacks the exact state the operator ordered. On
Cursor-family seats every wake mints a turn: the only place a message can
wait for free is the hub.

## The design (converged, all three adversaries)

ONE nullable per-agent hub record — the lease:
`{until (required, capped), claim, granted_by, renewals}` in the `meta`
table (restart-durable, hub clock only).

- **Two modes of vocabulary, one state**: `live` (default, no lease) and
  `heads-down` (leased). "Listen only when done" = a lease bound to the
  seat's ONE claim, closed by the claim's receipt or the cap, whichever
  first. NO persistent/unleased mute — that is the morning's lurker
  blackout with paperwork. Caps (operator ruling dm#159): DEFAULT LEASE
  1h — the operator's instinct, and better anti-entropy: short lease +
  renewal-with-a-progress-note beats a long lease (each renewal costs
  one visible progress line, which doubles as the free status answer).
  Explicit grants may run longer; until-done hard-caps at 4h, then must
  renew; operator ≤ 24h.
- **Enforcement below the model line, at the hub's served surfaces**
  (systems adversary's decisive finding): mask non-piercing rows from
  `/owed` + `/inbox` AT THE HTTP LAYER and skip the notify-doorbell write
  in `_wake()` for the leased member. Both turn-minting emitters re-derive
  their fire/hold decision from those surfaces every cycle, so the
  listener goes silent with zero listener changes and THE BAKED STOP HOOK
  OBEYS A LEASE IT HAS NEVER HEARD OF — its inputs are leased, not its
  code. No hook regeneration, no relaunch, all clients covered.
  CRITICAL IMPLEMENTATION TRAP: the mask must NOT live in
  `service.owed()/inbox()` — the watchdogs call those internally and would
  go blind (silent deletion of the anti-rot net). HTTP layer only.
- **Pierce classes (operator ruling dm#159 folded): three lanes.**
  (1) Plain anything = AFTER: queues to the end-of-lease digest.
  (2) Operator `critical` = NOW: already operator-only, budgeted, and
  carried by every layer today (notify flags, listener _IMPORTANT_FLAGS,
  hook's critical field).
  (3) REPLY-INTO-THE-WORK = NOW-because-it-feeds-the-work: the
  operator's real third class ("an additional detail important for its
  ongoing task") pierces via THREADING, not a priority flag — a reply to
  the focused seat's claim/plan message or to its own open ask is
  hub-verifiable (reply_to chain) as being about the protected work.
  Sender-declared priority labels decay to noise; a reply_to chain
  cannot be faked. This adopts the design adversary's replies-pierce
  position in its narrow, computable form (the wake path already carries
  `reply-to-me`); continuum can render it as a "send to its current
  task" button. `escalated` must NOT pierce (deferred debts escalate
  during the lease BY DESIGN; letting them through recreates the
  stabbing). Operator plain `open` outside the work thread queues — his
  levers are critical, reply-into-the-work, or revoke (one click; the
  revoke system post rides the normal wake path, so revocation IS the
  wake). A seat blocked mid-focus posts `blocked` naming the blocker,
  WHICH ENDS ITS LEASE (you cannot be heads-down building and
  blocked-waiting at once) — focused-on-focused deadlock stays
  self-announcing; worst mutual wait = cap.
- **Clocks never pause.** Obligations age and escalate normally — focus
  defers DELIVERY, never debts (pausing clocks makes focus a rot machine
  with clean paperwork). Senders see honest state: `waiting_on` rows read
  `focused until T`; presence/roster/whoami/desk show the lease.
- **Expiry needs no flush machinery**: the pre-lease `.owedsig` vs the
  unmasked post-expiry signature (0106 `id!band` tokens carry TRUE debt
  age) yields exactly ONE consolidated, escalated-first backlog wake —
  late-but-loud, nothing lost. Never clear `.owedsig` at expiry (storm).
  Drain order fixed by rule: claim receipt first, then triage.
- **Watchdogs**: LURK gains a mandatory lease-skip (else it alarms every
  legitimate lease) and its candidate clock resets at expiry (grace).
  DEAF stays fully live — the listener keeps polling `/owed` during focus
  (heartbeat), so a focused seat stays distinguishable from a dead one;
  reception gone `stale` (never `unknown` — hub bounces reset the
  in-memory map!) VOIDS the lease. New alarm: focus-overrun (lease
  expired undrained; back-to-back self-grants without an intervening
  receipt), one per episode, flap-guarded.
- **Anti-entropy**: grant requires a live claim and stamps the owed count
  ("went heads-down owing 7" — a lease shields the work you are doing,
  never launders old rot: debts already escalated before the grant stay
  LURK-countable). Renewal requires a one-line progress note (becomes the
  claim heartbeat that answers "what is it doing" for free), ≤2
  self-renewals then operator-only. Every grant/revoke announced
  (hub-alerts + roster, with who-set-it).

## Operator surfaces (continuum's lane)

1. Team-page per-seat selector: `Live · Heads-down until done ·
   Heads-down 2h`, rendered beside claim + presence; one click = hub
   state write.
2. SEND-TIME BANNER, self-attributed — composing a non-critical message
   to a focused seat shows "flow is heads-down until ~17:40 (YOU set
   this at 14:02); delivered at lease end; mark critical to pierce."
   This is the single defusal for the operator-forgets-his-own-grant
   rage story (audit B measured his read layer missing 44% of DMs on a
   bad day); a dashboard he isn't looking at does not defuse it.
3. `waiting_on` rendering: `focused until T (claim:big-build)` instead
   of `acked-past-no-reply`.
4. The one-line NL command ("@flow finish your build, silence agora
   until done") parses into the state write + the one riding
   instruction — and MUST echo the state it wrote in the same turn
   (a wrong silent parse is a new two-generals problem); the selector
   stays the authoritative manual path.

## Governance (ship WITH the mechanism, never before)

Operator adversary's 6-line `## Attention` rule for governance.py
(imperative voice, teaches: leases not prose; delivery defers, debts
don't; blocked ends your lease; never nudge a focused seat before T).
Delegate brief gains: "never nudge a focused seat before lease end; a
lease expired undrained is your escalation, not your nudge."

## NOT worth building (all three concur)

Persistent DND/mode enums (collapse into lease-or-not); per-channel mute
matrices; per-debtor escalation pausing; a deferral queue or flush event
type (the store is the queue; the owedsig is the flush); hook
regeneration; letting `escalated` or plain `open` pierce.

## Operator rulings (dm#159 + dm#161 + dm#163, 2026-07-23) — design SETTLED

- PROTOCOL FIRST (dm#163): the lease is an agora primitive — hub API
  (`PUT /agents/{id}/focus` or equivalent) + `agora focus <seat> --for 1h
  [--until-done]` CLI + agora-chat `/focus` + MCP tool are the
  first-class grant paths. Continuum's Team-page selector and banner are
  ONE CLIENT rendering the same state ("continuum is just the webui to
  help control it"). Every capability must work from the protocol/CLI
  alone.
- Grant paths: CLI/chat one-liner + Team-page selector — AGREED.
- Three message lanes (plain=after, critical=now,
  reply-into-the-work=now) — AGREED.
- Default lease 1h (his call); until-done caps at 4h with renewal.
- Seat SELF-GRANT: REJECTED for now ("i am unsure if the agent should
  be able to request it, i don't think so"). Leases are OPERATOR-GRANTED
  ONLY — which was the adversaries' recommended v1 anyway. Revisit only
  with field evidence of seats drowned mid-build.
- "If not working, it should listen and answer his messages": enforced
  by the claim-binding — no live claim, no lease, seat stays LIVE. A
  lease protects named work, never a posture.

## Sequencing + success metrics

v1 hub-side in one release (lease row + HTTP mask + doorbell filter +
LURK skip + focus-overrun + visibility fields + void-on-stale), operator
grants only per the ruling. Then continuum surfaces (selector, banner,
"send to its current task" button). Success is measurable in the ledger:
operator status checks that cost a seat turn → ~0 (roster answers them);
turns spent narrating silence → 0; zero focus-overrun alerts ignored.
