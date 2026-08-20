"""Declining an ask: discharge that does not claim to be an answer (0153).

An ask is discharged by a reply naming its id in `answers`. Until this
change, a reply that REFUSED an ask — "this should not be done", "this is
not mine" — had the identical wire shape, and the only carrier of the
refusal was English in the body. Nothing mechanical reads prose, so the
digest credited a refuser under `decided`, the asker was pointed at a
non-answer to consume, and `3/3` could mean three refusals.

`declines` names the refused subset. The hub folds it into `answers`, so a
decline clears its row exactly as an answer does — that is deliberate, and
the console's Decline action depends on it. What changes is only that the
record can say which of the two happened.
"""

from __future__ import annotations

import pytest

from agora.hub.obligations import discharge_state
from agora.hub.service import HubError, HubService
from agora.db import Database
from agora.models import Message, PostMessage, Status


@pytest.fixture()
def service() -> HubService:
    return HubService(Database(":memory:"), rate_per_minute=600.0)


@pytest.fixture()
def team(service):
    alice, _ = service.register_agent("alice", "Alice", mission="seat alice")
    bob, _ = service.register_agent("bob", "Bob", mission="seat bob")
    service.create_channel(alice, "design", private=True)
    service.join_channel(bob, "design",
                         service.create_invite(alice, "design", invitee="bob"))
    return alice, bob


def _msg(sender, data=None, status="open"):
    return Message(id="x", channel="c", seq=1, sender=sender, status=status, data=data)


def _envelope(service, viewer, message_id):
    return next((e for e in service.inbox(viewer) if e.id == message_id), None)


def _ask(service, alice, *asks):
    return service.post_message(alice, "design", PostMessage(
        status=Status.open, title="seam", body="questions",
        asks=[{"id": str(i), "text": t} for i, t in enumerate(asks, start=1)]))


# -- the mechanism -------------------------------------------------------------


def test_decline_discharges_exactly_like_an_answer(service, team):
    """The whole point of the item: declining stays legal and terminal. Same
    discharge, same unpin, same progress — only the record differs."""
    alice, bob = team
    m = _ask(service, alice, "should we build X?")
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, declines=["1"],
        body="X should not be built: it duplicates Y."))

    env = _envelope(service, alice, m.id)
    assert env is None or env.pending_asks == []
    state = service._discharge(m, service.db.replies_to(m.id))
    assert state.discharged is True and state.pending == []
    assert state.progress == "1/1", "a decline counts toward discharge"
    assert state.declined == ["1"], "...and is nameable as a refusal"


def test_declines_are_folded_into_answers_on_the_wire(service, team):
    """One carrier for discharge. `answers` keeps its documented meaning —
    the ask ids this reply discharges — so every existing reader, including
    already-persisted rows and older clients, is untouched."""
    alice, bob = team
    m = _ask(service, alice, "a?", "b?")
    r = service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, answers=["1"], declines=["2"],
        body="a is 4k; b is out of scope."))
    assert r.data["answers"] == ["1", "2"]
    assert r.data["declines"] == ["2"]


def test_a_plain_answer_carries_no_declines_key(service, team):
    alice, bob = team
    m = _ask(service, alice, "a?")
    r = service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, answers=["1"], body="4k"))
    assert "declines" not in r.data


def test_bare_answers_behave_exactly_as_before(service, team):
    """The absence of a disposition is never 'answered by default' anywhere it
    would change a count — it IS an answer, as it always was."""
    alice, bob = team
    m = _ask(service, alice, "a?")
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, answers=["1"], body="4k"))
    state = service._discharge(m, service.db.replies_to(m.id))
    assert state.discharged is True and state.declined == []
    digest = service.channel_digest(alice, "design")
    row = next(d for d in digest["decided"] if d["id"] == m.id)
    assert row["answered_by"] == ["bob"] and "declined_by" not in row
    assert digest["counts"]["declined_asks"] == 0


# -- what reads it -------------------------------------------------------------


def test_a_declined_ask_owes_the_asker_no_consumption(service, team):
    """A refusal is terminal: there is nothing in it to adopt or reject, so
    pointing the asker at it would be asking them to consume a non-answer."""
    alice, bob = team
    m = _ask(service, alice, "should we build X?")
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, declines=["1"], body="no: duplicates Y"))
    assert [r.id for r in service.owed(alice).to_consume] == []


def test_an_answered_ask_still_owes_consumption(service, team):
    alice, bob = team
    m = _ask(service, alice, "cap?")
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, answers=["1"], body="4k"))
    rows = service.owed(alice).to_consume
    assert [r.id for r in rows] == [m.id] and rows[0].your_asks == ["1"]


def test_a_mixed_reply_owes_consumption_for_the_answered_half_only(service, team):
    alice, bob = team
    m = _ask(service, alice, "cap?", "should we build X?")
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, answers=["1"], declines=["2"],
        body="4k; X should not be built"))
    rows = service.owed(alice).to_consume
    assert len(rows) == 1 and rows[0].your_asks == ["1"]


def test_digest_reports_the_decline_separately_and_credits_nobody(service, team):
    alice, bob = team
    m = _ask(service, alice, "should we build X?")
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, declines=["1"], body="no"))
    digest = service.channel_digest(alice, "design")
    row = next(d for d in digest["decided"] if d["id"] == m.id)
    assert row["answered_by"] == [], "a refusal is not an answer to credit"
    assert row["declined_by"] == ["bob"] and row["declined_asks"] == ["1"]
    assert digest["counts"]["declined_asks"] == 1


def test_digest_credits_a_mixed_reply_on_both_ledgers(service, team):
    alice, bob = team
    m = _ask(service, alice, "cap?", "should we build X?")
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, answers=["1"], declines=["2"], body="."))
    row = next(d for d in service.channel_digest(alice, "design")["decided"]
               if d["id"] == m.id)
    assert row["answered_by"] == ["bob"] and row["declined_by"] == ["bob"]
    assert row["declined_asks"] == ["2"]


def test_the_askers_headline_names_the_refusal(service, team):
    """`ask_progress` reads 1/1 for a refusal — it discharged — so without
    the named ids the surface that exists to tell the asker where they stand
    says nothing."""
    alice, bob = team
    m = _ask(service, alice, "should we build X?")
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, declines=["1"], body="no"))
    env = service.envelope_for(alice.id, service.db.get_message(m.id))
    assert env.ask_progress == "1/1" and env.declined_asks == ["1"]


def test_one_answer_beats_one_decline_on_a_canvass(service, team):
    """Multi-addressee ask: if anyone answered it substantively, the ask was
    answered. Only an ask NOBODY answered reads as declined."""
    alice, bob = team
    carol, _ = service.register_agent("carol", "Carol", mission="seat carol")
    service.join_channel(carol, "design",
                         service.create_invite(alice, "design", invitee="carol"))
    m = service.post_message(alice, "design", PostMessage(
        status=Status.open, title="roll call", body="who takes it?",
        asks=[{"id": "1", "text": "take it?", "to": ["bob", "carol"]}]))
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, declines=["1"], body="not mine"))
    service.post_message(carol, "design", PostMessage(
        status=Status.reply, reply_to=m.id, answers=["1"], body="I will"))
    state = service._discharge(m, service.db.replies_to(m.id))
    assert state.discharged is True and state.declined == []


def test_an_all_declined_canvass_reads_as_declined(service, team):
    alice, bob = team
    carol, _ = service.register_agent("carol", "Carol", mission="seat carol")
    service.join_channel(carol, "design",
                         service.create_invite(alice, "design", invitee="carol"))
    m = service.post_message(alice, "design", PostMessage(
        status=Status.open, title="roll call", body="who takes it?",
        asks=[{"id": "1", "text": "take it?", "to": ["bob", "carol"]}]))
    for seat in (bob, carol):
        service.post_message(seat, "design", PostMessage(
            status=Status.reply, reply_to=m.id, declines=["1"], body="not mine"))
    state = service._discharge(m, service.db.replies_to(m.id))
    assert state.discharged is True and state.declined == ["1"]


# -- refusals teach ------------------------------------------------------------


def test_decline_needs_a_reply_that_names_its_parent(service, team):
    alice, bob = team
    _ask(service, alice, "a?")
    with pytest.raises(HubError) as e:
        service.post_message(bob, "design", PostMessage(
            status=Status.fyi, declines=["1"], body="no"))
    assert "declines[]" in str(e.value.detail), "teach the field the sender used"


def test_decline_of_an_unknown_ask_is_refused_by_name(service, team):
    alice, bob = team
    m = _ask(service, alice, "a?")
    with pytest.raises(HubError) as e:
        service.post_message(bob, "design", PostMessage(
            status=Status.reply, reply_to=m.id, declines=["9"], body="no"))
    assert "unknown ask ids" in str(e.value.detail)
    assert "declines: ['9']" in str(e.value.detail), "name the field they typed"


def test_a_mixed_refusal_names_both_fields(service, team):
    """The id came from `declines`; teaching the sender about `answers` is
    the wrong gesture."""
    alice, bob = team
    m = _ask(service, alice, "a?")
    with pytest.raises(HubError) as e:
        service.post_message(bob, "design", PostMessage(
            status=Status.reply, reply_to=m.id, answers=["8"], declines=["9"],
            body="."))
    detail = str(e.value.detail)
    assert "answers: ['8']" in detail and "declines: ['9']" in detail


def test_you_cannot_decline_your_own_ask(service, team):
    alice, _ = team
    m = _ask(service, alice, "a?")
    with pytest.raises(HubError) as e:
        service.post_message(alice, "design", PostMessage(
            status=Status.reply, reply_to=m.id, declines=["1"], body="never mind"))
    assert "your own asks" in str(e.value.detail), (
        "abandoning your own thread is status=resolved, not a decline")


def test_you_cannot_decline_an_ask_addressed_to_someone_else(service, team):
    alice, bob = team
    carol, _ = service.register_agent("carol", "Carol", mission="seat carol")
    service.join_channel(carol, "design",
                         service.create_invite(alice, "design", invitee="carol"))
    m = service.post_message(alice, "design", PostMessage(
        status=Status.open, title="seam", body="q",
        asks=[{"id": "1", "text": "take it?", "to": ["carol"]}]))
    with pytest.raises(HubError) as e:
        service.post_message(bob, "design", PostMessage(
            status=Status.reply, reply_to=m.id, declines=["1"], body="no"))
    assert "not addressed to you" in str(e.value.detail), (
        "declining for someone else would unpin them without their word")


def test_empty_and_malformed_declines_are_refused(service, team):
    alice, bob = team
    m = _ask(service, alice, "a?")
    for bad in ([], "1"):
        with pytest.raises(HubError) as e:
            service.post_message(bob, "design", PostMessage(
                status=Status.reply, reply_to=m.id, data={"declines": bad},
                body="no"))
        assert "declines" in str(e.value.detail)


def test_a_decline_needs_no_reason(service, team):
    """Accepted, never required. A mandatory rationale measures compliance,
    not thought — the same reading the gate rows take."""
    alice, bob = team
    m = _ask(service, alice, "a?")
    r = service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, declines=["1"], body=""))
    assert r.data["declines"] == ["1"]


def test_raw_data_declines_are_validated_too(service, team):
    """A hand-built `data` payload cannot smuggle a decline past the checks."""
    alice, bob = team
    m = _ask(service, alice, "a?")
    with pytest.raises(HubError):
        service.post_message(bob, "design", PostMessage(
            status=Status.reply, reply_to=m.id, body="no",
            data={"declines": ["9"]}))


def test_a_decline_never_obliges_the_asker_to_reply(service, team):
    """A decline carries `answers`, which is what marks a reply terminal:
    without the fold, refusing an ask would open a NEW directive debt on the
    seat that asked — and rot into SLA escalation."""
    alice, bob = team
    m = _ask(service, alice, "should we build X?")
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, to=["alice"], declines=["1"],
        body="no: duplicates Y"))
    assert [r.id for r in service.owed(alice).to_answer] == []


def test_retracting_a_decline_re_pends_the_ask(service, team):
    """The tombstone drops `data` wholesale, so a retracted refusal takes its
    disposition with it — symmetric with retracting an answer."""
    alice, bob = team
    m = _ask(service, alice, "should we build X?")
    r = service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, declines=["1"], body="no"))
    service.retract_message(bob, "design", r.id)
    state = service._discharge(m, service.db.replies_to(m.id))
    assert state.pending == ["1"] and state.declined == []


def test_consuming_your_own_fully_declined_thread_is_a_no_op(service, team):
    """`consumes=[<root>]` is the batch gesture the docs recommend. A thread
    whose every reply declined owes nothing — refusing the ref would teach
    the wrong gesture for the right act."""
    alice, bob = team
    m = _ask(service, alice, "should we build X?")
    service.post_message(bob, "design", PostMessage(
        status=Status.reply, reply_to=m.id, declines=["1"], body="no"))
    posted = service.post_message(alice, "design", PostMessage(
        status=Status.fyi, body="read it", consumes=[m.id]))
    assert posted.data["consumes"] == [m.id]


def test_consuming_someone_elses_thread_is_still_refused(service, team):
    alice, bob = team
    m = _ask(service, alice, "a?")
    with pytest.raises(HubError):
        service.post_message(bob, "design", PostMessage(
            status=Status.fyi, body="x", consumes=[m.id]))


# -- pure logic ----------------------------------------------------------------


def test_discharge_state_declined_is_a_subset_of_answered():
    parent = _msg("alice", data={"asks": [{"id": "1", "text": "a"},
                                          {"id": "2", "text": "b"}]})
    state = discharge_state(parent, [
        _msg("bob", data={"answers": ["1", "2"], "declines": ["2"]}, status="reply"),
    ])
    assert state.answered == ["1", "2"] and state.declined == ["2"]
    assert set(state.declined) <= set(state.answered)
