"""Standing rulings registry (0113): operator-authored ruling:* store rows."""

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
    r = client.post("/agents", json={"id": agent_id, "operator": operator},
                    headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_channel(client, owner, name, *members):
    client.post("/channels", json={"name": name}, headers=owner)
    for m in members:
        invite = client.post(f"/channels/{name}/invites", json={},
                             headers=owner).json()["invite_token"]
        client.post(f"/channels/{name}/join",
                    json={"invite_token": invite}, headers=m)


def test_ruling_store_is_operator_only():
    client = make_client()
    op = register(client, "op", operator=True)
    flow = register(client, "flow")
    make_channel(client, op, "room", flow)
    body = {
        "text": "8317-only for gateway endpoints",
        "scope": ["gateway"],
        "source_message_id": "01TESTMSG00000000000000001",
        "active": True,
    }
    assert client.put("/channels/room/store/ruling:8317-only", headers=op,
                      json={"value": body}).status_code == 200
    assert client.put("/channels/room/store/ruling:8317-only", headers=flow,
                      json={"value": body}).status_code == 403


def test_ruling_schema_validated_at_write_time():
    client = make_client()
    op = register(client, "op", operator=True)
    make_channel(client, op, "room")
    bad = client.put("/channels/room/store/ruling:bad", headers=op,
                     json={"value": {"text": "x"}})
    assert bad.status_code == 400
    assert "scope" in bad.text or "source_message_id" in bad.text


def test_active_rulings_surface_in_channel_digest():
    client = make_client()
    op = register(client, "op", operator=True)
    flow = register(client, "flow")
    make_channel(client, op, "room", flow)
    client.put("/channels/room/store/ruling:no-token-rotate", headers=op,
               json={"value": {
                   "text": "Never rotate auth tokens without operator word",
                   "scope": ["*"],
                   "source_message_id": "01TESTMSG00000000000000002",
                   "active": True,
               }})
    client.put("/channels/room/store/ruling:revoked-old", headers=op,
               json={"value": {
                   "text": "Old rule",
                   "scope": ["code"],
                   "source_message_id": "01TESTMSG00000000000000003",
                   "active": False,
               }})
    digest = client.get("/channels/room/digest", headers=flow).json()
    assert digest["counts"]["rulings"] == 1
    assert digest["rulings"][0]["key"] == "ruling:no-token-rotate"
    assert "Never rotate" in digest["rulings"][0]["value"]["text"]


def test_ruling_ack_clears_unacknowledged_in_digest():
    client = make_client()
    op = register(client, "op", operator=True)
    flow = register(client, "flow")
    make_channel(client, op, "room", flow)
    client.put("/channels/room/store/ruling:8317-only", headers=op,
               json={"value": {
                   "text": "Gateway endpoints must use port 8317 only",
                   "scope": ["flow", "gateway"],
                   "source_message_id": "01TESTMSG00000000000000004",
                   "active": True,
               }})
    digest = client.get("/channels/room/digest", headers=flow).json()
    assert digest["counts"]["unacknowledged_rulings"] == 1
    assert digest["unacknowledged_rulings"][0]["key"] == "ruling:8317-only"
    ack = client.post("/channels/room/ruling-acks", headers=flow,
                      json={"keys": ["ruling:8317-only"]})
    assert ack.status_code == 200
    digest2 = client.get("/channels/room/digest", headers=flow).json()
    assert digest2["counts"]["unacknowledged_rulings"] == 0


def test_ruling_ack_rejects_out_of_scope_seat():
    client = make_client()
    op = register(client, "op", operator=True)
    code = register(client, "code")
    make_channel(client, op, "room", code)
    client.put("/channels/room/store/ruling:gateway-only", headers=op,
               json={"value": {
                   "text": "Gateway-only rule",
                   "scope": ["gateway"],
                   "source_message_id": "01TESTMSG00000000000000005",
                   "active": True,
               }})
    r = client.post("/channels/room/ruling-acks", headers=code,
                    json={"keys": ["ruling:gateway-only"]})
    assert r.status_code == 403


def test_rulings_required_gate_blocks_post_until_acked():
    client = make_client()
    op = register(client, "op", operator=True)
    flow = register(client, "flow")
    make_channel(client, op, "room", flow)
    client.put("/channels/room/store/channel:meta", headers=op,
               json={"value": {"rulings_required": True}})
    client.put("/channels/room/store/ruling:no-rotate", headers=op,
               json={"value": {
                   "text": "Never rotate tokens without operator word",
                   "scope": ["flow"],
                   "source_message_id": "01TESTMSG00000000000000006",
                   "active": True,
               }})
    blocked = client.post("/channels/room/messages",
                          headers=flow,
                          json={"body": "hello", "status": "fyi"})
    assert blocked.status_code == 409
    assert "ruling-acks" in blocked.text
    client.post("/channels/room/ruling-acks", headers=flow,
                json={"keys": ["ruling:no-rotate"]})
    ok = client.post("/channels/room/messages",
                     headers=flow,
                     json={"body": "hello", "status": "fyi"})
    assert ok.status_code == 200
