# Proposed: Bound the consultation sweep (unindexed scan, decided consultations recomputed)

## Metadata
- Created: 2026-11-09
- Status: Proposed
- Completed: N/A

## ADR status
- Governing ADRs: None
- ADR impact: None (performance of an existing derivation; consultation semantics unchanged)

## Context
Follow-up from `completed/0154_collaboration_graph.md` (consultations shipped
in the same change, 0.18.0). The consultation timers ride the fast collection
watchdog, so this sweep runs on every hub whether or not any consultation
exists.

## Current code reality
- `HubService.vote_watchdog` (`src/agora/hub/service.py`) runs
  `_consultation_sweep` every `VOTE_SWEEP_SECONDS = 30.0`
  (`service.py:211`), alongside the vote, mission-change and fs-trigger
  sweeps. `dark_sweep` also calls it (every 300 s by default).
- `_consultation_sweep` (`src/agora/hub/consultations.py`) holds
  `db.orchestration_lock` for the whole pass and iterates
  `db.consultation_messages()` with no channel filter.
- `consultation_messages` (`src/agora/db.py:2083`) filters on
  `json_extract(data, '$.consultation.kind') = 'consultation-v1'`. No index
  covers that expression (the `messages` indexes are `(channel, seq)`,
  `reply_to`, the `task_review` expression and `created_at`, `db.py:128-135`),
  so each pass scans the whole `messages` table (all channels, all history).
- The query returns decided and cancelled consultations on purpose (its
  docstring: a later revised or retracted answer must still invalidate a
  decision). For each root, `_consultation_state` reads all replies, re-reads
  retracted replies unredacted, fetches and hashes each declared artifact (up to
  32) and checks the membership of eligible seats. Decided and cancelled roots
  then produce `event=None` and are skipped, after that work is done.
- The same lock serializes message inserts (`post_message`, `service.py:3133`),
  `store_set` (`service.py:5844`), `fs_write` and `fs_delete`
  (`service.py:7339`, `7539`).
- Not measured on a real hub database.

## Problem or opportunity
Sweep cost grows with total message history and with every consultation ever
posted, not with the consultations that can still change. The cost is paid
twice a minute while holding the lock writes need.

## Proposed direction
Options, not a commitment:
1. Make the root lookup indexed: an expression or partial index that matches
   the query exactly (SQLite uses an expression index only for an identical
   expression), or a derived list of consultation root ids maintained at post
   and retract time.
2. Re-evaluate a decided or cancelled root only when an input changed since
   the last pass: a new or retracted reply to the root, or a new revision of a
   declared artifact path. The `consultation-notified:<id>` meta receipt
   already records the last event and could carry those input markers.
3. Compute states outside the lock and take it only to recheck and publish the
   roots whose event changed.

## Why it might matter
On a long-lived hub this is a full scan of `messages` every 30 seconds under
the write lock, even with zero live consultations.

## Promotion criteria
A measurement on a copy of a real fleet hub showing either `EXPLAIN QUERY PLAN`
scanning `messages`, sweep wall time or lock hold above an agreed bound, or
write-latency spikes on the 30-second cadence.

## Validation ideas
- `EXPLAIN QUERY PLAN` for `consultation_messages()` before and after.
- Time `_consultation_sweep` on a database with about 100k messages and 0, 10
  and 500 consultations.
- Keep `tests/test_consultations.py` green, in particular
  `test_snapshot_refuses_late_change_then_reopens_recorded_decision`,
  `test_withdrawn_conclusion_does_not_restore_a_previous_decision` and the
  crash/dedupe notification tests.

## Non-goals
- No change to consultation semantics. A decided consultation must still move
  to `needs_reconciliation` on a late change.
- No stored consultation state that could disagree with the ledger; any cache
  needs an explicit invalidation rule.

## Guidance for future agents
Measure first. If the scan is cheap at real sizes, record the numbers here and
deprecate this item.
