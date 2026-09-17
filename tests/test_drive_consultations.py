"""Actual HTTP state drives one reconsideration, without a model or GPU."""
import time
from types import SimpleNamespace

import pytest

from test_drive_answer_wait import KEY, Room


@pytest.fixture
def room(tmp_path, monkeypatch):
    room = Room(tmp_path, monkeypatch)
    return room


def collective(room, **policy):
    response = room.client.post("/channels/task-1/messages", headers=room.seats["director"], json={
        "status": "open", "body": "Review these interpretations together.",
        "asks": [{"id": "review", "text": "Which interpretation fits the evidence?", "to": ["reviewer", "peer"]}],
        "data": {"consultation": {"action": "Commit the selected repair", "required": ["reviewer", "peer"], **policy}}})
    assert response.status_code == 200, response.text
    room.root = response.json()["id"]
    return room.claim()


def second(room):
    response = room.client.post("/channels/task-1/messages", headers=room.seats["peer"], json={
        "status": "reply", "reply_to": room.root, "answers": ["review"],
        "body": "The proposed repair leaves a contradiction in another passage."})
    assert response.status_code == 200, response.text
    return response.json()


def test_one_collection_transition_one_turn_and_restart_receipt(room):
    collective(room)
    room.answer()
    assert room.driver._continuation_snapshot() is None
    room.answer()
    assert room.driver._continuation_snapshot() is None
    second(room)
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert len(room.spawned) == 1 and "get_consultation" in room.spawned[0]
    assert room.driver._continuation_snapshot() is None
    assert room.new_driver()._continuation_snapshot() is None


def test_collection_does_not_block_unrelated_claim(room):
    collective(room)
    room.answer()
    response = room.client.put("/channels/task-1/store/claim:independent", headers=room.seats["director"], json={
        "value": {"owner": "director", "status": "working", "next_step": "Inspect the layout independently"},
        "expect_version": 0})
    assert response.status_code == 200, response.text
    snapshot = room.driver._continuation_snapshot()
    assert snapshot[1] == "claim:independent"
    assert room.driver._chain_step(snapshot)


def test_timer_releases_a_reconsideration_not_a_decision(room, monkeypatch):
    deadline = time.time() + 300
    collective(room, deadline=deadline)
    room.answer()
    assert room.driver._continuation_snapshot() is None
    assert room.driver._answer_wait_deadline == deadline
    monkeypatch.setattr("agora.hub.consultations.time", SimpleNamespace(time=lambda: deadline))
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert room.driver._continuation_snapshot() is None
    state = room.client.get(f"/channels/task-1/messages/{room.root}/consultation", headers=room.seats["director"]).json()
    response = room.client.post(f"/channels/task-1/messages/{room.root}/consultation/conclusion",
        headers=room.seats["director"], json={"outcome": "decided", "expected_version": state["version"],
                                           "body": "Proceed with the first interpretation"})
    assert response.status_code == 409, response.text
    second(room)
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert len(room.spawned) == 2


def test_lost_question_reconsiders_even_if_another_question_is_still_collecting(room):
    row = collective(room)
    first = room.root
    response = room.client.post("/channels/task-1/messages", headers=room.seats["director"], json={
        "status": "open", "body": "Another independent question", "to": ["peer"],
        "asks": [{"id": "other", "text": "Verify this other dependency", "to": ["peer"]}]})
    assert response.status_code == 200, response.text
    other = response.json()["id"]
    room.claim({**row["value"], "waiting_for_answers": [
        {"channel": "task-1", "message_id": first}, {"channel": "task-1", "message_id": other}]}, row["version"])
    assert room.client.post(f"/channels/task-1/messages/{other}/retract", headers=room.seats["director"]).status_code == 200
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert len(room.spawned) == 1


@pytest.mark.parametrize("status", ["waiting_for_answers", "waiting_for_artifacts"])
def test_status_label_alone_cannot_silently_buy_more_work(room, status):
    response = room.client.put(f"/channels/task-1/store/{KEY}", headers=room.seats["director"], json={
        "value": {"owner": "director", "status": status, "next_step": "wait"}, "expect_version": 0})
    assert response.status_code == 400 and "does not declare a dependency" in response.text


def test_active_artifact_wait_is_a_real_dependency_and_spends_its_receipt(room):
    value = {"owner": "director", "status": "active", "source_message_id": room.root,
             "waiting_for_artifacts": [{"channel": "task-1", "path": "result.md", "min_version": 1}]}
    room.claim(value)
    assert room.driver._continuation_snapshot() is None
    response = room.client.put("/channels/task-1/fs/result.md", headers=room.seats["peer"],
                                json={"content": "The verified result", "expect_version": 0})
    assert response.status_code == 200, response.text
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert room.new_driver()._continuation_snapshot() is None


def test_graph_http_is_visible_only_to_channel_members(room):
    response = room.client.get("/channels/task-1/collaboration-graph", headers=room.seats["director"])
    assert response.status_code == 200 and response.json()["nodes"]
    service = room.client.app.state.service
    service.db.remove_member("task-1", "peer")
    assert room.client.get("/channels/task-1/collaboration-graph", headers=room.seats["peer"]).status_code == 403


def test_decision_dependency_does_not_release_at_collection_readiness(room):
    row = collective(room)
    room.claim({**row["value"], "waiting_for_answers": [{"channel": "task-1", "message_id": room.root,
                                                         "condition": "decided"}]}, row["version"])
    room.answer(); second(room)
    assert room.driver._continuation_snapshot() is None
    url = f"/channels/task-1/messages/{room.root}/consultation"
    state = room.client.get(url, headers=room.seats["director"]).json()
    response = room.client.post(url + "/conclusion", headers=room.seats["director"], json={
        "outcome": "decided", "expected_version": state["version"], "body": "Use two incidents to reconcile both passages"})
    assert response.status_code == 200, response.text
    assert room.driver._chain_step(room.driver._continuation_snapshot())
    assert len(room.spawned) == 1
    assert room.new_driver()._continuation_snapshot() is None
