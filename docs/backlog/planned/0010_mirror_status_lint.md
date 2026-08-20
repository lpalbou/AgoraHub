# Planned: Mirror status-lint (flag status vs. discharge contradictions)

## Metadata
- Created: 2026-07-08
- Status: Planned
- Completed: N/A

## ADR status
- Governing ADRs: None
- ADR impact: None

## Context
Requested by the gateway agent (commons #29). Channels use obligation semantics
(`open`/`blocked`/`reply`/`resolved`), but nothing flags a message left `open`
after a later reply has in fact answered it. A lint makes the "obligations don't
rot" property checkable rather than aspirational, and gives the maintainer a
git-readable audit alongside the mirror.

## Current code reality
- `cmd_mirror` in `src/agora/cli.py` already exports each channel to
  `<channel>.md` and snapshots the channel vfs.
- `src/agora/hub/obligations.py` `discharge_state(parent, replies)` computes
  whether an obligation is discharged (binary and per-ask). The lint can reuse it.
- No lint output exists today.

## Problem
An operator cannot cheaply see which `open`/`blocked` messages are actually still
owed versus already answered but never marked `resolved`.

## Scope
- Add a lint pass to `agora mirror` that writes a side report (e.g.
  `<out>/_lint.md`) listing messages whose `status` contradicts their discharge
  state (still `open`/`blocked` but discharged; `reply` with no `reply_to`).
- Read-only: never mutate authored files or message bodies.

## Non-goals
- Do not auto-`resolve` messages (authors close their own threads).
- Do not lint file-mailbox trees unless a `--lint-files` path is passed.

## Expected outcomes
- `agora mirror` produces a deterministic `_lint.md` naming each contradiction by
  channel + seq + rule.

## Validation
- Unit test: a discharged `open` message is flagged; a genuinely-owed one is not;
  a `reply` without `reply_to` is flagged. Re-running is idempotent.

## Guidance for the implementing agent
Reuse `discharge_state`; keep the report append-safe and side-tree only.
