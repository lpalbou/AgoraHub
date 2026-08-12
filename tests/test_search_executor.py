"""Search compiler + executor (agora-0132, build step 3).

The compiler property (H5): ANY byte string either compiles to a valid
FTS5 MATCH or raises the ONE typed error — never a 500, never live FTS
syntax semantics. The executor properties: membership scoping with
existence non-disclosure, six fixed sections, zero-hit relaxation
(loud), thread collapse, structural recency ordering, snippet offsets.
"""

from __future__ import annotations

import random
import string

import pytest

from agora.db import Database
from agora.hub import search as sx


# -- compiler -------------------------------------------------------------

def test_compiler_drops_punctuation_and_REFUSES_over_cap_queries():
    """Punctuation-only tokens are dropped (they match every markdown
    bullet). Over-cap terms and over-long queries are REFUSED.

    NO SILENT LIMIT (operator, standing). Both caps used to apply as a
    slice: a 300-char term searched its first 64 characters, and an 11-term
    query searched the first 8. Both returned 200 with a confident, wrong
    result set — and an unexpected result set is the one thing a searcher
    blames on the corpus rather than on the query."""
    assert sx.compile_terms("delegate - UI") == ["delegate", "UI"]
    assert sx.compile_terms("w1 w2 w3 w4 w5 w6 w7 w8") == [
        f"w{i}" for i in range(1, 9)]           # exactly MAX_TERMS is fine

    with pytest.raises(sx.SearchQueryError):
        sx.compile_terms("--- ::: ...")

    with pytest.raises(sx.SearchQueryError, match="at most"):
        sx.compile_terms("w1 w2 w3 w4 w5 w6 w7 w8 w9 w10")

    # An over-long single term names itself and its length.
    with pytest.raises(sx.SearchQueryError) as exc:
        sx.compile_terms("y" * 100)
    assert "100 characters" in str(exc.value)

    # ...and the whole-query length gate still fires first for huge inputs.
    with pytest.raises(sx.SearchQueryError, match="1..256"):
        sx.compile_terms("x" * 300)


def test_hyphen_compounds_match_across_forms():
    """Tokenizer v2 (0134): compounds split at INDEX time, so 'thumbs down'
    finds 'thumbs-down' docs and vice versa — no query-side expansion
    tricks (the v1 tokenchars made 70% of live vocabulary reachable only
    by the exact joined form)."""
    db = Database(":memory:")
    db.create_channel("room", private=True, created_by="a")
    db.add_member("room", "a")
    db.insert_message("room", "a", kind="message", status="fyi",
                      urgency="inbox", title="t", body="that was a thumbs-down moment",
                      data=None, reply_to=None)
    with db.read_transaction() as conn:
        ex = sx.SearchExecutor(conn, "a")
        for q in ("thumbs down", "thumbs-down"):
            rep = ex.run(sx.compile_terms(q), {})
            assert rep["sections"]["messages"]["total"] == 1, q


def test_compiler_fuzz_property_never_raw_fts_semantics():
    """For any input: valid MATCH or SearchQueryError. Exercised against a
    REAL fts table so 'valid' means SQLite accepts it."""
    db = Database(":memory:")
    specials = '"*:()^-,' + "'"
    rng = random.Random(20260724)
    corpus = [
        'title:orchestration', 'AND voting', '"unterminated', 'NEAR(a b)',
        'a OR b', '-x', '((((', 'name*', 'a:b:c', '"" "" ""', '\u0001\u0002',
    ]
    for _ in range(200):
        n = rng.randint(1, 30)
        corpus.append("".join(rng.choice(string.ascii_letters + specials + " ")
                              for _ in range(n)))
    for q in corpus:
        try:
            terms = sx.compile_terms(q)
        except sx.SearchQueryError:
            continue
        match = sx.compile_match(terms)
        with db._lock:  # any exception here fails the property
            db._conn.execute(
                "SELECT count(*) FROM search_fts WHERE search_fts MATCH ?",
                (match,)).fetchone()
    # And column-filter syntax is neutralized into a phrase, not semantics.
    with db._lock:
        db._conn.execute(
            "INSERT INTO search_docs (kind, channel, ref, title, text, created_at)"
            " VALUES ('message','room','m1','orchestration','plain body', 1.0)")
        db._conn.execute(
            "INSERT INTO search_fts(rowid, title, text)"
            " SELECT doc_id, title, text FROM search_docs")
        db._conn.commit()
    m = sx.compile_match(sx.compile_terms("title:orchestration"))
    with db._lock:
        n = db._conn.execute(
            "SELECT count(*) AS n FROM search_fts WHERE search_fts MATCH ?",
            (m,)).fetchone()["n"]
    assert n == 0  # the literal phrase "title:orchestration" matches nothing


# -- executor -------------------------------------------------------------

def _seed() -> Database:
    db = Database(":memory:")
    for ch, members in [("room", ("alice", "bob")), ("vault", ("carol",))]:
        db.create_channel(ch, private=True, created_by=members[0])
        for a in members:
            db.add_member(ch, a)
    db.insert_message("room", "alice", kind="message", status="open",
                      urgency="inbox", title="quorum question",
                      body="should the zebra quorum be seven", data=None,
                      reply_to=None)
    root = db.insert_message("room", "bob", kind="message", status="fyi",
                             urgency="inbox", title="zebra thread root",
                             body="zebra context one", data=None, reply_to=None)
    db.insert_message("room", "alice", kind="message", status="reply",
                      urgency="inbox", title="re: zebra",
                      body="zebra context two agreed", data=None,
                      reply_to=root.id)
    db.store_set("room", "decision:zebra-shape", {"summary": "zebra rules the plain"},
                 "alice")
    db.fs_put("room", "fs/zebra.md", {"content": "zebra file notes"}, "alice")
    db.store_set("vault", "decision:secret", {"summary": "the zebra vault ruling"},
                 "carol")
    db.insert_message("vault", "carol", kind="message", status="fyi",
                      urgency="inbox", title="private zebra",
                      body="vault zebra words", data=None, reply_to=None)
    return db


def _run(db: Database, caller: str, q: str, **kw):
    with db.read_transaction() as conn:
        ex = sx.SearchExecutor(conn, caller)
        return ex.run(sx.compile_terms(q), kw.pop("filters", {}), **kw)


def test_grouped_report_is_membership_scoped_with_no_existence_leak():
    db = _seed()
    rep = _run(db, "alice", "zebra")
    assert set(rep["sections"].keys()) == set(sx.SECTIONS)
    # alice sees room content only — vault contributes NOTHING, not even
    # to totals.
    all_channels = {h["channel"] for s in rep["sections"].values()
                    for h in s["hits"]}
    assert all_channels == {"room"}
    assert rep["sections"]["decisions"]["total"] == 1
    # carol sees exactly the inverse.
    rep_c = _run(db, "carol", "zebra")
    chans_c = {h["channel"] for s in rep_c["sections"].values()
               for h in s["hits"]}
    assert chans_c == {"vault"}
    # Filtering to a non-member channel == filtering to a nonexistent one.
    rep_nm = _run(db, "alice", "zebra", filters={"channels": ["vault"]})
    rep_nx = _run(db, "alice", "zebra", filters={"channels": ["nope"]})
    strip = lambda r: {n: (s["shown"], s["total"]) for n, s in r["sections"].items()}
    assert strip(rep_nm) == strip(rep_nx)
    assert all(v == (0, 0) for v in strip(rep_nm).values())


def test_sections_route_kinds_and_open_threads_split():
    db = _seed()
    rep = _run(db, "alice", "zebra")
    assert rep["sections"]["open_threads"]["hits"][0]["status"] == "open"
    assert rep["sections"]["decisions"]["hits"][0]["ref"] == "decision:zebra-shape"
    assert rep["sections"]["files"]["hits"][0]["ref"] == "fs/zebra.md"
    # messages section excludes the open/blocked rows (no double-serve).
    msg_statuses = {h["status"] for h in rep["sections"]["messages"]["hits"]}
    assert "open" not in msg_statuses


def test_thread_collapse_one_row_per_root_with_count():
    db = _seed()
    rep = _run(db, "alice", "zebra context")
    msgs = rep["sections"]["messages"]["hits"]
    assert len(msgs) == 1                     # two thread messages, one row
    assert msgs[0]["thread_hits"] == 2


def test_zero_hit_relaxation_is_loud():
    db = _seed()
    rep = _run(db, "alice", "who broke the zebra quorum")
    assert rep["relaxed"] is True
    assert rep["sections"]["open_threads"]["total"] >= 1
    strict = _run(db, "alice", "zebra quorum")
    assert strict["relaxed"] is False


def test_snippets_carry_offsets_never_sentinels():
    db = _seed()
    rep = _run(db, "alice", "zebra")
    hit = rep["sections"]["decisions"]["hits"][0]
    assert "\u0001" not in hit["snippet"] and "\u0002" not in hit["snippet"]
    assert hit["highlights"], "highlight offsets expected"
    s, l = hit["highlights"][0]
    assert hit["snippet"][s:s + l].lower().startswith("zebra")


def test_votes_dimension_filter_sort_and_browse(tmp_path):
    """Operator dm#174: 'see the most up/down votes to see good/bad work
    and draw lessons.' rated=down filters to downvoted hits; sort=votes
    orders by net; with rated set, q may be EMPTY (browse mode). Default
    ranking stays vote-free."""
    import sys
    from fastapi.testclient import TestClient
    from agora.hub.app import create_app
    client = TestClient(create_app(db_path=":memory:", admin_key="k",
                                   rate_per_minute=600.0))
    A = {"Authorization": "Bearer k"}
    def reg(aid):
        return {"Authorization":
                f"Bearer {client.post('/agents', json={'id': aid}, headers=A).json()['api_key']}"}
    ka, kb = reg("alice"), reg("bob")
    client.post("/channels", json={"name": "room"}, headers=ka)
    t = client.post("/channels/room/invites", json={}, headers=ka).json()["invite_token"]
    client.post("/channels/room/join", json={"invite_token": t}, headers=kb)
    good = client.post("/channels/room/messages",
                       json={"body": "the good gizmo work"}, headers=ka).json()
    bad = client.post("/channels/room/messages",
                      json={"body": "the bad gizmo work"}, headers=ka).json()
    client.put(f"/channels/room/messages/{good['id']}/rating",
               json={"value": 1}, headers=kb)
    client.put(f"/channels/room/messages/{bad['id']}/rating",
               json={"value": -1}, headers=kb)

    # rated=down narrows to the downvoted hit.
    r = client.get("/search", params={"q": "gizmo", "rated": "down"},
                   headers=kb).json()
    refs = [h["ref"] for h in r["messages"]["hits"]]
    assert refs == [bad["id"]]
    # sort=votes = net DESC (the /top precedent): best-rated leads; the
    # worst-work lens is rated=down.
    r = client.get("/search", params={"q": "gizmo", "sort": "votes"},
                   headers=kb).json()
    refs = [h["ref"] for h in r["messages"]["hits"]]
    assert refs[0] == good["id"] and bad["id"] in refs
    # Browse mode: no q at all, rated=any — both rated messages, votes on hits.
    r = client.get("/search", params={"rated": "any", "sort": "votes"},
                   headers=kb)
    assert r.status_code == 200
    hits = r.json()["messages"]["hits"]
    assert {h["ref"] for h in hits} == {good["id"], bad["id"]}
    assert all(h["ratings"]["up"] or h["ratings"]["down"] for h in hits)
    # Empty q WITHOUT rated stays the typed 400.
    assert client.get("/search", params={"q": ""}, headers=kb).status_code == 400


def test_kind_filter_keeps_fixed_shape_and_recent_cursor_pages():
    db = _seed()
    for i in range(12):
        db.store_set("room", f"decision:d{i}", {"summary": f"pagino ruling {i}"},
                     "alice")
    rep = _run(db, "alice", "pagino", filters={"kind": "decision"}, sort="recent",
               limit=10)
    assert set(rep["sections"].keys()) == set(sx.SECTIONS)   # fixed shape
    assert rep["sections"]["messages"]["total"] == 0          # gated by kind
    assert rep["sections"]["decisions"]["shown"] == 10
    assert rep["sections"]["decisions"]["total"] == 12
    assert rep["next_cursor"]
    page2 = _run(db, "alice", "pagino", filters={"kind": "decision"},
                 sort="recent", limit=10, cursor=rep["next_cursor"])
    refs1 = {h["ref"] for h in rep["sections"]["decisions"]["hits"]}
    refs2 = {h["ref"] for h in page2["sections"]["decisions"]["hits"]}
    assert len(refs2) == 2 and not (refs1 & refs2)            # no overlap
