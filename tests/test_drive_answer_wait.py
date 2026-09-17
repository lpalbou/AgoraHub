"""Driver replay of owner-declared answer waits against the real hub route."""
from urllib.parse import quote, urlsplit

import pytest
from fastapi.testclient import TestClient

import agora.drive as drive_mod
from agora.drive import DRIVE_CHAIN_WAIT, Driver
from agora.hub.app import create_app


KEY = "claim:review-synthesis"


class Room:
    def __init__(self, tmp_path, monkeypatch):
        self.client = TestClient(create_app(db_path=":memory:", admin_key="admin", rate_per_minute=100000))
        self.seats = {}
        for name in ("operator", "director", "reviewer", "peer"):
            response = self.client.post("/agents", json={"id": name, "operator": name == "operator"},
                                        headers={"Authorization": "Bearer admin"})
            assert response.status_code == 200, response.text
            self.seats[name] = {"Authorization": "Bearer " + response.json()["api_key"]}
        response = self.client.post("/channels", json={"name": "task-1", "private": False},
                                    headers=self.seats["operator"])
        assert response.status_code == 200, response.text
        for name in ("director", "reviewer", "peer"):
            assert self.client.post("/channels/task-1/join", json={}, headers=self.seats[name]).status_code == 200
        response = self.client.post("/channels/task-1/messages", headers=self.seats["director"], json={
            "status": "open", "to": ["reviewer"], "body": "Please review the readiness conclusion.",
            "asks": [{"id": "review", "text": "Approve or decline the readiness conclusion.", "to": ["reviewer"]}],
        })
        assert response.status_code == 200, response.text
        self.root = response.json()["id"]
        self.spawned = []
        monkeypatch.setenv("AGORA_HOME", str(tmp_path))
        monkeypatch.setattr("agora.config.get_cached_key", lambda *_: self.seats["director"]["Authorization"].split()[1])
        monkeypatch.setattr("httpx.get", lambda url, **kwargs: self.client.get(
            urlsplit(url).path, params=kwargs.get("params"), headers=kwargs.get("headers")))
        self.home = tmp_path
        self.driver = self.new_driver()

    def new_driver(self):
        return Driver("director", "http://isolated", cwd=self.home, harness="codex", spawn=self.spawn)

    def spawn(self, prompt, _sid):
        self.spawned.append(prompt)
        return "work-session", True

    def claim(self, value=None, version=0, writer="director"):
        value = value or {"owner": "director", "status": "blocked awaiting review",
                          "source_message_id": self.root,
                          "blocked_on": "seat", "needs_from": "reviewer", "needs": "the named review answer",
                          "waiting_for_answers": [{"channel": "task-1", "message_id": self.root, "after_seq": 0}]}
        response = self.client.put("/channels/task-1/store/" + quote(KEY, safe=":"), headers=self.seats[writer],
                                   json={"value": value, "expect_version": version})
        assert response.status_code == 200, response.text
        return response.json()

    def answer(self, *, decline=False):
        payload = {"status": "reply", "reply_to": self.root, "body": "Review complete.",
                   ("declines" if decline else "answers"): ["review"]}
        response = self.client.post("/channels/task-1/messages", headers=self.seats["reviewer"], json=payload)
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def room(tmp_path, monkeypatch):
    return Room(tmp_path, monkeypatch)


def test_same_answer_wait_survives_prose_and_version_rewrites_then_restarts(room):
    row = room.claim()
    assert room.driver._continuation_snapshot() is None
    room.answer()
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert len(room.spawned) == 1
    rewritten = room.claim({**row["value"], "status": "blocked awaiting same review",
                            "next_step": "Reconsider the review answer"}, row["version"])
    assert room.driver._continuation_snapshot() is None
    assert room.new_driver()._continuation_snapshot() is None
    assert rewritten["version"] > row["version"]


def test_active_answer_wait_blocks_model_work_despite_prose_and_versions(room):
    row = room.claim({"owner": "director", "status": "active awaiting review",
                      "source_message_id": room.root, "blocked_on": "seat", "needs_from": "reviewer",
                      "needs": "the named review answer", "next_step": "wait for reviewer",
                      "waiting_for_answers": [{"channel": "task-1", "message_id": room.root}]})
    assert room.driver._continuation_snapshot() is None
    rewritten = room.claim({**row["value"], "status": "working same review", "next_step": "still waiting"}, row["version"])
    assert room.driver._continuation_snapshot() is None
    assert not room.spawned
    assert rewritten["version"] > row["version"]


def _idle_boundary(driver, monkeypatch):
    """Run exactly one no-wake listener return through the real scheduler."""
    returns = iter([0])

    def listen(**_kw):
        try:
            return next(returns)
        except StopIteration:
            raise SystemExit(0)

    monkeypatch.setattr(drive_mod, "run_listen", listen)
    with pytest.raises(SystemExit):
        driver.run()


def test_answer_wait_is_not_idle_but_answer_and_expiry_are_actionable(
        room, monkeypatch):
    """The scheduler must not turn a parked answer wait into lane work.

    This uses the actual store row and reply-state route.  A peer's unrelated
    FYI produces real reception traffic, which would otherwise satisfy the
    initiative lane's traffic gate.  The unchanged wait still produces no
    work turn; its required reply and its deadline each make a continuation
    snapshot that the ordinary work path runs.
    """
    room.claim()
    assert room.driver._continuation_snapshot() is None
    assert room.driver._answer_wait_pending is True
    assert room.client.post("/channels/task-1/messages", headers=room.seats["peer"], json={
        "status": "fyi", "body": "Unrelated traffic; the review answer is still absent.",
    }).status_code == 200
    assert room.driver.run_turn() is True
    _idle_boundary(room.driver, monkeypatch)
    assert len(room.spawned) == 1, "only the unrelated reception turn ran"

    room.answer()
    _idle_boundary(room.driver, monkeypatch)
    assert len(room.spawned) == 2

    current_version = room.client.get("/channels/task-1/store/" + quote(KEY, safe=":"),
                                      headers=room.seats["director"]).json()["version"]
    room.claim({"owner": "director", "status": "blocked awaiting review",
                "source_message_id": room.root, "blocked_on": "seat",
                "needs_from": "reviewer", "needs": "a later reviewer response",
                "waiting_for_answers": [{"channel": "task-1", "message_id": room.root,
                                         "after_seq": 999}], "wait_until": 1.0},
               version=current_version)
    expiring = room.new_driver()
    monkeypatch.setattr(drive_mod.time, "time", lambda: 2.0)
    assert expiring._continuation_snapshot() is not None
    _idle_boundary(expiring, monkeypatch)
    assert len(room.spawned) == 3


def test_active_answer_event_spends_one_receipt_across_rewrite_and_restart(room):
    row = room.claim({"owner": "director", "status": "active awaiting review",
                      "source_message_id": room.root, "blocked_on": "seat", "needs_from": "reviewer",
                      "needs": "the named review answer",
                      "waiting_for_answers": [{"channel": "task-1", "message_id": room.root}]})
    room.answer()
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert len(room.spawned) == 1
    rewritten = room.claim({**row["value"], "status": "working after the answer"}, row["version"])
    assert room.driver._continuation_snapshot() is None
    assert room.new_driver()._continuation_snapshot() is None
    assert rewritten["version"] > row["version"]


def test_later_answer_threshold_is_a_new_event_once(room):
    row = room.claim()
    first = room.answer()
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    changed = room.claim({**row["value"], "waiting_for_answers": [
        {"channel": "task-1", "message_id": room.root, "after_seq": first["seq"]}
    ]}, row["version"])
    assert room.driver._continuation_snapshot() is None
    room.answer(decline=True)
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert len(room.spawned) == 2
    assert changed["version"] > row["version"]


def test_crash_timeout_and_cancel_do_not_lose_or_bypass_wait_event(room):
    row = room.claim(); room.answer()
    crashing = room.new_driver()
    crashing._spawn = lambda *_: (_ for _ in ()).throw(RuntimeError("crash before receipt"))
    with pytest.raises(RuntimeError):
        crashing._chain_step(crashing._continuation_snapshot())
    retry = room.new_driver()
    retry._spawn = lambda *_: (None, False)
    retry._last_turn_stage = "infrastructure"
    assert retry._chain_step(retry._continuation_snapshot())
    assert room.new_driver()._continuation_snapshot() is not None
    snap = room.new_driver()._continuation_snapshot()
    room.claim({**row["value"], "status": "cancelled"}, row["version"], writer="operator")
    assert not room.new_driver()._chain_step(snap)


def test_expiry_and_retracted_root_each_make_one_reconsideration(room, monkeypatch):
    row = room.claim({"owner": "director", "status": "active waiting", "source_message_id": room.root,
                      "blocked_on": "seat", "needs_from": "reviewer", "needs": "the named review answer",
                      "waiting_for_answers": [{"channel": "task-1", "message_id": room.root}],
                      "wait_until": 1.0})
    monkeypatch.setattr(drive_mod.time, "time", lambda: 2.0)
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert len(room.spawned) == 1
    assert room.driver._continuation_snapshot() is None
    rewritten = room.claim({**row["value"], "status": "working after expiry"}, row["version"])
    assert room.new_driver()._continuation_snapshot() is None
    assert rewritten["version"] > row["version"]
    assert room.driver._listen_window(None) == DRIVE_CHAIN_WAIT
    # A fresh declaration observes the root's later retraction as a distinct
    # failure event; a repeated scan remains suppressed by its durable receipt.
    monkeypatch.setattr(drive_mod.time, "time", __import__("time").time)
    changed = room.claim({**rewritten["value"], "wait_until": None}, rewritten["version"])
    assert room.client.post(f"/channels/task-1/messages/{room.root}/retract", headers=room.seats["director"]).status_code == 200
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert room.driver._continuation_snapshot() is None
    assert changed["version"] > row["version"]


def test_expiry_releases_one_reconsideration_even_when_artifact_is_still_missing(room, monkeypatch):
    room.claim({"owner": "director", "status": "blocked waiting", "source_message_id": room.root,
                "blocked_on": "seat", "needs_from": "reviewer", "needs": "the named review answer",
                "waiting_for_answers": [{"channel": "task-1", "message_id": room.root}],
                "waiting_for_artifacts": [{"channel": "task-1", "path": "shared/not-yet.md", "min_version": 1}],
                "wait_until": 1.0})
    monkeypatch.setattr(drive_mod.time, "time", lambda: 2.0)
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert len(room.spawned) == 1


def test_malformed_reply_state_fails_closed_without_crashing(room, monkeypatch):
    room.claim()
    class Broken:
        status_code = 200
        def json(self):
            raise ValueError("truncated response")
    monkeypatch.setattr("httpx.get", lambda *_args, **_kwargs: Broken())
    assert room.driver._continuation_snapshot() is None
    assert not room.spawned
