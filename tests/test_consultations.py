"""Collect perspectives before deciding; exercise real storage and boundaries."""
import json
import time
from types import SimpleNamespace

import pytest

from agora.db import Database
from agora.hub.ratelimit import RateLimiter
from agora.hub.service import HubError, HubService
from agora.models import PostMessage, Status


@pytest.fixture
def lab(tmp_path):
    path = tmp_path / "hub.db"
    hub = HubService(Database(str(path)), rate_per_minute=600000)
    hub.ratelimiter = RateLimiter(rate_per_minute=600000, burst=1000)
    seats = {}
    for name in ("owner", "x", "y", "z", "w", "operator"):
        seats[name], _ = hub.register_agent(name, name, operator=name == "operator")
    hub.create_channel(seats["owner"], "room", private=False)
    for name in seats.keys() - {"owner"}:
        hub.join_channel(seats[name], "room", None)
    yield hub, seats, path
    hub.db.close()


def question(lab, **policy):
    hub, seats, _ = lab
    return hub.post_message(seats["owner"], "room", PostMessage(
        title="Resolve chronology", body="Which interpretation fits the current passages?",
        status=Status.open, asks=[{"id": "chronology", "text": "Examine the affected passages",
                                  "to": ["x", "y", "z", "w"]}],
        data={"consultation": {"action": "Commit the chronology repair", "required": ["x", "y"],
                               "eligible": ["x", "y", "z"], **policy}}))


def respond(lab, root, seat, *, decline=False, ack=False):
    hub, seats, _ = lab
    fields = {} if ack else {"declines" if decline else "answers": ["chronology"]}
    return hub.post_message(seats[seat], "room", PostMessage(
        body="The two passages describe different events; a flashback will not reconcile them.",
        status=Status.fyi if ack else Status.reply, reply_to=root.id, **fields))


def state(lab, root):
    hub, seats, _ = lab
    return hub.consultation_state(seats["owner"], "room", root.id)


def conclude(lab, root, **kwargs):
    hub, seats, _ = lab
    return hub.conclude_consultation(seats["owner"], "room", root.id,
        kwargs.get("outcome", "decided"), kwargs.get("version", state(lab, root)["version"]),
        "Preserve two incidents: the artifact evidence rules out a single flashback.")


@pytest.mark.parametrize("order", [("x", "y"), ("y", "x")])
def test_named_participants_not_first_reply_and_disagreement_is_participation(lab, order):
    root = question(lab)
    respond(lab, root, order[0])
    assert state(lab, root)["status"] == "collecting"
    with pytest.raises(HubError, match="collection conditions"):
        conclude(lab, root)
    respond(lab, root, order[1])
    assert state(lab, root)["ready"]
    conclude(lab, root)
    assert state(lab, root)["status"] == "decided"


def test_distinct_seats_optional_answers_and_acknowledgements(lab):
    root = question(lab, min_responses=3)
    for _ in range(4):
        respond(lab, root, "x")
    respond(lab, root, "y", ack=True)
    respond(lab, root, "w")  # welcome evidence, outside declared eligible set
    assert state(lab, root)["response_count"] == 1
    assert not state(lab, root)["ready"]
    respond(lab, root, "z")
    assert state(lab, root)["missing_required"] == ["y"]
    respond(lab, root, "y")
    assert state(lab, root)["ready"]


def test_decline_is_visible_and_does_not_supply_a_perspective(lab):
    root = question(lab)
    respond(lab, root, "x")
    respond(lab, root, "y", decline=True)
    snapshot = state(lab, root)
    assert snapshot["status"] == "incomplete"
    assert snapshot["declined"] == ["y"] and snapshot["missing_required"] == ["y"]
    with pytest.raises(HubError):
        conclude(lab, root)
    respond(lab, root, "y")
    assert state(lab, root)["ready"]


@pytest.mark.parametrize("timeout,expected,ready", [("incomplete", "incomplete", False), ("proceed", "timed_out", True)])
def test_deadline_is_explicit_missing_feedback_not_consent(lab, monkeypatch, timeout, expected, ready):
    deadline = time.time() + 300
    root = question(lab, deadline=deadline, on_timeout=timeout,
                    **({"required": [], "min_responses": 2} if timeout == "proceed" else {}))
    respond(lab, root, "x")
    monkeypatch.setattr("agora.hub.consultations.time", SimpleNamespace(time=lambda: deadline))
    snapshot = state(lab, root)
    assert snapshot["status"] == expected and snapshot["ready"] == ready
    assert "y" in snapshot["missing_eligible"] and not snapshot["participation_complete"]
    assert snapshot["missing_required"] == ([] if ready else ["y"])
    if ready:
        decision = conclude(lab, root)
        assert decision.data["consultation_conclusion"]["collection"] == "timed_out"


def test_minimum_window_holds_even_after_all_answers(lab, monkeypatch):
    release = time.time() + 300
    root = question(lab, not_before=release, deadline=release + 60)
    respond(lab, root, "x"); respond(lab, root, "y")
    assert state(lab, root)["participation_complete"] and not state(lab, root)["ready"]
    assert state(lab, root)["next_event_at"] == release
    monkeypatch.setattr("agora.hub.consultations.time", SimpleNamespace(time=lambda: release))
    assert state(lab, root)["ready"]


def test_advisory_count_deadline_never_waives_required_participants(lab, monkeypatch):
    deadline = time.time() + 300
    root = question(lab, min_responses=3, deadline=deadline, on_timeout="proceed")
    respond(lab, root, "x")
    monkeypatch.setattr("agora.hub.consultations.time", SimpleNamespace(time=lambda: deadline))
    assert state(lab, root)["status"] == "incomplete"
    with pytest.raises(HubError):
        conclude(lab, root)
    respond(lab, root, "y")
    assert state(lab, root)["status"] == "timed_out" and state(lab, root)["ready"]
    assert state(lab, root)["response_count"] == 2 and state(lab, root)["missing_required"] == []


def test_snapshot_refuses_late_change_then_reopens_recorded_decision(lab):
    root = question(lab)
    respond(lab, root, "x"); respond(lab, root, "y")
    old = state(lab, root)
    respond(lab, root, "x")
    with pytest.raises(HubError, match="changed"):
        conclude(lab, root, version=old["version"])
    first = conclude(lab, root)
    respond(lab, root, "y")
    assert state(lab, root)["status"] == "needs_reconciliation"
    second = conclude(lab, root)
    assert second.id != first.id and state(lab, root)["status"] == "decided"


def test_artifact_binding_and_unrelated_edits(lab):
    hub, seats, _ = lab
    file = hub.fs_write(seats["owner"], "room", "passages.md", "two dated incidents", expect_version=0)
    root = question(lab, artifacts=[{"kind": "fs", "ref": f"passages.md@{file.version}"}])
    respond(lab, root, "x"); respond(lab, root, "y")
    assert state(lab, root)["ready"]
    hub.fs_write(seats["owner"], "room", "unrelated.md", "layout", expect_version=0)
    assert state(lab, root)["ready"]
    hub.fs_write(seats["owner"], "room", "passages.md", "materially changed dates", expect_version=file.version)
    assert state(lab, root)["status"] == "stale"
    with pytest.raises(HubError):
        conclude(lab, root)


def test_only_owner_or_existing_closure_authority_can_decide_and_cancel(lab):
    hub, seats, _ = lab
    root = question(lab)
    snapshot = state(lab, root)
    with pytest.raises(HubError, match="only the requester"):
        hub.conclude_consultation(seats["x"], "room", root.id, "cancelled", snapshot["version"], "Ignore my colleagues")
    with pytest.raises(HubError, match="use conclude_consultation"):
        hub.post_message(seats["owner"], "room", PostMessage(body="I choose now", status=Status.resolved, reply_to=root.id))
    with pytest.raises(HubError, match="reserved"):
        hub.post_message(seats["x"], "room", PostMessage(body="forged", data={"consultation_conclusion": {"outcome": "decided"}}))
    conclude(lab, root, outcome="cancelled")
    respond(lab, root, "x"); respond(lab, root, "y")
    assert state(lab, root)["status"] == "cancelled"
    with pytest.raises(HubError):
        conclude(lab, root)


def test_optional_membership_change_does_not_invalidate_reconciled_evidence(lab):
    hub, seats, _ = lab
    root = question(lab)
    respond(lab, root, "x")
    respond(lab, root, "y")
    conclude(lab, root)
    before = state(lab, root)
    hub.db.remove_member("room", "z")
    assert state(lab, root)["status"] == "decided"
    assert state(lab, root)["version"] == before["version"]
    hub.join_channel(seats["z"], "room", None)
    assert state(lab, root)["version"] == before["version"]


def test_crash_between_timer_notice_append_and_notification_recovers_delivery(lab, monkeypatch):
    hub, _, _ = lab
    root = question(lab)
    respond(lab, root, "x")
    respond(lab, root, "y")
    delivered = []
    def crash(message):
        raise RuntimeError("crash after append before notification")
    monkeypatch.setattr(hub, "_wake", crash)
    with pytest.raises(RuntimeError, match="before notification"):
        hub._consultation_sweep()
    events = [m for m in hub.db.get_messages("room") if (m.data or {}).get("consultation_event")]
    assert len(events) == 1
    assert hub.db.meta_get("consultation-notified:" + root.id) is None
    monkeypatch.setattr(hub, "_wake", lambda message: delivered.append(message.id))
    assert hub._consultation_sweep() == [root.id]
    assert delivered == [events[0].id]
    assert hub._consultation_sweep() == []
    assert delivered == [events[0].id]
    assert len([m for m in hub.db.get_messages("room") if (m.data or {}).get("consultation_event")]) == 1


def test_notification_is_durable_deduplicated_and_does_not_create_reply_debt(lab):
    hub, seats, path = lab
    root = question(lab)
    respond(lab, root, "x")
    assert hub._consultation_sweep() == []
    respond(lab, root, "y")
    before = state(lab, root)
    assert hub._consultation_sweep() == [root.id]
    assert hub._consultation_sweep() == []
    events = [m for m in hub.db.get_messages("room") if (m.data or {}).get("consultation_event")]
    assert len(events) == 1 and events[0].status == Status.fyi and events[0].urgency.value == "next_turn"
    assert not any(r.id == events[0].id for r in hub.owed(seats["owner"]).to_answer)
    hub.db.close()
    hub.db = Database(str(path))
    restarted = HubService(hub.db, rate_per_minute=600000)
    assert restarted.consultation_state(seats["owner"], "room", root.id) == before
    assert restarted._consultation_sweep() == []
    assert hub.db.verify_channel("room")["ok"]


@pytest.mark.parametrize("policy", [
    {"required": ["x", "x"]}, {"required": ["w"]}, {"eligible": ["owner"]},
    {"min_responses": True}, {"min_responses": 9}, {"deadline": -1},
    {"not_before": 10, "deadline": 5}, {"on_timeout": "approve"},
    {"on_timeout": "proceed"}, {"unexpected": 3}, {"artifacts": "passages.md"},
])
def test_bad_policies_fail_before_any_question_is_posted(lab, policy):
    hub, _, _ = lab
    count = len(hub.db.consultation_messages())
    with pytest.raises(HubError):
        question(lab, **policy)
    assert len(hub.db.consultation_messages()) == count


def test_browsing_does_not_consume_and_private_question_is_not_visible(lab):
    hub, seats, _ = lab
    root = question(lab)
    answer = respond(lab, root, "x")
    state(lab, root)
    assert not hub.db.has_read(answer.id, "owner")
    hub.db.remove_member("room", "w")
    with pytest.raises(HubError):
        hub.consultation_state(seats["w"], "room", root.id)
    assert "different events" not in json.dumps(hub.reply_state(seats["owner"], "room", root.id))


def test_retracting_latest_answer_does_not_resurrect_an_older_perspective(lab):
    hub, seats, _ = lab
    root = question(lab)
    respond(lab, root, "x")
    newer = respond(lab, root, "x")
    respond(lab, root, "y")
    conclude(lab, root)
    hub.retract_message(seats["x"], "room", newer.id)
    snapshot = state(lab, root)
    assert snapshot["status"] == "needs_reconciliation"
    assert snapshot["missing_required"] == ["x"] and snapshot["withdrawn"] == ["x"]
    assert not snapshot["ready"]
    assert "different events" not in json.dumps(snapshot)


def test_withdrawn_conclusion_does_not_restore_a_previous_decision(lab):
    hub, seats, _ = lab
    root = question(lab)
    respond(lab, root, "x"); respond(lab, root, "y")
    conclude(lab, root)
    respond(lab, root, "x")
    newer = conclude(lab, root)
    hub.retract_message(seats["owner"], "room", newer.id)
    assert state(lab, root)["status"] == "needs_reconciliation"
    assert state(lab, root)["conclusion"] == newer.id


def test_delivery_dependency_is_explicit_and_does_not_freeze_execution(lab):
    hub, seats, _ = lab
    source = hub.post_message(seats["operator"], "room", PostMessage(
        title="Commission", body="Deliver the full corrected manuscript", status=Status.open, to=["owner"]))
    key = f"task:msg-{source.seq}"
    root = question(lab, task={"channel": "room", "key": key})
    row = hub.db.store_get("room", key)
    # Traceability alone never gives the question's author a task-wide veto.
    assert hub.task_context(seats["owner"], "room", key)["consultations"] == []
    hub.store_set(seats["operator"], "room", key,
        {**row.value, "consultations": [root.id]}, expect_version=row.version)
    assert hub.task_context(seats["owner"], "room", key)["ready"]
    assert hub.prepare_task_delivery(seats["owner"], "room", key)["blockers"][0]["status"] == "collecting"
    hub.fs_write(seats["owner"], "room", "result.md", "The complete book", expect_version=0)
    report = PostMessage(body="Finished the complete corrected book", status=Status.resolved, reply_to=source.id,
                         data={"evidence": [{"kind": "fs", "ref": "result.md@1"}]})
    with pytest.raises(HubError, match="declared consultations"):
        hub.post_message(seats["owner"], "room", report)
    row = hub.db.store_get("room", key)
    with pytest.raises(HubError, match="task row is written"):
        hub.store_set(seats["z"], "room", key, {**row.value, "consultations": []}, expect_version=row.version)
    respond(lab, root, "x"); respond(lab, root, "y")
    # Participation alone still is not a decision, so delivery stays pending.
    assert hub.prepare_task_delivery(seats["owner"], "room", key)["blockers"][0]["status"] == "ready"
    conclude(lab, root)
    assert "post_message" in hub.prepare_task_delivery(seats["owner"], "room", key)
    # New evidence after synthesis requires reconciliation before publication.
    respond(lab, root, "x")
    with pytest.raises(HubError, match="needs_reconciliation"):
        hub.post_message(seats["owner"], "room", report)
    conclude(lab, root)
    hub.post_message(seats["owner"], "room", report)
    assert hub.db.store_get("room", key).value["status"] == "delivered"


def test_many_followups_cannot_hide_the_last_required_respondent(lab):
    root = question(lab)
    for _ in range(70):
        respond(lab, root, "x")
    assert state(lab, root)["response_count"] == 1
    respond(lab, root, "y")
    assert state(lab, root)["ready"] and state(lab, root)["response_count"] == 2


def test_participant_completion_refusal_explains_a_usable_answer_route(lab):
    hub, seats, _ = lab
    root = question(lab)
    with pytest.raises(HubError, match="status=reply"):
        hub.post_message(seats["x"], "room", PostMessage(body="Completed the review",
            status=Status.resolved, reply_to=root.id, answers=["chronology"]))
    respond(lab, root, "x")
    assert state(lab, root)["answered"] == ["x"]


def test_conclusion_and_new_answer_serialize_without_a_falsely_current_decision(lab):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    root = question(lab)
    respond(lab, root, "x"); respond(lab, root, "y")
    version = state(lab, root)["version"]
    barrier = Barrier(2)
    def decide():
        barrier.wait()
        try:
            conclude(lab, root, version=version)
            return True
        except HubError as exc:
            assert exc.status_code == 409
            return False
    def revise():
        barrier.wait()
        respond(lab, root, "y")
    with ThreadPoolExecutor(2) as pool:
        decision, revision = pool.submit(decide), pool.submit(revise)
        accepted = decision.result()
        revision.result()
    assert state(lab, root)["status"] == ("needs_reconciliation" if accepted else "ready")


@pytest.mark.asyncio
async def test_fast_watchdog_handles_consultations_even_when_vote_sweep_fails(lab, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    hub, _, _ = lab
    calls = []
    def broken_vote():
        calls.append("vote")
        raise RuntimeError("independent vote failure")
    monkeypatch.setattr(hub, "vote_sweep", broken_vote)
    monkeypatch.setattr(hub, "_consultation_sweep", lambda: calls.append("consultation"))
    monkeypatch.setattr(hub, "_mission_change_sweep", lambda: calls.append("mission"))
    sleeps = []
    async def sleep(interval):
        sleeps.append(interval)
        if len(sleeps) > 1:
            raise asyncio.CancelledError()
    async def run(function):
        return function()
    monkeypatch.setattr("agora.hub.service.asyncio", SimpleNamespace(sleep=sleep, to_thread=run))
    with pytest.raises(asyncio.CancelledError):
        await hub.vote_watchdog()
    assert calls == ["vote", "consultation", "mission"] and sleeps == [30.0, 30.0]
