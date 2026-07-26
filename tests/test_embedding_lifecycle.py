"""Embedding lifecycle (agora-0137 step 7): the manager's state machine
through the HTTP surface, against the fake endpoint."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("numpy")

from agora.hub.app import create_app
from tests.fake_embed import FakeEmbedServer

ADMIN_KEY = "test-admin"


@pytest.fixture()
def server():
    s = FakeEmbedServer().start()
    yield s
    s.stop()


def _client(tmp_path, embedding=None) -> TestClient:
    app = create_app(db_path=str(tmp_path / "hub.db"), admin_key=ADMIN_KEY,
                     rate_per_minute=600.0, embedding=embedding)
    return TestClient(app)


def _operator(client) -> dict:
    r = client.post("/agents", json={"id": "op", "operator": True},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def _post(client, headers, body="hello world"):
    client.post("/channels", json={"name": "room", "private": False},
                headers=headers)
    client.post("/channels/room/messages", json={"body": body},
                headers=headers)


def test_disabled_until_configured_then_fills_to_ready(tmp_path, server):
    client = _client(tmp_path)
    op = _operator(client)
    assert client.get("/admin/embedding", headers=op).json()["state"] == "disabled"
    _post(client, op)
    r = client.put("/admin/embedding", headers=op,
                   json={"url": server.url, "model": "m"})
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] and body["probe"]["ok"]
    deadline = time.time() + 15
    state = ""
    while time.time() < deadline:
        state = client.get("/admin/embedding", headers=op).json()["state"]
        if state == "ready":
            break
        time.sleep(0.2)
    assert state == "ready"


def test_model_change_is_gated_and_fills_blue_green(tmp_path, server):
    client = _client(tmp_path, embedding={"url": server.url, "model": "m"})
    op = _operator(client)
    _post(client, op)
    deadline = time.time() + 15
    while time.time() < deadline:
        if client.get("/admin/embedding", headers=op).json()["state"] == "ready":
            break
        time.sleep(0.2)
    # Same model: idempotent probe, never a change.
    r = client.put("/admin/embedding", headers=op,
                   json={"url": server.url, "model": "m"})
    assert r.status_code == 200 and r.json()["changed"] is False
    # Different model without acceptance: the R3 gate.
    r = client.put("/admin/embedding", headers=op,
                   json={"url": server.url, "model": "m2"})
    assert r.status_code == 409
    assert "accept_recompute" in r.json()["detail"]
    # Accepted: pending fill starts; old model keeps serving (state ready).
    r = client.put("/admin/embedding", headers=op,
                   json={"url": server.url, "model": "m2",
                         "accept_recompute": True})
    assert r.status_code == 200 and r.json()["pending"] is True
    deadline = time.time() + 20
    flipped = False
    while time.time() < deadline:
        s = client.get("/admin/embedding", headers=op).json()
        if s.get("model") == "m2" and not s.get("pending_model"):
            flipped = True
            break
        time.sleep(0.2)
    assert flipped, "blue/green fill never flipped"


def test_disable_keeps_vectors_unless_erase(tmp_path, server):
    client = _client(tmp_path, embedding={"url": server.url, "model": "m"})
    op = _operator(client)
    _post(client, op)
    deadline = time.time() + 15
    while (time.time() < deadline and client.get(
            "/admin/embedding", headers=op).json()["state"] != "ready"):
        time.sleep(0.2)
    r = client.delete("/admin/embedding", headers=op)
    assert r.status_code == 200 and r.json()["erased_rows"] == 0
    assert client.get("/admin/embedding",
                      headers=op).json()["state"] == "disabled"


def test_non_operator_cannot_touch_embedding(tmp_path):
    client = _client(tmp_path)
    r = client.post("/agents", json={"id": "seat"},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    seat = {"Authorization": f"Bearer {r.json()['api_key']}"}
    assert client.get("/admin/embedding", headers=seat).status_code == 403
