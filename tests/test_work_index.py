"""Work-id activity index (0093): the hub half of the Option-A stitch.

What must hold: the ruled id grammar (S0: <package>-<NNNN>, last-hyphen
parse, URL-safe) is one shared definition; GET /work/{id} returns claims +
decisions + citing messages across ONLY the caller's channels (membership
is the gate); structured `item_ref` citations are validated at post time
(rot cannot enter the index) while prose mentions stay free and index as
'mention'; pointer-claim keys that parse as work ids must agree with their
value.item; free-text claims keep working untouched.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agora.hub.app import create_app
from agora.models import parse_work_id

ADMIN_KEY = "test-admin"
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}


def make_client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0, dark_watch_seconds=0)
    return TestClient(app)


def register(client, agent_id):
    r = client.post("/agents", json={"id": agent_id}, headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


# -- grammar -------------------------------------------------------------------


def test_work_id_grammar():
    assert parse_work_id("agora-0093") == ("agora", "0093")
    # Last-hyphen parse: package names may carry hyphens.
    assert parse_work_id("abstract-core-0017") == ("abstract-core", "0017")
    assert parse_work_id("x-1") == ("x", "1")
    for bad in ("agora#0093", "AGORA-0093", "agora-", "-0093", "agora",
                "agora-00a3", "agora 0093", "", "agora-0093-"):
        assert parse_work_id(bad) is None, bad


# -- the index -----------------------------------------------------------------


def test_work_index_gathers_claims_decisions_messages():
    client = make_client()
    ka, kb = register(client, "alpha"), register(client, "beta")
    client.post("/channels", json={"name": "room", "private": False},
                headers=ka)
    client.post("/channels/room/join", json={}, headers=kb)

    # A pointer claim, a decision citing the id, and two messages: one
    # structured (item_ref) and one prose mention.
    client.put("/channels/room/store/claim:agora-0093", headers=ka,
               json={"value": {"owner": "alpha", "item": "agora-0093",
                               "card": "fw-0017/S2"}, "expect_version": 0})
    client.put("/channels/room/store/decision:stitch-shape", headers=ka,
               json={"value": {"summary": "agora-0093 ships the index"},
                     "expect_version": 0})
    r = client.post("/channels/room/messages", headers=ka,
                    json={"title": "receipt", "body": "index built",
                          "status": "fyi", "data": {"item_ref": "agora-0093"}})
    assert r.status_code == 200, r.text
    client.post("/channels/room/messages", headers=kb,
                json={"title": "note", "body": "watching agora-0093 land",
                      "status": "fyi"})

    out = client.get("/work/agora-0093", headers=kb).json()
    assert out["item_id"] == "agora-0093"
    assert len(out["claims"]) == 1
    assert out["claims"][0]["value"]["owner"] == "alpha"
    assert len(out["decisions"]) == 1
    assert out["decisions"][0]["key"] == "decision:stitch-shape"
    vias = {m["via"] for m in out["messages"]}
    assert vias == {"item_ref", "mention"}
    assert len(out["messages"]) == 2


def test_work_index_is_membership_gated():
    client = make_client()
    ka, kout = register(client, "alpha"), register(client, "outsider")
    client.post("/channels", json={"name": "sekrit", "private": True},
                headers=ka)
    client.put("/channels/sekrit/store/claim:agora-0093", headers=ka,
               json={"value": {"owner": "alpha", "item": "agora-0093"},
                     "expect_version": 0})
    client.post("/channels/sekrit/messages", headers=ka,
                json={"title": "t", "body": "agora-0093 work", "status": "fyi"})

    # The outsider sees an EMPTY index, not an error — private rooms simply
    # do not contribute rows to a non-member's view.
    out = client.get("/work/agora-0093", headers=kout).json()
    assert out["claims"] == [] and out["messages"] == []
    # The member sees everything.
    out = client.get("/work/agora-0093", headers=ka).json()
    assert len(out["claims"]) == 1 and len(out["messages"]) == 1


def test_work_index_rejects_non_id():
    client = make_client()
    ka = register(client, "alpha")
    r = client.get("/work/not_an_id!", headers=ka)
    assert r.status_code == 400 and "ruled form" in r.text


# -- citation validation ---------------------------------------------------------


def test_item_ref_validated_at_post_time():
    client = make_client()
    ka = register(client, "alpha")
    client.post("/channels", json={"name": "room", "private": False},
                headers=ka)
    r = client.post("/channels/room/messages", headers=ka,
                    json={"title": "t", "body": "b", "status": "fyi",
                          "data": {"item_ref": "Not A Work Id"}})
    assert r.status_code == 400 and "work id" in r.text
    # Prose stays free: the same string in the body posts fine.
    r = client.post("/channels/room/messages", headers=ka,
                    json={"title": "t", "body": "Not A Work Id", "status": "fyi"})
    assert r.status_code == 200


def test_pointer_claim_key_value_consistency():
    client = make_client()
    ka = register(client, "alpha")
    client.post("/channels", json={"name": "room", "private": False},
                headers=ka)
    # Key parses as a work id and value.item disagrees: refused, teaching.
    r = client.put("/channels/room/store/claim:agora-0093", headers=ka,
                   json={"value": {"owner": "alpha", "item": "agora-0094"},
                         "expect_version": 0})
    assert r.status_code == 400 and "ONE id" in r.text
    # Agreeing value.item passes.
    r = client.put("/channels/room/store/claim:agora-0093", headers=ka,
                   json={"value": {"owner": "alpha", "item": "agora-0093"},
                         "expect_version": 0})
    assert r.status_code == 200
    # Free-text claims (task part is NOT a work id) stay untouched even
    # with an item field — they are not pointer claims.
    r = client.put("/channels/room/store/claim:fix-the-voice", headers=ka,
                   json={"value": {"owner": "alpha", "item": "whatever"},
                         "expect_version": 0})
    assert r.status_code == 200


# -- unified backlog rows (0103): work:<id> index contract -----------------------


def _room_with_two(client):
    ka, kb = register(client, "alpha"), register(client, "beta")
    client.post("/channels", json={"name": "room", "private": False},
                headers=ka)
    client.post("/channels/room/join", json={}, headers=kb)
    return ka, kb


def test_work_row_key_must_parse_and_status_from_the_file_words():
    client = make_client()
    ka, _ = _room_with_two(client)
    # Unparseable key: refused, teaching.
    r = client.put("/channels/room/store/work:not-an-id-", headers=ka,
                   json={"value": {"title": "x", "status": "planned"},
                         "expect_version": 0})
    assert r.status_code == 400 and "work id" in r.text
    # Derived words are never stored (continuum's S0 clause, mechanical).
    r = client.put("/channels/room/store/work:agora-0102", headers=ka,
                   json={"value": {"title": "x", "status": "in_progress"},
                         "expect_version": 0})
    assert r.status_code == 400 and "DERIVED" in r.text
    r = client.put("/channels/room/store/work:agora-0102", headers=ka,
                   json={"value": {"title": "x", "status": "done"},
                         "expect_version": 0})
    assert r.status_code == 400
    # Off-vocabulary word: refused with the closed set named.
    r = client.put("/channels/room/store/work:agora-0102", headers=ka,
                   json={"value": {"title": "x", "status": "someday"},
                         "expect_version": 0})
    assert r.status_code == 400 and "proposed|planned" in r.text
    # The file's own directory words pass.
    r = client.put("/channels/room/store/work:agora-0102", headers=ka,
                   json={"value": {"title": "x", "status": "planned",
                                   "owner": "alpha",
                                   "card": "docs/backlog/planned/0102.md"},
                         "expect_version": 0})
    assert r.status_code == 200


def test_work_rows_list_and_work_index_fold():
    client = make_client()
    ka, kb = _room_with_two(client)
    for wid, status in (("agora-0101", "completed"), ("agora-0102", "planned")):
        client.put(f"/channels/room/store/work:{wid}", headers=ka,
                   json={"value": {"title": f"item {wid}", "status": status,
                                   "owner": "alpha", "card": f"x/{wid}.md",
                                   "receipt": "c1" if status == "completed" else None},
                         "expect_version": 0})
    rows = client.get("/channels/room/work", headers=kb).json()
    assert [r["id"] for r in rows] == ["agora-0101", "agora-0102"]
    assert rows[0]["status"] == "completed" and rows[0]["receipt"] == "c1"
    assert rows[1]["updated_by"] == "alpha" and rows[1]["version"] == 1
    # Any member may repair a stale mirror (file-wins): beta updates.
    client.put("/channels/room/store/work:agora-0102", headers=kb,
               json={"value": {"title": "item agora-0102", "status": "completed",
                               "owner": "alpha", "card": "x/agora-0102.md"},
                     "expect_version": 1})
    rows = client.get("/channels/room/work", headers=ka).json()
    assert rows[1]["status"] == "completed" and rows[1]["updated_by"] == "beta"
    # The stitch surface shows the index row beside claims/messages.
    out = client.get("/work/agora-0102", headers=ka).json()
    assert len(out["work_rows"]) == 1
    assert out["work_rows"][0]["value"]["status"] == "completed"
    # Non-members: the list is membership-gated like any store read.
    kout = register(client, "outsider")
    r = client.get("/channels/room/work", headers=kout)
    assert r.status_code == 403
