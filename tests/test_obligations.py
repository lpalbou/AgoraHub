"""Structured asks/answers (v0.5.1): per-ask obligation discharge.

The agents' unanimous #1 request. A message may carry numbered `asks`; a reply
discharges specific ones via `answers`. The obligation stays open (pinned +
escalating) until EVERY ask is answered — so a partial reply no longer silently
closes the whole message (the partial-answer rot the file protocol suffered).
Unaddressed messages without asks keep the original binary "any reply
discharges" behavior. Addressed work asks are tighter.
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
    alice, _ = service.register_agent("alice", "Alice", mission="seat alice")
    bob, _ = service.register_agent("bob", "Bob", mission="seat bob")
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
    carol, _ = service.register_agent("carol", "Carol", mission="seat carol")
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
    carol, _ = service.register_agent("carol", "Carol", mission="seat carol")
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
    carol, _ = service.register_agent("carol", "Carol", mission="seat carol")
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


def test_only_named_seat_may_answer_an_addressed_ask(service, team):
    alice, bob = team
    carol, _ = service.register_agent("carol", "Carol", mission="seat carol")
    service.join_channel(carol, "design", service.create_invite(alice, "design", invitee="carol"))
    m = service.post_message(alice, "design", PostMessage(
        status=Status.open, body="q",
        asks=[{"id": "1", "text": "bob only", "to": ["bob"]}]))
    with pytest.raises(HubError) as e:
        service.post_message(carol, "design", PostMessage(
            status=Status.reply, reply_to=m.id, body="I have context", answers=["1"]))
    assert e.value.status_code == 400
    assert "not addressed to you" in e.value.detail


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
    """fyi/open/resolved stand alone; blocked is an addressed help contract."""
    alice, _ = team
    for status in (Status.fyi, Status.open, Status.resolved):
        m = service.post_message(alice, "design", PostMessage(
            status=status, body=f"standalone {status.value}"))
        assert m.reply_to is None


def test_blocked_is_never_refused(service, team):
    """"I am stuck" is the single most important escalation gesture in the
    system, so it is always delivered.

    It used to require BOTH a structured ask and an explicit addressee, which
    made a plain "boss, I'm blocked on the schema ruling" a 400 — even in a
    two-party DM, where the addressee is structurally the only other party. The
    structured form is better and is what the rules teach; the hub teaches it
    with a non-waking sender doorbell rather than by refusing to carry the
    message.
    """
    alice, bob = team
    bare = service.post_message(alice, "design", PostMessage(
        status=Status.blocked, body="stuck on the airelays key"))
    assert bare.status == Status.blocked

    m = service.post_message(alice, "design", PostMessage(
        status=Status.blocked, body="need a decision", to=[bob.id],
        asks=[{"id": "1", "text": "choose the compatible schema?"}]))
    assert m.status == Status.blocked and m.to == [bob.id]


def test_assignee_is_a_real_addressee(service, team):
    """The storm-review assignee gap: an assignee creates owed debt, so it
    must pass the membership gate and set the addressing flags — a ghost
    name must not satisfy the blocked contract while waking the whole room."""
    alice, bob = team
    with pytest.raises(HubError, match="non-member"):
        service.post_message(alice, "design", PostMessage(
            status=Status.blocked, body="stuck",
            asks=[{"id": "1", "text": "unblock me?", "assignee": "ghost"}]))
    with pytest.raises(HubError, match="yourself"):
        service.post_message(alice, "design", PostMessage(
            status=Status.blocked, body="stuck",
            asks=[{"id": "1", "text": "unblock me?", "assignee": alice.id}]))
    m = service.post_message(alice, "design", PostMessage(
        status=Status.blocked, body="stuck",
        asks=[{"id": "1", "text": "unblock me?", "assignee": bob.id}]))
    env = _envelope(service, bob, m.id)
    assert env is not None and env.to_me and env.addressed


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
    """The optional ask `assignee` is control-stripped BEFORE the membership
    gate (so a member name wrapped in control chars resolves to the member),
    and an oversized name is REFUSED by the cap itself, naming the field.

    It used to be sliced to 64 and then rejected downstream as a non-member,
    which reported the wrong problem: the author saw "not a member" for a
    name that WAS a member's, mangled by a cap nobody mentioned."""
    from agora.models import TextTooLong

    alice, bob = team
    m = service.post_message(alice, "design", PostMessage(
        status=Status.open, body="q",
        asks=[{"id": "1", "text": "a", "assignee": "\tbob\n"}]))
    stored = service.db.get_message(m.id).data["asks"][0]["assignee"]
    assert stored == bob.id
    with pytest.raises(TextTooLong) as exc:
        service.post_message(alice, "design", PostMessage(
            status=Status.open, body="q",
            asks=[{"id": "1", "text": "a", "assignee": "bob" + "x" * 200}]))
    assert "ask assignee" in str(exc.value)
    # A ghost name of legal length is still a membership problem, as before.
    with pytest.raises(HubError, match="non-member"):
        service.post_message(alice, "design", PostMessage(
            status=Status.open, body="q",
            asks=[{"id": "1", "text": "a", "assignee": "nobody"}]))


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


# -- operator broadcasts: the 75-second discharge (live, 2026-08-01) ----------


def _op_msg(sender="laurent", to=None, data=None, status="open"):
    return Message(id="x", channel="c", seq=1, sender=sender, status=status,
                   data=data, to=to or [])


OPS = frozenset({"laurent"})


def test_operator_broadcast_survives_a_bystanders_partial_reply():
    """THE FIVE-REQUIREMENT CLOSE. at-test#382 was an operator broadcast
    carrying five requirements; a seat answered part of it 75 seconds later
    and binary discharge closed the whole thread — nothing pending, nothing
    escalating, four requirements silently abandoned. A bystander does not
    get to say what the human meant."""
    parent = _op_msg()
    bystander = _msg("editor", status="reply")
    assert discharge_state(parent, [bystander], OPS).discharged is False
    assert discharge_state(parent, [bystander], OPS).closed is False
    # Two bystanders are no better than one.
    assert discharge_state(parent, [bystander, _msg("reader", status="reply")],
                           OPS).discharged is False


def test_operator_broadcast_discharges_on_the_operators_own_word():
    """The human can always settle their own request."""
    parent = _op_msg()
    assert discharge_state(parent, [_msg("laurent", status="reply")],
                           OPS).discharged is True
    assert discharge_state(parent, [_msg("laurent", status="resolved")],
                           OPS).closed is True


def test_operator_broadcast_discharges_on_the_delegates_resolved():
    """The reporting delegate owns operator requests end to end (ruling
    2026-08-01), so their `resolved` is an accountable completion claim —
    but a mere reply from them is progress, not delivery."""
    parent = _op_msg()
    dels = frozenset({"reader"})
    cited = {"evidence": [{"kind": "fs", "ref": "out.md@3"}]}
    assert discharge_state(parent, [_msg("reader", status="reply")],
                           OPS, dels).discharged is False
    # A BARE `resolved` no longer closes an operator request (2026-08-04):
    # it is the shape a delegate used to assert a delivery it had not made.
    assert discharge_state(parent, [_msg("reader", status="resolved")],
                           OPS, dels).discharged is False
    # Pointing at what was delivered does close it.
    assert discharge_state(parent, [_msg("reader", data=cited,
                                         status="resolved")],
                           OPS, dels).discharged is True
    # A non-delegate's `resolved` still carries no authority here.
    assert discharge_state(parent, [_msg("editor", data=cited,
                                         status="resolved")],
                           OPS, frozenset()).discharged is False


def test_addressed_operator_commission_survives_any_mere_reply():
    """ADDRESSED IS NOT WEAKER (2026-08-04). The tightening originally
    keyed on operator messages naming NOBODY, so a commission that DID name
    its delegate kept any-reply discharge: scifi-novel#40 — an end-to-end
    novel commission addressed to its delegate — was closed on every ledger
    by a bystander's reply 67 seconds in, and when the fleet stalled mid-
    packaging nothing owed, chased, or escalated for 17.5 hours. An
    ask-less operator message now takes the operator rule whoever it
    names: only the operator's own word or the delegate's `resolved`
    completion report discharges it."""
    named = _op_msg(to=["book-assistant"])
    dels = frozenset({"book-assistant"})
    # A bystander's reply no longer closes the human's commission...
    assert discharge_state(named, [_msg("book-editor", status="reply")],
                           OPS, dels).discharged is False
    # ...and neither does the delegate's own planning ack.
    assert discharge_state(named, [_msg("book-assistant", status="reply")],
                           OPS, dels).discharged is False
    # The delegate's completion report DOES — when it CITES what it built;
    # a bare `resolved` is the unfalsifiable assertion this rule refuses.
    assert discharge_state(named, [_msg("book-assistant", status="resolved")],
                           OPS, dels).discharged is False
    assert discharge_state(
        named, [_msg("book-assistant", status="resolved",
                     data={"evidence": [{"kind": "fs", "ref": "n.md@7"}]})],
        OPS, dels).discharged is True
    assert discharge_state(named, [_msg("laurent", status="reply")],
                           OPS, dels).discharged is True


def test_peer_broadcasts_keep_plain_binary_discharge():
    """The operator rule is the exception; peers keep the cheap default."""
    peer = _msg("reader")
    assert discharge_state(peer, [_msg("editor", status="reply")],
                           OPS).discharged is True


def test_addressed_peer_work_ask_no_longer_discharges_on_a_bare_reply():
    """2026-08-11: an addressed peer work ask stays open through 'on it'.
    The addressee may clear their OWN /owed row later with a linked claim,
    but the thread itself remains open until an authoritative close."""
    addressed = Message(id="x", channel="c", seq=1, sender="reader",
                        status="open", to=["editor"], created_at=200.0)
    assert discharge_state(
        addressed, [_msg("editor", status="reply")], OPS,
        frozenset(), 0.0, 0.0, 0.0, 100.0,
    ).discharged is False
    # Pre-epoch rows keep the historical cheap rule.
    legacy = Message(id="y", channel="c", seq=2, sender="reader",
                     status="open", to=["editor"], created_at=50.0)
    assert discharge_state(
        legacy, [_msg("editor", status="reply")], OPS,
        frozenset(), 0.0, 0.0, 0.0, 100.0,
    ).discharged is True


ASKS3 = {"asks": [{"id": "1", "text": "who takes what?"},
                  {"id": "2", "text": "what could fail?"},
                  {"id": "3", "text": "what will you show me?"}]}
DELEGATES = frozenset({"reader"})


def test_answering_a_commissions_QUESTIONS_does_not_deliver_its_WORK():
    """rtype-open#10, measured 2026-08-06.

    The operator posted a build commission — hours of work in the BODY —
    and attached three kickoff asks: "who takes what?", "what could make
    this fail?", "what will you show me as proof?". Sixteen minutes in, ONE
    seat answered all three. From that instant the hub read the whole
    commission as settled: zero /owed rows, zero doctor rows, board empty,
    digest "29/29 decided". Ten hours later no game existed and no surface
    knew the request was alive.

    The perverse part: the ask-less operator rule was already strict, so
    ATTACHING GOOD QUESTIONS opted the commission OUT of every protection
    the at-test#382 and scifi-novel#40 incidents bought. Doctrine told
    operators to prefer the shape that was least protected.

    Per-ask discharge is UNCHANGED — a seat that answered its own row is
    not re-pinned. What changed is that clearing the questions no longer
    clears the instruction."""
    parent = _msg("laurent", data=ASKS3)
    answered = _msg("editor", data={"answers": ["1", "2", "3"]}, status="reply")

    ds = discharge_state(parent, [answered], OPS, DELEGATES, 0.0, 1.0)
    assert ds.pending == []                      # the QUESTIONS are answered
    assert ds.answered == ["1", "2", "3"]        # per-ask state is unchanged
    assert ds.discharged is False                # the COMMISSION is not
    assert ds.closed is False

    # A bare `resolved` from the delegate is still not delivery.
    bare = _msg("reader", status="resolved")
    assert discharge_state(parent, [answered, bare], OPS, DELEGATES,
                           0.0, 1.0).closed is False

    # Citing what was delivered settles it.
    cited = _msg("reader", status="resolved",
                 data={"evidence": [{"kind": "fs", "ref": "game.html@3"}]})
    assert discharge_state(parent, [answered, cited], OPS, DELEGATES,
                           0.0, 1.0).closed is True

    # ...and so does the operator's own word.
    assert discharge_state(parent, [answered, _msg("laurent", status="reply")],
                           OPS, DELEGATES, 0.0, 1.0).closed is True


def test_evidence_the_hub_could_not_verify_is_not_delivery():
    """`_validate_evidence` goes to real trouble to refuse to claim it saw
    bytes it cannot see — an `external` ref is stamped `verified: false`.
    The discharge gate threw that away, so a fabricated sha256 over a path
    that does not exist closed a commission."""
    parent = _msg("laurent", data=ASKS3)
    answered = _msg("editor", data={"answers": ["1", "2", "3"]}, status="reply")
    unverifiable = _msg("reader", status="resolved", data={"evidence": [
        {"kind": "external", "ref": "~/Desktop/rtype.app",
         "sha256": "0" * 64, "verified": False}]})
    assert discharge_state(parent, [answered, unverifiable], OPS, DELEGATES,
                           0.0, 1.0).closed is False
    # One resolvable citation alongside it is enough.
    mixed = _msg("reader", status="resolved", data={"evidence": [
        {"kind": "external", "ref": "x", "verified": False},
        {"kind": "fs", "ref": "game.html@3", "verified": True}]})
    assert discharge_state(parent, [answered, mixed], OPS, DELEGATES,
                           0.0, 1.0).closed is True


def test_the_asks_tightening_does_not_rewrite_the_past():
    """Its OWN epoch. Reusing the 2026-08-04 key would re-judge every
    ask-carrying operator message settled since it shipped — the
    132-message storm these guards exist to prevent."""
    old = Message(id="x", channel="c", seq=1, sender="laurent",
                  status="open", data=ASKS3, created_at=100.0)
    answered = _msg("editor", data={"answers": ["1", "2", "3"]}, status="reply")
    # epoch is AFTER the message: judged by the old rule, stays settled.
    assert discharge_state(old, [answered], OPS, DELEGATES,
                           0.0, 200.0).closed is True
    # a message posted after the epoch gets the new rule.
    fresh = Message(id="y", channel="c", seq=2, sender="laurent",
                    status="open", data=ASKS3, created_at=300.0)
    assert discharge_state(fresh, [answered], OPS, DELEGATES,
                           0.0, 200.0).closed is False


def test_a_peers_ask_carrying_message_is_unchanged():
    """The tightening is operator-only. An ordinary seat's canvass still
    discharges when its asks are answered."""
    parent = _msg("reader", data=ASKS3)
    answered = _msg("editor", data={"answers": ["1", "2", "3"]}, status="reply")
    assert discharge_state(parent, [answered], OPS, DELEGATES,
                           0.0, 1.0).closed is True


def test_commission_stays_owed_through_the_delegates_own_ack():
    """The /owed engagement rule ("replying at all drops the row") erased
    the novel commission from its delegate's ledger 62 seconds in: for a
    binary operator commission there is no other seat to carry the
    remainder, so the ack silenced the only debt and nothing chased the
    17.5h stall. The row now survives the delegate's mere replies and
    clears only on the completion report (`resolved`) or the operator's
    own engagement."""
    from agora.models import AgentInfo

    service = HubService(Database(":memory:"), rate_per_minute=600.0)
    op, _ = service.register_agent("laurent", "Laurent", operator=True, mission="seat laurent")
    delegate, _ = service.register_agent("assistant", "Assistant", mission="seat assistant")
    peer, _ = service.register_agent("editor", "Editor", mission="seat editor")
    service.create_channel(op, "novel", private=False)
    for a in (delegate, peer):
        service.join_channel(a, "novel", None)
    service.set_delegation("assistant", ["reporting", "operational"])

    m = service.post_message(op, "novel", PostMessage(
        body="write the novel end to end", status=Status.open,
        to=["assistant"]))

    def owed_ids(agent):
        return {r.id for r in service.owed(agent).to_answer}

    assert m.id in owed_ids(delegate)
    # A bystander's reply changes nothing for the delegate.
    service.post_message(peer, "novel", PostMessage(
        body="sounds exciting", status=Status.reply, reply_to=m.id))
    assert m.id in owed_ids(delegate)
    # The delegate's own planning ack no longer silences the commission.
    service.post_message(delegate, "novel", PostMessage(
        body="ack — decomposing now", status=Status.reply, reply_to=m.id))
    assert m.id in owed_ids(delegate)
    # A completion report that CITES NOTHING does not clear it either — the
    # 2026-08-04 shape: "5.1MB, 3 embedded images ... /path/to/novel", where
    # the channel filesystem held no such file. Since 2026-08-11 (fund1) the
    # hub refuses the shape outright, naming the recipe: silently achieving
    # nothing taught the live delegate nothing — it posted three uncited
    # "delivery complete" resolveds in a row.
    with pytest.raises(HubError) as exc:
        service.post_message(delegate, "novel", PostMessage(
            body="delivered: md/docx/pdf verified", status=Status.resolved,
            reply_to=m.id))
    assert exc.value.status_code == 400
    assert "data.evidence" in exc.value.detail
    assert m.id in owed_ids(delegate)
    # Pointing at the artifact is necessary but — in a room with peers —
    # not sufficient: an uncontested delivery (every citation self-authored)
    # is refused until a peer's verdict is cited too (2026-08-12 ruling).
    service.fs_write(delegate, "novel", "the_novel.md", "# ch1\n",
                     description="the manuscript")
    with pytest.raises(HubError) as exc:
        service.post_message(delegate, "novel", PostMessage(
            body="delivered", status=Status.resolved, reply_to=m.id,
            data={"evidence": [{"kind": "fs", "ref": "the_novel.md@1"}]}))
    assert exc.value.status_code == 400
    assert "uncontested" in exc.value.detail
    service.fs_write(peer, "novel", "review-novel.md",
                     "# verdict: checked against the commission\n",
                     description="editor's adversarial review")
    service.store_set(delegate, "novel", "plan:novel",
                      {"slices": {"assistant": "write", "editor": "review"}},
                      expect_version=0)
    service.post_message(delegate, "novel", PostMessage(
        body="delivered; planned and editor-reviewed", status=Status.resolved,
        reply_to=m.id,
        data={"evidence": [{"kind": "store", "ref": "plan:novel"},
                           {"kind": "fs", "ref": "the_novel.md@1"},
                           {"kind": "fs", "ref": "review-novel.md@1"}]}))
    assert m.id not in owed_ids(delegate)


def test_peer_ack_stays_owed_until_a_linked_claim_exists():
    """A peer's 'taking it' reply is not completion. The worker's own debt
    stays live until the room contains a linked claim row for the work."""
    service = HubService(Database(":memory:"), rate_per_minute=600.0)
    asker, _ = service.register_agent("reader", "Reader", mission="seat reader")
    worker, _ = service.register_agent("editor", "Editor", mission="seat editor")
    service.create_channel(asker, "design", private=False)
    service.join_channel(worker, "design", None)

    m = service.post_message(asker, "design", PostMessage(
        body="take the export lane", title="export lane", status=Status.open,
        to=["editor"]))

    def owed_ids(agent):
        return {r.id for r in service.owed(agent).to_answer}

    assert m.id in owed_ids(worker)
    service.post_message(worker, "design", PostMessage(
        body="on it", status=Status.reply, reply_to=m.id))
    assert m.id in owed_ids(worker)

    service.store_set(worker, "design", "claim:msg-1", {
        "owner": "editor", "status": "in_progress",
        "source_message_id": m.id, "next_step": "build the export lane",
    })
    assert m.id not in owed_ids(worker)


def test_linked_claim_matches_the_channel_seq_form_too():
    """Models cite claim sources as the human-readable `channel#seq` every
    doc and digest uses, not the message ULID. fund4 (2026-08-12): the
    id-only comparison left the excusal dead — 8 of 11 delegate reception
    turns scored `debt-remains` while a live claim named `commons#6`."""
    service = HubService(Database(":memory:"), rate_per_minute=600.0)
    asker, _ = service.register_agent("reader", "Reader", mission="seat reader")
    worker, _ = service.register_agent("editor", "Editor", mission="seat editor")
    service.create_channel(asker, "design", private=False)
    service.join_channel(worker, "design", None)
    m = service.post_message(asker, "design", PostMessage(
        body="take the export lane", title="export lane", status=Status.open,
        to=["editor"]))
    service.post_message(worker, "design", PostMessage(
        body="on it", status=Status.reply, reply_to=m.id))
    assert m.id in {r.id for r in service.owed(worker).to_answer}
    service.store_set(worker, "design", "claim:export", {
        "owner": "editor", "status": "in_progress",
        "source_message_id": f"design#{m.seq}",
        "next_step": "build the export lane",
    })
    assert m.id not in {r.id for r in service.owed(worker).to_answer}


def test_operator_engagement_also_clears_the_commission():
    """The human's own word settles their request wherever it stands."""
    service = HubService(Database(":memory:"), rate_per_minute=600.0)
    op, _ = service.register_agent("laurent", "Laurent", operator=True, mission="seat laurent")
    delegate, _ = service.register_agent("assistant", "Assistant", mission="seat assistant")
    service.create_channel(op, "novel", private=False)
    service.join_channel(delegate, "novel", None)
    service.set_delegation("assistant", ["reporting"])
    m = service.post_message(op, "novel", PostMessage(
        body="commission", status=Status.open, to=["assistant"]))
    service.post_message(delegate, "novel", PostMessage(
        body="ack", status=Status.reply, reply_to=m.id))
    assert m.id in {r.id for r in service.owed(delegate).to_answer}
    service.post_message(op, "novel", PostMessage(
        body="never mind — cancelling this", status=Status.reply,
        reply_to=m.id))
    assert m.id not in {r.id for r in service.owed(delegate).to_answer}


# -- evidence citations: a report you can check (2026-08-04) -----------------


def _novel_room():
    service = HubService(Database(":memory:"), rate_per_minute=600.0)
    op, _ = service.register_agent("laurent", "Laurent", operator=True, mission="seat laurent")
    delegate, _ = service.register_agent("assistant", "Assistant", mission="seat assistant")
    service.create_channel(op, "novel", private=False)
    service.join_channel(delegate, "novel", None)
    service.set_delegation("assistant", ["reporting"])
    return service, op, delegate


def test_evidence_must_resolve_in_this_channel():
    """THE PLACEHOLDER DELIVERY. The live report cited 'Channel filesystem:
    /path/to/novel' while the channel held no docx and zero blobs. A
    citation that does not resolve is refused by name, exactly as a dangling
    `settled_by` pointer already is."""
    service, op, delegate = _novel_room()
    for bad, why in [
        ({"kind": "fs", "ref": "the_novel.docx@7"}, "no such version"),
        ({"kind": "fs", "ref": "/path/to/novel"}, "not path@version"),
        ({"kind": "store", "ref": "claim:nope"}, "no such row"),
        ({"kind": "blob", "ref": "a" * 40}, "not uploaded"),
        ({"kind": "wishful", "ref": "x"}, "unknown kind"),
    ]:
        with pytest.raises(HubError) as e:
            service.post_message(delegate, "novel", PostMessage(
                body="done", status=Status.fyi, data={"evidence": [bad]}))
        assert e.value.status_code == 400, why


def test_evidence_size_comes_from_the_server_not_the_sender():
    """Attachments already refuse to let a message misdescribe its file
    ("size/content_type always come from the blob row"). A delivery claim
    gets the same treatment: the sender's 5.1MB is overwritten by the truth."""
    service, op, delegate = _novel_room()
    service.fs_write(delegate, "novel", "the_novel.md", "# ch1\n",
                     description="the manuscript")
    m = service.post_message(delegate, "novel", PostMessage(
        body="delivered", status=Status.fyi,
        data={"evidence": [{"kind": "fs", "ref": "the_novel.md@1",
                            "size_bytes": 5_100_000,   # the seat's claim
                            "updated_by": "somebody-else",
                            "verified": True}]}))
    ref = m.data["evidence"][0]
    assert ref["size_bytes"] != 5_100_000       # server truth won
    assert ref["updated_by"] == "assistant"     # attribution is the hub's
    assert ref["verified"] is True


def test_external_evidence_is_hash_pinned_and_never_marked_verified():
    """Work delivered outside agora's surfaces (a file on the operator's
    Desktop) still gets to be cited — but the hub must never imply it
    checked bytes it cannot read. A hash is falsifiable in one command;
    '/path/to/novel' is not."""
    service, op, delegate = _novel_room()
    with pytest.raises(HubError):     # no sha256
        service.post_message(delegate, "novel", PostMessage(
            body="d", status=Status.fyi,
            data={"evidence": [{"kind": "external", "ref": "~/Desktop/x.pdf"}]}))
    m = service.post_message(delegate, "novel", PostMessage(
        body="d", status=Status.fyi,
        data={"evidence": [{"kind": "external", "ref": "~/Desktop/x.pdf",
                            "sha256": "b" * 64, "size_bytes": 7156666,
                            "verified": True}]}))
    assert m.data["evidence"][0]["verified"] is False


def test_the_operator_rule_change_does_not_rewrite_the_past():
    """SEMANTICS CHANGES MUST NOT REWRITE THE PAST. Tightening the operator
    rule re-opened 132 ask-less operator messages on the live hub that had
    been discharged under the old rule — every one instantly SLA-breached,
    across 23 seats, the oldest 19 days. This is the same class the
    `_directive_epoch` guard exists for, and it needed the same guard."""
    epoch = 1_000_000.0
    old = Message(id="x", channel="c", seq=1, sender="laurent", status="open",
                  to=["worker"], created_at=epoch - 60)
    new = Message(id="y", channel="c", seq=2, sender="laurent", status="open",
                  to=["worker"], created_at=epoch + 60)
    bystander = [_msg("editor", status="reply")]
    # Settled under the rule in force when it was written: stays settled.
    assert discharge_state(old, bystander, OPS,
                           operator_rule_epoch=epoch).discharged is True
    # Written after the change: judged by the new rule.
    assert discharge_state(new, bystander, OPS,
                           operator_rule_epoch=epoch).discharged is False
    # With no epoch configured the new rule applies to everything (unit use).
    assert discharge_state(old, bystander, OPS).discharged is False


# -- a roll call is not answered by the first voter ------------------------

def test_a_canvass_is_not_discharged_by_one_of_its_addressees():
    """THE PARTICIPATION HOLE (measured 2026-08-06: 9 of 28 multi-addressee
    asks on this hub discharged with a named seat still silent).

    An ask carrying `to=[a,b,c]` was fully answered the instant ANYONE
    replied with that id. The silent addressees were unpinned, dropped from
    /owed, erased from the asker's `waiting_on`, and their `to_me` flipped
    back to false — another seat's reply doing what the addressee's own bare
    read is explicitly forbidden to do.

    This matters most in exactly the room it was built for: an operator
    asking several perspectives to converge. The critic's silence must be a
    standing debt, not a row a colleague's answer quietly deletes."""
    canvass = _msg("reader", data={"asks": [
        {"id": "1", "text": "your view?", "to": ["editor", "critic", "qa"]}]})

    one = _msg("editor", data={"answers": ["1"]}, status="reply")
    ds = discharge_state(canvass, [one], OPS, DELEGATES, 0.0, 0.0, 1.0)
    assert ds.pending == ["1"] and ds.discharged is False

    two = _msg("critic", data={"answers": ["1"]}, status="reply")
    assert discharge_state(canvass, [one, two], OPS, DELEGATES,
                           0.0, 0.0, 1.0).pending == ["1"]

    three = _msg("qa", data={"answers": ["1"]}, status="reply")
    ds = discharge_state(canvass, [one, two, three], OPS, DELEGATES,
                         0.0, 0.0, 1.0)
    assert ds.pending == [] and ds.answered == ["1"] and ds.discharged is True


def test_a_seat_the_ask_never_named_cannot_answer_for_the_room():
    """scifi-novel#18: three writers named, one answered, and the discharge
    was helped along by a seat that was never named at all."""
    canvass = _msg("reader", data={"asks": [
        {"id": "1", "text": "your view?", "to": ["editor", "critic"]}]})
    stranger = _msg("bystander", data={"answers": ["1"]}, status="reply")
    assert discharge_state(canvass, [stranger], OPS, DELEGATES,
                           0.0, 0.0, 1.0).pending == ["1"]


def test_an_unaddressed_ask_still_discharges_on_any_reply():
    """The tightening reaches only asks that NAMED someone. The asker's own
    declaration is the whole rule — want one answer? name one seat."""
    open_ask = _msg("reader", data={"asks": [{"id": "1", "text": "anyone?"}]})
    assert discharge_state(open_ask, [_msg("editor", data={"answers": ["1"]},
                                           status="reply")],
                           OPS, DELEGATES, 0.0, 0.0, 1.0).discharged is True


def test_the_canvass_rule_does_not_rewrite_the_past():
    """28 historical multi-addressee asks would otherwise re-open at once,
    instantly SLA-breached — the 132-message storm, again."""
    old = Message(id="x", channel="c", seq=1, sender="reader", status="open",
                  created_at=100.0, data={"asks": [
                      {"id": "1", "text": "q", "to": ["editor", "critic"]}]})
    one = _msg("editor", data={"answers": ["1"]}, status="reply")
    assert discharge_state(old, [one], OPS, DELEGATES,
                           0.0, 0.0, 200.0).discharged is True
