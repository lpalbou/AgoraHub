"""Executable contracts for shared task ownership, routing and bounded context."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agora.db import Database
from agora.hub.service import HubError, HubService
from agora.models import PostMessage, Status


@pytest.fixture
def lab():
    hub = HubService(Database(":memory:"), rate_per_minute=6000)
    seats = {}
    for name in ("operator", "manager", "director", "worker", "peer"):
        seats[name], _ = hub.register_agent(name, name, operator=name == "operator", mission=name)
    for channel in ("release", "swarm", "agora-work"):
        hub.create_channel(seats["operator"], channel, private=False)
        for name, agent in seats.items():
            if name != "operator":
                hub.join_channel(agent, channel, None)
    yield hub, seats
    hub.db.close()


def task(hub, seats, channel):
    root = hub.post_message(seats["operator"], channel, PostMessage(
        body="Deliver the requested outcome with evidence", title=channel,
        status=Status.open, to=["manager"]))
    key = f"task:msg-{root.seq}"
    row = hub.db.store_get(channel, key)
    hub.store_set(seats["operator"], channel, key, {**row.value,
        "primary_channel": channel, "coordinator": "manager", "director": "director",
        "work_type": channel}, expect_version=row.version)
    return {"channel": channel, "key": key}


def update(hub, agent, ref, **fields):
    row = hub.db.store_get(**ref)
    return hub.store_set(agent, **ref, value={**row.value, **fields}, expect_version=row.version)


def test_source_is_immutable_and_requester_can_still_accept(lab):
    hub, seats = lab
    ref = task(hub, seats, "agora-work")
    own = hub.post_message(seats["manager"], "agora-work", PostMessage(body="my root", status=Status.fyi))
    with pytest.raises(HubError, match="immutable"):
        update(hub, seats["manager"], ref, source=f"agora-work#{own.seq}", status="accepted")
    update(hub, seats["operator"], ref, status="accepted")
    assert hub.db.store_get(**ref).value["requester"] == "operator"


def test_one_primary_task_and_live_assignees(lab):
    hub, seats = lab
    first = task(hub, seats, "agora-work")
    with pytest.raises(HubError, match="already has"):
        task(hub, seats, "agora-work")
    for field in ("work_type", "primary_channel"):
        with pytest.raises(HubError, match="immutable"):
            update(hub, seats["operator"], first, **{field: None})
    for field in ("coordinator", "director"):
        with pytest.raises(HubError, match="current member"):
            update(hub, seats["operator"], first, **{field: "missing"})


def test_dependencies_require_acceptance_and_reject_cycle(lab):
    hub, seats = lab
    a, b, c = [task(hub, seats, ch) for ch in ("release", "swarm", "agora-work")]
    update(hub, seats["operator"], b, depends_on=[a])
    update(hub, seats["operator"], c, depends_on=[b])
    assert not hub.task_context(seats["worker"], **b)["ready"]
    for ref, deps in ((a, [c]), (a, [a])):
        with pytest.raises(HubError, match="acyclic"):
            update(hub, seats["operator"], ref, depends_on=deps)
    with pytest.raises(HubError, match="does not exist"):
        update(hub, seats["operator"], a, depends_on=[{**b, "key": "task:missing"}])
    # A delivered report is insufficient. This DB setup models the hub's
    # stamped delivery; only the requester's verdict unlocks dependent work.
    row = hub.db.store_get(**a)
    hub.db.store_set(**a, value={**row.value, "status": "delivered"}, updated_by="agora-work", expect_version=row.version)
    assert not hub.task_context(seats["worker"], **b)["ready"]
    update(hub, seats["operator"], a, status="accepted")
    assert hub.task_context(seats["worker"], **b)["ready"]
    assert not hub.task_context(seats["worker"], **c)["ready"]


def test_concurrent_opposite_edges_cannot_both_commit(lab):
    hub, seats = lab
    a, b = task(hub, seats, "release"), task(hub, seats, "swarm")
    barrier = Barrier(2)
    def write(ref, dep):
        barrier.wait()
        try:
            update(hub, seats["operator"], ref, depends_on=[dep])
            return "committed"
        except HubError as exc:
            assert "acyclic" in str(exc)
            return "refused"
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(write, a, b), pool.submit(write, b, a)]
        assert sorted(f.result() for f in futures) == ["committed", "refused"]


def test_route_resolves_role_with_cas_and_no_side_recipient(lab):
    hub, seats = lab
    ref = task(hub, seats, "agora-work")
    version = hub.task_context(seats["worker"], **ref)["version"]
    msg = PostMessage(body="Resolve the conflicting requirements", title="Choose a contract",
                      status=Status.open, asks=[{"id": "1", "text": "Which contract?"}])
    posted = hub.route_task(seats["worker"], **ref, role="manager", expect_version=version, message=msg)
    assert posted.to == ["manager"] and posted.data["asks"][0]["to"] == ["manager"]
    fyi = hub.route_task(seats["worker"], **ref, role="director", expect_version=version,
                         message=PostMessage(body="Evidence changed", status=Status.fyi))
    assert fyi.to == ["director"] and not hub.owed(seats["director"]).to_answer
    for data in ({"asks": [{"id": "2", "text": "do extra", "to": ["peer"]}]},
                 {"data": {"asks": [{"id": "2", "text": "do extra", "to": ["peer"]}]}}):
        with pytest.raises(HubError, match="recipients"):
            hub.route_task(seats["worker"], **ref, role="manager", expect_version=version,
                           message=PostMessage(body="bad", status=Status.open, **data))
    update(hub, seats["operator"], ref, coordinator="peer")
    with pytest.raises(HubError, match="changed"):
        hub.route_task(seats["worker"], **ref, role="manager", expect_version=version, message=msg)
    current = hub.task_context(seats["worker"], **ref)
    routed = hub.route_task(seats["worker"], **ref, role="manager", expect_version=current["version"], message=msg)
    assert routed.to == ["peer"]
    hub.db.remove_member("agora-work", "peer")
    with pytest.raises(HubError, match="reachable"):
        hub.route_task(seats["worker"], **ref, role="manager", expect_version=current["version"], message=msg)


def test_briefing_is_private_bounded_and_reports_manager_state_without_posts(lab):
    hub, seats = lab
    task(hub, seats, "release")
    hub.create_channel(seats["operator"], "secret", private=True)
    secret = hub.post_message(seats["operator"], "secret", PostMessage(body="secret-value", status=Status.open))
    for i in range(30):
        hub.store_set(seats["worker"], "release", f"claim:{i:02}",
                      {"owner": "worker", "status": "working", "next_step": "test seam " + str(i)}, expect_version=0)
    before = hub.db.get_channel("release").model_dump()
    brief = hub.briefing(seats["director"])
    assert brief["omitted"]["claims"] == 18
    assert brief["sections"]["claims"][0]["owner"] == "worker"
    assert "secret" not in json.dumps(brief) and secret.id not in json.dumps(brief)
    assert len(json.dumps(brief, ensure_ascii=False).encode()) <= 12000
    assert brief == hub.briefing(seats["director"])
    assert before == hub.db.get_channel("release").model_dump()
    row = hub.db.store_get("release", "claim:00")
    hub.store_set(seats["worker"], "release", "claim:00", {**row.value, "next_step": "new discriminating check"}, expect_version=row.version)
    changed = hub.briefing(seats["director"])
    assert changed["sections"]["claims"][0]["next_step"] == "new discriminating check"


def test_fyi_has_no_reply_debt_ask_does_and_critical_requires_read(lab):
    hub, seats = lab
    for name in ("operator", "peer"):
        hub.post_message(seats[name], "agora-work", PostMessage(body="Useful evidence", status=Status.fyi, to=["worker"]))
    assert hub.owed(seats["worker"]).to_answer == []
    ask = hub.post_message(seats["peer"], "agora-work", PostMessage(body="Test this assumption", status=Status.open,
                           asks=[{"id": "1", "text": "Run the counterexample", "to": ["worker"]}]))
    assert [r.id for r in hub.owed(seats["worker"]).to_answer] == [ask.id]


def test_linked_claim_is_owned_and_driver_waits_for_real_task_state(lab, monkeypatch):
    from agora.drive import Driver
    hub, seats = lab
    upstream, downstream = task(hub, seats, "release"), task(hub, seats, "swarm")
    update(hub, seats["operator"], downstream, depends_on=[upstream])
    value = {"owner": "worker", "status": "working", "task": downstream, "next_step": "verify"}
    hub.store_set(seats["worker"], "swarm", "claim:build", value, expect_version=0)
    with pytest.raises(HubError, match="owner"):
        hub.store_set(seats["peer"], "swarm", "claim:build", {"status": "done"}, expect_version=1)
    with pytest.raises(HubError, match="owner"):
        hub.store_set(seats["peer"], "swarm", "claim:build", {"task": None}, expect_version=1)
    with pytest.raises(HubError, match="object"):
        hub.store_set(seats["peer"], "swarm", "claim:build", None, expect_version=1)
    with pytest.raises(HubError, match="owner"):
        hub.store_set(seats["peer"], "swarm", "claim:build", {"next_step": "erase state"}, expect_version=1)
    import httpx
    def read(url, **kw):
        return httpx.Response(200, json=hub.task_context(seats["worker"], **downstream),
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", read)
    monkeypatch.setattr("agora.drive._config.get_cached_key", lambda *a: "test-key")
    driver = object.__new__(Driver)
    driver.agent_id, driver.hub = "worker", "http://test"
    assert not driver._continuable("claim:build", value, "swarm")
    update(hub, seats["operator"], upstream, status="accepted")
    assert driver._continuable("claim:build", value, "swarm")


def test_reputation_uses_visible_work_type_history_and_private_notes(lab):
    hub, seats = lab
    task(hub, seats, "release")
    hub.db.reputation_cast("release", "worker", "manager", "trust", 1, "release: executed receipt checks")
    hub.set_note(seats["manager"], "worker", "release: good falsification; release#5")
    view = hub.advisors(seats["manager"], "release")
    assert view["rooms"][0]["ratings"][0]["target"] == "worker"
    assert view["rooms"][0]["ratings"][0]["score"] == 1
    assert view["private_notes"]
    assert hub.advisors(seats["peer"], "release")["private_notes"] == []
    assert hub.advisors(seats["peer"], "unknown")["rooms"] == []
    hub.db.remove_member("release", "peer")
    assert hub.advisors(seats["peer"], "release")["rooms"] == []


def test_priority_fyi_is_delivered_without_waking_other_assignees(lab):
    from agora.hub.notify_sink import notify_line
    from agora.listen import qualifies, parse_line
    hub, seats = lab
    msg = hub.post_message(seats["peer"], "swarm", PostMessage(
        title="The observed endpoint changed", body="Read the new contract before resuming",
        status=Status.fyi, urgency="interrupt", to=["worker"]))
    # Read the real delivery envelope, not a hand-authored flag dictionary.
    envelopes = hub.inbox(seats["worker"])
    envelope = next(e for e in envelopes if e.id == msg.id)
    event = parse_line(notify_line(envelope))
    assert event["urgency"] == "interrupt"
    assert qualifies(event, "worker", important_only=True)
    assert not hub.owed(seats["worker"]).to_answer
    event["flags"] = "addressed"
    assert not qualifies(event, "manager", important_only=True)


def test_decision_and_role_survive_briefing_pressure(lab):
    hub, seats = lab
    task(hub, seats, "release")
    hub.store_set(seats["worker"], "release", "gate:choice", {
        "status": "asked", "owner": "operator", "q": "Which release scope?"}, expect_version=0)
    assert hub.briefing(seats["operator"])["sections"]["decisions"][0]["q"] == "Which release scope?"
    assert "director" in hub.briefing(seats["director"])["assignments"]


@pytest.mark.parametrize("bad", [None, 7, "task"])
def test_bad_task_payload_is_controlled_error(lab, bad):
    hub, seats = lab
    ref = task(hub, seats, "release")
    with pytest.raises(HubError) as exc:
        hub.store_set(seats["operator"], **ref, value=bad, expect_version=2)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("payload", [
    {"asks": [{"id": "1", "text": "Choose", "assignee": "peer"}]},
    {"asks": [{"id": "1", "text": "Is @peer correct?"}]},
    {"body": "Consider @peer evidence"},
    {"data": {"asks": [{}]}},
])
def test_route_cannot_widen_recipients_or_crash_on_malformed_asks(lab, payload):
    hub, seats = lab
    ref = task(hub, seats, "release")
    version = hub.task_context(seats["worker"], **ref)["version"]
    message = PostMessage(**{"body": "Choose a contract", "status": "open", **payload})
    with pytest.raises(HubError) as exc:
        hub.route_task(seats["worker"], **ref, role="manager", expect_version=version, message=message)
    assert exc.value.status_code == 400
    assert not hub.owed(seats["peer"]).to_answer
