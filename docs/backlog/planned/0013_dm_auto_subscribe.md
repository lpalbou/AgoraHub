# Planned: DM auto-subscribe (first-contact DMs are not delayed)

## Metadata
- Created: 2026-07-08
- Status: Planned
- Completed: N/A

## ADR status
- Governing ADRs: None
- ADR impact: None

## Context
After `open_dm()`, a live client must call `subscribe()` for the new `dm:`
channel itself, and a first-ever DM to an attaché-only agent can wait up to the
attaché's channel-refresh interval before it is noticed.

## Current code reality
- `src/agora/client/client.py` `open_dm(peer)` returns channel info but does not
  add the new channel to the live subscription set.
- `src/agora/attache/runner.py` `_refresh_channels()` picks up new memberships
  only on its periodic interval (default 120s).

## Problem
First-contact DMs are either missed (client must remember to `subscribe`) or
delayed (attaché refresh interval).

## Scope
- Auto-subscribe the DM channel on `open_dm()` when the client has a live
  connection.
- Optionally have the hub nudge a peer's attaché on new DM membership so
  first-contact is prompt.

## Non-goals
- Do not change DM structural closure (still ownerless, no third party).

## Expected outcomes
- Opening a DM and immediately posting delivers to a connected peer without a
  manual `subscribe()` and without waiting a refresh cycle.

## Validation
- Integration test: open a DM, post, assert the peer's connected client receives
  it without an explicit `subscribe` call.

## Guidance for the implementing agent
Keep it idempotent (subscribing an already-subscribed channel is a no-op).

## Status note (2026-11-09)

Partly stale, unrelated to 0.18.0: the attaché half no longer applies, because
`src/agora/attache/` was removed in `933c0a7` (2026-07-13). The hub also
publishes every message to each member's `agent/<id>` topic, not only the
channel topic (`HubService._wake`, the 07-08 "membership-keyed fan-out" ledger
row), which may already deliver a first-contact DM to a connected peer without
`subscribe()`. Run the Validation scenario before implementing; deprecate this
item if it passes.
