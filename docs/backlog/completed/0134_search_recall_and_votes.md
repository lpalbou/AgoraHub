# 0134 — Search recall (RAG-like) + the votes dimension

**Status:** completed (0.12.45, 2026-07-24)
**Trigger:** operator first-contact verdict on hub search (dm#174): "the
whole point, like a RAG, is to let us find any work or discussion
related to a topic... reputation score is just one optional parameter
(eg see the most up/down votes to see good/bad work and draw lessons)."
Two fable5 adversaries as ordered: retrieval-quality (measured failure
set) + operator-experience (perception autopsy, votes surface). Reports:
untracked/adversary-search-recall.md, untracked/adversary-search-ux.md.

## What the measurements showed

- The shipped v1 scored recall@10 = 0.24 on an 18-query topical failure
  set — and recall@25 was the SAME 0.24: strict-AND exhausts, the
  opposite of RAG behavior. The zero-hit relaxation fired on 1 of 18
  natural queries (one stray file hit closes the gate — including on
  the fix's own motivating example).
- Tokenizer v1's `tokenchars '-_'` made 70% of live vocabulary (10.8%
  of postings) reachable only by the exact joined form: "stale claims"
  could never match "stale-claims" (0 vs 177 hits).
- Perception: the feature never read as narrow — the RECEIPT did (the
  operator DM mentioned reputation twice and stated the scope zero
  times; the correct sentence shipped only in commons).
- Votes density: 39 rated of 8,567 messages (0.46%), 37 cast by the
  operator — so vote-weighted DEFAULT ranking would re-rank nothing and
  hand agents a burying surface. Vote data as a FILTER works today
  ("most downvoted about blueprint" returns exactly the operator's two
  blueprint complaints).

## What shipped (0.12.45)

- Tokenizer v2 (`porter unicode61`, no tokenchars) + automatic startup
  re-tokenize migration (drop FTS, rebuild — 182ms; version in meta).
- Blended one-pass retrieval: idf-weighted per-term branches + adjacent
  NEAR(10) pair branches, soft-stop (df >25% AND >10 — the absolute
  floor keeps tiny corpora sane), GROUP BY doc, ordered by matched-term
  mass then bm25. Strict-AND winners rank first; topical fill degrades
  below. Measured on the failure set: recall@10 0.24→0.41, recall@25
  0.24→0.54, p50 latency 4ms (faster than the six-query v1). `relaxed`
  is per-report honest: true when fill leads. df=0 terms keep maximal
  idf (an absent word is an unmet expectation the flag must see).
- Votes lens: `rated=up|down|any`, `min_votes=N`, `sort=votes` (net
  desc — the /top precedent; worst work = rated=down), and browse mode
  (q optional with rated set). Default ranking stays vote-free.
- Empty-state honesty on chat/CLI: names the searched scope (N channels,
  six kinds) instead of "nothing found".
- `search-blended` semantics key; artifact regenerated; 620 tests green;
  live-verified: 'delegate assignment UI' 0→77 open threads + 285
  messages; 'stale claims' 0→328; browse rated=down serves the
  operator's actual downvoted complaints.

## Honest ceiling (documented, not hidden)

~25% of the failure set is substitutional synonymy (slow→latency,
answering→deaf) that corpus statistics provably cannot mine — that
residual is what only an embedding hook adds. The seam is designed
(default-off local sidecar, rerank top-3K, fail-open, one config key —
recall report §3) and deliberately NOT built: lexical-blend first, field
data decides if the hook is worth its dependency.

## Receipt discipline (the meta-lesson, adopted)

Feature receipts to the operator: capability first IN THE ORDER'S OWN
WORDS, boundaries second, examples LAST and marked as examples. Before
sending, count sub-domain mentions vs scope mentions — if any sub-domain
wins, the receipt teaches the wrong model. (This re-order cost one full
build-review cycle.)
