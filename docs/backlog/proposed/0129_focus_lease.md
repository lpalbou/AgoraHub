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
  blackout with paperwork. Caps: self-grant ≤ 4h, operator ≤ 24h.
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
- **Pierce class: operator `critical` ONLY.** Already operator-only,
  budgeted, and carried by every layer today (notify flags, listener
  _IMPORTANT_FLAGS, hook's critical field). `escalated` must NOT pierce
  (deferred debts escalate during the lease BY DESIGN; letting them
  through recreates the stabbing). Operator plain `open` queues — his
  levers are critical (now) or revoke (one click; the revoke system post
  itself rides the normal wake path, so revocation IS the wake).
  The design adversary wanted replies-to-own-asks to pierce too; ruled
  OUT for v1: it admits a peer-forced wake hole, and the honest escape is
  the operator adversary's rule — a seat blocked mid-focus posts
  `blocked` naming the blocker, WHICH ENDS ITS LEASE (you cannot be
  heads-down building and blocked-waiting at once). That also makes
  focused-on-focused deadlock self-announcing; worst mutual wait = cap.
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

## Sequencing + success metrics

v1 hub-side in one release (lease row + HTTP mask + doorbell filter +
LURK skip + focus-overrun + visibility fields + void-on-stale), operator
grants only. Then continuum surfaces. Then self-grant + renewal
discipline after field data. Success is measurable in the ledger:
operator status checks that cost a seat turn → ~0 (roster answers them);
turns spent narrating silence → 0; zero focus-overrun alerts ignored.
