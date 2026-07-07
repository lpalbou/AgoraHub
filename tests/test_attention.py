"""Tests of the v0.2 attention model: envelopes, escalation, critical, budgets,
channel metadata, colleague notes.

These illustrate intended semantics; the policy logic is general-purpose.
"""

from __future__ import annotations

import time

import pytest

from agora.db import Database
from agora.hub.service import CHANNEL_META_KEY, HubError, HubService
from agora.models import PostMessage, Status, Urgency


@pytest.fixture()
def service() -> HubService:
    return HubService(Database(":memory:"), rate_per_minute=600.0,
                      interrupts_per_hour=2, criticals_per_hour=2)


@pytest.fixture()
def team(service):
    """alice (owner) + bob (member) + op (operator member) in channel 'design'."""
    alice, _ = service.register_agent("alice", "Alice")
    bob, _ = service.register_agent("bob", "Bob")
    op, _ = service.register_agent("op", "Operator", operator=True)
    service.create_channel(alice, "design", private=True)
    for member in (bob, op):
        token = service.create_invite(alice, "design", invitee=member.id)
        service.join_channel(member, "design", invite_token=token)
    # Start everyone past the system messages.
    top = service.db.last_seq("design")
    for member in (alice, bob, op):
        service.ack_inbox(member, {"design": top})
    return alice, bob, op


# -- envelope inlining policy ---------------------------------------------------

def test_small_body_is_inlined(service, team):
    alice, bob, _ = team
    service.post_message(alice, "design", PostMessage(body="short note", title="hi"))
    [envelope] = service.inbox(bob)
    assert envelope.body == "short note"
    assert envelope.body_bytes == len("short note")


def test_large_broadcast_body_is_envelope_only(service, team):
    alice, bob, _ = team
    service.post_message(alice, "design", PostMessage(body="x" * 5000, title="big dump"))
    [envelope] = service.inbox(bob)
    assert envelope.body is None            # headline only: fetch deliberately
    assert envelope.body_bytes == 5000
    assert envelope.title == "big dump"


def test_addressed_large_body_is_inlined_up_to_cap(service, team):
    alice, bob, _ = team
    service.post_message(alice, "design", PostMessage(body="y" * 3000, to=["bob"]))
    [envelope] = service.inbox(bob)
    assert envelope.to_me is True
    assert envelope.body is not None        # addressed to you: read decision near-certain


def test_reply_to_me_is_computed_by_hub(service, team):
    alice, bob, _ = team
    question = service.post_message(bob, "design", PostMessage(body="z" * 2000, status=Status.open))
    service.post_message(alice, "design",
                         PostMessage(body="w" * 2000, status=Status.reply, reply_to=question.id))
    [envelope] = service.inbox(bob)
    assert envelope.reply_to_me is True
    assert envelope.body is not None


def test_title_is_sanitized_and_capped(service, team):
    alice, bob, _ = team
    service.post_message(alice, "design",
                         PostMessage(body="b", title="a\nb\x00c" + "!" * 300))
    [envelope] = service.inbox(bob)
    assert "\n" not in envelope.title and "\x00" not in envelope.title
    assert len(envelope.title) <= 120


# -- obligation escalation ---------------------------------------------------------

def test_unanswered_obligation_escalates_after_sla(service, team):
    alice, bob, _ = team
    # Tight SLA via channel metadata (owner-set).
    service.store_set(alice, "design", CHANNEL_META_KEY,
                      {"response_sla_minutes": 0.0005})  # ~30ms
    message = service.post_message(alice, "design",
                                   PostMessage(body="please decide", status=Status.open))
    time.sleep(0.05)
    [envelope] = service.inbox(bob)
    assert envelope.escalated is True
    assert envelope.effective_urgency == Urgency.interrupt
    # Once answered, the obligation stops escalating.
    service.post_message(bob, "design",
                         PostMessage(body="decided", status=Status.reply, reply_to=message.id))
    refreshed = [e for e in service.inbox(bob) if e.id == message.id]
    assert refreshed == [] or refreshed[0].escalated is False


# -- critical broadcasts ---------------------------------------------------------------

def test_critical_requires_operator(service, team):
    alice, bob, _ = team
    with pytest.raises(HubError) as e:
        service.post_message(alice, "design", PostMessage(body="stop", critical=True))
    assert e.value.status_code == 403


def test_critical_is_sticky_until_actually_read(service, team):
    alice, bob, op = team
    message = service.post_message(op, "design",
                                   PostMessage(body="freeze deployments", critical=True))
    [envelope] = service.inbox(bob)
    assert envelope.critical and envelope.body == "freeze deployments"
    # Cursor ack does NOT clear a critical...
    service.ack_inbox(bob, {"design": message.seq})
    assert [e.id for e in service.inbox(bob)] == [message.id]
    # ...only a deliberate read does.
    service.read_message(bob, "design", message.id)
    assert service.inbox(bob) == []


def test_critical_budget(service, team):
    _, _, op = team
    service.post_message(op, "design", PostMessage(body="c1", critical=True))
    service.post_message(op, "design", PostMessage(body="c2", critical=True))
    with pytest.raises(HubError) as e:
        service.post_message(op, "design", PostMessage(body="c3", critical=True))
    assert e.value.status_code == 429


# -- interrupt budget ------------------------------------------------------------------

def test_interrupts_downgrade_when_budget_exhausted(service, team):
    alice, bob, _ = team
    for _ in range(2):  # within budget (2/hour in this fixture)
        service.post_message(alice, "design",
                             PostMessage(body="now!", urgency=Urgency.interrupt))
    third = service.post_message(alice, "design",
                                 PostMessage(body="now again!", urgency=Urgency.interrupt))
    assert third.urgency == Urgency.next_turn
    assert third.downgraded is True         # crying wolf is visible to receivers


# -- reading with reply-chain ancestors ---------------------------------------------------

def test_read_message_includes_unread_ancestors(service, team):
    alice, bob, _ = team
    m1 = service.post_message(alice, "design",
                              PostMessage(body="proposal: migrate?", status=Status.open))
    m2 = service.post_message(alice, "design",
                              PostMessage(body="objection raised", reply_to=m1.id))
    m3 = service.post_message(alice, "design",
                              PostMessage(body="agreed, NOT migrating", status=Status.reply,
                                          reply_to=m2.id))
    # Bob reads only the last message; the unread chain comes with it, oldest first.
    chain = service.read_message(bob, "design", m3.id)
    assert [m.id for m in chain] == [m1.id, m2.id, m3.id]
    # Receipts recorded: reading m3 again no longer drags ancestors.
    assert [m.id for m in service.read_message(bob, "design", m3.id)] == [m3.id]


# -- channel metadata (reserved store keys) ------------------------------------------------

def test_channel_meta_is_owner_writable_only(service, team):
    alice, bob, _ = team
    with pytest.raises(HubError) as e:
        service.store_set(bob, "design", CHANNEL_META_KEY, {"purpose": "takeover"})
    assert e.value.status_code == 403
    service.store_set(alice, "design", CHANNEL_META_KEY,
                      {"purpose": "runtime-memory seam", "norms": "asks numbered",
                       "expected_traffic": ["asks", "decisions"], "response_sla_minutes": 30})
    info = service.channel_info(bob, "design")
    assert info["meta"]["purpose"] == "runtime-memory seam"
    assert info["response_sla_minutes"] == 30


def test_channel_meta_rejects_unknown_fields(service, team):
    alice, _, _ = team
    with pytest.raises(HubError) as e:
        service.store_set(alice, "design", CHANNEL_META_KEY, {"priority_boost": 11})
    assert e.value.status_code == 400


def test_ordinary_store_keys_stay_member_writable(service, team):
    _, bob, _ = team
    entry = service.store_set(bob, "design", "claim:adapter", {"owner": "bob"})
    assert entry.version == 1


# -- colleague notes -----------------------------------------------------------------------

def test_notes_are_private_and_revisable(service, team):
    alice, bob, _ = team
    service.set_note(bob, "alice", "precise, but verify version claims")
    service.set_note(bob, "alice", "revised: version claims were correct after all")
    [note] = service.get_notes(bob, "alice")
    assert "revised" in note["note"]
    # Privacy: alice sees her own (empty) notes, never bob's.
    assert service.get_notes(alice) == []


def test_note_requires_registered_subject(service, team):
    _, bob, _ = team
    with pytest.raises(HubError) as e:
        service.set_note(bob, "ghost", "who?")
    assert e.value.status_code == 404
