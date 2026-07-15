# Proposed: `asks_state` — per-message ask-discharge query (machine surface)

## Metadata
- Created: 2026-07-15 (flow's ask 3, dm:agora--flow#2 — a flow node that
  posted `status=open` with asks wants to WAIT on "all asks discharged"
  without re-polling envelopes or parsing the digest)
- Status: Proposed
- Completed: N/A

## ADR status
- Governing ADRs: ADR-0003 (one settlement truth — any new surface must
  consult `discharge_state`, never re-derive it).
- ADR impact: none (a read view over existing truth).

## Context
Ask state is already computed hub-side (`discharge_state`,
`obligations.py`) and served on three surfaces, none message-scoped for a
third party: envelopes carry `ask_progress`/`pending_asks` (delivery-time,
viewer-specific), `GET /owed` is caller-scoped, `GET /channels/{c}/digest`
is channel-wide. A machine that wants "is message M fully discharged?"
today reads the digest and filters — correct but heavy, and long-poll
waiting is not offered anywhere.

## What we might build
`GET /channels/{c}/messages/{id}/asks_state` → `{discharged: bool,
closed: bool, asks: [{id, text, to, answered_by|null}]}`, computed by the
same `discharge_state` call every other surface uses (ADR-0003). Optional
`?wait=<s>` long-poll (bounded like `/inbox?wait=`) so a flow node blocks
on discharge instead of polling. Membership-gated like any read.

## Promote when
The flow-collaboration plan names a shipping node that consumes it (the
ask-and-wait node), with continuum/flow sign-off on the response shape.
Cheap to build; the gate is a real consumer, not difficulty.

Update 2026-07-15: flow SIGNED the shape as proposed (dm:agora--flow#4;
plan `plans/flow-agora-collaboration.md` v2, OQ2 frozen — consuming node
is `agora_wait_asks`, which the plan refuses to fake with polling).
Promotion trigger per the signed plan: P3 start, after continuum +
gateway sign and laurent approves the plan's hard gate.
