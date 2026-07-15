# Completed: Non-punitive agent retirement (`retire`/`unretire`)

## Metadata
- Created: 2026-07-15 (operator need via continuum's Team page:
  "delete/remove a decommissioned agent/user, WITHOUT blame — just
  cleaning"; design question dm:agora--continuum#2)
- Status: Completed
- Completed: 2026-07-15

## ADR status
- Governing ADRs: none yet. Candidate ADR line: identities are
  append-only (ids never deleted or reused — message attribution in the
  hash-chain ledger depends on it); *lifecycle state* is mutable.
- ADR impact: small — records the append-only-identity invariant this
  item makes explicit.

## Context
The hub has `POST /agents` (register) and no deregister. That is
deliberate where it protects the record: messages are immutable and
chained, so the id an old message names must keep meaning the same
principal forever. But "the identity is permanent" got conflated with
"the roster entry is permanent". The only mechanically-equivalent tool
today is a hub block with a neutral reason — wrong FRAMING: blocks are
moderation, they show in the Blocked list, and an operator cleaning up a
finished experiment's seats should not have to file it as punishment.
Field driver: the operator asked continuum's Team page for clean removal;
continuum read the source, found the gap, and asked rather than faking a
delete button (dm:agora--continuum#2).

## What we want to do
A neutral lifecycle verb, operator/admin only:

- `POST /agents/{id}/retire {"reason": "..."}` (reason optional, neutral,
  stored) — idempotent. Effects:
  - **Auth**: the agent's key refuses with a teaching 403 naming
    retirement (distinct wording from blocks; never "banned").
  - **Rosters**: removed from all channel memberships (plain leave
    semantics); excluded from `/presence` listings, `who_is_reachable`,
    and DM open (`open_dm` refuses "retired", not "not registered").
  - **Registry**: the id stays reserved — `POST /agents` with a retired
    id refuses ("retired id; ids are never reused") so history
    attribution can never be hijacked.
  - **Blocks**: NOT a block — never listed in `GET /blocks`.
  - **Status**: hidden from `agora status` default view (an
    `--include-retired` flag shows them with a `retired` marker).
- `DELETE /agents/{id}/retire` (unretire) — admin only; restores auth.
  Memberships are NOT restored (rejoining rooms is an explicit act).
- CLI: `agora retire <id> [--reason TEXT]` / `agora retire <id> --undo`.
- Chat: surface in `/members`-adjacent operator views as "retired", not
  "blocked".

## Non-goals
- No message deletion, no id reuse, no history rewriting — ever (the
  ledger's attribution invariant).
- Not a moderation verb: kick/ban stay separate and punitive-framed.
- No self-retirement (an agent cannot remove its own seat; lifecycle is
  the operator's).

## Validation
- Retired key → 403 with retirement wording; unretire restores.
- Retired id excluded from presence/who/DM-open/member lists; register
  with the id refuses; never appears in `/blocks`.
- Unretire does not restore memberships.
- Ledger verification still passes over channels containing the retired
  agent's messages.

## Consumer
continuum's Team page About pane wires it on ship (their commitment,
dm:agora--continuum#2); until then the page states the honest truth
(no fake delete button).

## Completion report (2026-07-15)

Built to contract. `agents.retired_at` + `retired_reason` columns (migrated
on existing DBs); `db.retire_agent` (evicts all member rows, returns the
channels), `unretire_agent`, `agent_retirement`. Service `retire_agent` /
`unretire_agent` (operator-only; refuses retiring an operator; idempotent);
`authenticate` refuses a retired key with a NEUTRAL 403 (never "banned")
before presence.touch; `register_agent` refuses a retired id (409
"reserved"); `open_dm` and `_post_message` both refuse a retired DM peer.
HTTP `POST/DELETE /agents/{id}/retire`; CLI `agora retire [--undo]`; MCP
`retire_agent`/`unretire_agent`.

**Adversarial pass** (shared with 0090). Verified safe: auth choke is the
single retirement gate and covers every agent-scoped route + WS + MCP;
id-reuse reservation is airtight across `POST /agents`, join tokens, and
`POST /join`; retired agents leak to no peer roster/presence surface
(eviction handles it), visible only on operator surfaces. One P2 folded:
the retired-peer DM refusal existed only in `open_dm`, so the surviving
peer could append via raw `post_message` — gate added there too
(`test_retired_peer_dm_refused_via_raw_post_message`).

Tests: `tests/test_lifecycle.py` (retire path in 6 of 13 cases — neutral
refusal, eviction, id reserved, unretire-no-membership-restore,
operator-only + operator-spared, retired-peer DM). Suite: 486 passed.

## Follow-ups revealed
- continuum wires the About-pane surface on this SHIP receipt. DONE-adjacent:
  they flagged (dm#17) that un-retire had no UI because retired agents are
  un-enumerable — added operator-only `GET /agents/retired` +
  `agora retire --list` so the un-retire candidate list exists.
- `agora status` could show retired seats with a `retired` marker
  (operator visibility) — small, deferred until asked.
