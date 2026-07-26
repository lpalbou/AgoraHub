"""Semantic retrieval + fusion, pure functions (agora-0137 step 6).

The executor calls these with snapshots it already owns: the vector
matrix (one model — the invariant lives in the store), the query vector,
and the corpus rows visible to the CALLER (membership scoping happens
BEFORE cosine: vectors carry no ACL, the caller's doc set is the gate,
same doctrine as the FTS membership JOIN).

Measured design (retrieval cycles, 22-query live-corpus eval):
- Chunk scores MAX-POOL to their parent doc BEFORE ranking — one entry
  per doc enters fusion, and the existing thread-collapse/report
  pipeline downstream stays untouched.
- Hash EQUALITY between the vector row and the caller-visible doc row:
  an edited doc's stale vector never serves (the redaction oracle).
- Fusion is per-SECTION weighted RRF (k=60, w_lex=1, w_sem=2): global
  single-list fusion evicted 26/61 work-section rows (re-breaking the
  0134 sectioned-report fix), and w_sem=2 was the sweep's winner —
  unweighted fusion diluted measured rescues 0.33→0.00.
- The semantic candidate list caps at 400 (the lexical pool's own cap):
  no similarity floor exists — noise sits at 0.40-0.48 while true tails
  sit at 0.50 (measured); a threshold would cut rescues before noise.
"""

from __future__ import annotations

from typing import Any

RRF_K = 60
W_LEX = 1.0
W_SEM = 2.0
SEM_POOL = 400


def semantic_candidates(snapshot: dict[str, Any], query_vec: list[float],
                        visible: dict[tuple[str, str, str], str],
                        *, pool: int = SEM_POOL) -> list[tuple[str, str, str]]:
    """Ranked (kind, channel, ref) for ONE model snapshot, scoped to the
    caller's `visible` docs ({key: text_hash}). Chunks max-pool to docs.
    Returns at most `pool` keys, best first."""
    import numpy as np

    mat = snapshot["mat"]
    if mat.shape[0] == 0:
        return []
    q = np.asarray(query_vec, dtype=np.float32)
    norm = float(np.linalg.norm(q))
    if norm == 0.0:
        return []
    q = q / norm
    scores = mat @ q                       # rows are pre-normalized
    best: dict[tuple[str, str, str], float] = {}
    for (kind, channel, ref, _chunk), vhash, score in zip(
            snapshot["keys"], snapshot["hashes"], scores.tolist()):
        key = (kind, channel, ref)
        cur_hash = visible.get(key)
        if cur_hash is None or cur_hash != vhash:
            continue                        # not visible, or stale vector
        if score > best.get(key, float("-inf")):
            best[key] = score               # max-pool chunk -> doc
    ranked = sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))
    return [key for key, _ in ranked[:pool]]


def rrf_fuse(lexical: list[Any], semantic: list[Any], *,
             k: int = RRF_K, w_lex: float = W_LEX,
             w_sem: float = W_SEM) -> list[Any]:
    """Weighted reciprocal-rank fusion of two ranked id lists (the caller
    fuses PER SECTION). Deterministic tie-break: fused score desc, then
    lexical rank, then semantic rank, then the id itself — reruns are
    byte-identical (retrieval P3)."""
    lex_rank = {key: i for i, key in enumerate(lexical)}
    sem_rank = {key: i for i, key in enumerate(semantic)}
    fused: dict[Any, float] = {}
    for key, i in lex_rank.items():
        fused[key] = fused.get(key, 0.0) + w_lex / (k + i + 1)
    for key, i in sem_rank.items():
        fused[key] = fused.get(key, 0.0) + w_sem / (k + i + 1)
    big = 1 << 30
    return sorted(fused, key=lambda key: (
        -fused[key], lex_rank.get(key, big), sem_rank.get(key, big),
        str(key)))
