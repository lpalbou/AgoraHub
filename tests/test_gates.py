"""Owner gates: a delegate that is not the owner's proxy must ASK.

The operator's requirement, 2026-08-04: "the delegate must be the safest
agent of the bunch... it can be delegated several roles, the highest being
to act on behalf of the user. if not granted, when there are key decisions,
it should gate the work with key clear simple questions for the human owner."

Two live falsifications shaped every design decision here, and each has a
test below:

1. TEACHING IS NEGOTIABLE. A delegate read a charter that named `fs_remove`
   a key decision, said so in its own reasoning, and deleted the files
   anyway — arguing "restorable via version history". True about the hub,
   false about the owner, who wanted them kept. The restored files came back
   RE-AUTHORED, not as the bytes that were there. So the gate is a 403.

2. A SEAT-SCOPED GATE IS BYPASSED BY DISPATCH. A delegate correctly refused
   to delete, then addressed the delete to a plain member, who did it. So
   the gate binds on the CHANNEL and refuses every non-owner seat.
"""

from __future__ import annotations

import pytest

from agora.db import Database
from agora.hub.service import CHANNEL_META_KEY, HubError, HubService
from agora.models import AgentInfo


@pytest.fixture()
def lab():
    service = HubService(Database(":memory:"), rate_per_minute=600.0)
    owner, _ = service.register_agent("owner", "Owner", operator=True, mission="seat owner")
    delegate, _ = service.register_agent("dele", "Delegate", mission="seat dele")
    worker, _ = service.register_agent("work", "Worker", mission="seat work")
    service.create_channel(owner, "lab", private=False)
    for a in (delegate, worker):
        service.join_channel(a, "lab", None)
    service.set_delegation("dele", ["reporting", "operational"])
    service.fs_write(delegate, "lab", "draft.md", "hello",
                     description="a draft")
    return service, owner, delegate, worker


def _gate_on(service, owner, *acts):
    service.store_set(owner, "lab", CHANNEL_META_KEY,
                      {"gated_acts": list(acts)})


# -- the room opts in ---------------------------------------------------------


def test_an_ungated_room_is_completely_unchanged(lab):
    """Default absent: no room changes behaviour until its owner opts in —
    the same contract `norms_required` has."""
    service, owner, delegate, worker = lab
    assert service.fs_delete(delegate, "lab", "draft.md") is True
    service.store_set(delegate, "lab", "decision:x", {"v": 1})


def test_gated_acts_vocabulary_is_closed(lab):
    """Every name must map to an act the hub already mediates — otherwise it
    is vocabulary that can never fire, which is its own failure mode here."""
    service, owner, delegate, worker = lab
    with pytest.raises(HubError) as e:
        service.store_set(owner, "lab", CHANNEL_META_KEY,
                          {"gated_acts": ["launch_missiles"]})
    assert e.value.status_code == 400


# -- the refusal --------------------------------------------------------------


def test_a_delegate_without_proxy_is_refused_the_gated_act(lab):
    """Falsification 1: the delegate that talked itself past the charter."""
    service, owner, delegate, worker = lab
    _gate_on(service, owner, "fs_remove", "decision")
    with pytest.raises(HubError) as e:
        service.fs_delete(delegate, "lab", "draft.md")
    assert e.value.status_code == 403
    assert "gate:" in str(e.value.detail)          # names the way out
    with pytest.raises(HubError):
        service.store_set(delegate, "lab", "decision:identity", {"v": "x"})


def test_the_gate_cannot_be_laundered_through_a_plain_member(lab):
    """Falsification 2, and the reason the gate binds on the CHANNEL: the
    delegate refused the act itself and dispatched it to a member, who had
    no delegation to lose and did it."""
    service, owner, delegate, worker = lab
    _gate_on(service, owner, "fs_remove")
    with pytest.raises(HubError) as e:
        service.fs_delete(worker, "lab", "draft.md")
    assert e.value.status_code == 403
    assert "laundering" in str(e.value.detail)


def test_the_owner_and_the_operator_are_never_gated(lab):
    """A gate protects the owner's intent; it never blocks the owner."""
    service, owner, delegate, worker = lab
    _gate_on(service, owner, "fs_remove")
    assert service.fs_delete(owner, "lab", "draft.md") is True


def test_declaring_a_phase_complete_is_gated_but_ordinary_phase_work_is_not(lab):
    """Completing a phase unblocks the next one for the whole room."""
    service, owner, delegate, worker = lab
    _gate_on(service, owner, "phase_complete")
    service.store_set(delegate, "lab", "phase:novel",
                      {"current": "draft", "status": "open",
                       "steward": "dele", "next": "review"})
    with pytest.raises(HubError) as e:
        service.store_set(delegate, "lab", "phase:novel",
                          {"current": "draft", "status": "complete",
                           "steward": "dele", "next": "review"})
    assert e.value.status_code == 403


# -- the proxy tier -----------------------------------------------------------


def test_proxy_clears_the_gate_in_its_scope_only(lab):
    """The top tier: "act on my behalf". Scoped, because an unscoped grant
    taken to unstick ONE room would silently clear every gate on the hub."""
    service, owner, delegate, worker = lab
    service.create_channel(owner, "other", private=False)
    service.join_channel(AgentInfo(id="dele", name="dele"), "other", None)
    _gate_on(service, owner, "fs_remove")
    service.set_delegation("dele", ["reporting", "proxy"], scope="lab")
    assert service.has_proxy("dele", "lab") is True
    assert service.has_proxy("dele", "other") is False
    assert service.fs_delete(delegate, "lab", "draft.md") is True


def test_a_proxy_grant_must_name_its_scope(lab):
    """Fleet-wide authority to act as the owner is chosen, never arrived at
    by omission — the lesson of the operator flag, which has no revocation
    path because nobody decided it should be permanent."""
    service, owner, delegate, worker = lab
    with pytest.raises(HubError) as e:
        service.set_delegation("dele", ["proxy"])
    assert e.value.status_code == 400 and "--scope" in str(e.value.detail)
    with pytest.raises(HubError):
        service.set_delegation("dele", ["proxy"], scope="no-such-channel")
    g = service.set_delegation("dele", ["proxy"], scope="*")
    assert g["scope"] == "*" and service.has_proxy("dele", "anywhere") is True


def test_proxy_is_short_lived_by_default(lab):
    """An owner means "act for me" for an afternoon, not a quarter."""
    service, owner, delegate, worker = lab
    g = service.set_delegation("dele", ["proxy"], scope="lab")
    assert round((g["expires_at"] - g["granted_at"]) / 86400, 2) == 1.0
    with pytest.raises(HubError) as e:
        service.set_delegation("dele", ["proxy"], scope="lab",
                               ttl_seconds=20 * 86400)
    assert e.value.status_code == 400


# -- the gate row itself ------------------------------------------------------


def test_only_the_owner_can_answer_a_gate(lab):
    """A gate a delegate can grant itself is not a gate."""
    service, owner, delegate, worker = lab
    service.store_set(delegate, "lab", "gate:identity",
                      {"owner": "owner", "asked_by": "dele",
                       "status": "asked", "q": "research or product?",
                       "options": ["a: research", "b: product"]})
    with pytest.raises(HubError) as e:
        service.store_set(delegate, "lab", "gate:identity",
                          {"owner": "owner", "status": "granted"})
    assert e.value.status_code == 403
    service.store_set(owner, "lab", "gate:identity",
                      {"owner": "owner", "asked_by": "dele",
                       "status": "answered", "answer": "a: research"})


def test_a_gate_row_keeps_its_question(lab):
    """Live, two UNVALIDATED gate rows silently lost `q` and `default` on
    their first update, leaving a status word with nothing to answer."""
    service, owner, delegate, worker = lab
    with pytest.raises(HubError) as e:
        service.store_set(delegate, "lab", "gate:x",
                          {"owner": "owner", "status": "asked",
                           "quesion": "typo'd field"})
    assert e.value.status_code == 400
    with pytest.raises(HubError):      # a gate addressed to nobody
        service.store_set(delegate, "lab", "gate:y", {"status": "asked"})
    with pytest.raises(HubError):      # not a real status
        service.store_set(delegate, "lab", "gate:z",
                          {"owner": "owner", "status": "pondering"})


def test_a_gates_owner_cannot_be_reassigned(lab):
    """Otherwise the asker re-points the gate at itself and answers it."""
    service, owner, delegate, worker = lab
    service.store_set(delegate, "lab", "gate:q",
                      {"owner": "owner", "status": "asked", "q": "?"})
    with pytest.raises(HubError) as e:
        service.store_set(delegate, "lab", "gate:q",
                          {"owner": "dele", "status": "asked", "q": "?"})
    assert e.value.status_code == 403


def test_a_granted_gate_opens_the_act(lab):
    """The whole point: the owner says yes once, and the work proceeds."""
    service, owner, delegate, worker = lab
    _gate_on(service, owner, "fs_remove")
    service.store_set(delegate, "lab", "gate:rm",
                      {"owner": "owner", "asked_by": "dele",
                       "acts": ["fs_remove"],
                       "status": "asked", "q": "delete the old drafts?"})
    with pytest.raises(HubError):
        service.fs_delete(delegate, "lab", "draft.md")
    service.store_set(owner, "lab", "gate:rm",
                      {"owner": "owner", "asked_by": "dele",
                       "acts": ["fs_remove"], "status": "granted"})
    assert service.fs_delete(delegate, "lab", "draft.md") is True


def test_an_expired_grant_does_not_authorize(lab):
    """A gate answered last week is not consent today."""
    import time

    service, owner, delegate, worker = lab
    _gate_on(service, owner, "fs_remove")
    service.store_set(owner, "lab", "gate:rm",
                      {"owner": "owner", "asked_by": "dele",
                       "acts": ["fs_remove"], "status": "granted",
                       "expires_at": str(time.time() - 60)})
    with pytest.raises(HubError):
        service.fs_delete(delegate, "lab", "draft.md")


def test_a_grant_is_scoped_to_one_act_and_one_asker(lab):
    """THE SKELETON KEY (found and fixed 2026-08-04). The grant check passed
    on ANY granted gate row in the channel — so one grant about picking a
    title let an unrelated member delete files and write decisions. An owner
    answering a question is not handing out the room."""
    service, owner, delegate, worker = lab
    _gate_on(service, owner, "fs_remove", "decision")
    service.store_set(delegate, "lab", "gate:title",
                      {"owner": "owner", "asked_by": "dele",
                       "acts": ["decision"], "status": "asked",
                       "q": "which title?"})
    service.store_set(owner, "lab", "gate:title",
                      {"owner": "owner", "asked_by": "dele",
                       "acts": ["decision"], "status": "granted"})
    # The granted act, for the seat that asked: allowed.
    service.store_set(delegate, "lab", "decision:title", {"v": "x"})
    # A DIFFERENT act, same seat: still refused.
    with pytest.raises(HubError):
        service.fs_delete(delegate, "lab", "draft.md")
    # The SAME act, a different seat: still refused.
    with pytest.raises(HubError):
        service.store_set(worker, "lab", "decision:other", {"v": "y"})


def test_a_grant_must_say_yes_to_something(lab):
    """Fail closed: a grant naming no act would authorize every act."""
    service, owner, delegate, worker = lab
    service.store_set(delegate, "lab", "gate:q",
                      {"owner": "owner", "asked_by": "dele",
                       "status": "asked", "q": "?"})
    with pytest.raises(HubError) as e:
        service.store_set(owner, "lab", "gate:q",
                          {"owner": "owner", "asked_by": "dele",
                           "status": "granted"})
    assert e.value.status_code == 400
    with pytest.raises(HubError):      # unknown act class
        service.store_set(owner, "lab", "gate:q",
                          {"owner": "owner", "asked_by": "dele",
                           "acts": ["launch_missiles"], "status": "granted"})
