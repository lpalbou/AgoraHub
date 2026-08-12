"""Standing rulings registry (0113): operator-authored ruling:* store rows."""

from __future__ import annotations

import pytest
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


# -- reachability: a ruling nobody can read or ack is not a feature ----------
#
# 2026-08-06. `ruling:` rows had 0 uses in 30 days, and the first reading of
# that was "delete it". That was wrong: the rows were never REACHABLE. A
# driven seat may use only the Agora MCP tools, and there were none — no way
# to read a ruling, no way to acknowledge one, so `rulings_required` could
# only ever refuse a seat that had no means to clear the refusal. The
# capability was fine; the wiring was missing.


def test_a_seat_can_read_and_ack_rulings_through_mcp():
    """The whole loop over the surface a driven seat actually has."""
    import inspect

    from agora.mcp import server

    src = inspect.getsource(server.build_server)
    assert "def read_rulings(" in src, "no way for a seat to READ a ruling"
    assert "def ack_rulings(" in src, "no way for a seat to ACK a ruling"


def test_rulings_required_is_clearable_by_the_seat_it_blocks():
    """The refusal must name a way out the blocked seat can actually take.
    Before the MCP tools existed this gate was a dead end: refuse, with no
    reachable remedy."""
    from agora.db import Database
    from agora.hub.service import CHANNEL_META_KEY, HubError, HubService
    from agora.models import PostMessage, Status

    svc = HubService(Database(":memory:"), rate_per_minute=600.0)
    op, _ = svc.register_agent("laurent", "L", operator=True, mission="seat laurent")
    worker, _ = svc.register_agent("w", "W", mission="seat w")
    svc.create_channel(op, "room", private=False)
    svc.join_channel(worker, "room", None)

    src = svc.post_message(op, "room", PostMessage(
        body="Ruling: all art is generated in-repo.", status=Status.fyi))
    svc.store_set(op, "room", "ruling:no-external-assets",
                  {"text": "All art is generated in-repo; no downloads.",
                   "scope": ["*"], "active": True,
                   "source_message_id": src.id})
    svc.store_set(op, "room", CHANNEL_META_KEY, {"rulings_required": True})

    with pytest.raises(HubError) as e:
        svc.post_message(worker, "room", PostMessage(body="hi", status=Status.fyi))
    assert e.value.status_code == 409

    # The seat can SEE what blocks it...
    digest = svc.channel_digest(worker, "room")
    assert [r["key"] for r in digest["rulings"]] == ["ruling:no-external-assets"]
    assert [r["key"] for r in digest["unacknowledged_rulings"]] == [
        "ruling:no-external-assets"]

    # ...ack it, and proceed. That is the loop that did not exist.
    svc.ack_rulings(worker, "room", ["ruling:no-external-assets"])
    assert svc.post_message(worker, "room",
                            PostMessage(body="hi", status=Status.fyi)).seq > 0


def test_a_delegate_cannot_forge_a_ruling():
    """A ruling binds future work, so authorship is operator-only. A
    `ruling`-power delegate signs off WITHIN a scope; it does not mint
    standing constraints."""
    from agora.db import Database
    from agora.hub.service import HubError, HubService

    svc = HubService(Database(":memory:"), rate_per_minute=600.0)
    op, _ = svc.register_agent("laurent", "L", operator=True, mission="seat laurent")
    dele, _ = svc.register_agent("d", "D", mission="seat d")
    svc.create_channel(op, "room", private=False)
    svc.join_channel(dele, "room", None)
    svc.set_delegation("d", ["ruling", "reporting"])

    with pytest.raises(HubError) as e:
        svc.store_set(dele, "room", "ruling:mine",
                      {"text": "x", "active": True, "source_message_id": "x"})
    assert e.value.status_code == 403
