"""THE INVARIANT: a sender never sees their own message as unread.

Operator rule (2026-08-20), now law for this repo:

    A sender must NEVER see their own message as unread in their own inbox,
    unless there is an excellent justification that is documented and
    machine-distinguishable by clients.

Authorship is not attention. A seat that had to triage its own posts would
pay a turn to read what it just wrote, and — worse — every anti-lurk alarm
downstream (`agora status` unread, the stop hook, the dark/deaf watchdogs)
counts inbox rows, so self-delivery inflates exactly the numbers the fleet
uses to find seats that are NOT working.

The hub honours this in five independent places, and this file pins ALL of
them at once rather than trusting any single guard:

  * `service.inbox()` cursor sweep      — `message.sender != agent.id`
  * `db.unread_criticals`               — `m.sender != ?` in SQL
  * `db.obligation_candidates`          — `m.sender != ?` in SQL
  * `_is_addressed_debt` / `_operator_delegate_debt` — `m.sender == viewer_id`
  * `owed().to_answer`                  — `if m.sender == agent.id: continue`

...plus the surfaces a client reads as "you owe this": the notify file
(`<seat>-inbox.log`, which IS an inbox), and the board's `pending_on_me`,
whose own docstring calls it "the inbox stickiness predicate served as a
query". The board is where the invariant BROKE (2026-08-20): it tested only
`to`, and message-level `to` is the one self-address the post gate still
allows, so `to=["me"]` read as PENDING ON YOU while /inbox and /owed both
said the message owed nobody.

THE ONE JUSTIFIED CASE, and why it is not a violation: an author does owe
CLOSURE on their own settled thread ("status=resolved + reply_to +
decision:<slug>"). The hub serves that as `owed().to_close` — a separate,
named row class carrying `answered_by`/`answered_at`, never an `Envelope`.
`test_own_thread_closure_duty_is_a_separate_named_class` pins that shape: if
anyone ever moves the closure duty into `/inbox`, this file fails.

If a test here fails, do not relax it. Deliver the duty on a surface a client
can tell apart from unread mail.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agora.db import Database
from agora.hub.notify_sink import NotifySink
from agora.hub.service import CHANNEL_META_KEY, HubError, HubService
from agora.models import PostMessage, Status, Urgency


@pytest.fixture()
def service(tmp_path) -> HubService:
    return HubService(Database(":memory:"), rate_per_minute=6000.0,
                      notify_sink=NotifySink(tmp_path / "notify"))


@pytest.fixture()
def fleet(service):
    """An operator, the seat under test, and two peers in one room."""
    op, _ = service.register_agent("op", "Op", operator=True, mission="seat op")
    alice, _ = service.register_agent("alice", "Alice", mission="seat alice")
    bob, _ = service.register_agent("bob", "Bob", mission="seat bob")
    carol, _ = service.register_agent("carol", "Carol", mission="seat carol")
    service.create_channel(alice, "design", private=True)
    for seat in (op, bob, carol):
        service.join_channel(
            seat, "design",
            service.create_invite(alice, "design", invitee=seat.id))
    return op, alice, bob, carol


def own_envelopes(service, seat):
    """Every envelope in this seat's OWN inbox that this seat itself wrote."""
    return [e for e in service.inbox(seat) if e.sender == seat.id]


def assert_no_own_delivery(service, seat, where: str) -> None:
    """The invariant, asserted loudly on every surface a client reads as mail."""
    own = own_envelopes(service, seat)
    assert not own, (
        f"INVARIANT BROKEN ({where}): {seat.id} received {len(own)} envelope(s) "
        f"for messages {seat.id} SENT — "
        + ", ".join(f"{e.channel}#{e.seq} status={e.status.value} "
                    f"critical={e.critical} escalated={e.escalated} id={e.id}"
                    for e in own)
        + ". A sender never triages their own post; if this is a genuine duty "
          "(e.g. closing your own thread) it belongs in owed().to_close or "
          "another named class, NOT in an indistinguishable inbox envelope.")
    owed_own = [r for r in service.owed(seat).to_answer if r.sender == seat.id]
    assert not owed_own, (
        f"INVARIANT BROKEN ({where}): owed().to_answer names {seat.id}'s own "
        f"messages {[r.id for r in owed_own]} as answers THEY owe.")
    board_own = [r for r in service.board(seat)["pending_on_me"]
                 if r["sender"] == seat.id]
    assert not board_own, (
        f"INVARIANT BROKEN ({where}): board.pending_on_me — 'the inbox "
        f"stickiness predicate served as a query', rendered as PENDING ON YOU "
        f"by the CLI, the chat TUI, MCP get_board and the driver's turn "
        f"context — carries {seat.id}'s OWN messages "
        f"{[(r['channel'], r['seq']) for r in board_own]}.")


def assert_notify_file_clean(service, seat_id: str, where: str) -> None:
    """The hub-written notify file is literally `<seat>-inbox.log`: a tailer
    (`agora listen`) treats each line as arriving mail. Own posts must never
    appear — hub doorbells about your post are fine, they carry sender='hub',
    a reserved id no agent can register."""
    path = Path(service.notify_sink._dir) / f"{seat_id}-inbox.log"
    if not path.exists():
        return
    own = [json.loads(line) for line in path.read_text().splitlines()
           if json.loads(line)["sender"] == seat_id]
    assert not own, (
        f"INVARIANT BROKEN ({where}): {seat_id}-inbox.log carries "
        f"{len(own)} line(s) authored by {seat_id}: {own}")


# -- the table: every shape a seat can post -----------------------------------
#
# Each entry is (label, payload-factory). The factory takes the peers' ids so
# a shape can address someone real. Shapes the post gate REFUSES are pinned
# separately below (`test_self_addressing_is_refused_where_the_hub_refuses_it`)
# — a refusal is a defence, and it must not silently become an acceptance.

MESSAGE_SHAPES = [
    ("fyi broadcast",
     lambda: PostMessage(body="fyi", title="fyi")),
    ("fyi addressed to a peer",
     lambda: PostMessage(body="fyi bob", title="fyi-bob", to=["bob"])),
    ("fyi SELF-addressed",
     lambda: PostMessage(body="note to self", title="fyi-self", to=["alice"])),
    ("open broadcast (no to)",
     lambda: PostMessage(body="anyone?", title="open", status=Status.open)),
    ("open SELF-addressed",
     lambda: PostMessage(body="mine", title="open-self", status=Status.open,
                         to=["alice"])),
    ("open addressed to a peer",
     lambda: PostMessage(body="bob?", title="open-bob", status=Status.open,
                         to=["bob"])),
    ("open addressed to SELF AND a peer",
     lambda: PostMessage(body="us", title="open-both", status=Status.open,
                         to=["alice", "bob"])),
    ("blocked broadcast",
     lambda: PostMessage(body="stuck", title="blocked", status=Status.blocked)),
    ("blocked SELF-addressed",
     lambda: PostMessage(body="stuck alone", title="blocked-self",
                         status=Status.blocked, to=["alice"])),
    ("open with per-ask to (canvass)",
     lambda: PostMessage(body="canvass", title="canvass", status=Status.open,
                         asks=[{"id": "q1", "text": "bob?", "to": ["bob"]},
                               {"id": "q2", "text": "carol?", "to": ["carol"]}])),
    ("open with an ask assignee",
     lambda: PostMessage(body="assigned", title="assigned", status=Status.open,
                         asks=[{"id": "q1", "text": "sign?", "assignee": "bob"}])),
    ("open with an UNADDRESSED ask (everyone's)",
     lambda: PostMessage(body="who?", title="open-ask", status=Status.open,
                         asks=[{"id": "q1", "text": "who takes this?"}])),
    ("open self-addressed WITH a peer ask",
     lambda: PostMessage(body="mixed", title="mixed", status=Status.open,
                         to=["alice"],
                         asks=[{"id": "q1", "text": "bob?", "to": ["bob"]}])),
    ("interrupt urgency (budget may downgrade it)",
     lambda: PostMessage(body="urgent", title="urgent", status=Status.open,
                         urgency=Urgency.interrupt)),
    ("self-@mention in the body",
     lambda: PostMessage(body="ping @alice — mine to do", title="selfmention",
                         status=Status.open)),
]


def _post_every_shape(service, seat, channel="design"):
    """Post the whole table from `seat`; returns the stored messages."""
    posted = []
    for label, factory in MESSAGE_SHAPES:
        payload = factory()
        if seat.id != "alice":
            # The table names alice as the self-address target; retarget it.
            payload = payload.model_copy(update={
                "to": [seat.id if t == "alice" else t for t in payload.to]})
        posted.append((label, service.post_message(seat, channel, payload)))
    return posted


# -- the broad pin -------------------------------------------------------------


def test_no_shape_a_seat_posts_ever_lands_in_its_own_inbox(service, fleet):
    """The table-driven pin: post EVERY shape, then walk the inbox through
    every lifecycle state that could resurrect a message (fresh, acked past,
    read, replied to, escalated past the SLA) and assert zero own envelopes
    at each step. This is the test that must fail loudly if anyone
    re-introduces self-delivery."""
    op, alice, bob, carol = fleet
    posted = _post_every_shape(service, alice)
    assert len(posted) == len(MESSAGE_SHAPES)
    assert_no_own_delivery(service, alice, "fresh, cursor at zero")

    # Acking is what strips ordinary unread and leaves ONLY the sticky classes
    # (unread criticals, obligation candidates, addressed debts) — the paths
    # that survive a cursor and were never proven to exclude the author.
    service.ack_inbox(alice, {"design": service.db.last_seq("design")})
    assert_no_own_delivery(service, alice, "after acking to head")

    # Reading a peer's traffic must not drag the author's own rows back.
    for _label, m in posted:
        service.read_message(alice, "design", m.id)
    assert_no_own_delivery(service, alice, "after reading every own message")

    # A peer replying re-opens threads on every surface; still not the author's.
    for _label, m in posted:
        if m.status in (Status.open, Status.blocked):
            service.post_message(bob, "design", PostMessage(
                body="noted", status=Status.reply, reply_to=m.id))
    assert_no_own_delivery(service, alice, "after peer replies arrived")

    assert_notify_file_clean(service, "alice", "after the full lifecycle")
    # ...and the peers DID get the traffic, so this is not a vacuous pass.
    assert any(e.sender == "alice" for e in service.inbox(bob)), \
        "sanity: bob must see alice's messages, or the pin proves nothing"


def test_escalation_past_the_sla_never_repins_the_author(service, fleet):
    """Attack 6: escalation raises a rotting obligation to `interrupt` and
    re-delivers it. Does the hub re-pin it to EVERYONE including the author?"""
    op, alice, bob, carol = fleet
    service.store_set(alice, "design", CHANNEL_META_KEY,
                      {"response_sla_minutes": 0.001})
    _post_every_shape(service, alice)
    service.ack_inbox(alice, {"design": service.db.last_seq("design")})
    time.sleep(0.15)
    assert_no_own_delivery(service, alice, "own obligations rotted past the SLA")
    assert any(e.escalated for e in service.inbox(bob)), \
        "sanity: the SLA must actually have escalated something for bob"
    # The re-wake sweeps write notify lines from owed().to_answer.
    service.presence.mark_reception("alice")
    service._escalation_rewake_sweep()
    service._dropped_wake_sweep()
    assert_notify_file_clean(service, "alice", "after the re-wake sweeps")


def test_own_critical_never_pins_its_operator_author(service, fleet):
    """Attack 1: `unread_criticals` is sticky by construction — it ignores the
    cursor entirely and unpins only on a deliberate read receipt."""
    op, alice, bob, carol = fleet
    m = service.post_message(op, "design", PostMessage(
        body="ALL STOP", title="crit", critical=True))
    assert m.critical
    assert_no_own_delivery(service, op, "own critical, unread and unacked")
    service.ack_inbox(op, {"design": service.db.last_seq("design")})
    assert_no_own_delivery(service, op, "own critical after acking")
    service.read_message(op, "design", m.id)
    assert_no_own_delivery(service, op, "own critical after reading it")
    assert any(e.critical for e in service.inbox(bob)), \
        "sanity: the critical must actually pin bob"


def test_own_critical_survives_the_author_leaving_and_rejoining(service, fleet):
    """Attack 1 (last shape): leaving drops the membership row and rejoining
    resets the cursor — a fresh cursor over old traffic is exactly how an
    author could be handed their own history back."""
    op, alice, bob, carol = fleet
    service.post_message(op, "design", PostMessage(body="crit", critical=True))
    service.post_message(op, "design", PostMessage(body="mine", status=Status.open,
                                                   to=["op"]))
    service.leave_channel(op, "design")
    service.join_channel(op, "design",
                         service.create_invite(alice, "design", invitee="op"))
    assert_no_own_delivery(service, op, "author left and rejoined the channel")


def test_addressee_left_fallback_does_not_repin_the_author(service, fleet):
    """Attack 2: when NO addressee is still available the hub reverts an
    addressed obligation to BROADCAST pinning so it cannot rot in the dark.
    Broadcast pinning means 'every member' — and the author is a member."""
    op, alice, bob, carol = fleet
    service.post_message(alice, "design", PostMessage(
        body="bob only", title="to-bob", status=Status.open, to=["bob"]))
    service.ack_inbox(alice, {"design": service.db.last_seq("design")})
    service.leave_channel(bob, "design")
    assert_no_own_delivery(service, alice, "sole addressee LEFT the channel")
    assert any(e.title == "to-bob" for e in service.inbox(carol)), \
        "sanity: the fallback must actually re-pin the remaining bystanders"


def test_hub_blocked_addressee_fallback_does_not_repin_the_author(service, fleet):
    """Attack 2 (review F3): a hub-blocked addressee counts as unavailable,
    which triggers the same broadcast fallback."""
    op, alice, bob, carol = fleet
    service.post_message(alice, "design", PostMessage(
        body="bob only", title="to-bob", status=Status.open, to=["bob"]))
    service.ack_inbox(alice, {"design": service.db.last_seq("design")})
    service.impose_block(op, "bob", scope="hub", seconds=None, reason="test")
    assert_no_own_delivery(service, alice, "sole addressee hub-BLOCKED")


def test_own_addressed_directives_are_never_debts_to_their_author(service, fleet):
    """Attack 3: `_addressed_debts` pins reply/fyi directives on the seats they
    NAME. An author naming themselves must not thereby oblige themselves."""
    op, alice, bob, carol = fleet
    root = service.post_message(bob, "design", PostMessage(
        body="root", status=Status.open))
    for payload in (
        PostMessage(body="now do X", status=Status.reply, to=["alice"],
                    reply_to=root.id),
        PostMessage(body="alice and bob do X", status=Status.reply,
                    to=["alice", "bob"], reply_to=root.id),
        PostMessage(body="fyi self", status=Status.fyi, to=["alice"]),
    ):
        service.post_message(alice, "design", payload)
    service.ack_inbox(alice, {"design": service.db.last_seq("design")})
    assert_no_own_delivery(service, alice, "own reply/fyi directives naming self")


def test_operator_self_directives_never_oblige_the_operator(service, fleet):
    """Attack 3/4: an OPERATOR sender obliges named seats AND the reporting
    delegate whatever the status. Both carve-outs must still stop at the
    author — an operator naming themselves owes themselves nothing."""
    op, alice, bob, carol = fleet
    service.set_delegation("bob", ["reporting"])
    service._delegations_cache_at = 0.0
    root = service.post_message(alice, "design", PostMessage(
        body="root", status=Status.open))
    service.post_message(op, "design", PostMessage(
        body="op names self", status=Status.reply, to=["op"], reply_to=root.id))
    service.post_message(op, "design", PostMessage(
        body="op names nobody", status=Status.reply, to=[], reply_to=root.id))
    service.post_message(op, "design", PostMessage(
        body="op fyi to self", status=Status.fyi, to=["op"]))
    service.post_message(op, "design", PostMessage(
        body="op open, nobody named", status=Status.open))
    service.ack_inbox(op, {"design": service.db.last_seq("design")})
    assert_no_own_delivery(service, op, "operator's own lines, incl. self-named")
    # Sanity: the delegate DOES carry the operator's unaddressed lines.
    assert any(e.sender == "op" for e in service.inbox(bob)), \
        "sanity: the reporting delegate must owe the operator's lines"


def test_reporting_delegate_never_owes_its_own_messages(service, fleet):
    """Attack 4: the delegate's hub-routed duty is keyed on the OPERATOR being
    the sender, but a delegate that is later granted operator status (or that
    simply posts a lot) must never be routed its own traffic."""
    op, alice, bob, carol = fleet
    service.set_delegation("alice", ["reporting"])
    service._delegations_cache_at = 0.0
    _post_every_shape(service, alice)
    service.ack_inbox(alice, {"design": service.db.last_seq("design")})
    assert_no_own_delivery(service, alice, "reporting delegate's own traffic")


def test_own_dm_and_group_messages_never_come_back(service, fleet):
    """Attack 5: DMs and groups are ordinary channels with reserved names, so
    they inherit every inbox path — including the DM-specific board branch."""
    op, alice, bob, carol = fleet
    service.post_dm(alice, "bob", PostMessage(body="dm open", title="dm-open",
                                              status=Status.open))
    service.post_dm(alice, "bob", PostMessage(body="dm fyi", title="dm-fyi"))
    service.create_group(alice, "grp", ["bob", "carol"])
    # create_group INVITES (joining stays the invitee's auditable act).
    for seat in (bob, carol):
        service.join_channel(seat, "grp",
                             service.create_invite(alice, "grp", invitee=seat.id))
    _post_every_shape(service, alice, channel="grp")
    for channel in service.db.channels_of("alice"):
        service.ack_inbox(alice, {channel: service.db.last_seq(channel)})
    assert_no_own_delivery(service, alice, "own DM and group traffic")
    assert any(e.title == "dm-open" for e in service.inbox(bob)), \
        "sanity: the DM must reach the peer"


def test_retracted_and_resolved_own_messages_stay_out(service, fleet):
    """Attack 7: retraction rewrites a message in place and closure changes
    its discharge state — neither may hand it back to its author."""
    op, alice, bob, carol = fleet
    m = service.post_message(alice, "design", PostMessage(
        body="oops", title="oops", status=Status.open, to=["alice", "bob"]))
    service.retract_message(alice, "design", m.id)
    assert_no_own_delivery(service, alice, "own retracted open message")
    m2 = service.post_message(alice, "design", PostMessage(
        body="q", title="q", status=Status.open, to=["alice"]))
    service.post_message(bob, "design", PostMessage(
        body="a", status=Status.reply, reply_to=m2.id))
    service.ack_inbox(alice, {"design": service.db.last_seq("design")})
    assert_no_own_delivery(service, alice, "own thread after a peer's reply")
    service.post_message(alice, "design", PostMessage(
        body="closing", status=Status.resolved, reply_to=m2.id))
    assert_no_own_delivery(service, alice, "own thread after the author closed it")


def test_synthetic_hub_notices_can_never_borrow_the_viewers_id(service, fleet):
    """Attack 7: the hub mints ephemeral envelopes (the stale-client notice,
    every sender-facing doorbell) under sender='hub'. That is only safe while
    'hub' is unregistrable — if the reservation ever lapses, a seat named
    'hub' would receive hub notices as its OWN mail."""
    op, alice, bob, carol = fleet
    with pytest.raises(HubError):
        service.register_agent("hub", "Hub", mission="impostor")
    with pytest.raises(HubError):
        service.register_agent("all", "All", mission="impostor")


def test_self_addressing_is_refused_where_the_hub_refuses_it(service, fleet):
    """The post-time defences that keep the self-address surface SMALL. Each
    refusal is load-bearing: `pending_addressees` and `ask_addressees` feed
    the inbox pin and the board's assignee set, so a self ask-`to` or
    self-`assignee` would recreate the board bug inside the ask machinery.

    Message-level `to` is deliberately NOT on this list — it stays legal, and
    the read surfaces are what must exclude the author. If a future change
    makes it a 400 as well, update this test; do not delete the read-side
    guards it documents."""
    op, alice, bob, carol = fleet
    with pytest.raises(HubError, match="address an ask"):
        service.post_message(alice, "design", PostMessage(
            body="q", status=Status.open,
            asks=[{"id": "q1", "text": "me?", "to": ["alice"]}]))
    with pytest.raises(HubError, match="assign an"):
        service.post_message(alice, "design", PostMessage(
            body="q", status=Status.open,
            asks=[{"id": "q1", "text": "me?", "assignee": "alice"}]))
    with pytest.raises(HubError, match="with yourself"):
        service.open_dm(alice, "alice")
    # A self-@mention must not silently promote the author into `to`.
    m = service.post_message(alice, "design", PostMessage(
        body="@alice will handle it", status=Status.open))
    assert "alice" not in m.to
    # ...but message-level `to` self-address IS accepted, which is exactly why
    # the read surfaces carry the guard.
    m2 = service.post_message(alice, "design", PostMessage(
        body="mine", status=Status.open, to=["alice"]))
    assert m2.to == ["alice"]
    assert_no_own_delivery(service, alice, "the accepted self-address")


# -- the justified case: closure duty, deliberately NOT an envelope -----------


def test_own_thread_closure_duty_is_a_separate_named_class(service, fleet, monkeypatch):
    """The 'excellent justification' the operator rule allows for.

    An author DOES owe closure on their own fully-answered thread (hub rules:
    "Close your own thread: status=resolved + reply_to + decision:<slug>").
    The hub serves that duty as `owed().to_close` — a CloseRow, not an
    Envelope: it names `answered_by`/`answered_at`, it never escalates, it
    never wakes, and both the CLI and the MCP header render it under an
    explicit ADVISORY banner. That is what "machine-distinguishable" means
    here.

    This test is the tripwire on the alternative: if the closure duty is ever
    moved into `/inbox` as an ordinary envelope, `assert_no_own_delivery`
    below fails and whoever moved it must first give clients a field to tell
    it apart (the `Envelope` model has none today)."""
    op, alice, bob, carol = fleet
    m = service.post_message(alice, "design", PostMessage(
        body="settle this", title="mine", status=Status.open))
    service.post_message(bob, "design", PostMessage(
        body="settled", status=Status.reply, reply_to=m.id))
    # to_close has a minimum age so a live thread is not nagged mid-exchange.
    monkeypatch.setattr("agora.hub.service.TO_CLOSE_MIN_AGE_SECONDS", 0.0)
    report = service.owed(alice)
    rows = [r for r in report.to_close if r.id == m.id]
    assert rows, ("the author's own settled thread must surface as a closure "
                  "duty somewhere — if this class was removed, say so "
                  "deliberately rather than by deleting the test")
    assert rows[0].answered_by == "bob"
    # ...and it is NOT mail: no envelope, no owed answer, no board row.
    assert_no_own_delivery(service, alice, "own thread awaiting closure")

def test_waiting_on_never_names_the_asker_themselves(service, fleet):
    """NOBODY WAITS ON THEMSELVES. `waiting_on` falls back to message-level
    `to` when an ask carries no per-ask `to`, and message-level `to` is the
    one self-address the post gate still allows — so the asker could be
    listed as a seat they are waiting on. A peer addressee must still be
    monitored (the row is the dispatcher's only delivery surface)."""
    _op, alice, _bob, _carol = fleet
    service.post_message(alice, "design", PostMessage(
        body="who takes this?", status=Status.open, to=["alice", "bob"],
        data={"asks": [{"id": "1", "text": "owner?"}]}))
    rows = service.owed(alice).waiting_on or []
    seats = [r.get("seat") if isinstance(r, dict) else getattr(r, "seat", None) for r in rows]
    assert "alice" not in seats, (
        f"the hub told alice she is waiting on herself: {rows}")
    assert "bob" in seats, (
        f"the peer addressee vanished from the monitoring surface: {rows}")
