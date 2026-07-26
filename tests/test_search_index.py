"""Search index sync (agora-0132, build step 2): every write choke point
keeps the FTS corpus true, and the failure class this guards is SILENT —
a missed site means retracted/deleted content stays matchable (the oracle
the design forbids) or new content never becomes findable. One test per
choke point, plus rebuild determinism (the drift eraser)."""

from __future__ import annotations

import sqlite3

from agora.db import Database
from agora import search_index as si


def _docs(db: Database, kind: str | None = None) -> list[tuple]:
    q = "SELECT kind, channel, ref, title, text FROM search_docs"
    args: tuple = ()
    if kind:
        q += " WHERE kind = ?"
        args = (kind,)
    with db._lock:
        return [tuple(r) for r in db._conn.execute(q + " ORDER BY kind, ref", args)]


def _match(db: Database, term: str) -> list[str]:
    """Raw FTS probe: refs of docs matching the term (the oracle check)."""
    with db._lock:
        return [r["ref"] for r in db._conn.execute(
            "SELECT sd.ref FROM search_fts f JOIN search_docs sd"
            " ON sd.doc_id = f.rowid WHERE search_fts MATCH ?", (term,))]


def test_message_insert_and_retract_sync():
    db = Database(":memory:")
    m = db.insert_message("room", "alice", kind="message", status="fyi",
                          urgency="inbox", title="plan",
                          body="the zanzibar rollout starts tomorrow",
                          data=None, reply_to=None)
    assert _match(db, "zanzibar") == [m.id]
    db.retract_message(m.id, "alice")
    # The oracle check: the retracted words are unmatchable, not just
    # redacted at read (match-then-redact is forbidden by design).
    assert _match(db, "zanzibar") == []
    assert _docs(db, "message") == []


def test_ask_texts_are_indexed_bodies_alone_are_blind():
    db = Database(":memory:")
    m = db.insert_message("room", "alice", kind="message", status="open",
                          urgency="inbox", title="q", body="see asks",
                          data={"asks": [{"id": "1",
                                          "text": "should quorum be seven"}]},
                          reply_to=None)
    assert _match(db, "quorum") == [m.id]


def test_store_prefix_whitelist_default_closed():
    db = Database(":memory:")
    db.store_set("room", "decision:shape", {"summary": "we chose kelp"}, "alice")
    db.store_set("room", "claim:agora-0001", {"owner": "bob", "status": "building"}, "bob")
    db.store_set("room", "work:agora-0002", {"status": "planned", "title": "flux gate"}, "bob")
    db.store_set("room", "channel:meta", {"purpose": "unindexed meta"}, "alice")
    db.store_set("room", "random:thing", {"note": "never indexed"}, "alice")
    kinds = {d[0] for d in _docs(db)}
    assert kinds == {"decision", "claim", "work"}
    assert _match(db, "kelp") == ["decision:shape"]
    # F4: values are extracted text, never raw JSON — field names are not
    # matchable content.
    assert _match(db, "summary") == []
    # Unlisted prefixes never entered.
    assert _match(db, "unindexed") == [] and _match(db, "never") == []


def test_fs_head_lifecycle():
    db = Database(":memory:")
    db.fs_put("room", "fs/notes/plan.md", {"content": "the heliotrope design"}, "alice")
    assert _match(db, "heliotrope") == ["fs/notes/plan.md"]
    db.fs_remove("room", "fs/notes/plan.md", "alice")
    assert _match(db, "heliotrope") == []          # tombstone unmatchable (H2)
    db.fs_put("room", "fs/notes/plan.md", {"content": "heliotrope reborn"}, "alice")
    assert _match(db, "reborn") == ["fs/notes/plan.md"]  # re-create re-indexes


def test_agent_about_lifecycle_register_retire_unretire_delete():
    db = Database(":memory:")
    db.register_agent("kelvin", "kelvin", "key-1", about="owns the cryostat lane")
    assert _match(db, "cryostat") == ["kelvin"]
    db.retire_agent("kelvin", "done")
    assert _match(db, "cryostat") == []            # off every surface at retire
    db.unretire_agent("kelvin")
    assert _match(db, "cryostat") == ["kelvin"]    # restore brings the doc back
    db.retire_agent("kelvin", "done")
    db.delete_agent("kelvin")
    assert _match(db, "cryostat") == []
    # set_about upserts; clearing removes.
    db.register_agent("mira", "mira", "key-2", about="")
    assert _docs(db, "agent") == []                # empty about = no doc
    db.set_about("mira", "owns the flux capacitor")
    assert _match(db, "capacitor") == ["mira"]
    db.set_about("mira", "")
    assert _match(db, "capacitor") == []


def test_sentinels_stripped_at_ingest():
    db = Database(":memory:")
    db.insert_message("room", "alice", kind="message", status="fyi",
                      urgency="inbox", title="t",
                      body="forge\u0001d high\u0002light", data=None,
                      reply_to=None)
    row = _docs(db, "message")[0]
    assert "\u0001" not in row[4] and "\u0002" not in row[4]


def test_rebuild_is_deterministic_and_erases_drift():
    db = Database(":memory:")
    m1 = db.insert_message("room", "alice", kind="message", status="fyi",
                           urgency="inbox", title="a", body="alpha words",
                           data=None, reply_to=None)
    db.insert_message("room", "bob", kind="message", status="fyi",
                      urgency="inbox", title="b", body="beta words",
                      data=None, reply_to=None)
    db.store_set("room", "decision:d1", {"summary": "gamma ruling"}, "alice")
    db.register_agent("kel", "kel", "k", about="delta owner")
    db.retract_message(m1.id, "alice")
    before = _docs(db)

    # Sabotage the index out-of-band (simulated drift), then rebuild.
    with db._lock:
        db._conn.execute("DELETE FROM search_docs")
        db._conn.execute("INSERT INTO search_fts(search_fts) VALUES ('rebuild')")
        db._conn.commit()
    assert db.search_drift()["message_drift"] == 1
    counts = db.rebuild_search_index()
    assert counts["message"] == 1 and counts["decision"] == 1 and counts["agent"] == 1
    assert _docs(db) == before                     # deterministic
    assert db.search_drift()["message_drift"] == 0
    assert _match(db, "alpha") == []               # retracted stays out


def test_read_transaction_pool_on_file_backed_db(tmp_path):
    """R1/R2 smoke on a real file: the pool opens read-only AFTER the
    writer (WAL sidecars exist), serves a consistent snapshot, and a
    write through the search connection fails loudly (query_only)."""
    db = Database(str(tmp_path / "hub.db"))
    db.insert_message("room", "alice", kind="message", status="fyi",
                      urgency="inbox", title="t", body="epsilon start",
                      data=None, reply_to=None)
    with db.read_transaction() as conn:
        n1 = conn.execute("SELECT COUNT(*) AS n FROM search_docs").fetchone()["n"]
        # A concurrent write lands mid-transaction...
        db.insert_message("room", "alice", kind="message", status="fyi",
                          urgency="inbox", title="t2", body="zeta later",
                          data=None, reply_to=None)
        # ...and the open snapshot must NOT see it (one report, one world).
        n2 = conn.execute("SELECT COUNT(*) AS n FROM search_docs").fetchone()["n"]
        assert n1 == n2 == 1
        try:
            conn.execute("DELETE FROM search_docs")
            raise AssertionError("query_only connection accepted a write")
        except sqlite3.OperationalError:
            pass
    # A fresh transaction sees the new state.
    with db.read_transaction() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM search_docs").fetchone()["n"] == 2
    db.close()


def test_text_hash_rides_every_doc_and_migration_backfills(tmp_path):
    """agora-0137 build step 1: every indexed doc carries doc_hash(title,
    text) — the semantic layer's change detector — and a pre-0137 hub
    (rows with empty hashes) is backfilled at boot. An edit CHANGES the
    hash: that inequality is what stops a stale vector from serving."""
    path = str(tmp_path / "hub.db")
    db = Database(path)
    db.insert_message("room", "alice", kind="message", status="fyi",
                      urgency="inbox", title="t", body="the first prose",
                      data=None, reply_to=None)
    row = db._conn.execute(
        "SELECT title, text, text_hash FROM search_docs").fetchone()
    assert row["text_hash"] == si.doc_hash(row["title"], row["text"])
    first_hash = row["text_hash"]
    # Simulate a pre-0137 hub: blank the hashes, close, re-open.
    db._conn.execute("UPDATE search_docs SET text_hash = ''")
    db._conn.commit()
    db.close()
    db2 = Database(path)
    row2 = db2._conn.execute(
        "SELECT text_hash FROM search_docs").fetchone()
    assert row2["text_hash"] == first_hash          # backfill, same content
    # Different content = different hash (the whole point).
    assert si.doc_hash("t", "edited prose") != first_hash
    db2.close()
