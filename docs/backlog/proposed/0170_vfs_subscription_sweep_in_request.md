# Proposed: Take the VFS-subscription sweep out of the write request

## Metadata
- Created: 2026-11-09
- Status: Proposed
- Completed: N/A

## ADR status
- Governing ADRs: None
- ADR impact: None (delivery timing; notification semantics unchanged)

## Context
VFS subscriptions shipped in 0.18.0 (`docs/vfs-triggers.md`; partial
`proposed/0145`). The revision archive is the event source and the 30-second
sweep is the backstop, so durability does not depend on the in-request call
described below. Recorded as a follow-up of `completed/0154_collaboration_graph.md`,
which shipped with it.

## Current code reality
- `fs_write` (`src/agora/hub/service.py:7333`) and `fs_delete` (`:7536`) hold
  `db.orchestration_lock` and call `_fs_trigger_after_write()`
  (`service.py:7439`, `:7561`) after the commit and audit row, before they
  return.
- `_fs_trigger_after_write` (`src/agora/hub/fs_triggers.py:113`) runs the
  whole `_fs_trigger_sweep()`, which is hub-wide, not scoped to the written
  path. `db.fs_pending_subscriptions(limit=256)` (`src/agora/db.py:3033`)
  returns up to 256 subscriptions across all channels whose file head is ahead
  of their cursor. For each it rotates the queue, checks membership and channel
  state, reads up to 32 revisions (`fs_revision_events`), and per revision
  inserts a message and calls `_wake`.
- `_wake` (`service.py:3855`) publishes to every member of the channel and,
  with the notify sink on, renders a viewer-specific envelope and appends a
  notify-file line per member.
- Exceptions are logged and swallowed. The 30-second `vote_watchdog` and the
  dark sweep run the same sweep, so a failed in-request pass is retried.
- The lock is the one `post_message`, `store_set` and other VFS writes take.
  Not measured.

## Problem or opportunity
One write's latency includes notification work for every pending subscription
in the hub, up to 256 × 32 revisions, while it holds the lock other writers
need. In steady state the pending set is mostly the written path's own
subscribers, and each notice fans out to every channel member. But a
subscription whose delivery failed (`_wake` returned False, so its cursor
is kept) stays pending and is retried inside every later write, in any channel:
an unrelated writer pays for it.

## Proposed direction
- Scope the in-request pass to the written `(channel, path)`, for example a
  `fs_pending_subscriptions(channel, path)` variant, and leave the hub-wide pass
  to the watchdog; or
- run the pass after the lock is released, or signal a background task from
  the write.

Either way, keep the archive as the event source, the per-subscription cursor
and the dedupe key.

## Why it might matter
Write latency grows with subscriber count, room size and any stuck recipient,
in a path every artifact edit takes.

## Promotion criteria
A measured `fs_write` latency or lock hold attributable to this pass, for
example p95 write latency with N pending subscriptions, or a fleet run showing
write stalls while subscriptions are active.

## Validation ideas
- Keep `tests/test_fs_triggers.py` (17 tests) green, including the crash,
  dedupe and rotation (no starvation) cases.
- Add a test that a write to path A does not deliver path-B notifications
  inside the request, or delivers them outside the lock.
- Timing harness: 256 pending subscriptions, one unrelated write.

## Non-goals
- No change to what a notification is (FYI, no reply debt, a seat's own
  writes do not notify it).
- Do not make delivery best-effort; the watchdog backstop stays.

## Guidance for future agents
Read `_fs_trigger_sweep` and the crash-recovery comments first; the
advance-after-wake ordering is what makes delivery durable.
