# Planned: Attaché deferred delivery (re-offer skipped wakes)

## Metadata
- Created: 2026-07-08
- Status: Planned
- Completed: N/A

## ADR status
- Governing ADRs: None
- ADR impact: None

## Context
The attaché wakes a headless harness on new messages. When it skips a wake
(agent presence is `working`, or the trigger budget is exhausted), it still
advances its local delivery cursor, so those messages are never re-offered.

## Current code reality
- `src/agora/attache/runner.py`: `run()` advances `self.cursors` and calls
  `_save_state()` after `_deliver(...)`, but `_deliver` returns early when
  `only_when_idle` and presence is `working`, or when the budget is exhausted.
- The design assumes the agent self-drains its inbox while working, which holds
  for MCP/runner agents but not for a purely attaché-driven idle harness.

## Problem
A skipped wake's messages can be lost for a purely attaché-driven agent.

## Scope
- Track "deferred" seqs separately from delivered ones; re-offer them when
  presence returns to `idle` (or the budget refills), before advancing the cursor.

## Non-goals
- Do not wake a `working` agent (that would double-deliver); only re-offer once it
  is idle.
- Do not touch the agent's server-side read cursor (the attaché keeps its own).

## Expected outcomes
- Messages that arrive while the agent is working are delivered once the agent
  goes idle, not dropped.

## Validation
- Unit/integration test: simulate presence `working` during a wake, then `idle`;
  assert the skipped envelopes are delivered exactly once.

## Guidance for the implementing agent
Advance the persisted cursor only for envelopes actually delivered; keep a
bounded deferred set.

## Status note (2026-11-09)

Stale code reality, unrelated to 0.18.0: `src/agora/attache/` no longer exists.
It was removed in `933c0a7` (2026-07-13, "Cursor reception is a tuned background
monitor"). Reception is now `agora listen` / `agora drive` plus hub-written
notify files. Before implementing, check whether the deferred-delivery defect
exists in the listener; if it does not, deprecate this item.
