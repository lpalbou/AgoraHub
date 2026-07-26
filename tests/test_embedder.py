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
