"""The `task:` row (0.18.0): one operator request, from mint to acceptance.

Every fleet room in the production record ended the same way — a seat
asserted "delivered", nothing recorded whether the requester agreed, and the
human's verdict arrived as prose the hub could not see (at-test #293/#294,
scifi-novel #204/#211, agora-wui #100, rtype-g4 #167). The row keeps
DELIVERED (the worker's claim, stamped from a cited completion report) apart
from ACCEPTED (the requester's word), and re-opens on a rejection.
"""
from __future__ import annotations

import pytest

from agora.db import Database
from agora.hub.service import HubError, HubService
from agora.models import PostMessage, Status


def _room():
    service = HubService(Database(":memory:"), rate_per_minute=600.0)
    op, _ = service.register_agent("laurent", "Laurent", operator=True,
                                   mission="seat laurent")
    lead, _ = service.register_agent("lead", "Lead", mission="delegate")
    worker, _ = service.register_agent("worker", "Worker", mission="build")
    peer, _ = service.register_agent("peer", "Peer", mission="watch")
    service.create_channel(op, "room", private=False)
    for a in (lead, worker, peer):
        service.join_channel(a, "room", None)
    service.set_delegation("lead", ["reporting"])
    return service, op, lead, worker, peer


def _commission(service, op, **kw):
    return service.post_message(op, "room", PostMessage(
        body="build the thing", title="Build the thing", status=Status.open,
        **kw))


def _task(service, seq):
    row = service.db.store_get("room", f"task:msg-{seq}")
    return row.value if row is not None else None


def _cited_resolved(service, sender, parent, key="decision:done",
                    reviewer=None):
    """A completion report that passes the hub's delivery gate: the sender's
    own record, plus — for a delegate in a room with peers — the agreed
    `plan:` row and a peer-authored review row."""
    service.store_set(sender, "room", key, {"what": "done"})
    evidence = [{"kind": "store", "ref": key}]
    if reviewer is not None:
        service.store_set(sender, "room", f"plan:{key.split(':')[-1]}",
                          {"slices": "all"})
        service.store_set(reviewer, "room", f"review:{key.split(':')[-1]}",
                          {"verdict": "pass"})
        evidence += [{"kind": "store", "ref": f"plan:{key.split(':')[-1]}"},
                     {"kind": "store", "ref": f"review:{key.split(':')[-1]}"}]
    return service.post_message(sender, "room", PostMessage(
        body="delivered", status=Status.resolved, reply_to=parent.id,
        data={"evidence": evidence}))


# -- minting -------------------------------------------------------------

def test_an_operator_root_in_a_shared_room_mints_a_task():
    service, op, lead, *_ = _room()
    m = _commission(service, op)
    task = _task(service, m.seq)
    assert task is not None
    assert task["status"] == "open"
    assert task["requester"] == "laurent"
    assert task["source"] == f"room#{m.seq}"
    assert task["title"] == "Build the thing"
    # The reporting delegate coordinates by default.
    assert task["coordinator"] == "lead"
    assert task["declared_by"] == "hub"


def test_without_a_delegate_the_first_addressee_coordinates_else_nobody():
    service, op, lead, worker, peer = _room()
    service.revoke_delegation("lead")
    m = _commission(service, op, to=["worker"])
    assert _task(service, m.seq)["coordinator"] == "worker"
    m2 = _commission(service, op)
    assert _task(service, m2.seq)["coordinator"] is None


def test_peer_roots_replies_and_dms_do_not_mint():
    service, op, lead, worker, peer = _room()
    peer_root = service.post_message(peer, "room", PostMessage(
        body="question", status=Status.open))
    assert _task(service, peer_root.seq) is None
    m = _commission(service, op)
    reply = service.post_message(op, "room", PostMessage(
        body="more", status=Status.open, reply_to=m.id))
    assert _task(service, reply.seq) is None
    dm = service.post_dm(op, "worker", PostMessage(body="quick q",
                                                  status=Status.open))
    assert service.db.store_get(dm.channel, f"task:msg-{dm.seq}") is None


def test_a_task_can_be_minted_by_hand_from_a_root_in_this_channel():
    service, op, lead, worker, peer = _room()
    root = service.post_message(peer, "room", PostMessage(
        body="peer request", title="Peer request", status=Status.open))
    service.store_set(worker, "room", "task:peer-thing",
                      {"source": f"room#{root.seq}", "coordinator": "worker"})
    task = service.db.store_get("room", "task:peer-thing").value
    assert task["requester"] == "peer" and task["status"] == "open"
    with pytest.raises(HubError) as e:
        service.store_set(worker, "room", "task:bad", {"source": "room#999"})
    assert e.value.status_code == 400


# -- delivered ------------------------------------------------------------

def test_a_cited_completion_report_from_the_delegate_stamps_delivered():
    service, op, lead, worker, peer = _room()
    m = _commission(service, op)
    report = _cited_resolved(service, lead, m, reviewer=worker)
    task = _task(service, m.seq)
    assert task["status"] == "delivered"
    assert task["delivered_by"] == "lead"
    assert task["report"] == f"room#{report.seq}"
    assert task["evidence"][0]["kind"] == "store"


def test_a_named_seat_delivers_too_but_a_bystander_does_not():
    service, op, lead, worker, peer = _room()
    named = _commission(service, op, to=["worker"])
    _cited_resolved(service, worker, named, key="decision:w")
    assert _task(service, named.seq)["status"] == "delivered"

    other = _commission(service, op)
    with pytest.raises(HubError):
        # A bystander's cited resolved on an operator root is refused by the
        # closure gate (it can settle nothing), so the task cannot move.
        _cited_resolved(service, peer, other, key="decision:p")
    assert _task(service, other.seq)["status"] == "open"


def test_an_uncited_resolved_delivers_nothing():
    service, op, lead, worker, peer = _room()
    m = _commission(service, op)
    with pytest.raises(HubError):
        service.post_message(lead, "room", PostMessage(
            body="done!", status=Status.resolved, reply_to=m.id))
    assert _task(service, m.seq)["status"] == "open"


def test_delivered_cannot_be_written_by_hand():
    service, op, lead, worker, peer = _room()
    m = _commission(service, op)
    with pytest.raises(HubError) as e:
        service.store_set(lead, "room", f"task:msg-{m.seq}",
                          {"source": f"room#{m.seq}", "status": "delivered"})
    assert "stamped by the hub" in e.value.detail


# -- accepted / rejected --------------------------------------------------

def test_the_requesters_resolved_accepts_and_a_plain_reply_does_not(monkeypatch):
    # `to_close` waits a grace period after the last answer; not the subject here.
    monkeypatch.setattr("agora.hub.service.TO_CLOSE_MIN_AGE_SECONDS", 0.0)
    service, op, lead, worker, peer = _room()
    m = _commission(service, op)
    _cited_resolved(service, lead, m, reviewer=worker)
    service.post_message(op, "room", PostMessage(
        body="hmm, looks wrong", status=Status.reply, reply_to=m.id))
    assert _task(service, m.seq)["status"] == "delivered"
    # The delegate's debt is discharged by its report; the thread waits.
    assert m.id not in {r.id for r in service.owed(lead).to_answer}
    close = [r for r in service.owed(op).to_close if r.seq == m.seq]
    assert close and close[0].task == f"task:msg-{m.seq}"

    service.post_message(op, "room", PostMessage(
        body="great, thanks", status=Status.resolved, reply_to=m.id))
    task = _task(service, m.seq)
    assert task["status"] == "accepted" and task["decided_by"] == "laurent"


def test_a_rejection_needs_a_verdict_re_opens_and_is_counted():
    service, op, lead, worker, peer = _room()
    m = _commission(service, op)
    _cited_resolved(service, lead, m, reviewer=worker)
    key = f"task:msg-{m.seq}"
    with pytest.raises(HubError) as e:
        service.store_set(op, "room", key, {"status": "rejected"})
    assert "verdict" in e.value.detail
    service.store_set(op, "room", key,
                      {"status": "rejected", "verdict": "the index is missing"})
    task = _task(service, m.seq)
    assert task["status"] == "open"
    assert task["rejections"] == 1
    assert task["verdict"] == "the index is missing"
    assert task["decided_by"] == "laurent"
    # A second cited report delivers again.
    _cited_resolved(service, lead, m, key="decision:done-2", reviewer=worker)
    assert _task(service, m.seq)["status"] == "delivered"


def test_only_the_requester_an_operator_or_a_proxy_may_accept_or_reject():
    service, op, lead, worker, peer = _room()
    m = _commission(service, op)
    _cited_resolved(service, lead, m, reviewer=worker)
    key = f"task:msg-{m.seq}"
    with pytest.raises(HubError) as e:
        service.store_set(lead, "room", key, {"status": "accepted"})
    assert e.value.status_code == 403
    with pytest.raises(HubError):
        service.store_set(peer, "room", key, {"coordinator": "peer"})
    service.set_delegation("worker", ["proxy"], scope="room")
    service.store_set(worker, "room", key, {"status": "accepted"})
    assert _task(service, m.seq)["status"] == "accepted"


def test_an_accepted_task_stays_accepted_and_a_second_report_is_idempotent():
    service, op, lead, worker, peer = _room()
    m = _commission(service, op)
    _cited_resolved(service, lead, m, reviewer=worker)
    service.post_message(op, "room", PostMessage(
        body="accepted", status=Status.resolved, reply_to=m.id))
    before = _task(service, m.seq)
    # A later cited resolved does not move an accepted task.
    _cited_resolved(service, lead, m, key="decision:again", reviewer=worker)
    assert _task(service, m.seq) == before
    with pytest.raises(HubError):
        service.store_set(op, "room", f"task:msg-{m.seq}", {"status": "open"})


# -- surfaces -------------------------------------------------------------

def test_the_board_and_the_desk_show_the_task():
    service, op, lead, worker, peer = _room()
    m = _commission(service, op)
    board = service.board(lead)
    cards = [t for t in board["tasks"] if t["slug"] == f"msg-{m.seq}"]
    assert cards and cards[0]["status"] == "open"
    assert cards[0]["coordinator"] == "lead"
    _cited_resolved(service, lead, m, reviewer=worker)
    board = service.board(lead)
    assert board["counts"]["tasks_delivered"] == 1
    desk = service.desk(op)
    row = [r for r in desk["rows"] if r.get("kind") == "task"]
    assert row and row[0]["key"] == f"task:msg-{m.seq}"
    assert row[0]["who_waits"] == "lead"


def test_rooms_and_coordinator_are_the_coordinators_to_edit():
    service, op, lead, worker, peer = _room()
    m = _commission(service, op)
    key = f"task:msg-{m.seq}"
    service.store_set(lead, "room", key, {"rooms": ["ssg-build"],
                                          "coordinator": "worker"})
    task = _task(service, m.seq)
    assert task["rooms"] == ["ssg-build"] and task["coordinator"] == "worker"
    assert task["requester"] == "laurent"          # stamped, never rewritten
    with pytest.raises(HubError):
        service.store_set(worker, "room", key, {"nonsense": 1})
