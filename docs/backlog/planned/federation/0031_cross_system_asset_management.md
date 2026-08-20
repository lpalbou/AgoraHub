# Planned: Cross-system asset management (ownership, eviction, retention)

## Metadata
- Created: 2026-07-08
- Status: Planned
- Completed: N/A
- Area: assets / channels / hub

## ADR status
- Governing ADRs: [ADR-0001](../../../adr/0001-federation-topology-and-handles.md)
  (Proposed) — single-hub topology.
- ADR impact: None of its own; follows ADR-0001 and shares the owner-remove
  groundwork with `0030`. Introduces no new durable policy beyond retention
  wording.

## Context
When named entities on different systems share a channel, the channel's assets —
the key/value store, the virtual file system (vfs), and the ledger — are the shared
things they read and write. The design pass found membership-based access is
adequate for collaborative discussion among trusted members; the real gaps are
eviction and the fate of a closed room's data.

## Current code reality
- Store, VFS, and ledger are consistently membership-gated
  (`src/agora/hub/service.py` store/`fs_*`/`channel_ledger` all call
  `require_membership`). CAS prevents silent clobber; the VFS has an audit trail;
  the ledger is a per-channel hash chain.
- Per-value size cap exists (256 KiB); there are **no per-agent or per-hub
  quotas**.
- `channel:meta.state=closed` stops new posts but does **not** purge or archive
  the channel's store/VFS/ledger — closed-room data persists indefinitely.
- There is **no owner-initiated member removal** (shared with `0030`), so an
  ex-member with a still-valid key retains access until they self-leave.
- History is append-only by design: a removed member's past contributions remain
  in the channel record.

## Problem
Two concrete gaps for cross-system rooms: an owner cannot evict a member, and a
closed room's assets live forever with no retention story.

## What we want to do
Give owners eviction and give operators a clear, documented retention/purge path
for closed rooms — without building an ACL engine or quotas that a trusted set
does not need.

## Requirements
- **Owner eviction** (shared with `0030`): removing a member immediately cuts
  their store/VFS/ledger access (enforced by `require_membership`).
- **Closed-room retention**: document that `state=closed` means a read-only
  archive; provide an optional operator-only purge (e.g. `agora admin
  purge-channel <c>`) that removes a closed channel's assets deliberately.

## Suggested implementation
Reuse the `0030` remove endpoint for eviction; add a small operator purge command
guarded by the admin key and by `state=closed`.

## Scope
Trusted-room asset management for Model A.

## Non-goals
- Per-asset ACLs beyond channel membership.
- Ownership-transfer policy (audit `updated_by` stays metadata; use CAS +
  convention).
- Per-agent / per-hub disk quotas.
- Automated garbage collection of closed rooms.
These are deferred until multi-tenant or hostile use, which is out of scope.

## Dependencies and related tasks
- `0030` (owner-remove endpoint, topology ADR).
- Relates to the closed-channel lifecycle already shipped (v0.5.3).

## Expected outcomes
- An owner can evict a member and that member loses asset access at once. A
  closed room is a documented read-only archive, with an explicit operator purge
  when data must be removed.

## Validation
- Tests: an evicted member is refused on store/VFS/ledger; a purge on a closed
  channel removes its assets and is refused on an open channel.
- Doc check: closed-room retention is stated in `SECURITY.md`/`docs/faq.md`.

## Progress checklist
- [ ] Owner eviction cuts asset access (with `0030`).
- [ ] Documented closed-room retention policy.
- [ ] Optional operator `purge-channel` (closed-only) + test.

## Guidance for the implementing agent
Keep membership as the access primitive; resist adding an ACL layer for a trusted
set. Re-check current code and report drift.
