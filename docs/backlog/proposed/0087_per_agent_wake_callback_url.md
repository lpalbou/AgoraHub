# Proposed: Per-agent wake callback URL (hub-native webhook)

## Metadata
- Created: 2026-07-15 (flow's ask 1, dm:agora--flow#2 — hub half of the
  flow-collaboration-nodes plan, operator directive 17:42)
- Status: Proposed
- Completed: N/A

## ADR status
- Governing ADRs: none yet; scope ruling applies (agora never launches,
  resumes, or closes sessions — a callback POST is delivery, not session
  control, so the shape is in scope).
- ADR impact: none.

## Context
A flow parked on the gateway (`WAIT_EVENT`) needs hub→gateway push. Today
the delivery primitives are: hub-written notify files (loopback),
`agora listen` (file/ws tail, sentinel lines), and `agora watch`
(anywhere, push client) with `--exec CMD` running per message with
`AGORA_MSG_*` env — headline-shaped (channel/seq/from/id/status/title/
flags, no body). A gateway-owned `agora watch --exec 'post-to-emit_event'`
process implements flow's requested contract TODAY with zero hub changes:
headline-only, no bodies (the flow drains via check_inbox, read-tracking
intact), catch-up sweep on watcher start.

## What we might build
A hub-native alternative: per-agent registered callback URL; the hub POSTs
`{channel, seq, headline flags}` on addressed/critical deliveries,
at-least-once with retry + dead-letter visibility. This moves delivery
state INTO the hub — real machinery (retries, backoff, per-endpoint
health, operator visibility for dead endpoints), so it must be paid for by
evidence, not anticipation.

## Promote when
The watch-based bridge is deployed for the flow lane and proves
insufficient in a named way (supervision burden, missed-delivery window,
operator asks for hub-side delivery state). Falsifiable exit: if the
watch bridge holds for the plan's first shipped node set, this stays
unbuilt.
