"""Declared social expectations and goal/work/review provenance stay connected."""
import json

import pytest

from agora.hub.collaboration_graph import collaboration_graph
from agora.hub.service import HubError, HubService
from agora.models import PostMessage, Status
from test_consultations import lab, question, respond, state, conclude
from test_task_orchestration import lab as task_lab, task, update


def test_task_parent_is_decomposition_not_an_execution_prerequisite(task_lab):
    hub, seats = task_lab
    parent = task(hub, seats, "release")
    child = task(hub, seats, "swarm")
    update(hub, seats["operator"], child, parent=parent, purpose="Verify the recovery path required by the release")
    assert hub.task_context(seats["worker"], **child)["ready"]
    # Parent waiting on child acceptance is useful and does not form a
    # decomposition cycle. A reverse parent link really would form a cycle.
    update(hub, seats["operator"], parent, depends_on=[child])
    with pytest.raises(HubError, match="parent links must be acyclic"):
        update(hub, seats["operator"], parent, parent=child)
    graph = collaboration_graph(hub, seats["worker"], "swarm")
    assert any(e["relation"] == "contributes_to" and e["target"] == "store:release/" + parent["key"]
               for e in graph["edges"])
    hub.db.remove_member("release", "worker")
    assert hub.task_context(seats["worker"], **child)["parent"] == {"unavailable": True}
    graph = collaboration_graph(hub, seats["worker"], "swarm")
    assert "store:release/" not in json.dumps(graph)
    assert graph["unavailable_references"] > 0


def test_current_shared_plan_remains_connected_outside_the_message_page(lab):
    hub, seats, _ = lab
    root = question(lab)
    hub.store_set(seats["owner"], "room", "plan:chronology", {
        "owner": "owner", "source_message_id": f"room#{root.seq}",
        "purpose": "Keep the consumer's timeline consistent with both passages",
        "status": "agreed"}, expect_version=0)
    graph = collaboration_graph(hub, seats["x"], "room", since_seq=root.seq)
    plan = next(n for n in graph["nodes"] if n["id"] == "store:room/plan:chronology")
    assert plan["kind"] == "plan" and plan["status"] == "agreed"
    assert {"source": plan["id"], "relation": "responds_to", "target": "message:" + root.id} in graph["edges"]


@pytest.mark.parametrize("event_type", ["consultation", "mission"])
def test_real_trigger_notification_wakes_recipient_without_obliging_bystanders(lab, event_type):
    from agora.hub.notify_sink import NotifySink
    from agora.listen import parse_line, qualifies
    hub, seats, path = lab
    directory = path.parent / "notifications"
    hub.notify_sink = NotifySink(directory)
    if event_type == "consultation":
        root = question(lab)
        respond(lab, root, "x")
        respond(lab, root, "y")
        hub._consultation_sweep()
        channel, recipient, field = "room", "owner", "consultation_event"
    else:
        hub.set_mission("x", "Verify the changed commission against the complete result")
        channel, recipient, field = "commons", "x", "mission_changed"
    notice = next(m for m in hub.db.get_messages(channel) if (m.data or {}).get(field))
    for who, expected in ((recipient, True), ("z", False)):
        lines = [parse_line(line) for line in (directory / f"{who}-inbox.log").read_text().splitlines()]
        event = next(e for e in lines if e and e.get("id") == notice.id)
        assert qualifies(event, who, important_only=True) is expected
        assert not any(r.id == notice.id for r in hub.owed(seats[who]).to_answer)


def test_social_expectations_are_personal_and_roles_are_existing_facts(lab):
    hub, seats, _ = lab
    hub.set_about(seats["x"], "Ask me about chronology and contradictions")
    hub.set_note(seats["owner"], "x", "Checks both passages before proposing a repair")
    own = collaboration_graph(hub, seats["owner"], "room")
    other = collaboration_graph(hub, seats["y"], "room")
    assert any(n.get("about") == "Ask me about chronology and contradictions" for n in own["nodes"])
    assert any(e["relation"] == "expects_from" and e["private"] for e in own["edges"])
    assert not any(e["relation"] == "expects_from" for e in other["edges"])
    assert "Checks both passages" not in json.dumps(other)


def test_graph_links_request_question_action_answer_and_conclusion_without_dangling_edges(lab):
    hub, seats, _ = lab
    root = question(lab)
    respond(lab, root, "x"); respond(lab, root, "y")
    decision = conclude(lab, root)
    graph = collaboration_graph(hub, seats["owner"], "room", limit=1000)
    relations = {e["relation"] for e in graph["edges"]}
    assert {"authored", "asks", "answers", "concludes", "requests_input", "requires_perspective", "requires_consultation"} <= relations
    ids = {n["id"] for n in graph["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in graph["edges"])
    assert any(n.get("status") == "decided" and n["kind"] == "decision" for n in graph["nodes"])
    page = collaboration_graph(hub, seats["owner"], "room", limit=1)
    assert page["messages"]["next_since_seq"] is not None
    assert any(n.get("status") == "decided" for n in page["nodes"])
    assert not hub.db.has_read(decision.id, "x")


def test_latest_whole_task_review_is_visible_outside_message_page(task_lab):
    hub, seats = task_lab
    ref = task(hub, seats, "release")
    hub.fs_write(seats["manager"], "release", "result.md", "a complete integrated result", expect_version=0)
    review = hub.review_task(seats["peer"], **ref, verdict="approve",
        artifacts=[{"kind": "fs", "ref": "result.md@1"}], title="Checked complete scope", body="Checked all original requirements")
    graph = collaboration_graph(hub, seats["worker"], "release", limit=1)
    assert any(e["source"] == f"message:{review.id}" and e["relation"] == "reviews" for e in graph["edges"])
    assert any(e["relation"] == "reviews_artifact" for e in graph["edges"])


def test_briefing_reports_collection_and_colleague_routing_without_reading_answers(lab):
    hub, seats, _ = lab
    hub.set_about(seats["y"], "Continuity review")
    root = question(lab)
    reply = respond(lab, root, "x")
    briefing = hub.briefing(seats["owner"])
    pending = briefing["sections"]["consultations"][0]
    assert pending["status"] == "collecting" and pending["missing_required"] == ["y"]
    assert pending["read"]["tool"] == "get_consultation"
    assert any(r["seat"] == "y" for r in briefing["sections"]["colleagues"])
    assert not hub.db.has_read(reply.id, "owner")
    assert len(json.dumps(briefing).encode()) < 12000


def test_mission_change_is_a_durable_targeted_trigger_and_noop_is_silent(lab, monkeypatch):
    hub, seats, _ = lab
    original = hub._post_system
    def crash(*args, **kwargs):
        raise RuntimeError("crash before notice")
    monkeypatch.setattr(hub, "_post_system", crash)
    with pytest.raises(RuntimeError):
        hub.set_mission("x", "New commission: verify final output against all requirements")
    assert hub.db.get_mission("x").startswith("New commission")
    assert hub.db.meta_get("mission-change:x")
    monkeypatch.setattr(hub, "_post_system", original)
    restarted = HubService(hub.db, rate_per_minute=600000)
    assert restarted._mission_change_sweep() == ["x"]
    events = [m for m in hub.db.get_messages("commons") if (m.data or {}).get("mission_changed")]
    assert len(events) == 1 and events[0].to == ["x"]
    assert events[0].urgency.value == "next_turn" and events[0].status == Status.fyi
    assert "New commission" not in events[0].body
    restarted.set_mission("x", hub.db.get_mission("x"))
    assert restarted._mission_change_sweep() == []


def test_mission_notification_crash_after_publication_does_not_duplicate(lab, monkeypatch):
    hub, _, _ = lab
    hub.db.set_mission("x", "Changed charge", notify=True)
    original = hub.db.meta_delete
    def crash(*args, **kwargs):
        raise RuntimeError("crash after notice")
    monkeypatch.setattr(hub.db, "meta_delete", crash)
    with pytest.raises(RuntimeError):
        hub._mission_change_sweep()
    monkeypatch.setattr(hub.db, "meta_delete", original)
    assert hub._mission_change_sweep() == ["x"]
    events = [m for m in hub.db.get_messages("commons") if (m.data or {}).get("mission_changed")]
    assert len(events) == 1


def test_mission_notice_recovers_notification_after_ledger_append(lab, monkeypatch):
    hub, _, _ = lab
    hub.db.set_mission("x", "Changed charge", notify=True)
    def crash(message):
        raise RuntimeError("crash before notification")
    monkeypatch.setattr(hub, "_wake", crash)
    with pytest.raises(RuntimeError, match="before notification"):
        hub._mission_change_sweep()
    delivered = []
    monkeypatch.setattr(hub, "_wake", lambda message: delivered.append(message.id))
    assert hub._mission_change_sweep() == ["x"]
    events = [m for m in hub.db.get_messages("commons") if (m.data or {}).get("mission_changed")]
    assert len(events) == 1 and delivered == [events[0].id]
    assert hub.db.meta_get("mission-change:x") is None
    assert hub._mission_change_sweep() == []


def test_graph_explains_trigger_cause_and_recipient(lab):
    hub, seats, _ = lab
    root = question(lab)
    respond(lab, root, "x"); respond(lab, root, "y")
    hub._consultation_sweep()
    event = next(m for m in hub.db.get_messages("room") if (m.data or {}).get("consultation_event"))
    hub.post_message(seats["owner"], "room", PostMessage(
        status=Status.fyi, reply_to=event.id, body="The current consultation is under review."))
    graph = collaboration_graph(hub, seats["owner"], "room", limit=1000)
    trigger = next(n["id"] for n in graph["nodes"] if n["kind"] == "trigger")
    assert {"source": trigger, "relation": "reconsiders", "target": "message:" + root.id} in graph["edges"]
    assert {"source": trigger, "relation": "notifies", "target": "seat:owner"} in graph["edges"]
