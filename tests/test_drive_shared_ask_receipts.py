"""A shared ask's remaining respondents are not the answered seat's debt."""
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from agora.drive import Driver, TurnEvidence
from agora.hub.app import create_app


@pytest.mark.parametrize("case", ["answered", "promise_only", "second_ask_remaining"])
def test_shared_ask_receipt_uses_actual_reader_scoped_debt(tmp_path, monkeypatch, case):
    with TestClient(create_app(db_path=":memory:", admin_key="admin", rate_per_minute=100000)) as client:
        seats = {}
        for name in ("requester", "worker", "other"):
            response = client.post("/agents", headers={"Authorization": "Bearer admin"},
                                   json={"id": name, "operator": name == "requester"})
            assert response.status_code == 200, response.text
            seats[name] = {"Authorization": "Bearer " + response.json()["api_key"]}
        response = client.post("/channels", headers=seats["requester"], json={"name": "review", "private": False})
        assert response.status_code == 200, response.text
        for name in ("worker", "other"):
            assert client.post("/channels/review/join", headers=seats[name], json={}).status_code == 200
        asks = [{"id": "1", "text": "Give your assessment.", "to": ["worker", "other"]}]
        if case == "second_ask_remaining":
            asks.append({"id": "2", "text": "Also verify the actual artifact.", "to": ["worker"]})
        response = client.post("/channels/review/messages", headers=seats["requester"], json={
            "status": "open", "to": ["worker", "other"], "body": "Please assess the shared proposal.", "asks": asks})
        assert response.status_code == 200, response.text
        question = response.json()
        monkeypatch.setenv("AGORA_HOME", str(tmp_path))
        monkeypatch.setattr("agora.config.get_cached_key", lambda _url, agent: seats[agent]["Authorization"].split()[1])
        monkeypatch.setattr("httpx.get", lambda url, **kw: client.get(
            urlsplit(url).path, params=kw.get("params"), headers=kw.get("headers")))
        driver = Driver("worker", "http://isolated", cwd=tmp_path, spawn=lambda *_: ("test", True))
        driver._reception_debt_verification_required = True
        driver._reception_debt_before = driver._reception_debt()
        assert question["id"] in driver._reception_debt_before.due
        if case == "promise_only":
            reply = {"status": "fyi", "reply_to": question["id"],
                     "body": "I intend to assess the proposal later.", "to": ["requester"]}
        else:
            reply = {"status": "reply", "reply_to": question["id"], "answers": ["1"],
                     "body": "I inspected the proposal; its assumptions are consistent with my component."}
        response = client.post("/channels/review/messages", headers=seats["worker"], json=reply)
        assert response.status_code == 200, response.text
        # A real, still-unanswered peer remains due in every case.
        other = Driver("other", "http://isolated", cwd=tmp_path, spawn=lambda *_: ("test", True))
        assert question["id"] in other._reception_debt().due
        after = driver._reception_debt()
        verdict = driver._verify_reception_debt(TurnEvidence(ok=True), "wake")
        if case == "answered":
            assert question["id"] not in after.due
            # The global ask is still pending, demonstrating the original defect.
            assert driver._message_pending_asks("review", question["seq"], question["id"]) == frozenset({"1"})
            assert verdict.ok
        else:
            assert question["id"] in after.due
            assert not verdict.ok and verdict.reason == "debt-remains"
