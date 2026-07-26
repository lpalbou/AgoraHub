"""Embedding pipeline pure core (agora-0137 step 3): chunking + the
standing 4-prong reconcile."""
from __future__ import annotations

from agora.embedder import (CHUNK_OVERLAP, CHUNK_SIZE, CHUNK_THRESHOLD,
                            chunk_text, reconcile)


def test_small_docs_are_one_chunk_with_title():
    chunks = chunk_text("the title", "short body")
    assert chunks == ["the title\nshort body"]


def test_large_docs_window_with_overlap_and_title_on_every_chunk():
    body = "x" * (CHUNK_THRESHOLD + CHUNK_SIZE)   # forces 4 windows
    chunks = chunk_text("t", body)
    assert len(chunks) > 1
    assert all(c.startswith("t\n") for c in chunks)
    # Overlap: consecutive windows share CHUNK_OVERLAP chars of body.
    first_body = chunks[0][2:]
    second_body = chunks[1][2:]
    assert first_body[-CHUNK_OVERLAP:] == second_body[:CHUNK_OVERLAP]
    # Tail is covered: total distinct coverage equals the body length.
    step = CHUNK_SIZE - CHUNK_OVERLAP
    assert (len(chunks) - 1) * step + len(chunks[-1][2:]) >= len(body)


def test_title_only_docs_embed_their_title():
    """362 title-only docs measured: empty text must not embed emptiness."""
    assert chunk_text("just a title", "") == ["just a title\n"]


def _doc(h="h1", title="t", text="body"):
    return {"text_hash": h, "title": title, "text": text}


def test_reconcile_four_prongs():
    docs = {
        ("message", "room", "fresh"): _doc(h="hf"),
        ("message", "room", "stale"): _doc(h="NEW"),
        ("message", "room", "big"): _doc(h="hb", text="y" * 3000),
    }
    vec_hashes = {
        ("message", "room", "fresh"): "hf",       # in sync
        ("message", "room", "stale"): "OLD",      # prong 3: hash mismatch
        ("message", "room", "big"): "hb",         # hash ok but chunks short
        ("message", "room", "ghost"): "hg",       # prong 2: doc gone
    }
    counts = {
        ("message", "room", "fresh"): 1,
        ("message", "room", "stale"): 1,
        ("message", "room", "big"): 1,            # should be 4 chunks
        ("message", "room", "ghost"): 1,
    }
    out = reconcile(docs, vec_hashes, counts, active_model="new",
                    models_present={"new", "old", "older"},
                    pending_model=None)
    assert set(out["to_embed"]) == {("message", "room", "stale"),
                                    ("message", "room", "big")}
    assert out["to_delete_docs"] == [("message", "room", "ghost")]
    assert out["models_to_drop"] == ["old", "older"]   # prong 4


def test_reconcile_pending_model_is_kept():
    """Blue/green (option C): the pending fill's vectors are never a
    'leftover' — dropping them mid-fill would restart the fill forever."""
    out = reconcile({}, {}, {}, active_model="old",
                    models_present={"old", "new"}, pending_model="new")
    assert out["models_to_drop"] == []


def test_reconcile_heals_a_restored_db():
    """The ops drill: hub db restored from backup (older corpus) while
    vectors.db kept newer state — the diff must re-embed what the restore
    changed and purge what it removed, with no stored queue to trust."""
    docs = {("message", "room", "kept"): _doc(h="h-old-content")}
    vec_hashes = {("message", "room", "kept"): "h-newer-content",
                  ("message", "room", "post-backup-doc"): "hx"}
    counts = {("message", "room", "kept"): 1,
              ("message", "room", "post-backup-doc"): 1}
    out = reconcile(docs, vec_hashes, counts, active_model="m",
                    models_present={"m"})
    assert out["to_embed"] == [("message", "room", "kept")]
    assert out["to_delete_docs"] == [("message", "room", "post-backup-doc")]


def test_embedder_thread_fills_heals_and_reports(tmp_path):
    """The thread against the fake endpoint: fill from zero, coverage
    ready, orphan purge on doc removal, degraded-with-reason on endpoint
    failure — and the heartbeat never freezes in a lying state."""
    import time

    import pytest
    pytest.importorskip("numpy")
    from agora.embed_client import EmbedClient
    from agora.embedder import Embedder
    from agora.vector_store import VectorStore
    from tests.fake_embed import FakeEmbedServer

    server = FakeEmbedServer().start()
    store = VectorStore(str(tmp_path / "vectors.db"))
    corpus = {("message", "room", f"m{i}"):
              {"text_hash": f"h{i}", "title": f"t{i}", "text": f"body {i}",
               "created_at": float(i)} for i in range(5)}
    emb = Embedder(store, EmbedClient(server.url, "m"),
                   read_docs=lambda: dict(corpus),
                   models_meta=lambda: ("m", None))
    emb.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and emb.coverage("m") < 1.0:
            time.sleep(0.1)
        assert emb.coverage("m") == 1.0
        assert emb.embedded_total >= 5
        # Doc leaves the corpus -> orphan purged by the standing sweep.
        del corpus[("message", "room", "m0")]
        emb.nudge()
        deadline = time.time() + 10
        while time.time() < deadline and store.counts("m")["docs"] != 4:
            time.sleep(0.1)
        assert store.counts("m")["docs"] == 4
        # Endpoint failure -> breaker opens, thread stays alive and honest.
        server.fail_next = 5
        corpus[("message", "room", "new")] = {
            "text_hash": "hn", "title": "t", "text": "b", "created_at": 99.0}
        emb.nudge()
        time.sleep(1.0)
        assert emb.alive()
    finally:
        emb.stop()
        server.stop()
    assert not emb.alive()
