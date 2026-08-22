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
from agora.hub.service import HubError, HubService
from agora.models import PostMessage


@pytest.fixture()
def service() -> HubService:
    return HubService(Database(":memory:"), rate_per_minute=600.0)


@pytest.fixture()
def fleet(service):
    """An operator, a reporting delegate, and two ordinary seats in a room."""
    op, _ = service.register_agent("laurent", "Laurent", operator=True, mission="seat laurent")
    reader, _ = service.register_agent("reader", "Reader", mission="seat reader")
    editor, _ = service.register_agent("editor", "Editor", mission="seat editor")
    at1, _ = service.register_agent("at1", "At1", mission="seat at1")
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


def test_an_unaddressed_operator_message_draws_no_warning(service, fleet):
    """Operator ruling, 2026-08-22: "a message on a channel with no @seat is
    totally ok, it just means it's addressed to all — then each seat decides
    if they can contribute or not."

    The hub used to warn the human that such a message "creates NO obligation
    for anyone". That was written when a room-wide open woke nobody, so the
    request really could evaporate; since the wake rule was repaired, an open
    naming nobody wakes every member. The room hears it, and what it obliges
    is a read."""
    op, reader, editor, _ = fleet
    m = _post(service, op, "reply", body="rebuild the PDF", title="task",
              root_seat=editor)
    assert m.id not in _owed_ids(service, reader)  # still obliges no ONE seat
    for room in (service.DARK_ALERTS_CHANNEL,
                 f"dm:hub--{op.id}" if "hub" < op.id else f"dm:{op.id}--hub"):
        assert not [x for x in service.db.get_messages(room, 0, 50)
                    if "HUB WARNING" in (x.body or "")], room


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
    # The delegate's own `resolved` settles it — but only as an accountable
    # claim, which since 2026-08-04 means one that POINTS at the delivery.
    # A bare `resolved` is the shape a delegate used to close a commission
    # it had not delivered, and nothing could tell it from a real one.
    # Since 2026-08-11 (fund1) the hub REFUSES it at post time with the
    # recipe: silently achieving nothing taught the delegate nothing — a
    # live seat posted three uncited "delivery complete" resolveds in a row,
    # each spawned by the very debt the previous one failed to clear.
    with pytest.raises(HubError) as exc:
        service.post_message(reader, "at-test", PostMessage(
            body="delivered: pdf rebuilt and reported", status="resolved",
            reply_to=task.id))
    assert exc.value.status_code == 400
    assert "data.evidence" in exc.value.detail
    assert task.id in _owed_ids(service, reader), \
        "an uncited completion report closed the operator's request"
    service.fs_write(reader, "at-test", "report.md", "# done\n",
                     description="the rebuilt report")
    # In a room with peers, the report must also cite a peer-authored
    # artifact (adversarial-review ruling, 2026-08-12).
    service.fs_write(editor, "at-test", "review-report.md",
                     "# verdict: matches the request\n",
                     description="editor's review")
    service.store_set(reader, "at-test", "plan:pdf",
                      {"slices": {"reader": "rebuild", "editor": "review"}},
                      expect_version=0)
    service.post_message(reader, "at-test", PostMessage(
        body="delivered: pdf rebuilt and reported", status="resolved",
        reply_to=task.id,
        data={"evidence": [{"kind": "store", "ref": "plan:pdf"},
                           {"kind": "fs", "ref": "report.md@1"},
                           {"kind": "fs", "ref": "review-report.md@1"}]}))
    assert task.id not in _owed_ids(service, reader)


def test_bystander_cannot_settle_operators_request_via_settled_by_pointer(
        service, fleet):
    """The operator-request guard in `_prepare_structured()` must refuse
    with a HubError, not crash on an undefined local."""
    op, reader, editor, at1 = fleet
    _delegate(service)
    task = service.post_message(op, "at-test", PostMessage(
        body="deliver the package and report it", status="open",
        title="package commission"))
    anchor = service.post_message(editor, "at-test", PostMessage(
        body="supporting note", status="fyi"))
    with pytest.raises(HubError) as exc:
        service.post_message(at1, "at-test", PostMessage(
            body="settled elsewhere", status="resolved", reply_to=task.id,
            data={"settled_by": anchor.id}))
    assert exc.value.status_code == 403
    assert "only the operator, or a reporting delegate citing evidence" in exc.value.detail


# -- fund1 regressions (2026-08-11): the funded opencode soak -----------------


def test_structured_commission_releases_addressee_who_answered_their_asks(
        service, fleet):
    """fund1 commons#6: a commission with per-seat asks pinned EVERY
    addressee until commission discharge — a seat that had answered the one
    ask naming it (envelope: asks_naming_you=[]) was still re-woken by its
    /owed row forever. An addressee is released once no pending ask names
    it; the reporting delegate alone carries the commission to completion."""
    op, reader, editor, at1 = fleet
    _delegate(service)  # reader is the reporting delegate
    task = service.post_message(op, "at-test", PostMessage(
        body="build the prototype; self-organize",
        status="open", title="commission", to=["reader", "editor", "at1"],
        asks=[{"id": "1", "text": "carry end to end", "to": ["reader"]},
              {"id": "2", "text": "your slice?", "to": ["editor"]},
              {"id": "3", "text": "your slice?", "to": ["at1"]}]))
    assert task.id in _owed_ids(service, editor)
    service.post_message(editor, "at-test", PostMessage(
        body="I own the editor slice", status="reply", reply_to=task.id,
        answers=["2"]))
    assert task.id not in _owed_ids(service, editor), \
        "an addressee who answered every ask naming it stayed pinned"
    # at1 has not answered ask 3: still owed.
    assert task.id in _owed_ids(service, at1)
    # The delegate answered its ask but still carries the commission until
    # the evidence-cited completion report.
    service.post_message(reader, "at-test", PostMessage(
        body="owned end to end", status="reply", reply_to=task.id,
        answers=["1"]))
    assert task.id in _owed_ids(service, reader), \
        "the reporting delegate was released before the completion report"


def test_askless_commission_still_pins_engaged_addressees(service, fleet):
    """The release is per-ask only: an ask-less operator broadcast keeps
    every addressee pinned after a mere engagement reply (the 75-second-
    discharge protection is unchanged)."""
    op, reader, editor, _ = fleet
    _delegate(service)
    task = service.post_message(op, "at-test", PostMessage(
        body="five requirements in prose", status="open",
        title="broadcast", to=["editor"]))
    service.post_message(editor, "at-test", PostMessage(
        body="on it", status="reply", reply_to=task.id))
    assert task.id in _owed_ids(service, editor), \
        "an engagement reply released an ask-less commission addressee"


def test_delegate_uncited_resolved_on_commission_is_refused_with_recipe(
        service, fleet):
    """fund1: the delegate posted three uncited 'delivery complete'
    resolveds, each spawned by the debt the previous one failed to clear.
    The hub now refuses the shape at post time and names the recipe."""
    op, reader, editor, _ = fleet
    _delegate(service)
    task = service.post_message(op, "at-test", PostMessage(
        body="build it", status="open", title="commission",
        asks=[{"id": "1", "text": "carry it", "to": ["reader"]}]))
    with pytest.raises(HubError) as exc:
        service.post_message(reader, "at-test", PostMessage(
            body="delivery complete", status="resolved", reply_to=task.id))
    assert exc.value.status_code == 400
    assert "data.evidence" in exc.value.detail
    # fund1's real shape: every ask answered, THEN the cited report.
    # A store citation satisfies it end to end — but in a room with peers
    # the delegate's own row is not enough (2026-08-12): a peer-authored
    # verdict must ride the evidence too. Then the commission leaves EVERY
    # seat's ledger, including the delegate's.
    service.post_message(reader, "at-test", PostMessage(
        body="owned; carrying it", status="reply", reply_to=task.id,
        answers=["1"]))
    service.store_set(reader, "at-test", "decision:delivered",
                      {"what": "shipped"}, expect_version=0)
    with pytest.raises(HubError) as exc:
        service.post_message(reader, "at-test", PostMessage(
            body="delivery complete", status="resolved", reply_to=task.id,
            data={"evidence": [{"kind": "store",
                                "ref": "decision:delivered"}]}))
    assert "uncontested" in exc.value.detail
    service.store_set(editor, "at-test", "review:delivered",
                      {"verdict": "checked against the commission"},
                      expect_version=0)
    # ...and a peer review alone is still not enough: the report must cite
    # the agreed plan it delivered under (plan-mandatory ruling, 2026-08-12).
    with pytest.raises(HubError) as exc:
        service.post_message(reader, "at-test", PostMessage(
            body="delivery complete; editor reviewed", status="resolved",
            reply_to=task.id,
            data={"evidence": [
                {"kind": "store", "ref": "decision:delivered"},
                {"kind": "store", "ref": "review:delivered"}]}))
    assert "plan" in exc.value.detail
    service.store_set(reader, "at-test", "plan:build",
                      {"slices": {"reader": "carry", "editor": "review"}},
                      expect_version=0)
    service.post_message(reader, "at-test", PostMessage(
        body="delivery complete; planned, built, editor reviewed",
        status="resolved", reply_to=task.id,
        data={"evidence": [{"kind": "store", "ref": "plan:build"},
                           {"kind": "store", "ref": "decision:delivered"},
                           {"kind": "store", "ref": "review:delivered"}]}))
    assert task.id not in _owed_ids(service, reader)
    assert task.id not in _owed_ids(service, editor)


def test_bystander_plain_resolved_is_not_refused(service, fleet):
    """The refusal is scoped to the reporting delegate (whose resolved IS
    the completion report). A bystander's plain resolved reply stays legal
    and simply does not close anything."""
    op, reader, editor, _ = fleet
    _delegate(service)
    task = service.post_message(op, "at-test", PostMessage(
        body="build it", status="open", title="commission"))
    service.post_message(editor, "at-test", PostMessage(
        body="fwiw looks done", status="resolved", reply_to=task.id))
    assert task.id in _owed_ids(service, reader)


def test_a_named_seat_can_close_an_operator_request_with_evidence(service, fleet):
    """The missing door, reported by three seats independently (commons#70,
    #74): an operator posts `open`, the named seat answers in-thread, and the
    row stays and escalates against the one seat that did the work.

    Every exit was shut. `answers=[...]` needs ask ids an ask-less commission
    does not have; `settled_by` is refused on an operator's request. The
    hub's advice was "Answer it", and answering was exactly what did not
    clear it — so a seat that delivered looked identical to a seat that
    ignored the human.

    The 2026-08-04 lesson is kept whole: a bare "on it" still settles
    nothing. What clears the row is a `resolved` reply CITING what was
    delivered — the same price the reporting delegate already paid."""
    op, reader, editor, _ = fleet
    f = service.fs_write(editor, "at-test", "plan.md", content="the plan")
    evidence = [{"kind": "fs", "ref": f"plan.md@{f.version}"}]

    commission = service.post_message(op, "at-test", PostMessage(
        body="write up the plan", status="open", title="plan", to=["editor"]))
    assert commission.id in _owed_ids(service, editor)

    # "on it" is engagement, not completion — the row stays.
    service.post_message(editor, "at-test", PostMessage(
        body="on it", status="reply", reply_to=commission.id))
    assert commission.id in _owed_ids(service, editor)

    # A cited completion report from the NAMED seat clears it.
    service.post_message(editor, "at-test", PostMessage(
        body="delivered", status="resolved", reply_to=commission.id,
        data={"evidence": evidence}))
    assert commission.id not in _owed_ids(service, editor)


def test_only_the_named_seat_or_a_delegate_may_report_completion(service, fleet):
    """The door is the ADDRESSEE's, not the room's, and an UNADDRESSED
    commission still has no addressee to pay the price — so it stays the
    operator's to close, which is the case the 2026-08-04 rule was for."""
    op, reader, editor, _ = fleet
    f = service.fs_write(editor, "at-test", "other.md", content="x")
    evidence = [{"kind": "fs", "ref": f"other.md@{f.version}"}]

    named = service.post_message(op, "at-test", PostMessage(
        body="editor, do X", status="open", title="x", to=["editor"]))
    service.post_message(reader, "at-test", PostMessage(
        body="I did it", status="resolved", reply_to=named.id,
        data={"evidence": evidence}))
    assert named.id in _owed_ids(service, editor), "a bystander closed it"

    unaddressed = service.post_message(op, "at-test", PostMessage(
        body="someone look at this", status="open", title="loose"))
    service.post_message(reader, "at-test", PostMessage(
        body="looked", status="resolved", reply_to=unaddressed.id,
        data={"evidence": evidence}))
    state = service._discharge(unaddressed, service.db.replies_to(unaddressed.id))
    assert not state.closed, "an unaddressed commission closed without the operator"


def test_store_evidence_accepts_the_documented_key_at_version(service, fleet):
    """Reported by agora-wui the first time anyone used the completion door
    in anger: `fs` evidence REQUIRES `path@version`, while `store` looked its
    ref up verbatim — so citing a store row the documented way searched for a
    key literally named `decision:foo@1` and answered "does not exist" about a
    row `store_get` had served the same minute. Opposite conventions, and an
    error that sent the author hunting a missing row instead of a wrong form."""
    op, reader, editor, _ = fleet
    service.store_set(editor, "at-test", "decision:thing", {"what": "ruled"}, None)
    commission = service.post_message(op, "at-test", PostMessage(
        body="do it", status="open", title="t", to=["editor"]))

    def close(ref):
        return service.post_message(editor, "at-test", PostMessage(
            body="delivered", status="resolved", reply_to=commission.id,
            data={"evidence": [{"kind": "store", "ref": ref}]}))

    posted = close("decision:thing@1")
    assert posted.data["evidence"][0]["ref"] == "decision:thing"  # normalized
    assert posted.data["evidence"][0]["verified"] is True
    assert commission.id not in _owed_ids(service, editor)
    close("decision:thing")                                       # bare: still fine

    # A stale version is refused with the truth — the store keeps only HEAD,
    # so v1 cannot be served back and stamping HEAD under the author's older
    # number would misrepresent what a reader will see.
    service.store_set(editor, "at-test", "decision:thing", {"what": "revised"}, 1)
    with pytest.raises(HubError) as bad:
        close("decision:thing@1")
    assert "now at v2" in bad.value.detail
    close("decision:thing@2")                                     # current: fine


def test_a_named_seats_uncited_resolved_is_refused_with_the_recipe(service, fleet):
    """THE ADDRESSEE WAS PAYING THE DELEGATE'S PRICE WITHOUT BEING TOLD IT
    (2026-08-22). `_operator_settled` accepts a cited `resolved` from a seat
    the operator NAMED as well as from the reporting delegate — that door is
    the 2026-08-04 fix. The post-time gate that teaches the contract fired
    only for delegates, so a named seat's evidence-less `resolved` was
    accepted in silence and discharged nothing.

    Live, on this hub: agora-wui posted three such replies on laurent's
    ask-less DM opens. All three were accepted, all three stayed ANSWER-owed,
    and the seat ran a controlled experiment to work out why. Every exit was
    shut and none of them was named — the exact complaint the 2026-08-04
    comment in `_message_data` records for the delegate."""
    op, reader, editor, _ = fleet
    commission = service.post_message(op, "at-test", PostMessage(
        body="have a look at this", status="open", title="look", to=["editor"]))

    with pytest.raises(HubError) as refused:
        service.post_message(editor, "at-test", PostMessage(
            body="had a look, here is what it is", status="resolved",
            reply_to=commission.id))
    assert "named you on at-test#" in refused.value.detail
    assert "data.evidence" in refused.value.detail
    # The refusal must name the OTHER legitimate exit, not just the priced
    # one: answering a question is a complete turn, and the asker closes it.
    assert "ordinary reply" in refused.value.detail

    # That other exit is real: the plain reply posts, and it leaves the row
    # owed — which is correct, and now discoverable rather than mysterious.
    service.post_message(editor, "at-test", PostMessage(
        body="had a look, here is what it is", status="reply",
        reply_to=commission.id))
    assert commission.id in _owed_ids(service, editor)

    # And the priced exit clears it.
    service.store_set(editor, "at-test", "decision:looked", {"what": "it is X"}, None)
    service.post_message(editor, "at-test", PostMessage(
        body="reported", status="resolved", reply_to=commission.id,
        data={"evidence": [{"kind": "store", "ref": "decision:looked@1"}]}))
    assert commission.id not in _owed_ids(service, editor)


def test_the_named_seat_pays_the_citation_but_not_the_delegates_review_gates(
        service, fleet):
    """Scope of the 2026-08-22 widening. The CITATION requirement follows
    `_operator_settled`, so it covers both seats. The adversarial-review and
    plan-citation gates are about how a DELEGATE delivers a commission — a
    named seat reporting its own slice must not be asked for a peer review of
    it, or the widening would make the addressee door unusable in exactly the
    peopled rooms it was built for."""
    op, reader, editor, at1 = fleet
    commission = service.post_message(op, "at-test", PostMessage(
        body="your slice", status="open", title="slice", to=["editor"]))
    service.store_set(editor, "at-test", "decision:my-slice", {"done": True}, None)
    # Self-authored, no peer review, no plan row: fine for a named seat.
    service.post_message(editor, "at-test", PostMessage(
        body="slice delivered", status="resolved", reply_to=commission.id,
        data={"evidence": [{"kind": "store", "ref": "decision:my-slice@1"}]}))
    assert commission.id not in _owed_ids(service, editor)

    # The delegate posting the identical report still meets the review gate.
    _delegate(service)
    commission2 = service.post_message(op, "at-test", PostMessage(
        body="the whole thing", status="open", title="all", to=["reader"]))
    service.store_set(reader, "at-test", "decision:whole", {"done": True}, None)
    with pytest.raises(HubError) as refused:
        service.post_message(reader, "at-test", PostMessage(
            body="delivered", status="resolved", reply_to=commission2.id,
            data={"evidence": [{"kind": "store", "ref": "decision:whole@1"}]}))
    assert "uncontested delivery" in refused.value.detail
