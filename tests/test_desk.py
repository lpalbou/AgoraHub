"""Operator desk (0111, M1+M3 from the c3860 staleness review).

What must hold: the desk is DERIVED at read time (state, not log) — an ask
addressed to the operator appears with age and disappears when engaged;
queue rows carrying a machine-checkable `done_when` predicate self-clear
into `satisfied` the instant the hub observes the act (the trigger
incident: 'WAITING ON YOU: agency retirement' six hours after the
retirement — impossible here); the vocabulary is validated at write time
with a teaching refusal; the surface is operator/reporting-delegate only.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agora.hub.app import create_app

ADMIN_KEY = "test-admin"
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}


def make_client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0, dark_watch_seconds=0)
    return TestClient(app)


def register(client, agent_id, operator=False):
    r = client.post("/agents", json={"id": agent_id, "mission": f"seat {agent_id}", "operator": operator},
                    headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_channel(client, owner, name, *members):
    client.post("/channels", json={"name": name, "private": False}, headers=owner)
    for m in members:
        client.post(f"/channels/{name}/join", json={}, headers=m)


def test_desk_is_gated_to_operators_and_reporting_delegates():
    client = make_client()
    op = register(client, "op", operator=True)
    plain = register(client, "flow")
    assert client.get("/desk", headers=plain).status_code == 403
    assert client.get("/desk", headers=op).status_code == 200
    # A reporting delegate stewards the desk (composes the digest FROM it).
    client.put("/admin/delegation", headers=ADMIN,
               json={"agent_id": "flow", "powers": ["reporting"]})
    assert client.get("/desk", headers=plain).status_code == 200


def test_desk_derives_asks_waiting_on_the_operator_and_clears_on_engagement():
    client = make_client()
    op = register(client, "op", operator=True)
    flow = register(client, "flow")
    make_channel(client, flow, "room", op)
    q = client.post("/channels/room/messages", headers=flow,
                    json={"title": "need your ruling", "body": "pick A or B",
                          "status": "open", "to": ["op"],
                          "asks": [{"id": "1", "text": "A or B?", "to": ["op"]}]}).json()
    desk = client.get("/desk", headers=op).json()
    asks = [r for r in desk["rows"] if r["kind"] == "ask"]
    assert len(asks) == 1
    assert asks[0]["what"] == "need your ruling"
    assert asks[0]["who_waits"] == "flow"
    assert "computed_at" in desk
    # The operator engages: the row is GONE at the next read — derived, not
    # carried.
    client.post("/channels/room/messages", headers=op,
                json={"body": "A", "status": "reply", "reply_to": q["id"],
                      "answers": ["1"]})
    desk = client.get("/desk", headers=op).json()
    assert not [r for r in desk["rows"] if r["kind"] == "ask"]


def test_desk_ask_label_falls_back_to_ask_text_when_titleless():
    """DM asks routinely omit a title; the desk must never show a bare
    '(untitled)' on the surface the operator reads first — fall back to the
    pending ask's text (c3866: laurent's own ask arrived titleless)."""
    client = make_client()
    op = register(client, "op", operator=True)
    flow = register(client, "flow")
    make_channel(client, flow, "room", op)
    client.post("/channels/room/messages", headers=flow,
                json={"body": "b", "status": "open", "to": ["op"],
                      "asks": [{"id": "1", "text": "ship it or hold?",
                                "to": ["op"]}]})
    desk = client.get("/desk", headers=op).json()
    labels = [r["what"] for r in desk["rows"] if r["kind"] == "ask"]
    assert "ship it or hold?" in labels
    assert "(untitled ask)" not in labels


def test_done_when_vocabulary_validated_at_write_time():
    client = make_client()
    op = register(client, "op", operator=True)
    make_channel(client, op, "room")
    # Unknown kind: teaching refusal.
    r = client.put("/channels/room/store/queue:op:x", headers=op,
                   json={"value": {"q": "wait on a pypi click",
                                   "done_when": {"kind": "pypi_click"}},
                         "expect_version": 0})
    assert r.status_code == 400 and "cannot observe" in r.text
    # Missing field: named.
    r = client.put("/channels/room/store/queue:op:x", headers=op,
                   json={"value": {"q": "retire bob",
                                   "done_when": {"kind": "retired"}},
                         "expect_version": 0})
    assert r.status_code == 400 and "missing" in r.text
    # Valid predicate passes.
    r = client.put("/channels/room/store/queue:op:x", headers=op,
                   json={"value": {"q": "retire bob",
                                   "done_when": {"kind": "retired",
                                                 "agent": "bob"}},
                         "expect_version": 0})
    assert r.status_code == 200, r.text


def test_desk_row_self_clears_when_the_act_happens():
    """The trigger incident, made impossible: a 'waiting on you: retire X'
    row moves to `satisfied` the INSTANT the retirement lands — it can never
    be reported as pending six hours later."""
    client = make_client()
    op = register(client, "op", operator=True)
    register(client, "bob")
    make_channel(client, op, "room")
    client.put("/channels/room/store/queue:op:retire-bob", headers=op,
               json={"value": {"q": "retire bob (decommissioned)",
                               "waiting": ["framework"],
                               "done_when": {"kind": "retired", "agent": "bob"}},
                     "expect_version": 0})
    desk = client.get("/desk", headers=op).json()
    assert any(r["key"] == "queue:op:retire-bob" for r in desk["rows"])
    assert not desk["satisfied"]
    # The operator retires bob (admin key path, 0.12.23).
    assert client.post("/agents/bob/retire", headers=ADMIN).status_code == 200
    desk = client.get("/desk", headers=op).json()
    assert not any(r.get("key") == "queue:op:retire-bob" for r in desk["rows"])
    sat = [r for r in desk["satisfied"] if r["key"] == "queue:op:retire-bob"]
    assert sat and "wait is over" in sat[0]["one_action"]


def test_done_when_decision_and_work_status_predicates():
    client = make_client()
    op = register(client, "op", operator=True)
    make_channel(client, op, "room")
    client.put("/channels/room/store/queue:op:d", headers=op,
               json={"value": {"q": "decide the naming",
                               "done_when": {"kind": "decision",
                                             "channel": "room",
                                             "slug": "naming"}},
                     "expect_version": 0})
    client.put("/channels/room/store/queue:op:w", headers=op,
               json={"value": {"q": "land agora-0001",
                               "done_when": {"kind": "work_status",
                                             "channel": "room",
                                             "item": "agora-0001",
                                             "status": "completed"}},
                     "expect_version": 0})
    desk = client.get("/desk", headers=op).json()
    assert len(desk["rows"]) == 2 and not desk["satisfied"]
    # Record the decision; move the work row to completed.
    client.put("/channels/room/store/decision:naming", headers=op,
               json={"value": {"summary": "ruled"}, "expect_version": 0})
    client.put("/channels/room/store/work:agora-0001", headers=op,
               json={"value": {"title": "x", "status": "completed",
                               "owner": "op", "card": "c.md"},
                     "expect_version": 0})
    desk = client.get("/desk", headers=op).json()
    assert not desk["rows"] and len(desk["satisfied"]) == 2
