"""End-to-end tests through the HTTP API and WebSocket (in-process TestClient)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agora.hub.app import create_app

ADMIN_KEY = "test-admin-key"


@pytest.fixture()
def client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY, rate_per_minute=600.0)
    return TestClient(app)


def register(client: TestClient, agent_id: str) -> dict:
    response = client.post(
        "/agents",
        json={"id": agent_id, "name": agent_id.title()},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['api_key']}"}


def test_registration_requires_admin_key(client):
    response = client.post("/agents", json={"id": "eve"},
                           headers={"Authorization": "Bearer not-admin"})
    assert response.status_code == 403


def test_rest_roundtrip(client):
    alice = register(client, "alice")
    bob = register(client, "bob")

    assert client.post("/channels", json={"name": "design"}, headers=alice).status_code == 200
    invite = client.post("/channels/design/invites", json={"agent_id": "bob"},
                         headers=alice).json()["invite_token"]
    assert client.post("/channels/design/join", json={"invite_token": invite},
                       headers=bob).status_code == 200

    posted = client.post("/channels/design/messages",
                         json={"body": "seam proposal", "title": "seam v1", "status": "open"},
                         headers=alice).json()
    assert posted["seq"] > 0

    # Bob's inbox surfaces it. An `open` message is an obligation: acking the
    # triage cursor does NOT bury it (v0.3 C-4); it clears when read/answered.
    inbox = client.get("/inbox", headers=bob).json()
    bodies = [m["body"] for m in inbox if m["kind"] == "message"]
    assert "seam proposal" in bodies
    top = max(m["seq"] for m in inbox)
    client.post("/inbox/ack", json={"cursors": {"design": top}}, headers=bob)
    assert any(m["id"] == posted["id"] for m in client.get("/inbox", headers=bob).json())
    # Reading it (deliberate) clears the obligation.
    client.get(f"/channels/design/messages/{posted['id']}", headers=bob)
    assert client.get("/inbox", headers=bob).json() == []

    # Channel store with CAS over HTTP.
    put = client.put("/channels/design/store/contract",
                     json={"value": {"v": 1}, "expect_version": 0}, headers=bob)
    assert put.json()["version"] == 1
    conflict = client.put("/channels/design/store/contract",
                          json={"value": {"v": 9}, "expect_version": 0}, headers=alice)
    assert conflict.status_code == 409

    # Outsider is rejected everywhere.
    eve = register(client, "eve")
    assert client.get("/channels/design/messages", headers=eve).status_code == 403
    assert client.put("/channels/design/store/contract", json={"value": 1},
                      headers=eve).status_code == 403


def test_websocket_fanout_and_backlog(client):
    alice = register(client, "alice")
    bob = register(client, "bob")
    client.post("/channels", json={"name": "design"}, headers=alice)
    invite = client.post("/channels/design/invites", json={},
                         headers=alice).json()["invite_token"]
    client.post("/channels/design/join", json={"invite_token": invite}, headers=bob)

    bob_key = bob["Authorization"].removeprefix("Bearer ")
    with client.websocket_connect(f"/ws?token={bob_key}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["design"], "since": {"design": 0}})
        assert ws.receive_json()["type"] == "subscribed"
        # Backlog: system join messages exist already; delivered as envelopes.
        backlog = ws.receive_json()
        assert backlog["type"] == "envelope"

        # Live fan-out: Alice posts over REST; Bob receives an envelope over WS
        # (small body -> inlined per the attention policy).
        client.post("/channels/design/messages",
                    json={"body": "live one", "urgency": "next_turn"}, headers=alice)
        frame = ws.receive_json()
        while frame["type"] == "envelope" and frame["envelope"]["body"] != "live one":
            frame = ws.receive_json()
        assert frame["envelope"]["body"] == "live one"
        assert frame["envelope"]["urgency"] == "next_turn"

        # Posting over WS works and returns confirmation.
        ws.send_json({"type": "post", "channel": "design", "body": "roger",
                      "status": "reply"})
        frame = ws.receive_json()
        while frame["type"] == "envelope":
            frame = ws.receive_json()
        assert frame["type"] == "posted"


def test_c3_cross_thread_push_and_longpoll(client):
    """C-3: posts arrive via a threadpool (sync REST handler) while the WS pump
    and long-poll waiter live on the event loop. The wake must cross that
    thread boundary. Exercises the real TestClient threading model."""
    alice = register(client, "alice")
    bob = register(client, "bob")
    client.post("/channels", json={"name": "design"}, headers=alice)
    invite = client.post("/channels/design/invites", json={},
                         headers=alice).json()["invite_token"]
    client.post("/channels/design/join", json={"invite_token": invite}, headers=bob)

    bob_key = bob["Authorization"].removeprefix("Bearer ")
    with client.websocket_connect(f"/ws?token={bob_key}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["design"]})
        assert ws.receive_json()["type"] == "subscribed"
        # Post from the REST handler (a worker thread); the WS pump on the loop
        # must still receive it (v0.3 would stall/crash on the thread boundary).
        client.post("/channels/design/messages",
                    json={"body": "cross-thread ping", "urgency": "next_turn"},
                    headers=alice)
        frame = ws.receive_json()
        while frame["type"] == "envelope" and frame["envelope"]["body"] != "cross-thread ping":
            frame = ws.receive_json()
        assert frame["envelope"]["body"] == "cross-thread ping"


def test_websocket_rejects_bad_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?token=bogus") as ws:
            ws.receive_json()


def test_websocket_subscribe_requires_membership(client):
    eve = register(client, "eve")
    alice = register(client, "alice")
    client.post("/channels", json={"name": "design"}, headers=alice)
    eve_key = eve["Authorization"].removeprefix("Bearer ")
    with client.websocket_connect(f"/ws?token={eve_key}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["design"]})
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["status"] == 403


def test_healthz(client):
    """A supervisor/proxy probing a remote hub needs a real health endpoint
    (the root `/` was the only introspection before)."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_websocket_auth_via_header(client):
    """The WS credential can travel in the Authorization header instead of the
    query string, so bearer keys don't leak into proxy/access logs on remote
    links. (The client now connects this way.)"""
    alice = register(client, "alice")
    bob = register(client, "bob")
    client.post("/channels", json={"name": "design"}, headers=alice)
    invite = client.post("/channels/design/invites", json={},
                         headers=alice).json()["invite_token"]
    client.post("/channels/design/join", json={"invite_token": invite}, headers=bob)
    with client.websocket_connect("/ws", headers={"Authorization": bob["Authorization"]}) as ws:
        ws.send_json({"type": "subscribe", "channels": ["design"]})
        assert ws.receive_json()["type"] == "subscribed"


def test_dm_created_after_connect_is_pushed_live(client):
    """A DM (or any membership) that comes into existence AFTER an agent's
    watcher connected must still be pushed to that live connection. This was
    the reaction-test failure in the field: observer's watcher subscribed to
    the channels of 15:34, the orchestrator's DM was born at 15:44, and the
    push had nowhere to go until a watcher restart."""
    alice = register(client, "alice")
    bob = register(client, "bob")
    client.post("/channels", json={"name": "design"}, headers=alice)
    invite = client.post("/channels/design/invites", json={},
                         headers=alice).json()["invite_token"]
    client.post("/channels/design/join", json={"invite_token": invite}, headers=bob)

    bob_key = bob["Authorization"].removeprefix("Bearer ")
    with client.websocket_connect(f"/ws?token={bob_key}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["design"]})
        assert ws.receive_json()["type"] == "subscribed"

        # Alice opens a DM to Bob — a channel that did not exist at connect
        # time — and the message must still reach Bob's live socket.
        client.post("/dms/bob/messages",
                    json={"body": "psst", "title": "new dm"}, headers=alice)
        frame = ws.receive_json()
        while not (frame["type"] == "envelope"
                   and frame["envelope"]["channel"].startswith("dm:")
                   and frame["envelope"]["sender"] == "alice"):
            frame = ws.receive_json()
        assert frame["envelope"]["title"] == "new dm"


def test_exactly_one_envelope_frame_per_message(client):
    """The same connection queue is registered under both the channel key and
    the agent/<id> key, so without pump-side dedup every message would be sent
    twice on the wire (audit M1)."""
    alice = register(client, "alice")
    bob = register(client, "bob")
    client.post("/channels", json={"name": "design"}, headers=alice)
    invite = client.post("/channels/design/invites", json={},
                         headers=alice).json()["invite_token"]
    client.post("/channels/design/join", json={"invite_token": invite}, headers=bob)

    bob_key = bob["Authorization"].removeprefix("Bearer ")
    with client.websocket_connect(f"/ws?token={bob_key}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["design"]})
        assert ws.receive_json()["type"] == "subscribed"
        client.post("/channels/design/messages",
                    json={"body": "once only"}, headers=alice)
        # Drain until the pong fence; count frames carrying our message.
        ws.send_json({"type": "ping"})
        copies = 0
        frame = ws.receive_json()
        while frame["type"] != "pong":
            if frame["type"] == "envelope" and frame["envelope"].get("body") == "once only":
                copies += 1
            frame = ws.receive_json()
        assert copies == 1


def test_no_live_push_after_leaving_channel(client):
    """Membership is the isolation boundary at DELIVERY time too: an agent
    that left a channel must stop receiving its live pushes on an already-
    open socket (audit H2)."""
    alice = register(client, "alice")
    bob = register(client, "bob")
    client.post("/channels", json={"name": "design"}, headers=alice)
    invite = client.post("/channels/design/invites", json={},
                         headers=alice).json()["invite_token"]
    client.post("/channels/design/join", json={"invite_token": invite}, headers=bob)

    bob_key = bob["Authorization"].removeprefix("Bearer ")
    with client.websocket_connect(f"/ws?token={bob_key}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["design"]})
        assert ws.receive_json()["type"] == "subscribed"
        client.post("/channels/design/leave", headers=bob)
        client.post("/channels/design/messages",
                    json={"body": "secret after leave"}, headers=alice)
        ws.send_json({"type": "ping"})
        frame = ws.receive_json()
        while frame["type"] != "pong":
            if frame["type"] == "envelope":
                assert frame["envelope"].get("body") != "secret after leave"
                assert frame["envelope"]["channel"] != "design" or \
                    frame["envelope"]["sender"] == "hub"  # pre-leave system msgs ok
            frame = ws.receive_json()


def test_hub_writes_notify_files_without_any_watcher(tmp_path):
    """Liveness without resident processes: the hub itself appends one JSON
    line per delivered message to <notify_dir>/<agent>-inbox.log for every
    member except the sender. No watcher, no supervisor, no OS service."""
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0, notify_dir=str(tmp_path))
    client = TestClient(app)
    alice = register(client, "alice")
    bob = register(client, "bob")
    client.post("/channels", json={"name": "design"}, headers=alice)
    invite = client.post("/channels/design/invites", json={},
                         headers=alice).json()["invite_token"]
    client.post("/channels/design/join", json={"invite_token": invite}, headers=bob)

    client.post("/channels/design/messages",
                json={"body": "hello file", "title": "hi", "status": "open",
                      "to": ["bob"]},
                headers=alice)

    import json as _json
    bob_lines = [_json.loads(line) for line in
                 (tmp_path / "bob-inbox.log").read_text().splitlines()]
    posted = [l for l in bob_lines if l.get("title") == "hi"]
    assert len(posted) == 1                      # exactly once, no duplicates
    assert posted[0]["channel"] == "design"
    assert posted[0]["from"] == "alice"
    assert posted[0]["kind"] == "message"        # tailers filter fs/system noise
    assert "to-me" in posted[0]["flags"]          # viewer-specific envelope
    assert posted[0]["preview"] == "hello file"
    # The sender's own post is not delivered back to them.
    alice_log = tmp_path / "alice-inbox.log"
    if alice_log.exists():
        assert all(_json.loads(l).get("title") != "hi"
                   for l in alice_log.read_text().splitlines())


def test_channel_digest_folds_history_into_knowledge(client):
    """The digest is mechanical distillation: open+asks -> open_questions with
    pending ask texts; discharge/resolved -> decided; decision:* store keys ->
    the decision record. Membership-gated like everything else."""
    alice = register(client, "alice")
    bob = register(client, "bob")
    client.post("/channels", json={"name": "design"}, headers=alice)
    invite = client.post("/channels/design/invites", json={},
                         headers=alice).json()["invite_token"]
    client.post("/channels/design/join", json={"invite_token": invite}, headers=bob)

    # An open question with two asks; bob answers one -> still open, 1 pending.
    q = client.post("/channels/design/messages", json={
        "body": "two things", "title": "seam questions", "status": "open",
        "asks": [{"id": "1", "text": "which store?"}, {"id": "2", "text": "which key?"}],
    }, headers=alice).json()
    client.post("/channels/design/messages", json={
        "body": "store A", "status": "reply", "reply_to": q["id"], "answers": ["1"],
    }, headers=bob)
    # A resolved post + the decision:* norm.
    client.post("/channels/design/messages", json={
        "body": "shipping it", "title": "seam shipped", "status": "resolved",
    }, headers=bob)
    client.put("/channels/design/store/decision:seam",
               json={"value": {"summary": "store A wins"}}, headers=bob)

    digest = client.get("/channels/design/digest", headers=bob).json()
    assert digest["counts"] == {"open_questions": 1, "decided_shown": 1,
                                "decided_total": 1, "decisions": 1}
    [openq] = digest["open_questions"]
    assert openq["title"] == "seam questions"
    assert openq["pending_asks"] == [{"id": "2", "text": "which key?"}]
    [dec] = digest["decided"]
    assert dec["title"] == "seam shipped" and dec.get("resolved") is True
    [rec] = digest["decisions"]
    assert rec["key"] == "decision:seam" and rec["value"]["summary"] == "store A wins"

    # Bob answers ask 2 -> the question moves from open to decided.
    client.post("/channels/design/messages", json={
        "body": "key K", "status": "reply", "reply_to": q["id"], "answers": ["2"],
    }, headers=bob)
    digest = client.get("/channels/design/digest", headers=bob).json()
    assert digest["counts"]["open_questions"] == 0
    assert any(d.get("answered_by") == ["bob"] for d in digest["decided"])

    # A question the ASKER resolves herself must not sit in open_questions
    # forever (self-contradiction with her resolved post — review H2).
    q2 = client.post("/channels/design/messages", json={
        "body": "never mind?", "title": "self-solved", "status": "open",
    }, headers=alice).json()
    client.post("/channels/design/messages", json={
        "body": "figured it out", "status": "resolved", "reply_to": q2["id"],
    }, headers=alice)
    digest = client.get("/channels/design/digest", headers=bob).json()
    assert digest["counts"]["open_questions"] == 0
    assert any(d.get("self_resolved") for d in digest["decided"])

    # Outsiders get nothing.
    eve = register(client, "eve")
    assert client.get("/channels/design/digest", headers=eve).status_code == 403


def test_admin_status_overview_flags_dark_agents(client):
    """/admin/status is the dead-agent alarm: per agent, presence + unread +
    pending obligations, admin-key only."""
    alice = register(client, "alice")
    bob = register(client, "bob")
    client.post("/channels", json={"name": "design"}, headers=alice)
    invite = client.post("/channels/design/invites", json={},
                         headers=alice).json()["invite_token"]
    client.post("/channels/design/join", json={"invite_token": invite}, headers=bob)
    client.post("/channels/design/messages",
                json={"body": "need an answer", "status": "open"}, headers=alice)

    # Agent keys are rejected; the admin key is required.
    assert client.get("/admin/status", headers=alice).status_code == 403
    rows = client.get("/admin/status",
                      headers={"Authorization": f"Bearer {ADMIN_KEY}"}).json()
    by_id = {r["agent_id"]: r for r in rows}
    assert by_id["bob"]["state"] == "active"            # recent REST activity
    assert by_id["bob"]["pending_obligations"] == 1     # the open message
    assert by_id["bob"]["oldest_pending_minutes"] is not None
    assert by_id["alice"]["pending_obligations"] == 0   # own message, not owed


def test_presence_derived_from_live_connection(client):
    """Presence must reflect reachability: an agent holding a live WebSocket
    (e.g. `agora watch`) reads as present without any heartbeat protocol, and
    goes offline when the socket closes. (Field report: /presence used to say
    offline/0.0 for everyone, even mid-connection.)"""
    alice = register(client, "alice")
    bob = register(client, "bob")
    client.post("/channels", json={"name": "design"}, headers=alice)
    invite = client.post("/channels/design/invites", json={},
                         headers=alice).json()["invite_token"]
    client.post("/channels/design/join", json={"invite_token": invite}, headers=bob)

    # Before any connection: bob has made authenticated REST calls (the join),
    # so he reads "active" — working over REST is not "offline" (an MCP-only
    # tab must not look dark while visibly working).
    assert client.get("/presence/bob", headers=alice).json()["state"] == "active"

    bob_key = bob["Authorization"].removeprefix("Bearer ")
    with client.websocket_connect(f"/ws?token={bob_key}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["design"]})
        assert ws.receive_json()["type"] == "subscribed"
        # Live socket -> present (default idle), fresh timestamp.
        seen = client.get("/presence/bob", headers=alice).json()
        assert seen["state"] == "idle"
        assert seen["updated_at"] > 0
        # A declared state stands while connected.
        ws.send_json({"type": "presence", "state": "working"})
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"  # frame processed
        assert client.get("/presence/bob", headers=alice).json()["state"] == "working"

    # Socket closed -> push gone, but recent REST activity still counts:
    # "active", not a hard offline (that comes when the window ages out).
    assert client.get("/presence/bob", headers=alice).json()["state"] == "active"


def test_presence_listing_scoped_to_shared_channels(client):
    """GET /presence lists exactly the agents the caller shares a channel with
    (self included) — 'who is listening?' as one query, without becoming a
    global who-exists oracle."""
    alice = register(client, "alice")
    bob = register(client, "bob")
    register(client, "stranger")  # shares nothing with alice
    client.post("/channels", json={"name": "design"}, headers=alice)
    invite = client.post("/channels/design/invites", json={},
                         headers=alice).json()["invite_token"]
    client.post("/channels/design/join", json={"invite_token": invite}, headers=bob)

    ids = {row["agent_id"] for row in client.get("/presence", headers=alice).json()}
    assert ids == {"alice", "bob"}  # no stranger

    bob_key = bob["Authorization"].removeprefix("Bearer ")
    with client.websocket_connect(f"/ws?token={bob_key}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["design"]})
        assert ws.receive_json()["type"] == "subscribed"
        rows = {r["agent_id"]: r["state"] for r in client.get("/presence", headers=alice).json()}
        assert rows["bob"] == "idle"      # live connection visible in the listing
        assert rows["alice"] == "active"  # alice is the caller: REST activity
