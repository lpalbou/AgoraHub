"""The operator's word always lands on someone (operator ruling, 2026-08-01).

THE FAILURE THIS FILE PINS. On 2026-08-01 the operator gave the fleet a
PDF-completion task and nothing was ever delivered. The mechanical reason was
not a crash, a timeout or a dark seat — every driver was alive and
heartbeating. It was that the request obliged NOBODY:

  * at-test#396 was posted as `status=reply, to=[]`. `open_obligations`
    covers only open/blocked; `_is_addressed_debt` required the viewer to be
    named in `to`; the sender-facing doorbell was gated on open/blocked. The
    message created ZERO obligation rows fleet-wide.
  * at-test#382 (an open broadcast carrying five requirements) was
    binary-discharged 75 seconds later by one seat's partial reply.

The ruling: "reader IS the delegate, his role is more to orchestrate with the
other agents ... he is the one with the responsibility making sure a request
is done end to end." So every operator message obliges the reporting delegate
— whatever its status, addressed or not — and a bystander cannot discharge a
human's broadcast on their behalf.
"""

from __future__ import annotations

import pytest

from agora.db import Database
from agora.hub.service import HubService
from agora.models import PostMessage


@pytest.fixture()
def service() -> HubService:
    return HubService(Database(":memory:"), rate_per_minute=600.0)


@pytest.fixture()
def fleet(service):
    """An operator, a reporting delegate, and two ordinary seats in a room."""
    op, _ = service.register_agent("laurent", "Laurent", operator=True)
    reader, _ = service.register_agent("reader", "Reader")
    editor, _ = service.register_agent("editor", "Editor")
    at1, _ = service.register_agent("at1", "At1")
    service.create_channel(op, "at-test", private=True)
    for seat in (reader, editor, at1):
        service.join_channel(
            seat, "at-test",
            service.create_invite(op, "at-test", invitee=seat.id))
    return op, reader, editor, at1


def _owed_ids(service, agent) -> set[str]:
    return {r.id for r in service.owed(agent).to_answer}


def _delegate(service, agent_id="reader"):
    service.set_delegation(agent_id, ["reporting"])


def _root(service, seat, channel="at-test"):
    """A thread to reply into. The live at-test#396 was exactly this shape:
    the operator's task arrived as a REPLY inside an existing thread."""
    return service.post_message(seat, channel, PostMessage(
        body="production export prepared", status="open", title="export"))


def _post(service, sender, status, body="task", channel="at-test",
          root_seat=None, **kw):
    """status=reply requires a parent (a bare reply discharges nothing), so
    supply one automatically — the shape the operator actually used. The
    parent is authored by `root_seat` when given: an operator-authored root
    would itself be an unaddressed operator message and confuse the count."""
    if status == "reply" and "reply_to" not in kw:
        kw["reply_to"] = _root(service, root_seat or sender).id
    return service.post_message(sender, channel, PostMessage(
        body=body, status=status, **kw))


# -- the to=[] hole -----------------------------------------------------------


@pytest.mark.parametrize("status", ["reply", "fyi", "open", "blocked"])
def test_operator_message_naming_nobody_obliges_the_delegate(
        service, fleet, status):
    """WHATEVER the status. #396 was a `reply`; the composer's status choice
    must never decide whether a human's request is owed by anyone."""
    op, reader, editor, at1 = fleet
    _delegate(service)
    parent = _root(service, editor)
    kw = {"reply_to": parent.id} if status == "reply" else {}
    m = service.post_message(op, "at-test", PostMessage(
        body="rebuild the PDF end to end", status=status, title="task", **kw))
    assert m.id in _owed_ids(service, reader), \
        f"operator {status} with to=[] obliged nobody"
    # Deliberately NOT oblige-all-members: that is the wake-storm shape
    # 0.12.55 killed. The delegate is the routing point, and only them.
    assert m.id not in _owed_ids(service, editor)
    assert m.id not in _owed_ids(service, at1)


def test_addressed_operator_message_keeps_its_addressees_and_adds_delegate(
        service, fleet):
    """The delegate is ADDED, never substituted: naming seats still works."""
    op, reader, editor, at1 = fleet
    _delegate(service)
    m = service.post_message(op, "at-test", PostMessage(
        body="editor, do the colophon", status="open", to=["editor"]))
    assert m.id in _owed_ids(service, editor)
    assert m.id in _owed_ids(service, reader)
    assert m.id not in _owed_ids(service, at1)


def test_peer_broadcast_is_unchanged_by_the_ruling(service, fleet):
    """The ruling is about OPERATOR traffic. A peer's unaddressed message
    must not start obliging the delegate — that would make every room
    remark the delegate's debt and rebuild the storm from the other side."""
    op, reader, editor, at1 = fleet
    _delegate(service)
    for status in ("reply", "fyi", "open"):
        m = _post(service, editor, status, body="thinking out loud")
        assert m.id not in _owed_ids(service, reader), status


def test_delegates_own_post_never_obliges_itself(service, fleet):
    op, reader, _, _ = fleet
    _delegate(service)
    m = _post(service, reader, "reply", body="status update")
    assert m.id not in _owed_ids(service, reader)


def test_operator_answer_reply_is_not_a_new_request(service, fleet):
    """A reply carrying `answers` discharges an ask; it is not a directive."""
    op, reader, editor, _ = fleet
    _delegate(service)
    root = service.post_message(reader, "at-test", PostMessage(
        body="which cover?", status="open",
        asks=[{"id": "1", "text": "which cover?"}]))
    ans = service.post_message(op, "at-test", PostMessage(
        body="the second one", status="reply",
        reply_to=root.id, answers=["1"]))
    assert ans.id not in _owed_ids(service, reader)


def test_delegate_reply_clears_the_debt(service, fleet):
    """Engagement discharges it like any other directive debt — the delegate
    is accountable, not permanently pinned."""
    op, reader, editor, _ = fleet
    _delegate(service)
    m = _post(service, op, "reply", body="rebuild the PDF", root_seat=editor)
    assert m.id in _owed_ids(service, reader)
    service.post_message(reader, "at-test", PostMessage(
        body="on it — decomposing into addressed asks", status="reply",
        reply_to=m.id))
    assert m.id not in _owed_ids(service, reader)


def test_revoking_the_delegation_stops_the_routing(service, fleet):
    op, reader, editor, _ = fleet
    _delegate(service)
    m = _post(service, op, "reply", body="task", root_seat=editor)
    assert m.id in _owed_ids(service, reader)
    service.set_delegation("reader", ["moderation"])  # no longer reporting
    assert m.id not in _owed_ids(service, reader)


# -- no delegate: the operator gets a real, findable warning ------------------


def test_without_a_delegate_the_operator_is_warned_in_a_real_dm(service, fleet):
    """Fallback per the ruling. NOT the ephemeral notify-file doorbell used
    for routing teaching — a human must be able to find this later, so it is
    a stored DM in the hub->operator room."""
    op, reader, editor, _ = fleet
    m = _post(service, op, "reply", body="rebuild the PDF", title="task",
              root_seat=editor)
    assert m.id not in _owed_ids(service, reader)  # nobody owes it
    dm = f"dm:hub--{op.id}" if "hub" < op.id else f"dm:{op.id}--hub"
    warns = [x for x in service.db.get_messages(dm, 0, 50)
             if "HUB WARNING" in (x.body or "")]
    assert len(warns) == 1
    assert "creates NO obligation" in warns[0].body
    assert "reporting" in warns[0].body
    # It is STORED (findable), unlike the ephemeral doorbell notices.
    assert warns[0].id and not warns[0].id.startswith("notice:")


def test_no_warning_when_a_delegate_exists_or_seats_are_named(service, fleet):
    op, reader, editor, _ = fleet
    dm = f"dm:hub--{op.id}" if "hub" < op.id else f"dm:{op.id}--hub"
    # Named seats: the operator already routed it.
    service.post_message(op, "at-test", PostMessage(
        body="editor do X", status="open", to=["editor"]))
    assert not [x for x in service.db.get_messages(dm, 0, 50)
                if "HUB WARNING" in (x.body or "")]
    # With a delegate: routed by construction.
    _delegate(service)
    _post(service, op, "reply", body="another task", root_seat=editor)
    assert not [x for x in service.db.get_messages(dm, 0, 50)
                if "HUB WARNING" in (x.body or "")]


# -- the 75-second discharge, end to end -------------------------------------


def test_bystander_reply_no_longer_closes_the_operators_broadcast(
        service, fleet):
    """at-test#382's exact shape, through the real service: the operator
    broadcasts several requirements, a seat replies to part of it, and the
    delegate must STILL owe the request."""
    op, reader, editor, _ = fleet
    _delegate(service)
    task = service.post_message(op, "at-test", PostMessage(
        body="check the illustrations, ensure consistency, generate 3 more "
             "per section, professional PDF rendering, pick author names",
        status="open", title="illustrations"))
    service.post_message(editor, "at-test", PostMessage(
        body="picked author names", status="reply", reply_to=task.id))
    assert task.id in _owed_ids(service, reader), \
        "a bystander's partial reply closed the operator's broadcast"
    # The delegate's own `resolved` is what settles it — an accountable
    # end-to-end completion claim, per the ruling.
    service.post_message(reader, "at-test", PostMessage(
        body="delivered: pdf rebuilt and reported", status="resolved",
        reply_to=task.id))
    assert task.id not in _owed_ids(service, reader)
