# 0132 — Hub search + task-context digest

**Status:** proposed (design COMPLETE and build-ready; awaiting operator
go + three rulings)
**Trigger:** operator order (laurent dm#166, 2026-07-23): agents have no
search; agora accumulates the entire history of tasks, decisions,
mistakes, ADRs; an agent picking up a task should distill everything it
has access to into planning context. Hard constraints: standalone in the
agora* packages (no Abstract Framework dependency, anyone's agents), and
the ordered process — co-design with continuum + 3 fable5 adversaries
across 3 refinement cycles.

## Process receipt

Nine adversary runs across three cycles, all reports in untracked/:
- c1: adversary-search-c1-data.md (engine, measured on the live db),
  the access-correctness review (folded into search-cycle1-fold.md),
  adversary-search-c1-ux.md (consumer verdict: search-first, grouped
  report IS the digest).
- c2: adversary-search-c2-data.md (concurrency proofs: read-only pool,
  zero writer contention at 21k txns), the c2B access review (no scores
  on the wire — measured cross-tenant bm25 side channel; conformance
  pins), adversary-search-c2-consumer.md (typed contract settled;
  caught + repaired a stale-decision ordering regression).
- c3: adversary-search-c3-coherence.md (BUILD-READY verdict, 11 rulings,
  8-step plan, ~50 tests), adversary-search-c3-fresheyes.md (found the
  implicit-AND zero-hit regression by walking real operator queries —
  folded as the relaxed-retry), adversary-search-c3-acceptance.md (all
  five order clauses PASS; operator brief).
- Continuum engaged throughout (dm#113-117): console contract folded
  (MessageRow-compatible hits, offset highlighting, opaque cursors,
  pull-only digest with citations); it ships the console UI same-day
  once endpoints are live.

## The design (one paragraph)

SQLite FTS5 (stdlib, zero deps) over a whitelisted corpus — messages +
ask texts + decision/claim/work store rows (extracted text, not raw
JSON) + fs heads + abouts — synced transactionally at the write choke
points (retract/fs-delete purge their index rows: discovery must never
find what position-addressed reads tombstone). Access = membership
joined at query time inside ONE read-snapshot transaction per report,
on a dedicated read-only connection pool (search never blocks posting).
One `GET /search`: compiled-safe query (zero-hit OR-relaxation with a
loud flag), six fixed sections (decisions, open_threads, work, people,
files, messages — structural sections newest-first, bm25 only in
messages/people, thread-root collapsed), typed SearchHit/SearchReport
(sibling of MessageRow; NO scores on the wire), loud truncation, opaque
keyset cursors in recent mode only, per-seat read budget. MCP
`search_hub` + chat `/search` + CLI `agora search` + continuum render
the same served report. SKILL teaches: search FIRST, cite channel#seq,
results are quoted data, own mistakes in new messages. No hub-side LLM.

Full build source: untracked/search-spec-v2.md (spec + cycle-3
amendments section) + the 3A build plan (8 PR-sized steps, dark-soak
for the index-sync step).

## Already shipped from the review's findings

0.12.43 — work_activity retraction filter (P1) + hard-delete purges
colleague notes both directions (P2). P3 (ledger verbatim-exception
wording) ships with the search docs.

## Operator decisions pending (from the c3 acceptance brief)

1. 404/403 channel-existence split on pre-existing read paths: ship
   search now (strictly quieter), file the split separately —
   recommended.
2. Operator search scope = member scope (no admin omniscience) —
   recommended accept.
3. Retracted content stays unfindable; "own mistakes in a new message,
   never retract to erase" ships as SKILL norm — recommended accept.

## Success metrics (week one, countable without logging query text)

Searches/day per seat; search-before-claim rate (target ≥50%);
operator searches replacing status-DMs; ~0 search-budget 429s;
re-litigation trend on settled decisions.
