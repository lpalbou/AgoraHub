"""Thread retraction (0097): the trail is the unit a human regrets.

`POST /channels/{c}/messages/{id}/retract_thread` retracts the named message
and every reply beneath it in ONE transaction. What must hold: the
single-message authority rule applied to EVERY member (operator may retract
anyone's, an author only their own) with a non-operator refused OUTRIGHT so
nothing is half-retracted; obligations cleared exactly as the single verb
clears them; descendants only, never ancestors; system/fs rows skipped, not
fatal; idempotent; the ledger untouched.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agora.hub.app import create_app

ADMIN_KEY = "test-admin"
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}


def make_client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=6000.0, dark_watch_seconds=0)
    return TestClient(app)


def register(client, agent_id, operator=False):
    r = client.post("/agents", json={"id": agent_id, "operator": operator},
                    headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def room(client):
    ka, kb = register(client, "alice"), register(client, "bob")
    kop = register(client, "op", operator=True)
    client.post("/channels", json={"name": "room", "private": False}, headers=ka)
    client.post("/channels/room/join", json={}, headers=kb)
    client.post("/channels/room/join", json={}, headers=kop)
    return ka, kb, kop


def post(client, key, **kw):
    r = client.post("/channels/room/messages", headers=key,
                    json={"title": kw.pop("title", "t"),
                          "body": kw.pop("body", "b"), **kw})
    assert r.status_code == 200, r.text
    return r.json()


def rows(client, key):
    return {m["id"]: m for m in
            client.get("/channels/room/messages", headers=key).json()}


def mixed_thread(client, ka, kb):
    """root(alice) -> r1(bob) -> r2(alice) -> r3(bob), plus a sibling branch
    and one message OUTSIDE the trail that must survive untouched."""
    root = post(client, ka, body="root words", status="open",
                asks=[{"id": "1", "text": "confirm?", "to": ["bob"]}])
    r1 = post(client, kb, body="r1 words", status="reply",
              reply_to=root["id"], answers=["1"])
    r2 = post(client, ka, body="r2 words", status="reply", reply_to=r1["id"])
    r3 = post(client, kb, body="r3 words", status="reply", reply_to=r2["id"])
    branch = post(client, kb, body="branch words", status="reply",
                  reply_to=root["id"])
    outside = post(client, ka, body="outside words")
    return root, r1, r2, r3, branch, outside


# -- authority -----------------------------------------------------------------

def test_operator_retracts_a_mixed_author_thread_whole():
    client = make_client()
    ka, kb, kop = room(client)
    root, r1, r2, r3, branch, outside = mixed_thread(client, ka, kb)

    r = client.post(f"/channels/room/messages/{root['id']}/retract_thread",
                    headers=kop)
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["count"] == 5          # root + r1 + r2 + r3 + branch
    assert report["root"] == root["id"]

    served = rows(client, kb)
    for msg in (root, r1, r2, r3, branch):
        assert served[msg["id"]]["retracted"] is True
        assert served[msg["id"]]["body"] == "[retracted by op]"
        assert served[msg["id"]]["title"] == ""
    # An unrelated message is untouched — the blast radius is the trail.
    assert served[outside["id"]]["retracted"] is False
    assert served[outside["id"]]["body"] == "outside words"


def test_non_operator_is_refused_and_nothing_is_retracted():
    """Partial application would leave exactly the noise the caller asked to
    be rid of while reporting success. Refuse, and say what blocked it."""
    client = make_client()
    ka, kb, _ = room(client)
    root, r1, r2, r3, branch, _ = mixed_thread(client, ka, kb)

    r = client.post(f"/channels/room/messages/{root['id']}/retract_thread",
                    headers=ka)
    assert r.status_code == 403
    assert "bob" in r.text and "NOTHING" in r.text

    served = rows(client, ka)
    for msg in (root, r1, r2, r3, branch):
        assert served[msg["id"]]["retracted"] is False, "a partial retraction happened"


def test_a_thread_that_is_entirely_yours_needs_no_operator():
    client = make_client()
    ka, kb, _ = room(client)
    root = post(client, ka, body="mine root")
    mine = post(client, ka, body="mine reply", status="reply",
                reply_to=root["id"])
    r = client.post(f"/channels/room/messages/{root['id']}/retract_thread",
                    headers=ka)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2
    served = rows(client, kb)
    assert served[mine["id"]]["retracted"] is True


def test_membership_is_still_required():
    client = make_client()
    ka, _, _ = room(client)
    eve = register(client, "eve", operator=True)
    root = post(client, ka, body="not for eve")
    r = client.post(f"/channels/room/messages/{root['id']}/retract_thread",
                    headers=eve)
    assert r.status_code == 403


# -- scope ---------------------------------------------------------------------

def test_scope_is_descendants_never_ancestors():
    """Called on a mid-thread reply, only that branch goes: a caller can only
    blast what they pointed at."""
    client = make_client()
    ka, kb, kop = room(client)
    root, r1, r2, r3, branch, _ = mixed_thread(client, ka, kb)

    r = client.post(f"/channels/room/messages/{r1['id']}/retract_thread",
                    headers=kop)
    assert r.json()["count"] == 3        # r1 + r2 + r3
    served = rows(client, kb)
    assert served[r1["id"]]["retracted"] is True
    assert served[r2["id"]]["retracted"] is True
    assert served[r3["id"]]["retracted"] is True
    assert served[root["id"]]["retracted"] is False   # ancestor survives
    assert served[branch["id"]]["retracted"] is False  # sibling branch survives


def test_trail_is_walked_from_the_db_not_a_client_window():
    """The root sits far above any page a client would have loaded; the hub
    still finds every reply. This is the case a client-side loop gets wrong."""
    client = make_client()
    ka, kb, kop = room(client)
    root = post(client, ka, body="ancient root")
    for i in range(20):
        post(client, kb if i % 2 else kop, body=f"noise {i}")
    tail = post(client, kb, body="late reply", status="reply",
                reply_to=root["id"])

    page = client.get("/channels/room/messages",
                      params={"since": tail["seq"] - 5}, headers=kop).json()
    assert not any(m["id"] == root["id"] for m in page), "root must be off-window"

    r = client.post(f"/channels/room/messages/{root['id']}/retract_thread",
                    headers=kop)
    assert r.json()["count"] == 2
    served = rows(client, kb)
    assert served[root["id"]]["retracted"] is True
    assert served[tail["id"]]["retracted"] is True


def test_system_rows_in_the_trail_are_skipped_not_fatal():
    """A member cannot retract a system/fs row; one join notice must not veto
    a whole thread retraction.

    The OPERATOR can (operator ruling, 2026-08-22): a system row is authored
    by `hub`, so author-retraction never reaches it, and without an operator
    door a hub notice posted into someone's room was unremovable by anyone,
    forever."""
    client = make_client()
    ka, kb, kop = room(client)
    root = post(client, ka, body="root")
    reply = post(client, kb, body="reply", status="reply", reply_to=root["id"])
    # Any system/fs row in the channel (joins produce them).
    history = client.get("/channels/room/messages", headers=kop).json()
    system = next(m for m in history if m["kind"] != "message")
    assert client.post(f"/channels/room/messages/{system['id']}/retract",
                       headers=ka).status_code == 403      # a member: no
    assert client.post(f"/channels/room/messages/{system['id']}/retract",
                       headers=kop).status_code == 200     # the operator: yes

    r = client.post(f"/channels/room/messages/{root['id']}/retract_thread",
                    headers=kop)
    assert r.status_code == 200 and r.json()["count"] == 2
    assert rows(client, kb)[reply["id"]]["retracted"] is True


# -- idempotence, obligations, ledger ------------------------------------------

def test_already_retracted_member_is_counted_not_re_stamped():
    client = make_client()
    ka, kb, kop = room(client)
    root = post(client, ka, body="root")
    reply = post(client, kb, body="reply", status="reply", reply_to=root["id"])
    client.post(f"/channels/room/messages/{reply['id']}/retract", headers=kb)

    r = client.post(f"/channels/room/messages/{root['id']}/retract_thread",
                    headers=kop)
    assert r.status_code == 200
    assert r.json()["already_retracted"] == [reply["id"]]
    # First retractor's identity sticks: bob retracted his own, not "op".
    served = rows(client, ka)
    assert served[reply["id"]]["body"] == "[retracted by bob]"
    assert served[root["id"]]["body"] == "[retracted by op]"


def test_thread_retraction_is_idempotent():
    client = make_client()
    ka, kb, kop = room(client)
    root = post(client, ka, body="root")
    post(client, kb, body="reply", status="reply", reply_to=root["id"])
    first = client.post(f"/channels/room/messages/{root['id']}/retract_thread",
                        headers=kop)
    second = client.post(f"/channels/room/messages/{root['id']}/retract_thread",
                         headers=kop)
    assert first.status_code == second.status_code == 200
    assert second.json()["count"] == 2
    assert len(second.json()["already_retracted"]) == 2


def test_thread_retraction_clears_every_obligation_in_the_trail():
    client = make_client()
    ka, kb, kop = room(client)
    root = post(client, ka, body="root", status="open",
                asks=[{"id": "1", "text": "answer me", "to": ["bob"]}])
    nested = post(client, ka, body="nested", status="open", reply_to=root["id"],
                  asks=[{"id": "2", "text": "and this", "to": ["bob"]}])
    owed = client.get("/owed", headers=kb).json()
    assert {root["id"], nested["id"]} <= {o["id"] for o in owed["to_answer"]}

    client.post(f"/channels/room/messages/{root['id']}/retract_thread",
                headers=kop)
    owed = client.get("/owed", headers=kb).json()
    assert not ({root["id"], nested["id"]}
                & {o["id"] for o in owed["to_answer"] + owed["to_consume"]
                   + owed["to_close"]})
    inbox = client.get("/inbox", headers=kb).json()
    assert not any(e["id"] in (root["id"], nested["id"])
                   and e["status"] in ("open", "blocked") for e in inbox)


def test_thread_retraction_leaves_the_ledger_verifiable():
    client = make_client()
    ka, kb, kop = room(client)
    root = post(client, ka, body="root words")
    post(client, kb, body="reply words", status="reply", reply_to=root["id"])
    client.post(f"/channels/room/messages/{root['id']}/retract_thread",
                headers=kop)
    led = client.get("/channels/room/ledger", headers=kop).json()
    assert led["verified"] is True and led["broken_at"] is None
    blob = str(led)
    assert "root words" not in blob and "reply words" not in blob


def test_missing_root_404s():
    client = make_client()
    ka, _, kop = room(client)
    r = client.post("/channels/room/messages/nope/retract_thread", headers=kop)
    assert r.status_code == 404
