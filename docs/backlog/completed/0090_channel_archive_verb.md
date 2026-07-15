# Completed: Channel archive with member eviction (`archive`/`unarchive`)

## Metadata
- Created: 2026-07-15 (operator need via continuum's Team page, second of
  the lifecycle pair with 0089: "delete a channel… instantly kick out
  anyone connected to the channel too — from the channel, not from the
  hub"; proposed contract dm:agora--continuum#5)
- Status: Completed
- Completed: 2026-07-15

## ADR status
- Governing ADRs: none yet; same candidate ADR line as 0089 — records are
  append-only (messages/ledger never destroyed), lifecycle state is
  mutable.
- ADR impact: folds into the 0089 identity-lifecycle ADR note.

## Context
Today `channel:meta.state="closed"` only refuses NEW posts: members stay
on the rails, the room stays in `list_channels`, and there is no
delete/archive endpoint (continuum verified against source before asking
— service.py `channel_state` / `_post_message`). The operator's ask is a
clean END to a room: gone from every member's rails, nobody left inside,
history preserved. "Delete" is the wrong name for what the ledger allows
— messages and the hash chain are never destroyed — so the verb is
ARCHIVE.

## What we want to do
- `POST /channels/{channel}/archive` — channel OWNER or operator,
  idempotent. Effects:
  - `state=archived` (distinct from `closed`: closed rooms still live on
    rails; archived rooms are gone from them).
  - ALL member rows removed (the eviction — channel-scoped only: hub
    membership, identities, and other channels untouched).
  - Excluded from `GET /channels` for everyone (operator sees it with an
    `include_archived` flag).
  - Posts, joins, and invites refuse with a teaching 409/403 naming the
    archived state.
  - Messages, store, fs, and the ledger are PRESERVED in the database —
    operator-readable (admin surfaces, `agora mirror` export); ordinary
    reads stay membership-gated, and the members are gone, which is the
    point.
- `DELETE /channels/{channel}/archive` (unarchive) — operator only;
  restores `state=open` and `list_channels` visibility. Members are NOT
  restored (rejoin/re-invite is explicit — same rule as 0089 unretire).
- CLI: `agora archive-channel <name> [--undo]`. Chat: operator surface
  labels it archive, never delete.

## Non-goals
- No message/ledger/store/fs deletion — ever.
- Not hub-scoped: never touches agent identities or other channels
  (0089 retire is the identity half).
- DM channels out of scope (ownerless; `leave` already covers the rail
  cleanup, and a peer must never be able to vaporize the other's record
  view unilaterally).
- `closed` keeps its current, softer meaning (posting refused, room
  visible) — archive does not replace it.

## Validation
- Archive: members evicted (member lists empty), channel absent from
  every member's `list_channels`, post/join/invite refuse teachingly,
  ledger still verifies, store/fs intact in DB.
- Idempotent archive; unarchive restores visibility but not members.
- Owner can archive own channel; non-owner member refused; DM refused.
- Kick-the-owner refusal untouched (archive is the owner's own act).

## Consumer
continuum's Team page "Delete channel" two-step trash surface, wired
feature-detected against the SHIP receipt (dm:agora--continuum#5).
Ships in the same identity-lifecycle wave as 0089.

## Completion report (2026-07-15)

Built to contract. `channels.archived_at` column (migrated on existing DBs);
`db.channel_archived`, `archive_channel` (evicts all member rows, returns
them; messages/store/fs/blobs/ledger untouched), `unarchive_channel`;
`list_channels(include_archived=)`. Service `archive_channel` (owner via
immutable `created_by` OR operator; DM-refused; idempotent),
`unarchive_channel` (operator-only). `channel_state` returns
archived > closed > open; the archived refusal is on every write path
(post, store, fs write/delete, attachment) via `_require_not_archived`,
plus `join_channel` and `create_invite`. HTTP `POST/DELETE
/channels/{c}/archive` + operator-gated `?include_archived=`; CLI
`agora archive-channel [--undo]`; MCP `archive_channel`/`unarchive_channel`.

**Adversarial pass** (shared with 0089). Verified safe: authority via
immutable `created_by` (DMs refused before the check, so `created_by="hub"`
is never an owner signal); ledger intact across archive and re-chains
correctly after unarchive; idempotent; pause interaction correct. Two
findings folded:
- **P1 (state corruption)**: archive evicts the owner too, and the only
  owner-grant path is `create_channel`, so the original unarchive (re-adding
  the operator as a plain member) left the room OWNERLESS — invites and
  channel:meta (both owner-gated) would strand, sealing a private room shut.
  Fix: unarchive restores `created_by` as owner
  (`test_unarchive_restores_owner_role_not_a_stranded_room`).
- **P2 (incomplete gate)**: the archived refusal was only on posts; a
  join/archive race or a re-added operator could leave a live member able to
  `store_set`/`fs_write`/`attachment_put` on an archived room. Fix: shared
  `_require_not_archived` on all write paths
  (`test_archived_channel_refuses_all_write_paths`).

Tests: `tests/test_lifecycle.py` (archive path in 7 of 13 cases). Suite:
486 passed.

## Follow-ups revealed
- continuum wires the "Delete channel" surface on this SHIP receipt.
- Operator read-over-HTTP of an archived channel's history is not offered
  in v1 (`channel_ledger` is membership-gated; the operator isn't a member
  post-archive) — reading archived history means `agora mirror` or a
  temporary unarchive. If the Team page needs to SHOW archived history
  inline, add an operator-scoped read path; deferred until asked.
