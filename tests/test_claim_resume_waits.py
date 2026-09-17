"""A successful resume must not silently retain an unrelated suspension."""
import pytest

from test_artifact_wait import KEY, Room


@pytest.fixture
def room(tmp_path, monkeypatch):
    return Room(tmp_path, monkeypatch)


def linked_value(room, **changes):
    service = room.client.app.state.service
    source = service.db.get_message(room.source)
    task = {"channel": "task-1", "key": f"task:msg-{source.seq}"}
    assert service.db.store_get(task["channel"], task["key"]) is not None
    return room.value(task=task, **changes)


@pytest.mark.parametrize("old_status", ["parked", "blocked — waiting", "paused", "on-hold"])
def test_linked_resume_rejects_omitted_gate_without_mutating_row(room, old_status):
    row = room.claim(linked_value(room, status=old_status))
    response = room.put({"owner": "director", "status": "ACTIVE! new independent work",
                         "waiting_for_answers": None, "next_step": "Make the new revision"}, row["version"])
    assert response.status_code == 400, response.text
    assert "explicitly acknowledge" in response.text
    assert "waiting_for_artifacts" in response.text
    assert "workspace files or attachments" in response.text
    current = room.client.app.state.service.db.store_get("task-1", KEY)
    assert current.version == row["version"]
    assert current.value == row["value"]


def test_legacy_state_resume_must_acknowledge_gate(room):
    row = room.claim(linked_value(room, status=None, state="parked:"))
    response = room.put({"state": "working"}, row["version"])
    assert response.status_code == 400
    assert "waiting_for_artifacts" in response.text


def test_repeat_retains_gate_and_explicit_clear_allows_work(room):
    row = room.claim(linked_value(room, status="parked"))
    repeated = room.claim({"status": "active", "waiting_for_artifacts": row["value"]["waiting_for_artifacts"]}, row["version"])
    assert room.driver._continuation_snapshot() is None
    cleared = room.claim({"waiting_for_artifacts": None}, repeated["version"])
    assert cleared["value"]["task"] == row["value"]["task"]
    assert room.driver._continuation_snapshot() is not None


@pytest.mark.parametrize("artifact_wait", [False, True])
def test_resume_requires_acknowledging_each_answer_and_artifact_wait(room, artifact_wait):
    question = room.client.post("/channels/task-1/messages", headers=room.seats["director"], json={
        "status": "open", "to": ["media"], "body": "Publish the source and reply when ready.",
        "asks": [{"id": "result", "to": ["media"], "text": "Deliver the completed source."}]}).json()
    row = room.claim(linked_value(room, status="parked", waiting_for_answers=[
        {"channel": "task-1", "message_id": question["id"]}],
        waiting_for_artifacts=room.value()["waiting_for_artifacts"] if artifact_wait else None))
    assert room.put({"status": "active"}, row["version"]).status_code == 400
    if artifact_wait:
        response = room.put({"status": "active", "waiting_for_answers": None}, row["version"])
        assert response.status_code == 400
        assert "waiting_for_artifacts" in response.text
    resumed = room.claim({"status": "active", "waiting_for_answers": None,
                          "waiting_for_artifacts": None}, row["version"])
    assert resumed["value"]["wait_until"] is None
    assert room.driver._continuation_snapshot() is not None


def test_progress_closure_permissions_and_cas_remain_intact(room):
    row = room.claim(linked_value(room, status="parked"))
    assert room.put({"status": "active"}, row["version"], writer="peer").status_code == 403
    assert room.put({"status": "active"}, 0).status_code == 409
    assert room.put({"status": "active"}, None).status_code == 400
    progress = room.claim({"next_step": "Still waiting for the same publication"}, row["version"])
    assert progress["value"]["waiting_for_artifacts"] == row["value"]["waiting_for_artifacts"]
    closed = room.claim({"status": "done"}, progress["version"])
    assert closed["value"]["waiting_for_artifacts"] == row["value"]["waiting_for_artifacts"]
    assert room.driver._continuation_snapshot() is None


def test_local_workspace_does_not_satisfy_vfs_wait(room):
    path = "shared/result.md"
    row = room.claim(linked_value(room, status="parked", waiting_for_artifacts=[
        {"channel": "task-1", "path": path, "min_version": 1}]))
    local = room.home / path
    local.parent.mkdir()
    local.write_text("Completed local result")
    assert room.driver._continuation_snapshot() is None
    room.file(path)
    assert room.driver._continuation_snapshot() == ("task-1", KEY, row["version"])


def test_clearing_claim_wait_does_not_accept_task_prerequisite(room):
    service = room.client.app.state.service
    requirement = room.client.post("/channels/other/messages", headers=room.seats["operator"], json={
        "status": "open", "to": ["media"], "body": "Deliver the prerequisite."}).json()
    dependency = {"channel": "other", "key": f"task:msg-{requirement['seq']}"}
    value = linked_value(room, status="parked")
    task = service.db.store_get(**value["task"])
    changed = room.client.put("/channels/task-1/store/" + task.key, headers=room.seats["operator"], json={
        "value": {**task.value, "depends_on": [dependency]}, "expect_version": task.version})
    assert changed.status_code == 200, changed.text
    row = room.claim(value)
    room.claim({"status": "active", "waiting_for_artifacts": None}, row["version"])
    assert room.driver._continuation_snapshot() is None
    assert room.driver._task_wait_pending
    assert service.db.store_get(**dependency).value["status"] == "open"
