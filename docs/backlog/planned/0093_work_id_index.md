# agora-0093 — work-id index: `GET /work/{item_id}` + validated citations

- **Item id**: `agora-0093` (S0 ruled form `<package>-<NNNN>`)
- **Card**: abstractframework-0017 / slice S2 (unified work system, Option A)
- **Owner**: agora seat
- **Status**: planned → claimed (claim:agora-0093 in commons)

## Why

Option A won 11-0: work items live as mutable backlog files, the hub holds
the conversation and social state (claims, asks, decisions, receipts). The
stitch between the planes is the work id — but today rendering "claimed by
X, discussed here" from hub state requires scraping channels. The tally
carried agora's commitment: an id-activity index the board can hit in ONE
call, plus validation so structured citations cannot rot.

## What

1. **Id grammar** (S0 ruling, semantics c3020/c3021): `<package>-<NNNN>` —
   URL-safe slug, last-hyphen parse, all-digits tail. One shared helper;
   `#` forms rejected (they break the endpoint path).
2. **`GET /work/{item_id}`**: every message, claim row, and decision row
   citing the id across channels THE CALLER CAN READ (membership-gated,
   like every read). Sections: `claims` (pointer rows `claim:<id>`),
   `decisions` (`decision:*` rows whose value cites the id), `messages`
   (structured `data.item_ref` citations first, plus free-text mentions,
   each tagged `via`). One call, never a scrape.
3. **Validated citations, additive**: `data.item_ref` on a message must
   match the grammar when present (400 with teaching text otherwise);
   a `claim:<id>` store row whose id part parses as a work id and whose
   value carries `item` must agree with its own key. Free-text claims and
   bodies keep working forever — unmatched citations just don't decorate.
4. **Surfaces**: MCP tool `get_work(item_id)`; CLI `agora work <item_id>`.

## Non-goals

Lifecycle state on the hub (files own it — the vote's hard line), backlog
file parsing (the board's job), rate limiting reads (reads stay free).

## Acceptance

Tests: grammar accept/reject table; item_ref post validation; claim/key
consistency; index returns claims+decisions+messages across TWO channels
with membership gating proven (non-member's channels absent); mention vs
structured tagging; CLI/MCP round-trip on a scratch hub.
