"""A posted report and a concurrent task edit must both remain visible."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from agora.db import Database, StoreConflict
from agora.hub.service import HubError, HubService
from agora.models import PostMessage, Status


@pytest.fixture
def lab():
    hub = HubService(Database(":memory:"), rate_per_minute=6000)
    operator, _ = hub.register_agent("operator", "Operator", operator=True, mission="operator")
    worker, _ = hub.register_agent("worker", "Worker", mission="build")
    hub.create_channel(operator, "room", private=False)
    hub.join_channel(worker, "room", None)
    request = hub.post_message(operator, "room", PostMessage(
        body="Build the requested result", status=Status.open, to=[worker.id]))
    key = f"task:msg-{request.seq}"
    hub.store_set(worker, "room", "decision:result", {"result": "built"}, expect_version=0)
    yield hub, operator, worker, request, key
    hub.db.close()


def delivery(request):
    return PostMessage(body="Delivered with evidence", status=Status.resolved,
                       reply_to=request.id,
                       data={"evidence": [{"kind": "store", "ref": "decision:result"}]})


@pytest.mark.parametrize("final_status", ["delivered", "accepted"])
@pytest.mark.parametrize("metadata_uses_cas", [False, True])
def test_concurrent_metadata_cannot_erase_task_projection(lab, monkeypatch,
                                                        final_status, metadata_uses_cas):
    hub, operator, worker, request, key = lab
    if final_status == "accepted":
        hub.post_message(worker, "room", delivery(request))
        actor, payload = operator, PostMessage(body="Accepted", status=Status.resolved, reply_to=request.id)
    else:
        actor, payload = worker, delivery(request)
    before = hub.db.store_get("room", key)
    about_to_project, resume_projection = Event(), Event()
    real_write = hub.db.store_set

    def pause_projection(channel, write_key, value, updated_by, expect_version=None):
        if write_key == key and updated_by == "hub" and value.get("status") == final_status:
            # The real task was read; the next operation spends its version.
            about_to_project.set()
            assert resume_projection.wait(5), "projection barrier was not released"
        return real_write(channel, write_key, value, updated_by, expect_version)

    monkeypatch.setattr(hub.db, "store_set", pause_projection)

    def change_title():
        try:
            hub.store_set(operator, "room", key, {"title": "Concurrent refined title"},
                          expect_version=before.version if metadata_uses_cas else None)
            return "written"
        except StoreConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        posting = pool.submit(hub.post_message, actor, "room", payload)
        assert about_to_project.wait(5)
        # Force the old bug's interleaving when the projection is unprotected.
        # When protected, let the writer wait behind it instead. No sleep or
        # timing assumption decides whether the metadata write has committed.
        projection_unprotected = hub.db.orchestration_lock.acquire(blocking=False)
        if projection_unprotected:
            hub.db.orchestration_lock.release()
        editing = pool.submit(change_title)
        try:
            if projection_unprotected:
                editing.result(timeout=5)
        finally:
            resume_projection.set()
        posted = posting.result(timeout=5)
        edit_result = editing.result(timeout=5)

    assert posted.status == Status.resolved
    projected = hub.db.store_get("room", key)
    assert projected.value["status"] == final_status
    assert not hub.task_context(worker, "room", key)["ready"]
    if metadata_uses_cas:
        # A stale explicit write conflicts. The client rereads and retries;
        # neither the report nor the operator's intended title is lost.
        assert edit_result == "conflict"
        hub.store_set(operator, "room", key, {"title": "Concurrent refined title"},
                      expect_version=projected.version)
    else:
        assert edit_result == "written"
    final = hub.db.store_get("room", key).value
    assert final["title"] == "Concurrent refined title"
    assert final["status"] == final_status
    assert final["report"] and final["evidence"]
    assert not hub.task_context(worker, "room", key)["ready"]


def test_rejection_reopens_work_then_delivery_and_acceptance_stop_it(lab):
    hub, operator, worker, request, key = lab
    assert hub.task_context(worker, "room", key)["ready"]
    first = hub.post_message(worker, "room", delivery(request))
    assert hub.db.store_get("room", key).value["status"] == "delivered"
    assert not hub.task_context(worker, "room", key)["ready"]
    current = hub.db.store_get("room", key)
    hub.store_set(operator, "room", key, {"status": "rejected", "verdict": "Add the missing proof"},
                  expect_version=current.version)
    assert hub.task_context(worker, "room", key)["ready"]
    second = hub.post_message(worker, "room", delivery(request))
    assert second.id != first.id
    assert hub.db.store_get("room", key).value["report"] == f"room#{second.seq}"
    assert not hub.task_context(worker, "room", key)["ready"]
    hub.post_message(operator, "room", PostMessage(body="Accepted", status=Status.resolved, reply_to=request.id))
    final = hub.db.store_get("room", key).value
    assert final["status"] == "accepted"
    assert final["decided_by"] == operator.id
    assert final["rejections"] == 1
    assert not hub.task_context(worker, "room", key)["ready"]


@pytest.mark.parametrize("status", ["open", "rejected", "delivered"])
def test_accepted_task_cannot_reopen_or_reblock_dependents(lab, status):
    hub, operator, worker, _, key = lab
    # Direct acceptance from open is a longstanding operator choice.
    current = hub.db.store_get("room", key)
    hub.store_set(operator, "room", key, {"status": "accepted"}, expect_version=current.version)
    accepted = hub.db.store_get("room", key)
    hub.create_channel(operator, "downstream", private=False)
    hub.join_channel(worker, "downstream", None)
    request = hub.post_message(operator, "downstream", PostMessage(
        body="Use accepted result", status=Status.open, to=[worker.id]))
    downstream = f"task:msg-{request.seq}"
    row = hub.db.store_get("downstream", downstream)
    hub.store_set(operator, "downstream", downstream,
                  {"depends_on": [{"channel": "room", "key": key}]}, expect_version=row.version)
    assert hub.task_context(worker, "downstream", downstream)["ready"]
    with pytest.raises(HubError) as exc:
        hub.store_set(operator, "room", key, {"status": status, "verdict": "Change accepted result"},
                      expect_version=accepted.version)
    assert exc.value.status_code == 400
    assert hub.db.store_get("room", key) == accepted
    assert hub.task_context(worker, "downstream", downstream)["ready"]
