"""Structured asks/answers (v0.5.1): per-ask obligation discharge.

The agents' unanimous #1 request. A message may carry numbered `asks`; a reply
discharges specific ones via `answers`. The obligation stays open (pinned +
escalating) until EVERY ask is answered — so a partial reply no longer silently
closes the whole message (the partial-answer rot the file protocol suffered).
Messages without asks keep the original binary "any reply discharges" behavior.
"""

from __future__ import annotations

import time

import pytest

from agora.db import Database
from agora.hub.obligations import discharge_state
from agora.hub.service import CHANNEL_META_KEY, HubError, HubService
from agora.models import Message, PostMessage, Status, Urgency


@pytest.fixture()
def service() -> HubService:
    return HubService(Database(":memory:"), rate_per_minute=600.0)


@pytest.fixture()
def team(service):
    alice, _ = service.register_agent("alice", "Alice")
    bob, _ = service.register_agent("bob", "Bob")
    service.create_channel(alice, "design", private=True)
    service.join_channel(bob, "design", service.create_invite(alice, "design", invitee="bob"))
    return alice, bob


def _ack_head(service, viewer, channel="design"):
    """Advance the viewer's triage cursor to head, so the inbox then reflects
    only the STICKY paths (undischarged obligations / unread criticals) — which
    is what discharge governs — not the ordinary unread-since-cursor sweep."""
    service.ack_inbox(viewer, {channel: service.db.last_seq(channel)})


def _envelope(service, viewer, message_id):
    return next((e for e in service.inbox(viewer) if e.id == message_id), None)


# -- pure discharge logic ------------------------------------------------------


def _msg(sender, data=None, status="open"):
    return Message(id="x", channel="c", seq=1, sender=sender, status=status, data=data)


def test_discharge_binary_when_no_asks():
    parent = _msg("alice")
    assert discharge_state(parent, []).discharged is False
    assert discharge_state(parent, [_msg("bob", status="reply")]).discharged is True
    # The asker's own reply never discharges its own obligation.
    assert discharge_state(parent, [_msg("alice", status="reply")]).discharged is False


def test_discharge_asks_requires_all_answered():
    parent = _msg("alice", data={"asks": [{"id": "1", "text": "a"}, {"id": "2", "text": "b"}]})
    partial = discharge_state(parent, [_msg("bob", data={"answers": ["1"]}, status="reply")])
    assert partial.discharged is False and partial.pending == ["2"] and partial.progress == "1/2"
    full = discharge_state(parent, [
        _msg("bob", data={"answers": ["1"]}, status="reply"),
        _msg("bob", data={"answers": ["2"]}, status="reply"),
    ])
    assert full.discharged is True and full.pending == [] and full.progress == "2/2"


# -- end-to-end through the service --------------------------------------------


def test_partial_answer_keeps_obligation_open_full_answer_clears(service, team):
    alice, bob = team
    carol, _ = service.register_agent("carol", "Carol")
    service.join_channel(carol, "design", service.create_invite(alice, "design", invitee="carol"))
    m = service.post_message(alice, "design", PostMessage(
        status=Status.open, title="seam", body="three questions",
        asks=[{"id": "1", "text": "cap?"}, {"id": "2", "text": "owner?"}, {"id": "3", "text": "when?"}]))
    env = _envelope(service, bob, m.id)
    assert env.ask_progress == "0/3" and env.pending_asks == ["1", "2", "3"]

    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, body="cap is 4k", answers=["1"]))
    # The ROOM still owes answers: a member who hasn't attended to the
    # message stays pinned with exact progress. The partial ANSWERER does
    # not — replying records their read receipt (0066), and receipts were
    # always the per-member unpin.
    _ack_head(service, carol)  # cursor past everything: only the STICKY path remains
    env = _envelope(service, carol, m.id)
    assert env is not None, "a partially-answered obligation must stay pinned in the inbox"
    assert env.ask_progress == "1/3" and env.pending_asks == ["2", "3"]
    _ack_head(service, bob)
    assert _envelope(service, bob, m.id) is None, "the answerer attended to it (receipt)"

    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, body="rest", answers=["2", "3"]))
    _ack_head(service, carol)
    assert _envelope(service, carol, m.id) is None, "fully answered -> obligation clears"


def test_answers_are_idempotent_and_order_independent(service, team):
    """Duplicate / out-of-order answers must collapse: answering 2, then 2
    again, then 1 fully discharges a 2-ask message and never miscounts.
    Progress is observed through a THIRD member (the answerer's own pin
    drops on reply — receipt semantics, 0066)."""
    alice, bob = team
    carol, _ = service.register_agent("carol", "Carol")
    service.join_channel(carol, "design", service.create_invite(alice, "design", invitee="carol"))
    m = service.post_message(alice, "design", PostMessage(
        status=Status.open, body="q", asks=[{"id": "1", "text": "a"}, {"id": "2", "text": "b"}]))
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, body="two", answers=["2"]))
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, body="two again", answers=["2"]))
    _ack_head(service, carol)
    env = _envelope(service, carol, m.id)
    assert env is not None and env.ask_progress == "1/2" and env.pending_asks == ["1"]
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, body="one", answers=["1"]))
    _ack_head(service, carol)
    assert _envelope(service, carol, m.id) is None


def test_multiple_answerers_union_discharges(service, team):
    """Ask 1 answered by one agent, ask 2 by another: the union discharges."""
    alice, bob = team
    carol, _ = service.register_agent("carol", "Carol")
    service.join_channel(carol, "design", service.create_invite(alice, "design", invitee="carol"))
    m = service.post_message(alice, "design", PostMessage(
        status=Status.open, body="q", asks=[{"id": "1", "text": "a"}, {"id": "2", "text": "b"}]))
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, body="one", answers=["1"]))
    service.post_message(carol, "design", PostMessage(
        status=Status.reply, reply_to=m.id, body="two", answers=["2"]))
    _ack_head(service, bob)
    assert _envelope(service, bob, m.id) is None, "union of answers from two agents discharges"


def test_legacy_message_without_asks_is_binary(service, team):
    alice, bob = team
    m = service.post_message(alice, "design", PostMessage(status=Status.open, body="decide?"))
    assert _envelope(service, bob, m.id) is not None
    service.post_message(bob, "design", PostMessage(status=Status.reply, reply_to=m.id, body="ok"))
    _ack_head(service, bob)  # past the cursor sweep; binary discharge unpins it
    assert _envelope(service, bob, m.id) is None  # any reply discharges (unchanged)


def test_partial_answer_still_escalates_past_sla(service, team):
    alice, bob = team
    service.store_set(alice, "design", CHANNEL_META_KEY, {"response_sla_minutes": 0.0005})
    m = service.post_message(alice, "design", PostMessage(
        status=Status.open, title="two", body="q",
        asks=[{"id": "1", "text": "a"}, {"id": "2", "text": "b"}]))
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, body="half", answers=["1"]))
    time.sleep(0.05)
    env = _envelope(service, bob, m.id)
    assert env is not None and env.escalated is True and env.effective_urgency == Urgency.interrupt


# -- validation ----------------------------------------------------------------


def test_asks_only_on_open_or_blocked(service, team):
    alice, _ = team
    with pytest.raises(HubError) as e:
        service.post_message(alice, "design", PostMessage(
            status=Status.fyi, body="x", asks=[{"id": "1", "text": "a"}]))
    assert e.value.status_code == 400


def test_duplicate_ask_ids_rejected(service, team):
    alice, _ = team
    with pytest.raises(HubError) as e:
        service.post_message(alice, "design", PostMessage(
            status=Status.open, body="x",
            asks=[{"id": "1", "text": "a"}, {"id": "1", "text": "b"}]))
    assert e.value.status_code == 400


def test_answers_only_on_reply_with_parent(service, team):
    alice, _ = team
    with pytest.raises(HubError) as e:
        service.post_message(alice, "design", PostMessage(
            status=Status.fyi, body="x", answers=["1"]))
    assert e.value.status_code == 400


def test_bare_reply_rejected(service, team):
    """0050: a status=reply without reply_to discharges nothing — the sender
    believes they answered while the obligation rots (live failure
    2026-07-08). Refused with a teaching 400 naming the fix."""
    alice, bob = team
    with pytest.raises(HubError) as e:
        service.post_message(bob, "design", PostMessage(
            status=Status.reply, body="answering into the void"))
    assert e.value.status_code == 400
    assert "reply_to" in e.value.detail
    # The same reply WITH a parent is accepted and discharges.
    m = service.post_message(alice, "design", PostMessage(
        status=Status.open, body="q"))
    reply = service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, body="answering the question"))
    assert reply.reply_to == m.id


def test_non_reply_statuses_stand_alone(service, team):
    """0050 non-goal guard: fyi/open/blocked/resolved never require a parent —
    a free-standing resolved close stays valid."""
    alice, _ = team
    for status in (Status.fyi, Status.open, Status.blocked, Status.resolved):
        m = service.post_message(alice, "design", PostMessage(
            status=status, body=f"standalone {status.value}"))
        assert m.reply_to is None


def test_answers_referencing_unknown_ask_rejected(service, team):
    alice, bob = team
    m = service.post_message(alice, "design", PostMessage(
        status=Status.open, body="q", asks=[{"id": "1", "text": "a"}]))
    with pytest.raises(HubError) as e:
        service.post_message(bob, "design", PostMessage(
            status=Status.reply, reply_to=m.id, body="?", answers=["9"]))
    assert e.value.status_code == 400


# -- authorship reservation (P4): fields exist now, no enforcement yet ---------


def test_raw_data_asks_are_validated_too(service, team):
    """Independent-tester finding: structured fields injected via the raw `data`
    payload (bypassing the typed params) must still be validated — no bypass."""
    alice, bob = team
    # duplicate ids smuggled via data -> rejected
    with pytest.raises(HubError) as e:
        service.post_message(alice, "design", PostMessage(
            status=Status.open, body="q",
            data={"asks": [{"id": "1", "text": "a"}, {"id": "1", "text": "b"}]}))
    assert e.value.status_code == 400
    # answers smuggled via data referencing a non-existent parent ask -> rejected
    m = service.post_message(alice, "design", PostMessage(
        status=Status.open, body="q", asks=[{"id": "1", "text": "a"}]))
    with pytest.raises(HubError) as e:
        service.post_message(bob, "design", PostMessage(
            status=Status.reply, reply_to=m.id, body="x", data={"answers": ["9"]}))
    assert e.value.status_code == 400


def test_assignee_is_sanitized_and_bounded(service, team):
    """The optional ask `assignee` is now capped and control-stripped like text."""
    alice, _ = team
    m = service.post_message(alice, "design", PostMessage(
        status=Status.open, body="q",
        asks=[{"id": "1", "text": "a", "assignee": "bob\n\t" + "x" * 200}]))
    stored = service.db.get_message(m.id).data["asks"][0]["assignee"]
    assert len(stored) <= 64 and "\n" not in stored and "\t" not in stored


def test_signature_is_echoed_on_envelope_verified_by_is_none(service, team):
    alice, bob = team
    m = service.post_message(alice, "design", PostMessage(
        status=Status.fyi, body="hi", signature="proof-token-abc"))
    env = _envelope(service, bob, m.id)
    assert env is not None and env.signature == "proof-token-abc"
    assert env.verified_by is None  # reserved: the hub attests nothing yet


def test_channel_authorship_required_flag_reserved_and_typed(service, team):
    alice, _ = team
    # accepted as a bool (reserved; not enforced)
    service.store_set(alice, "design", CHANNEL_META_KEY, {"authorship_required": True})
    info = service.channel_info(alice, "design")
    assert info["meta"]["authorship_required"] is True
    # non-bool rejected
    with pytest.raises(HubError) as e:
        service.store_set(alice, "design", CHANNEL_META_KEY, {"authorship_required": "yes"})
    assert e.value.status_code == 400
