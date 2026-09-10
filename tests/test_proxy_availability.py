"""Explicit absence is independent from the actual, scoped proxy grant."""

import json

import pytest

from agora.db import Database
from agora.hub.proxy_authority import ProxyAuthorityMixin
from agora.hub.service import CHANNEL_META_KEY, HubError, HubService
from agora.models import AgentInfo, PostMessage, Status


@pytest.fixture
def lab(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr("agora.hub.proxy_authority.time.time", lambda: clock[0])
    path = tmp_path / "availability.sqlite"
    service = HubService(Database(str(path)), db_path=str(path), rate_per_minute=6000)
    assert isinstance(service, ProxyAuthorityMixin), "HubService must use the real availability contract"
    owner, _ = service.register_agent("owner", "Owner", operator=True, mission="operator")
    other, _ = service.register_agent("other", "Other", operator=True, mission="other operator")
    delegate, _ = service.register_agent("delegate", "Delegate", mission="coordinate")
    worker, _ = service.register_agent("worker", "Worker", mission="build")
    for channel in ("room", "elsewhere"):
        service.create_channel(owner, channel, private=False)
        for member in (other, delegate, worker):
            service.join_channel(member, channel, None)
    yield service, owner, other, delegate, worker, clock, path
    service.db.close()


def grant(service, delegate, scope="room", ttl=3600):
    service.set_delegation(delegate.id, ["proxy"], scope=scope, ttl_seconds=ttl)


def test_explicit_absence_and_proxy_are_both_required(lab):
    service, owner, _, delegate, _, clock, _ = lab
    assert service.availability_for(owner.id)["state"] == "unknown"
    grant(service, delegate)
    assert service.has_proxy(delegate.id, "room")
    assert not service.proxy_allowed(delegate.id, "room", owner.id)
    service.set_availability(owner, clock[0] + 300)
    assert service.proxy_allowed(delegate.id, "room", owner.id)
    service.set_availability(owner, None)
    assert service.availability_for(owner.id)["state"] == "present"
    assert not service.proxy_allowed(delegate.id, "room", owner.id)


def test_silent_or_unrelated_operator_does_not_authorize(lab):
    service, owner, other, delegate, worker, clock, _ = lab
    grant(service, delegate)
    clock[0] += 120  # no heartbeat interpretation occurs
    service.set_availability(other, clock[0] + 300)
    assert not service.proxy_allowed(delegate.id, "room", owner.id)
    assert not service.proxy_allowed(delegate.id, "room", worker.id)
    assert not service.proxy_allowed(delegate.id, "room", "missing")
    assert service.proxy_allowed(delegate.id, "room", other.id)


def test_scope_membership_and_return_are_live(lab):
    service, owner, _, delegate, _, clock, _ = lab
    grant(service, delegate)
    service.set_availability(owner, clock[0] + 300)
    assert service.proxy_allowed(delegate.id, "room", owner.id)
    assert not service.proxy_allowed(delegate.id, "elsewhere", owner.id)
    grant(service, delegate, scope="*")
    assert service.proxy_allowed(delegate.id, "elsewhere", owner.id)
    assert not service.proxy_allowed(delegate.id, "nonexistent", owner.id)
    service.db.remove_member("room", delegate.id)
    assert not service.proxy_allowed(delegate.id, "room", owner.id)


def test_absence_expiry_is_not_presence_and_never_authorizes(lab):
    service, owner, _, delegate, _, clock, _ = lab
    grant(service, delegate)
    service.set_availability(owner, clock[0] + 10)
    clock[0] += 9
    assert service.proxy_allowed(delegate.id, "room", owner.id)
    clock[0] += 1
    assert service.availability_for(owner.id)["state"] == "unknown"
    assert not service.proxy_allowed(delegate.id, "room", owner.id)


def test_cached_grant_cannot_outlive_expiry_or_revocation(lab):
    service, owner, _, delegate, _, clock, _ = lab
    service.set_availability(owner, clock[0] + 300)
    grant(service, delegate, ttl=0.25)
    assert service.proxy_allowed(delegate.id, "room", owner.id)
    clock[0] += 0.5
    assert service.has_proxy(delegate.id, "room"), "positive stale-cache control"
    assert not service.proxy_allowed(delegate.id, "room", owner.id)
    grant(service, delegate)
    assert service.proxy_allowed(delegate.id, "room", owner.id)
    service.db.delegation_revoke(delegate.id)  # simulate another writer bypassing display cache
    assert service.has_proxy(delegate.id, "room"), "positive stale-cache control"
    assert not service.proxy_allowed(delegate.id, "room", owner.id)


@pytest.mark.parametrize("bad", [True, False, "1800000100", -1, 1_800_000_000,
                                 float("inf"), float("-inf"), float("nan"), 10 ** 1000, {}, []])
def test_invalid_absence_never_changes_existing_declaration(lab, bad):
    service, owner, _, _, _, clock, _ = lab
    before = service.set_availability(owner, clock[0] + 300)
    with pytest.raises(HubError) as exc:
        service.set_availability(owner, bad)
    assert exc.value.status_code == 400
    assert service.availability_for(owner.id) == before


def test_only_actual_operator_can_declare_own_availability(lab):
    service, owner, _, delegate, _, clock, _ = lab
    for actor in (delegate, AgentInfo(id=delegate.id, name="forged", operator=True)):
        with pytest.raises(HubError) as exc:
            service.set_availability(actor, clock[0] + 300)
        assert exc.value.status_code == 403
    assert service.availability_for(owner.id)["state"] == "unknown"


@pytest.mark.parametrize("raw", ["garbage", "null", "[]", "true",
    json.dumps({"principal": "owner", "state": "away", "away_until": 1_800_000_300}),
    json.dumps({"principal": "owner", "declared_by": "owner", "declared_at": 1_800_000_000,
                "state": "present"}),
    json.dumps({"principal": "owner", "declared_by": "other", "declared_at": 1_800_000_000,
                "state": "away", "away_until": 1_800_000_300}),
    json.dumps({"principal": "owner", "declared_by": "owner", "declared_at": 1_800_000_000,
                "state": "away", "away_until": float("inf")})])
def test_malformed_or_misattributed_state_is_unknown(lab, raw):
    service, owner, _, delegate, _, _, _ = lab
    grant(service, delegate)
    service.db.meta_set(service.AVAILABILITY_PREFIX + owner.id, raw)
    assert service.availability_for(owner.id)["state"] == "unknown"
    assert not service.proxy_allowed(delegate.id, "room", owner.id)


def test_availability_survives_new_service_and_normal_operations_stay_independent(lab):
    service, owner, _, delegate, _, clock, path = lab
    recorded = service.set_availability(owner, clock[0] + 300)
    grant(service, delegate)
    reopened = HubService(Database(str(path)), db_path=str(path), rate_per_minute=6000)
    try:
        assert reopened.availability_for(owner.id) == recorded
        assert reopened.proxy_allowed(delegate.id, "room", owner.id)
    finally:
        reopened.db.close()
    service.set_availability(owner, None)
    service.set_delegation(delegate.id, ["operational"], scope="room")
    assert service.may_administer_channel(delegate, "room")
    assert not service.proxy_allowed(delegate.id, "room", owner.id)


def test_actual_gate_decision_requires_named_owner_absence(lab):
    service, owner, other, delegate, _, clock, _ = lab
    grant(service, delegate)
    pending = {"owner": owner.id, "asked_by": delegate.id,
               "status": "asked", "q": "May the draft be removed?",
               "acts": ["fs_remove"]}
    service.store_set(delegate, "room", "gate:draft", pending)
    decision = dict(pending, status="granted")
    service.set_availability(other, clock[0] + 300)
    with pytest.raises(HubError) as exc:
        service.store_set(delegate, "room", "gate:draft", decision.copy())
    assert exc.value.status_code == 403
    assert service.db.store_get("room", "gate:draft").value["status"] == "asked"
    service.set_availability(owner, clock[0] + 300)
    service.store_set(delegate, "room", "gate:draft", decision.copy())
    stored = service.db.store_get("room", "gate:draft").value
    assert stored["status"] == "granted"
    assert stored["decided_by"] == delegate.id and stored["under_proxy"] is True
    service.set_availability(owner, None)
    with pytest.raises(HubError) as exc:
        service.store_set(delegate, "room", "gate:draft", decision.copy())
    assert exc.value.status_code == 403
    # An operator's own decision is independent of absence and proxy powers.
    service.store_set(owner, "room", "gate:draft", decision.copy())
    assert service.db.store_get("room", "gate:draft").value["decided_by"] == owner.id


def test_actual_gated_act_requires_current_absence_without_prior_grant(lab):
    service, owner, other, delegate, _, clock, _ = lab
    service.fs_write(delegate, "room", "draft.md", "draft", description="draft")
    service.store_set(owner, "room", CHANNEL_META_KEY, {"gated_acts": ["fs_remove"]})
    grant(service, delegate)
    service.set_availability(other, clock[0] + 300)
    with pytest.raises(HubError) as exc:
        service.fs_delete(delegate, "room", "draft.md")
    assert exc.value.status_code == 403
    service.set_availability(owner, clock[0] + 300)
    assert service.fs_delete(delegate, "room", "draft.md") is True
    service.fs_write(delegate, "room", "returned.md", "keep", description="draft")
    service.set_availability(owner, None)
    with pytest.raises(HubError) as exc:
        service.fs_delete(delegate, "room", "returned.md")
    assert exc.value.status_code == 403
    # Owner action remains available while explicitly present.
    assert service.fs_delete(owner, "room", "returned.md") is True


def test_actual_task_acceptance_uses_immutable_requester_absence(lab):
    service, owner, other, delegate, worker, clock, _ = lab
    grant(service, delegate)
    request = service.post_message(owner, "room", PostMessage(
        body="Build the result", title="Result", status=Status.open, to=[worker.id]))
    key = f"task:msg-{request.seq}"
    service.store_set(worker, "room", "decision:result", {"result": "built"})
    service.post_message(worker, "room", PostMessage(
        body="Result is built", status=Status.resolved, reply_to=request.id,
        data={"evidence": [{"kind": "store", "ref": "decision:result"}]}))
    assert service.db.store_get("room", key).value["status"] == "delivered"
    service.set_availability(other, clock[0] + 300)
    with pytest.raises(HubError) as exc:
        service.store_set(delegate, "room", key, {"status": "accepted"})
    assert exc.value.status_code == 403
    assert service.db.store_get("room", key).value["status"] == "delivered"
    service.set_availability(owner, clock[0] + 300)
    service.store_set(delegate, "room", key, {"status": "accepted"})
    accepted = service.db.store_get("room", key).value
    assert accepted["status"] == "accepted" and accepted["decided_by"] == delegate.id
    assert accepted["requester"] == owner.id
    # A completed decision is durable; return prevents new proxy decisions.
    service.set_availability(owner, None)
    assert service.db.store_get("room", key).value["status"] == "accepted"
    assert not service.proxy_allowed(delegate.id, "room", owner.id)


def test_operator_absence_never_creates_a_proxy_grant(lab):
    service, owner, _, delegate, _, clock, _ = lab
    service.set_availability(owner, clock[0] + 300)
    assert not service.proxy_allowed(delegate.id, "room", owner.id)
    service.set_delegation(delegate.id, ["operational"], scope="room")
    assert service.may_administer_channel(delegate, "room")
    assert not service.proxy_allowed(delegate.id, "room", owner.id)


def test_completed_proxy_gate_is_durable_but_does_not_allow_new_decisions(lab):
    service, owner, _, delegate, _, clock, _ = lab
    grant(service, delegate)
    service.set_availability(owner, clock[0] + 300)
    service.fs_write(delegate, "room", "approved.md", "draft", description="draft")
    service.store_set(owner, "room", CHANNEL_META_KEY, {"gated_acts": ["fs_remove"]})
    service.store_set(delegate, "room", "gate:approved", {
        "owner": owner.id, "asked_by": delegate.id, "status": "granted",
        "q": "Remove this draft?", "acts": ["fs_remove"]})
    service.set_availability(owner, None)
    assert not service.proxy_allowed(delegate.id, "room", owner.id)
    assert service.fs_delete(delegate, "room", "approved.md") is True
    with pytest.raises(HubError) as exc:
        service.store_set(delegate, "room", "gate:new-choice", {
            "owner": owner.id, "asked_by": delegate.id, "status": "answered",
            "q": "Release publicly?", "answer": "yes"})
    assert exc.value.status_code == 403
