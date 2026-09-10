"""Typed task reviews are an additive, server-owned delivery gate.

These regressions deliberately use the service surface.  They prove receipt
semantics and authority, rather than tying callers to review-message prose.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from agora.db import Database
from agora.hub.ratelimit import RateLimiter
from agora.hub.service import HubError, HubService
from agora.models import PostMessage, Status


@pytest.fixture()
def lab():
    hub = HubService(Database(":memory:"), rate_per_minute=600000)
    # The long-history lookup regression intentionally emits >100 messages.
    # Give only this in-memory fixture an explicit matching burst allowance.
    hub.ratelimiter = RateLimiter(rate_per_minute=600000, burst=1000)
    owner, _ = hub.register_agent("owner", "Owner")  # channel owner, not operator
    op, _ = hub.register_agent("op", "Operator", operator=True)
    lead, _ = hub.register_agent("lead", "Lead")
    peer_a, _ = hub.register_agent("peer-a", "Peer A")
    peer_b, _ = hub.register_agent("peer-b", "Peer B")
    ruling, _ = hub.register_agent("ruling", "Ruling", mission="settle review rulings")
    proxy, _ = hub.register_agent("proxy", "Proxy", mission="act for an absent requester")
    hub.create_channel(owner, "room", private=False)
    for agent in (op, lead, peer_a, peer_b, ruling, proxy):
        hub.join_channel(agent, "room", None)
    root = hub.post_message(op, "room", PostMessage(
        title="Commission", body="Produce the release audit.",
        status=Status.open, to=["lead"],
    ))
    return hub, owner, op, lead, peer_a, peer_b, ruling, proxy, root, f"task:msg-{root.seq}"


def _artifact(hub, lead, text="roadmap one"):
    file = hub.fs_write(lead, "room", "ROADMAP.md", text, expect_version=None)
    return [{"kind": "fs", "ref": f"ROADMAP.md@{file.version}",
             "sha256": file.sha256}]


def _review(hub, agent, task, verdict, artifacts, **kwargs):
    return hub.review_task(agent, "room", task, verdict, artifacts,
                           title=kwargs.pop("title", verdict),
                           body=kwargs.pop("body", "reviewed current artifact"),
                           **kwargs)


def _deliver(hub, lead, root, artifacts):
    return hub.post_message(lead, "room", PostMessage(
        title="Delivery", body="Current reviewed roadmap.", status=Status.resolved,
        reply_to=root.id, data={"evidence": artifacts},
    ))


def test_one_reviewer_approval_cannot_clear_another_reviewers_request_changes(lab):
    hub, _, _, lead, peer_a, peer_b, _, _, root, task = lab
    artifacts = _artifact(hub, lead)
    _review(hub, peer_a, task, "request_changes", artifacts)
    # Review lookup must be keyed/indexed, rather than a capped transcript scan.
    for index in range(103):
        hub.post_message(peer_b, "room", PostMessage(
            title="ordinary", body=f"ordinary coordination {index}", status=Status.fyi,
        ))
    _review(hub, peer_b, task, "approve", artifacts)

    summary = hub.task_review_summary("room", task)
    assert summary["enabled"] is True
    assert {row["reviewer"] for row in summary["rows"]} == {"peer-a", "peer-b"}
    assert {row["reviewer"] for row in summary["blockers"]} == {"peer-a"}
    with pytest.raises(HubError, match="request_changes"):
        _deliver(hub, lead, root, artifacts)

    # Only the blocking reviewer's later verdict settles that reviewer's state.
    _review(hub, peer_a, task, "approve", artifacts)
    assert hub.task_review_summary("room", task)["blockers"] == []
    _deliver(hub, lead, root, artifacts)


def test_delivery_needs_independent_current_approval_and_exact_artifact_set(lab):
    hub, _, _, lead, peer_a, _, _, _, root, task = lab
    first = _artifact(hub, lead, "first\n")
    _review(hub, lead, task, "approve", first)
    with pytest.raises(HubError, match="independent"):
        _deliver(hub, lead, root, first)

    _review(hub, peer_a, task, "approve", first)
    second = _artifact(hub, lead, "second\n")
    with pytest.raises(HubError, match="approval"):
        _deliver(hub, lead, root, second)
    with pytest.raises(HubError):
        _review(hub, peer_a, task, "approve", first)

    _review(hub, peer_a, task, "approve", second)
    _deliver(hub, lead, root, second)


def test_review_requires_current_open_task_and_matching_task_version(lab):
    hub, _, op, lead, peer_a, _, _, _, root, task = lab
    artifacts = _artifact(hub, lead)
    row = hub.db.store_get("room", task)
    with pytest.raises(HubError, match="task.*changed"):
        _review(hub, peer_a, task, "approve", artifacts,
                expect_task_version=row.version + 1)
    _review(hub, peer_a, task, "approve", artifacts,
            expect_task_version=row.version)

    # An operator reopening/updating the canonical task invalidates the old CAS.
    value = dict(row.value)
    value["status"] = "rejected"
    value["verdict"] = "Original task needs a current review after reopening."
    hub.store_set(op, "room", task, value, expect_version=row.version)
    reopened = hub.db.store_get("room", task)
    assert reopened.value["status"] == "open"
    with pytest.raises(HubError, match="task.*changed"):
        _review(hub, peer_a, task, "approve", artifacts,
                expect_task_version=row.version)
    # The pre-reopen approval is not a clearance for the reopened task.
    with pytest.raises(HubError, match="approval"):
        _deliver(hub, lead, root, artifacts)
    _review(hub, peer_a, task, "approve", artifacts,
            expect_task_version=reopened.version)


def test_only_reviewer_or_requester_operator_scoped_ruling_or_proxy_can_close_other_review(lab):
    hub, owner, op, lead, peer_a, peer_b, ruling, proxy, _, task = lab
    artifacts = _artifact(hub, lead)
    _review(hub, peer_a, task, "request_changes", artifacts)

    # Owning the channel is not a release-review override.
    with pytest.raises(HubError, match="authority|reviewer"):
        _review(hub, owner, task, "withdraw", [], reviewer="peer-a",
                reason="Owner cannot clear another peer review.")

    _review(hub, op, task, "withdraw", [], reviewer="peer-a",
            reason="Requester accepts the documented resolution.")
    _review(hub, peer_b, task, "request_changes", artifacts)
    hub.set_delegation(ruling.id, ["ruling"], scope="room")
    _review(hub, ruling, task, "withdraw", [], reviewer="peer-b",
            reason="Ruling closes the unavailable peer review.")

    _review(hub, peer_a, task, "request_changes", artifacts)
    hub.set_delegation(proxy.id, ["proxy"], scope="room")
    hub.set_availability(op, time.time() + 120)
    _review(hub, proxy, task, "withdraw", [], reviewer="peer-a",
            reason="Requester proxy records the settled resolution.")
    assert hub.task_review_summary("room", task)["blockers"] == []


def test_review_preserves_normal_reply_answers_and_consumes_semantics(lab):
    hub, _, op, lead, peer_a, _, _, _, _, task = lab
    artifacts = _artifact(hub, lead)
    peer_question = hub.post_message(peer_a, "room", PostMessage(
        title="Peer question", body="Which artifact is current?", status=Status.open,
        to=["lead"], asks=[{"id": "artifact", "text": "Name the artifact", "to": ["lead"]}],
    ))
    lead_answer = hub.post_message(lead, "room", PostMessage(
        title="Artifact answer", body="The current roadmap is cited.", status=Status.reply,
        reply_to=peer_question.id, answers=["artifact"],
    ))
    review_request = hub.post_message(op, "room", PostMessage(
        title="Review request", body="Review the current roadmap.", status=Status.open,
        to=["peer-a"], asks=[{"id": "scope", "text": "Review scope", "to": ["peer-a"]}],
    ))
    review = _review(hub, peer_a, task, "approve", artifacts,
                     reply_to=review_request.id, answers=["scope"],
                     consumes=[lead_answer.id])
    assert review.reply_to == review_request.id
    assert review.status == Status.reply
    assert review.data["answers"] == ["scope"]
    assert review.data["consumes"] == [lead_answer.id]


def test_reserved_review_metadata_is_refused_and_retracting_latest_becomes_withdrawal(lab):
    hub, _, _, lead, peer_a, _, _, _, root, task = lab
    artifacts = _artifact(hub, lead)
    with pytest.raises(HubError, match="reserved"):
        hub.post_message(peer_a, "room", PostMessage(
            title="Forged", body="not a review", data={"task_review": {"verdict": "approve"}},
        ))

    _review(hub, peer_a, task, "approve", artifacts)
    later_block = _review(hub, peer_a, task, "request_changes", artifacts)
    hub.retract_message(peer_a, "room", later_block.id)
    rows = hub.task_review_summary("room", task)["rows"]
    assert rows[0]["verdict"] == "withdraw"
    # The old approval must not revive after the newer block is retracted.
    with pytest.raises(HubError, match="approval"):
        _deliver(hub, lead, root, artifacts)
    _review(hub, peer_a, task, "approve", artifacts)
    _deliver(hub, lead, root, artifacts)


def _integrated_finding(hub, lead, root, task, artifact):
    key = f"finding:{task[5:]}:review-proof"
    hub.store_set(lead, "room", key, {
        "kind": "task-finding-v1", "task": {"channel": "room", "key": task},
        "state": "accepted", "source": f"room#{root.seq}",
        "evidence": [{"kind": "store", "ref": task}],
        "contract": "The final roadmap must preserve this audited release contract.",
    }, expect_version=0)
    row = hub.db.store_get("room", key)
    hub.store_set(lead, "room", key, {
        **row.value, "state": "disposed", "disposition": "incorporated",
        "artifact": {"path": "ROADMAP.md", "version": 1,
                     "sha256": artifact[0]["sha256"], "excerpt": "roadmap current"},
        "disposition_evidence": [{"kind": "fs", "ref": "ROADMAP.md@1"}],
    }, expect_version=row.version)


def test_preparation_blocks_new_objection_then_returns_reviewed_full_snapshot(lab):
    hub, _, _, lead, peer_a, _, _, _, root, task = lab
    roadmap = _artifact(hub, lead, "roadmap current\n")
    coverage = hub.fs_write(lead, "room", "COVERAGE.md", "coverage current\n")
    full = roadmap + [{"kind": "fs", "ref": f"COVERAGE.md@{coverage.version}",
                       "sha256": coverage.sha256}]
    _integrated_finding(hub, lead, root, task, roadmap)

    _review(hub, peer_a, task, "request_changes", full)
    blocked = hub.prepare_task_delivery(lead, "room", task)
    assert "post_message" not in blocked
    assert blocked["blockers"][0]["reviewer"] == "peer-a"

    _review(hub, peer_a, task, "approve", full)
    prepared = hub.prepare_task_delivery(lead, "room", task)
    evidence = prepared["post_message"]["evidence"]
    fs = {(item.get("channel", "room"), item["ref"])
          for item in evidence if item["kind"] == "fs"}
    assert fs == {("room", "ROADMAP.md@1"), ("room", "COVERAGE.md@1")}
    assert any(item["kind"] == "message" and item["ref"].startswith("room#")
               for item in evidence)

    delivered = hub.post_message(lead, "room", PostMessage(
        title="Delivery", body="Prepared reviewed delivery.", status=Status.resolved,
        reply_to=prepared["post_message"]["reply_to"], data={"evidence": evidence},
    ))
    assert hub.db.store_get("room", task).value["report"] == f"room#{delivered.seq}"


def test_search_index_failure_rolls_back_typed_review_and_later_review_works(lab, monkeypatch):
    import agora.db as db_module

    hub, _, _, lead, peer_a, _, _, _, _, task = lab
    artifacts = _artifact(hub, lead)
    before = hub.db.get_messages("room", 0, limit=10000)
    real_put = db_module._si.put_doc

    def explode(*_args, **_kwargs):
        raise RuntimeError("search index failed")

    monkeypatch.setattr(db_module._si, "put_doc", explode)
    with pytest.raises(RuntimeError, match="search index failed"):
        _review(hub, peer_a, task, "approve", artifacts)
    assert hub.db.get_messages("room", 0, limit=10000) == before
    assert hub.task_review_summary("room", task)["enabled"] is False

    monkeypatch.setattr(db_module._si, "put_doc", real_put)
    _review(hub, peer_a, task, "approve", artifacts)
    assert hub.task_review_summary("room", task)["rows"][0]["verdict"] == "approve"


def test_http_review_endpoint_roundtrip_surfaces_current_review():
    from fastapi.testclient import TestClient
    from agora.hub.app import create_app

    client = TestClient(create_app(db_path=":memory:", admin_key="admin", rate_per_minute=600000))

    def register(name, operator=False):
        response = client.post("/agents", json={"id": name, "operator": operator},
                               headers={"Authorization": "Bearer admin"})
        assert response.status_code == 200
        return {"Authorization": "Bearer " + response.json()["api_key"]}

    op, lead, peer = register("op", True), register("lead"), register("peer")
    assert client.post("/channels", json={"name": "room", "private": False}, headers=op).status_code == 200
    assert client.post("/channels/room/join", json={}, headers=lead).status_code == 200
    assert client.post("/channels/room/join", json={}, headers=peer).status_code == 200
    root = client.post("/channels/room/messages", json={"title": "T", "body": "b", "status": "open", "to": ["lead"]}, headers=op).json()
    written = client.put("/channels/room/fs/ROADMAP.md", json={"content": "roadmap", "expect_version": 0}, headers=lead)
    assert written.status_code == 200
    review = client.post(f"/channels/room/tasks/task:msg-{root['seq']}/reviews", json={
        "verdict": "approve", "artifacts": [{"kind": "fs", "ref": "ROADMAP.md@1"}],
        "title": "Review", "body": "Current artifact is coherent.",
    }, headers=peer)
    assert review.status_code == 200, review.text
    task = client.get(f"/channels/room/tasks/task:msg-{root['seq']}", headers=lead)
    assert task.status_code == 200
    assert task.json()["reviews"]["rows"][0]["verdict"] == "approve"


def test_cross_channel_artifact_identity_includes_channel_even_for_identical_bytes(lab):
    import base64

    hub, owner, _, lead, peer_a, _, _, _, root, task = lab
    text = "same logical bytes\n"
    local = _artifact(hub, lead, text)
    hub.create_channel(owner, "other", private=False)
    hub.join_channel(lead, "other", None)
    hub.join_channel(peer_a, "other", None)
    foreign = hub.fs_write(lead, "other", "ROADMAP.md",
                           content_b64=base64.b64encode(text.encode()).decode(),
                           expect_version=0)
    foreign_evidence = [{"kind": "fs", "channel": "other", "ref": "ROADMAP.md@1",
                         "sha256": foreign.sha256}]
    _review(hub, peer_a, task, "approve", foreign_evidence)
    assert foreign.sha256 == local[0]["sha256"]
    with pytest.raises(HubError, match="approval"):
        _deliver(hub, lead, root, local)


def test_review_retraction_serializes_with_final_validation_and_delivery_insert(lab, monkeypatch):
    hub, _, _, lead, peer_a, _, _, _, root, task = lab
    artifacts = _artifact(hub, lead)
    approval = _review(hub, peer_a, task, "approve", artifacts)
    final_validation, release_delivery = Event(), Event()
    retraction_attempted, db_retraction_entered = Event(), Event()
    real_validate = hub._validate_review_delivery
    real_retract = hub.db.retract_message
    calls = 0

    def hold_after_validation(*args, **kwargs):
        nonlocal calls
        result = real_validate(*args, **kwargs)
        calls += 1
        # post_message checks once before and once inside the lock.  Only the
        # latter is its final validation/append critical section.
        if calls == 2:
            final_validation.set()
            assert release_delivery.wait(2), "test did not release delivery"
        return result

    monkeypatch.setattr(hub, "_validate_review_delivery", hold_after_validation)

    def mark_db_retraction(*args, **kwargs):
        db_retraction_entered.set()
        return real_retract(*args, **kwargs)

    monkeypatch.setattr(hub.db, "retract_message", mark_db_retraction)

    def retract():
        retraction_attempted.set()
        return hub.retract_message(peer_a, "room", approval.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        delivery = executor.submit(_deliver, hub, lead, root, artifacts)
        assert final_validation.wait(2), "delivery did not reach its final review check"
        retraction = executor.submit(retract)
        assert retraction_attempted.wait(2), "retraction thread did not start"
        # The delivery owns the orchestration lock through its final check and
        # insert; a review retraction cannot even reach its database write in
        # that interval.  The DB hook makes the missing-lock mutation falsify
        # this assertion rather than relying on Future scheduling alone.
        assert not db_retraction_entered.wait(.25)
        release_delivery.set()
        delivered = delivery.result(timeout=2)
        retraction.result(timeout=2)

    assert hub.db.store_get("room", task).value["report"] == f"room#{delivered.seq}"
    assert hub.task_review_summary("room", task)["rows"][0]["verdict"] == "withdraw"
