"""Operator pause / stand-down (0069) and the decision board (0070).

Pause invariant under test: the shared world freezes for non-operators;
private state stays live; nothing ages toward its SLA while frozen.
Board invariant: every column is derived from the same settlement truth
the inbox uses — the board can never disagree with reality.
"""

import time

from fastapi.testclient import TestClient

from agora.hub.app import create_app

ADMIN_KEY = "test-admin"
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}


def make_client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0, dark_watch_seconds=0)
    return TestClient(app)


def register(client: TestClient, agent_id: str, operator: bool = False) -> dict[str, str]:
    r = client.post("/agents", json={"id": agent_id, "mission": f"seat {agent_id}", "operator": operator},
                    headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_channel(client: TestClient, owner: dict, name: str, *members: dict) -> None:
    client.post("/channels", json={"name": name}, headers=owner)
    for member in members:
        invite = client.post(f"/channels/{name}/invites", json={},
                             headers=owner).json()["invite_token"]
        client.post(f"/channels/{name}/join", json={"invite_token": invite},
                    headers=member)


def grant_reporting(client: TestClient, agent_id: str) -> None:
    """queue:* writes are gated on the reporting power since 0068."""
    r = client.put("/admin/delegation",
                   json={"agent_id": agent_id, "powers": ["reporting"]},
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    client.app.state.service._delegations_cache_at = 0.0


def pause(client: TestClient, reason: str = "") -> dict:
    r = client.put("/admin/pause", json={"reason": reason}, headers=ADMIN)
    assert r.status_code == 200, r.text
    client.app.state.service._bust_pause_cache()
    return r.json()


def resume(client: TestClient) -> None:
    assert client.delete("/admin/pause", headers=ADMIN).status_code == 200
    client.app.state.service._bust_pause_cache()


# -- 0069: pause ------------------------------------------------------------------

def test_pause_freezes_shared_world_but_not_private_state():
    client = make_client()
    op = register(client, "op", operator=True)
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob, op)
    pause(client, reason="catching up")

    # Writes refused with the stand-down 423.
    blocked = client.post("/channels/room/messages", json={"body": "x"}, headers=alice)
    assert blocked.status_code == 423 and "stand down" in blocked.json()["detail"]
    assert client.put("/channels/room/store/notes", json={"value": 1},
                      headers=alice).status_code == 423
    assert client.put("/channels/room/fs/doc.md", json={"content": "x"},
                      headers=alice).status_code == 423
    assert client.post("/channels", json={"name": "new-room"},
                       headers=alice).status_code == 423
    assert client.post("/dms/bob/messages", json={"body": "psst"},
                       headers=alice).status_code == 423

    # Reads, acks, receipts, presence stay open.
    assert client.get("/channels/room/messages", headers=alice).status_code == 200
    assert client.get("/inbox", headers=alice).status_code == 200
    assert client.post("/inbox/ack", json={"cursors": {"room": 1}},
                       headers=alice).status_code == 200
    assert client.get("/channels/room/digest", headers=alice).status_code == 200

    # Operator exceptions: operator posts; operator DMs both directions.
    assert client.post("/channels/room/messages", json={"body": "op speaks"},
                       headers=op).status_code == 200
    assert client.post("/dms/alice/messages", json={"body": "from op"},
                       headers=op).status_code == 200
    assert client.post("/dms/op/messages", json={"body": "to op"},
                       headers=alice).status_code == 200

    resume(client)
    assert client.post("/channels/room/messages", json={"body": "back"},
                       headers=alice).status_code == 200


def test_pause_is_visible_broadcast_and_idempotent():
    client = make_client()
    alice = register(client, "alice")
    make_channel(client, alice, "room")

    state1 = pause(client, reason="board review")
    state2 = pause(client)                      # idempotent: same pause
    assert state1["since"] == state2["since"]

    me = client.get("/whoami", headers=alice).json()
    assert me["hub_state"]["state"] == "paused"
    assert me["hub_state"]["reason"] == "board review"
    assert client.get("/healthz").json()["paused"] is True

    msgs = client.get("/channels/room/messages", headers=alice).json()
    assert any("HUB PAUSED" in m["body"] for m in msgs if m["kind"] == "system")
    resume(client)
    msgs = client.get("/channels/room/messages", headers=alice).json()
    assert any("HUB RESUMED" in m["body"] for m in msgs if m["kind"] == "system")
    assert client.get("/whoami", headers=alice).json()["hub_state"]["state"] == "open"


def test_pause_freezes_the_escalation_clock():
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)
    # SLA 0.005 min = 0.3s. The message spends its whole life paused: it must
    # NOT escalate, because paused time does not count toward the SLA.
    client.put("/channels/room/store/channel:meta",
               json={"value": {"response_sla_minutes": 0.005}}, headers=alice)
    q = client.post("/channels/room/messages",
                    json={"body": "q", "status": "open", "title": "q"},
                    headers=alice).json()
    pause(client)
    time.sleep(0.6)  # > SLA, all of it paused
    env = next(e for e in client.get("/inbox", headers=bob).json()
               if e["seq"] == q["seq"])
    assert env["escalated"] is False            # frozen clock
    resume(client)
    time.sleep(0.6)                             # now unpaused time passes
    env = next(e for e in client.get("/inbox", headers=bob).json()
               if e["seq"] == q["seq"])
    assert env["escalated"] is True             # clock resumed honestly


def test_pause_requires_admin_key():
    client = make_client()
    op = register(client, "op", operator=True)
    assert client.put("/admin/pause", json={}, headers=op).status_code == 403


# -- 0070: the decision board --------------------------------------------------

def test_board_derives_all_columns():
    client = make_client()
    flow, laurent, worker = (register(client, "flow"),
                             register(client, "laurent"),
                             register(client, "worker"))
    make_channel(client, flow, "room", laurent, worker)

    # pending on laurent: addressed ask.
    q = client.post("/channels/room/messages", headers=flow,
                    json={"body": "decide", "title": "ruling needed",
                          "status": "open", "to": ["laurent"],
                          "asks": [{"id": "1", "text": "A or B?"}]}).json()
    # proposal: unaddressed open question by someone else.
    client.post("/channels/room/messages", headers=worker,
                json={"body": "idea", "title": "proposal x", "status": "open",
                      "asks": [{"id": "1", "text": "anyone?"}]})
    # in progress + pending review + done.
    client.put("/channels/room/store/claim:build-x",
               json={"value": {"owner": "worker"}}, headers=worker)
    client.put("/channels/room/store/claim:ship-y",
               json={"value": {"done": True, "review": "operator"}}, headers=worker)
    client.put("/channels/room/store/decision:old-z",
               json={"value": {"summary": "done long ago"}}, headers=flow)
    # curated queue row for laurent (flow needs the reporting power, 0068).
    grant_reporting(client, "flow")
    client.put("/channels/room/store/queue:laurent:pick-a-or-b",
               json={"value": {"q": "pick A or B for x", "options": ["A", "B"],
                               "evidence": ["room#1"], "tier": "operator"}},
               headers=flow)

    b = client.get("/board", headers=laurent).json()
    assert b["viewer"] == "laurent"
    assert [r["seq"] for r in b["pending_on_me"]] == [q["seq"]]
    assert b["queue"][0]["q"] == "pick A or B for x"
    assert any(p["q"] == "proposal x" for p in b["proposals"])
    assert any(i["task"] == "build-x" for i in b["in_progress"])
    assert any(p["task"] == "ship-y" and p["review"] == "operator"
               for p in b["pending_review"])
    assert any(d["key"] == "decision:old-z" for d in b["done"])

    # Settlement truth is shared: closing the ask clears the board too.
    client.post("/channels/room/messages", headers=flow,
                json={"body": "decided elsewhere", "status": "resolved",
                      "reply_to": q["id"]})
    b = client.get("/board", headers=laurent).json()
    assert b["counts"]["pending_on_me"] == 0
    # A decision key with the claim's slug clears pending-review.
    client.put("/channels/room/store/decision:ship-y",
               json={"value": {"summary": "reviewed"}}, headers=laurent)
    b = client.get("/board", headers=laurent).json()
    assert b["counts"]["pending_review"] == 0


def test_board_pending_via_ask_assignee_and_viewer_scoping():
    client = make_client()
    flow, laurent = register(client, "flow"), register(client, "laurent")
    make_channel(client, flow, "room", laurent)
    client.post("/channels/room/messages", headers=flow,
                json={"body": "for laurent via assignee", "title": "assigned",
                      "status": "open",
                      "asks": [{"id": "1", "text": "sign?", "assignee": "laurent"}]})
    assert client.get("/board", headers=laurent).json()["counts"]["pending_on_me"] == 1
    # flow's own board: its own question is neither pending-on-it nor a proposal.
    b = client.get("/board", headers=flow).json()
    assert b["counts"]["pending_on_me"] == 0
    assert all(p["q"] != "assigned" for p in b["proposals"])


def test_queue_rows_are_sanitized_and_default_capped():
    """Review HIGH-1: rows reach the operator's terminal — control chars
    are stripped at write time and `default` is typed and capped."""
    client = make_client()
    flow = register(client, "flow")
    make_channel(client, flow, "room")
    grant_reporting(client, "flow")
    ok = client.put("/channels/room/store/queue:laurent:x",
                    json={"value": {"q": "pick\x1b[31m one\nnow",
                                    "options": ["A\x07 loud"],
                                    "default": "delegate\x1b proceeds"}},
                    headers=flow)
    assert ok.status_code == 200
    stored = client.get("/channels/room/store/queue:laurent:x",
                        headers=flow).json()["value"]
    assert "\x1b" not in stored["q"] and "\n" not in stored["q"]
    assert "\x07" not in stored["options"][0]
    assert "\x1b" not in stored["default"]
    bad = client.put("/channels/room/store/queue:laurent:x",
                     json={"value": {"q": "ok", "default": "y" * 300}},
                     headers=flow)
    assert bad.status_code == 400
    bad = client.put("/channels/room/store/queue:laurent:x",
                     json={"value": {"q": "ok", "since": "yesterday"}},
                     headers=flow)
    assert bad.status_code == 400


def test_escalation_counts_only_live_time_across_a_partial_pause():
    """Review test 2: a message created BEFORE the pause ages only by its
    live (unpaused) time — it escalates after SLA of live time, not sooner."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)
    client.put("/channels/room/store/channel:meta",
               json={"value": {"response_sla_minutes": 0.02}}, headers=alice)  # 1.2s
    q = client.post("/channels/room/messages",
                    json={"body": "q", "status": "open", "title": "q"},
                    headers=alice).json()
    time.sleep(0.3)                             # 0.3s live
    pause(client)
    time.sleep(1.5)                             # 1.5s paused (> SLA, all frozen)
    resume(client)
    env = next(e for e in client.get("/inbox", headers=bob).json()
               if e["seq"] == q["seq"])
    assert env["escalated"] is False            # live age ~0.3s < 1.2s SLA
    time.sleep(1.2)                             # push live age past the SLA
    env = next(e for e in client.get("/inbox", headers=bob).json()
               if e["seq"] == q["seq"])
    assert env["escalated"] is True


def test_leave_and_join_token_refused_during_pause():
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)
    token = client.post("/join-tokens", json={"agent_id": "late"},
                        headers=ADMIN).json()["token"]
    pause(client)
    assert client.post("/channels/room/leave", headers=bob).status_code == 423
    redeemed = client.post("/join", json={"token": token})
    assert redeemed.status_code == 423
    resume(client)
    assert client.post("/channels/room/leave", headers=bob).status_code == 200


def test_open_dm_question_is_pending_on_peer_not_proposal():
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    client.post("/dms/bob/messages",
                json={"body": "raw open dm", "title": "dm q", "status": "open"},
                headers=alice)
    b = client.get("/board", headers=bob).json()
    assert any(r["q"] == "dm q" for r in b["pending_on_me"])
    assert all(r["q"] != "dm q" for r in b["proposals"])


def test_queue_rows_are_schema_capped():
    client = make_client()
    flow = register(client, "flow")
    make_channel(client, flow, "room")
    grant_reporting(client, "flow")
    bad = client.put("/channels/room/store/queue:laurent:x",
                     json={"value": {"q": "y" * 200}}, headers=flow)
    assert bad.status_code == 400
    bad = client.put("/channels/room/store/queue:laurent:x",
                     json={"value": {"q": "ok", "surprise": 1}}, headers=flow)
    assert bad.status_code == 400
    bad = client.put("/channels/room/store/queue:laurent:x",
                     json={"value": {"q": "ok", "tier": "urgent!!"}}, headers=flow)
    assert bad.status_code == 400
    ok = client.put("/channels/room/store/queue:laurent:x",
                    json={"value": {"q": "ok", "tier": "delegate"}}, headers=flow)
    assert ok.status_code == 200
