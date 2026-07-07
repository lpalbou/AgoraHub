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
