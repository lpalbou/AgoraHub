"""Vector store (agora-0137 step 2): the standalone vectors.db contract.

What we want as behavior, each pinned by the adversary cycle that
demanded it: full-key isolation (cross-channel contamination), whole-input
hash atomicity, little-endian/dim/NaN hygiene, one-model matrices, and
disposability (delete_model + vacuum leave a servable store)."""
from __future__ import annotations

import math
import struct

import pytest

pytest.importorskip("numpy")

from agora.vector_store import VectorStore, pack_vector


def _store(tmp_path) -> VectorStore:
    return VectorStore(str(tmp_path / "vectors.db"))


def _row(ref="m1", channel="room", chunk=0, h="h1", values=None, kind="message"):
    return {"kind": kind, "channel": channel, "ref": ref, "chunk": chunk,
            "text_hash": h, "values": values or [1.0, 0.0, 0.0]}


def test_full_key_isolates_same_ref_across_channels(tmp_path):
    """The cycle-1 P1: the same fs ref exists in TWO channels with
    different content today — one vector must never serve both."""
    vs = _store(tmp_path)
    vs.put_batch("m", [_row(ref="fs/charter.md", channel="a", h="ha",
                            values=[1.0, 0.0, 0.0], kind="file"),
                       _row(ref="fs/charter.md", channel="b", h="hb",
                            values=[0.0, 1.0, 0.0], kind="file")])
    snap = vs.matrix("m", 3)
    assert len(snap["keys"]) == 2
    by_key = {k: h for k, h in zip(snap["keys"], snap["hashes"])}
    assert by_key[("file", "a", "fs/charter.md", 0)] == "ha"
    assert by_key[("file", "b", "fs/charter.md", 0)] == "hb"


def test_nan_clamped_dim_checked_and_little_endian(tmp_path):
    """Hygiene riders: NaN/inf clamp at write; wrong-dim rows are skipped
    loudly, never mis-shaped; the pack is explicit little-endian."""
    assert pack_vector([1.0, float("nan"), float("inf")]) == struct.pack(
        "<3f", 1.0, 0.0, 0.0)
    vs = _store(tmp_path)
    vs.put_batch("m", [_row(ref="ok", values=[1.0, 0.0, 0.0]),
                       _row(ref="short", values=[1.0, 0.0])])  # wrong dim
    snap = vs.matrix("m", 3)
    assert [k[2] for k in snap["keys"]] == ["ok"]
    assert snap["skipped_dim"] == 1
    assert not any(math.isnan(x) for x in snap["mat"].flatten().tolist())


def test_one_model_per_matrix_and_write_invalidates_cache(tmp_path):
    vs = _store(tmp_path)
    vs.put_batch("old", [_row(ref="d1", h="h1")])
    vs.put_batch("new", [_row(ref="d1", h="h1", values=[0.0, 1.0, 0.0])])
    snap_old = vs.matrix("old", 3)
    assert len(snap_old["keys"]) == 1        # never mixes models
    snap_new = vs.matrix("new", 3)
    assert len(snap_new["keys"]) == 1
    vs.put_batch("new", [_row(ref="d2", h="h2", values=[0.0, 0.0, 1.0])])
    assert len(vs.matrix("new", 3)["keys"]) == 2   # cache invalidated


def test_mixed_chunk_hashes_force_reembed(tmp_path):
    """A crash mid-edit can strand chunks with different hashes for one
    doc; the reconcile input must flag the doc stale, not trust either."""
    vs = _store(tmp_path)
    vs.put_batch("m", [_row(ref="big", chunk=0, h="old"),
                       _row(ref="big", chunk=1, h="new")])
    hashes = vs.hashes_for_model("m")
    assert hashes[("message", "room", "big")] == ""   # forced mismatch


def test_delete_model_then_vacuum_leaves_a_servable_store(tmp_path):
    """Disposability: dropping the old model after a flip (blue/green C)
    and vacuuming OUR OWN file must leave the new model serving."""
    vs = _store(tmp_path)
    vs.put_batch("old", [_row(ref=f"d{i}") for i in range(50)])
    vs.put_batch("new", [_row(ref="d0", values=[0.0, 1.0, 0.0])])
    assert vs.delete_model("old") == 50
    vs.vacuum()
    assert vs.counts("old") == {"docs": 0, "rows": 0}
    assert len(vs.matrix("new", 3)["keys"]) == 1


def test_doc_delete_purges_all_chunks_and_models(tmp_path):
    vs = _store(tmp_path)
    vs.put_batch("m1", [_row(ref="gone", chunk=0), _row(ref="gone", chunk=1)])
    vs.put_batch("m2", [_row(ref="gone", chunk=0)])
    vs.delete_doc("message", "room", "gone")
    assert vs.counts("m1")["rows"] == 0
    assert vs.counts("m2")["rows"] == 0
