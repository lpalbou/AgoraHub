"""Semantic retrieval + fusion pure functions (agora-0137 step 6)."""
from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from agora.hub.semantic import rrf_fuse, semantic_candidates


def _snapshot(rows):
    """rows: (kind, channel, ref, chunk, hash, vector)"""
    keys = [(r[0], r[1], r[2], r[3]) for r in rows]
    hashes = [r[4] for r in rows]
    mat = np.array([r[5] for r in rows], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return {"model": "m", "keys": keys, "hashes": hashes, "mat": mat / norms}


def test_membership_and_hash_equality_gate_before_cosine():
    snap = _snapshot([
        ("message", "a", "vis", 0, "h1", [1.0, 0.0]),
        ("message", "b", "hidden", 0, "h2", [1.0, 0.0]),   # not visible
        ("message", "a", "edited", 0, "OLD", [1.0, 0.0]),  # stale vector
    ])
    visible = {("message", "a", "vis"): "h1",
               ("message", "a", "edited"): "NEW"}
    out = semantic_candidates(snap, [1.0, 0.0], visible)
    assert out == [("message", "a", "vis")]


def test_chunks_max_pool_to_one_doc_entry():
    snap = _snapshot([
        ("message", "a", "big", 0, "h", [1.0, 0.0]),   # strong chunk
        ("message", "a", "big", 1, "h", [0.0, 1.0]),   # weak chunk
        ("message", "a", "other", 0, "h", [0.9, 0.1]),
    ])
    visible = {("message", "a", "big"): "h", ("message", "a", "other"): "h"}
    out = semantic_candidates(snap, [1.0, 0.0], visible)
    assert out[0] == ("message", "a", "big")       # pooled, once
    assert len(out) == 2


def test_zero_query_vector_returns_nothing():
    snap = _snapshot([("message", "a", "d", 0, "h", [1.0, 0.0])])
    assert semantic_candidates(snap, [0.0, 0.0],
                               {("message", "a", "d"): "h"}) == []


def test_rrf_weights_semantic_double_and_breaks_ties_deterministically():
    """w_sem=2: a semantic-only hit at rank 0 must outrank a lexical-only
    hit at rank 0 (2/(k+1) > 1/(k+1)) — the measured rescue preserver."""
    fused = rrf_fuse(["lex-only"], ["sem-only"])
    assert fused == ["sem-only", "lex-only"]
    # Reruns are byte-identical (deterministic tie-break on equal scores).
    a = rrf_fuse(["x", "y"], ["y", "x"])
    b = rrf_fuse(["x", "y"], ["y", "x"])
    assert a == b
    # A doc ranked well by BOTH beats either single-source doc.
    fused = rrf_fuse(["both", "lex-only"], ["both", "sem-only"])
    assert fused[0] == "both"
