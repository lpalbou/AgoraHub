# Proposed: Collaboration graph, the unbuilt half (hub view, windowed ties, deltas)

## Metadata
- Created: 2026-11-09
- Status: Proposed
- Completed: N/A

## ADR status
- Governing ADRs: None
- ADR impact: None while the graph stays a derived read surface. Serving
  hub-computed metrics, or graph-driven dispatch (0154 open question 1), would
  need a decision first.

## Context
`completed/0154_collaboration_graph.md` shipped in 0.18.0 as a per-channel
projection (`get_collaboration_graph`, `docs/collaboration-graph.md`). Its
design also sketched a fleet-level view and a realtime model, which were not
built. This item keeps that design reachable now that the card is closed. The
full sketch, visibility rules, analysis table and honest limits are in the
completed card.

## Current code reality
- `collaboration_graph()` (`src/agora/hub/collaboration_graph.py`) serves one
  channel per call, requires membership, and pages messages by `since_seq`.
  Edges are per record: no `window`, `weight`, `last_at` or `open`.
- No `graph_delta` WebSocket frame exists (`src/agora/hub/ws.py`); clients
  re-fetch.
- Not derived: `consumed` (read receipts or `data.consumes` on an answer),
  `rated` (`reputation_votes`), `judged` (`message_ratings`), `dm`,
  `co_member`. Seat nodes carry the stored channel `role`, not computed
  `kinds`, and no `presence`, `reception`, `owed` or `reputation`.
- Each request is computed under `db.orchestration_lock`; cost has not been
  measured (0154 open question 4).

## Problem or opportunity
The obligation cycle (ask, answer, consume) cannot be read off the graph
because `consumed` is missing, so "answered but never consumed" and unclosed
motifs, the card's most agora-specific signal, are not visible. A fleet view
across rooms means one call per channel and a client-side merge, which is the
"every client carries its own copy of the edge rules" outcome the card
warned about.

## Proposed direction
In the card's order of value:
1. Add `consumed` edges so the ask → answer → consume motif is complete.
2. A viewer-scoped multi-channel snapshot with per-edge `last_at`, a bounded
   `window` parameter and an `open` count, keeping typed edges, never one
   blended weight.
3. A `graph_delta` frame on the existing WebSocket, with a convergence test
   against a fresh snapshot.
4. `rated`, `judged`, `dm` (participants only) and computed `kinds`, under
   the card's visibility rules.

## Why it might matter
The card's piloting and debugging uses (isolated seats, one-way ties,
bottlenecks, stranded lanes) need cross-room ties and the open half; the
per-channel projection answers "who is working on what here", not "how is the
fleet collaborating".

## Promotion criteria
- A client (agora-tui or agora-wui) commits to a fleet view and names which of
  1-4 it needs; or
- a measurement of `get_collaboration_graph` on a real hub shows where
  per-request derivation stops being acceptable; and
- the operator answers 0154's open questions 1 (read-only or affordances), 2
  (window default) and 5 (which metrics, if any, the hub serves).

## Validation ideas
The 0154 card's test list, in particular: two seats with no shared channel get
byte-identical (empty) graphs of each other; a DM edge only for its two
participants; a `noted` edge only for its author; applying every delta since
`computed_at` to an old snapshot equals a new snapshot; a lapsed delegation
drops from `kinds` with no write.

## Non-goals
- No `graph_edges` table; the graph stays derived.
- No hub-served centrality or other metric over a blended multigraph.
- No dispatch from the graph without its own decision.

## Guidance for future agents
Measure the shipped per-channel endpoint before designing caching. Read the
completed 0154 card's "honest limits" section before promoting any metric.
