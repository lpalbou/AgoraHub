"""@mention addressing at post time (agora-0105)."""

from pathlib import Path

from fastapi.testclient import TestClient

from agora.hub.app import create_app
from agora.mentions import body_for_mention_scan, parse_mentions

ADMIN_KEY = "test-admin"


def make_client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0,
                     notify_dir=str(tmp_path / "notify"))
    return TestClient(app)


def register(client: TestClient, agent_id: str, operator: bool = False) -> dict[str, str]:
    r = client.post("/agents", json={"id": agent_id, "operator": operator},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_channel(client: TestClient, owner: dict, name: str, *members: dict) -> None:
    client.post("/channels", json={"name": name}, headers=owner)
    for member in members:
        invite = client.post(f"/channels/{name}/invites", json={},
                             headers=owner).json()["invite_token"]
        client.post(f"/channels/{name}/join", json={"invite_token": invite},
                    headers=member)


def test_parse_mentions_skips_nonce_fenced_quotes():
    body = (
        "Please comply \u27e6AGORA:abc:quote\u27e7\n"
        "laurent RULED @ghost must stop\u27e6/AGORA:abc\u27e7 "
        "and work with @agora now."
    )
    assert parse_mentions(body) == ["agora"]
    assert "@ghost" not in body_for_mention_scan(body)


def test_operator_ask_text_mention_populates_per_ask_to(tmp_path):
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    agora = register(client, "agora")
    make_channel(client, op, "room", agora)

    r = client.post("/channels/room/messages", json={
        "body": "please answer",
        "title": "q",
        "status": "open",
        "asks": [{"id": "1", "text": "@agora confirm the plan?"}],
    }, headers=op)
    assert r.status_code == 200, r.text
    msg = r.json()
    assert msg["data"]["asks"][0]["to"] == ["agora"]

    owed = client.get("/owed", headers=agora).json()
    assert any(row["asks_naming_you"] == ["1"] for row in owed["to_answer"])


def test_peer_ask_text_mention_populates_per_ask_to(tmp_path):
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    peer = register(client, "peer")
    agora = register(client, "agora")
    make_channel(client, op, "room", peer, agora)

    r = client.post("/channels/room/messages", json={
        "body": "please answer",
        "title": "q",
        "status": "open",
        "asks": [{"id": "1", "text": "@agora confirm the plan?"}],
    }, headers=peer)
    assert r.status_code == 200, r.text
    msg = r.json()
    assert msg["data"]["asks"][0]["to"] == ["agora"]

    owed = client.get("/owed", headers=agora).json()
    assert any(row["asks_naming_you"] == ["1"] for row in owed["to_answer"])


def test_operator_body_mention_populates_to(tmp_path):
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    agora = register(client, "agora")
    flow = register(client, "flow")
    make_channel(client, op, "room", agora, flow)

    r = client.post("/channels/room/messages", json={
        "body": "work with @agora and @flow on this",
        "title": "directive",
        "status": "open",
    }, headers=op)
    assert r.status_code == 200, r.text
    msg = r.json()
    assert sorted(msg["to"]) == ["agora", "flow"]

    owed = client.get("/owed", headers=agora).json()
    assert any(row["id"] == msg["id"] for row in owed["to_answer"])


def test_peer_body_mention_populates_to_without_owing_reply(tmp_path):
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    peer = register(client, "peer")
    target = register(client, "agora")
    make_channel(client, op, "room", peer, target)

    r = client.post("/channels/room/messages", json={
        "body": "thread update for @agora — please review",
        "title": "fyi",
        "status": "fyi",
    }, headers=peer)
    assert r.status_code == 200
    msg = r.json()
    assert msg["to"] == ["agora"]

    owed = client.get("/owed", headers=target).json()
    assert not any(row["id"] == msg["id"] for row in owed.get("to_answer", []))
