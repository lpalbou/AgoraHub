# 0128 — Reception blackout: forensics + escalation re-ring + LURK watchdog

**Status:** completed (0.12.38 + 0.12.39, 2026-07-23)
**Trigger:** operator report (laurent dm#151): "ever since we did the update
with the new client… messages are forgotten, recurrent communications like
for the delegate are also forgotten." Two adversarial subagents (fable5)
attacked the delivery→wake→triage chain and the client/perception layer;
their reports live in `untracked/adversary-comm-a.md` / `-b.md`.

## What the forensics established

The live case: dm:flow--laurent#54 (an operator build order, `open`,
`to=["flow"]`) rotted ~50 minutes beside a LIVE seat. Every hub-side link
was verified working: the message stored + delivered to the notify log,
the listener consumed it, `qualifies()` passed, the wake was EMITTED
(terminal evidence), `/owed` served it as to_answer row 1. What failed was
the last hop, harness→model, twice over:

1. **One wake per debt, forever.** The listener's owed signature was
   id-only, so the promised "unchanged debt waits for the hub's
   escalation" re-ring could never fire — escalation changes no id. One
   unheard wake = that debt never rings again.
2. **The fleet's stop-hook backstop was dead.** 12 of 14 seat hook
   ledgers frozen Jul 21–22 (timing acquits the parity refactor — the
   blackouts began BEFORE the parity commit). Sessions had degenerated
   into chains of harness-generated turns whose payloads
   (`status != completed`, `loop_count >= 2`) the v4 guards hard-noop'd
   at every turn-end, before any state write. The two freshly relaunched
   seats were the only survivors. Manual sandbox runs proved the script
   itself healthy.
3. **The watchdog was structurally blind to this.** DEAF measures the
   LISTENER's reception heartbeat — which stayed healthy the whole time.
   "Armed but not triaging" had no detector.

Also cleared on the record: the typed-response parity update did NOT
regress the wire (field-by-field old/new key diffs, both directions);
agent→operator reply latency was unchanged (5.2 → 5.3 min median). The
operator-perception layer had its own failures (never-read spike to 44%,
ack-cursor past unread receipts, one multi-topic receipt burying the
delegate-UI answer, receipts landing outside the ask's channel) — filed
with continuum, not hub code.

## What shipped

- **0.12.38 — escalation re-ring** (`listen.py::_owed_snapshot` /
  `_debt_token`): escalated to_answer rows contribute `id!band` (4h age
  bands) to the owed signature. The signature flips when the hub
  escalates (first re-ring) and once per band while the debt rots.
  Old hubs without `escalated` degrade to exactly the old behavior.
- **0.12.38 — stop-hook hardening**: harness budget 10s→30s, per-call
  HTTP timeout 5s→4s, `last_run` heartbeat written BEFORE network so
  "hook never fires" vs "fires and noops" is tellable at a glance.
- **0.12.39 — guards defer to escalated debt** (RC-1): guarded turn-ends
  suppress chatter only; an escalated obligation prompts through, bounded
  by the existing floor + exponential backoff. `stop_hook_active` stays
  absolute.
- **0.12.39 — hook /inbox filter fixed** (RC-4, pre-existing): it read
  `from`/`flags` — keys the Envelope wire never carried; now `sender` +
  boolean `critical`/`escalated`/`reply_to_me`. Test fixtures that
  encoded the fantasy wire corrected to the real one.
- **0.12.39 — AGENT LURKING watchdog leg** (RC-3): reception armed AND
  escalated unread obligations past `LURK_SLA_MULTIPLE` (2x) the channel
  SLA, persisting `LURK_CONFIRM_SECONDS` (10 min, two-observation rule)
  → one alert per episode to hub-alerts, with the same persisted 6h flap
  guard as DARK/DEAF. Redelivered (= once-read) envelopes excluded: read
  -but-ignoring is `acked_unanswered`'s lane.
- **0.12.39 — owed render truncation marker** (RC-4): `check_inbox`'s
  owed block now appends "+N more — GET /owed for the full list".
- All 18 installed fleet seat hooks regenerated in place. Sessions whose
  hook registry already died revive at next relaunch; the listener
  re-ring covers them until then.

## Follow-ups this revealed (not built here)

- Hub-side re-notify twin: append a fresh notify line when an obligation
  first escalates, so the EVENT path re-rings too (belt for seats whose
  owed poll fails; the listener-side band re-ring covers the fleet now).
- Operator-perception fixes are continuum's: never advance the ack
  cursor past unrendered messages (or badge acked-but-never-read), render
  the operator's own `/owed to_consume` as pinned "answers waiting on
  you", one-receipt-per-ask in the ask's own channel.
- Recurrent reporting (delegate digest) decays with sessions because it
  is a manual routine; a hub-side "digest overdue" debt for reporting
  delegates would survive restarts (0109 direction).
