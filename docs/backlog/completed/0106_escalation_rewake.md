# agora-0106 — escalation re-wake (DEMOTED to a backstop; root-cause first)

- **Origin**: 10h communication audit (2026-07-20), then operator ruling
  dm:agora--laurent#42: "this should not need escalation, identify the
  root problem on why the agent didn't respond and comply."

## Ruling: escalation is the WRONG primary fix

Root-cause analysis split the overnight non-response into two classes,
and re-firing wakes helps neither's real cause:

- **Class A — session/machine death (the 01:40-07:00 hole).** The DB
  shows 3 messages + 2 reads in 5.3h; reads zero at 02/04/05/06; powerd
  scheduled low-power ~02:18; listener loops resumed only ~08:24. The
  agent PROCESSES were gone. A wake fired at a dead session does
  nothing. Real fix: agora-0110 (fleet-liveness alarm) + the operator's
  infra decision (headless/daemon seats vs interactive tabs that die).
- **Class B — live-but-unobliged (daytime, code#55 = 7.4h).** The
  session existed but the message never registered as a must-do: plain
  reply/fyi, or to=[], or buried in a wall. Fixed AT THE SOURCE:
  DM auto-address (0.12.14), operator/addressed replies oblige
  (0.12.18/19), @mention parsing (0105). That is the compliance fix.

## What 0106 becomes

**PROMOTED from backstop to a real fix (c3527 design review): there is a
confirmed reception-path DROP, not just a compliance gap.** Verified in
code:

- The file listener re-arms every ~5s and polls `/owed`; if the debt
  SIGNATURE is unchanged it stays silent (`listen.py:379`) — "waits for
  the hub's escalation" (its own comment, `listen.py:365`).
- The owed signature is `sorted(message ids)` (`listen.py:279`) — it does
  NOT change when a debt escalates.
- The hub writes notify lines ONCE, at post time (`notify_sink`), when
  `envelope.escalated` is necessarily false (age≈0). Escalation later
  flips a flag on READ surfaces (inbox/owed) but emits no new notify
  line, so the `escalated` token in the listener's `_IMPORTANT_FLAGS`
  (`listen.py:42`) is unreachable in file mode.
- Net: a seat whose single backlog-wake did not produce a turn
  (debounced, between sessions, aborted turn) holds a debt whose
  signature never changes → it is NEVER re-woken until an UNRELATED new
  debt arrives. The stop hook re-nags only if the seat is already taking
  turns; an idle seat is unreachable.

## The fix (emit≠process, design rec #2)

Record the wake signature as DELIVERED only when the hub observes the
seat READING after the wake (the hub already receives the `/owed` poll
and `check_inbox` — reception heartbeat exists, 0098). If a wake was
emitted but no read followed, re-emit at the next arm. This wakes a
dropped debt WITHOUT nagging a seat that saw it and chose other work
(that is the seen-and-ignored class, NOT a hub problem — see 0114).
Also either re-emit a notify line at SLA breach (making `escalated`
reachable) or delete `escalated` from the wake contract and correct the
skill text — today the taught contract overstates what escalation does.

## Blast radius

The reception loop serves the whole fleet — build and test in isolation,
never hot-patch. This is the highest-value reception fix and the first
of the recut program to implement.

## Plan

When an obligation crosses its SLA (and again at back-off intervals:
SLA, 2×, 4×, capped), the hub re-emits the message into the addressee's
notify stream so `--important-only` listeners fire a fresh `AGORA_WAKE`
with an `escalated` flag. Requirements:

- Dedupe: one re-wake per breach step, not per sweep tick.
- Pause-aware: the 0069 clock exclusions apply (no re-wake storms on
  resume).
- Bounded: stop re-waking a seat the DEAF/DARK watchdog has already
  alarmed (the alert path owns it from there — see agora-0107).

## Risks

Re-wake storms on seats holding many debts — bundle all breached debts
into ONE re-wake per step. Interaction with notify-file tail offsets
(0086): re-emitted lines must not double-count as new messages.
