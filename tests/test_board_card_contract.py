"""The board's CARD contract: a claim row projected without dropping it.

laurent's complaint (commons#604, with a reference screenshot) read as "no
per-row state on the board". It was not a missing feature. `board()` served an
in-progress row as FIVE keys — channel, task, owner, updated_by, updated_at —
while the claim row behind it already carried `status`, `blocked_on`,
`needs_from`, `needs`, `next_step`, every one of them validated at write time.
A LOSSY PROJECTION, and four of the five badges in his reference need no new
computation: they need the projection to stop dropping its input.

Two things the hub adds on top of what the row says, and the whole point of
this file is that neither is allowed to become a guess:

* `state` — the hub's OWN lifecycle class, the one it has always computed to
  decide which array a row lands in. Serving it is disclosure. `status` stays
  the owner's verbatim words, and the hub never normalises one into the other
  (P1 item 7: never invent or infer a status word).
* `title` — a human sentence, plus `title_source` saying whether a seat wrote
  it or the hub de-hyphenated the key. agora-wui's renderer falls through to
  `id` for a titleless row, which is why a ULID was the headline on cards in
  his screenshot (board-redesign#18); a client cannot invent the sentence, so
  the hub supplies it and says which kind it is.

Every test here was run against a mutant of the line it guards.
"""

from __future__ import annotations

import pytest

from agora.db import Database
from agora.hub.service import HubService
from agora.models import AgentInfo


@pytest.fixture()
def hub() -> HubService:
    return HubService(Database(":memory:"), rate_per_minute=600.0)


def seat(hub: HubService, agent_id: str) -> AgentInfo:
    info, _ = hub.register_agent(agent_id, agent_id.title(), False,
                                 mission=f"seat {agent_id}")
    return info


@pytest.fixture()
def room(hub):
    lead = seat(hub, "lead")
    seat(hub, "worker")
    hub.create_channel(lead, "here", True)
    hub.db.add_member("here", "worker")
    return lead


def cards(hub, agent, bucket="in_progress"):
    return {r["task"]: r for r in hub.board(agent)[bucket]}


# -- the drop itself ----------------------------------------------------------

def test_the_card_carries_every_row_field_the_projection_dropped(hub, room):
    """The finding, as an assertion. Each of these was written by a seat, sat
    in the store, and did not reach the wire."""
    hub.store_set(room, "here", "claim:seam", {
        "owner": "lead", "status": "in progress — diagnosed, not yet fixed",
        "blocked_on": "seat", "needs_from": "worker",
        "needs": "the engine ref before I can measure",
        "next_step": "count the affected rows across every room",
        "source_message_id": "01M0VH0CDF9HSCS7Q0DX2Y1KM4"})

    card = cards(hub, room)["seam"]
    assert card["status"] == "in progress — diagnosed, not yet fixed"
    assert card["blocked_on"] == "seat"
    assert card["needs_from"] == "worker"
    assert card["needs"] == "the engine ref before I can measure"
    assert card["next_step"] == "count the affected rows across every room"
    assert card["source_message_id"] == "01M0VH0CDF9HSCS7Q0DX2Y1KM4"


def test_a_field_nobody_wrote_is_null_not_absent(hub, room):
    """"Checked and empty" and "never written" are different numbers — the
    distinction this room arrived at from four directions in one night. A key
    that is simply missing from the payload makes a client guess which it is;
    an explicit `null` does not, and it is what lets a renderer show a card
    without probing for keys."""
    hub.store_set(room, "here", "claim:bare", {"owner": "lead"})

    card = cards(hub, room)["bare"]
    for field in ("status", "blocked_on", "needs_from", "needs", "next_step"):
        assert field in card, f"{field} must be served, not omitted"
        assert card[field] is None


def test_an_empty_string_reads_as_unwritten(hub, room):
    """A row that carries `blocked_on: ""` (the live hub has them — a park
    cleared by blanking the field rather than removing it) must not serve an
    empty string a client would render as a blank badge."""
    hub.store_set(room, "here", "claim:blanked",
                  {"owner": "lead", "blocked_on": "", "needs": "   "})

    card = cards(hub, room)["blanked"]
    assert card["blocked_on"] is None
    assert card["needs"] is None


# -- state as a FIELD, never a bucket -----------------------------------------

def test_state_is_served_and_is_the_hubs_own_bucketing(hub, room):
    """agora-wui's identity argument, adopted outright: lifecycle-as-buckets
    makes a card changing lane a RE-BUCKETING, with no identity across the
    move. As a field, the same card carries a different value — and the value
    is the hub's own `_claim_parked`, so a client's lane can never disagree
    with the array the hub put the row in."""
    hub.store_set(room, "here", "claim:live", {"owner": "lead"})
    hub.store_set(room, "here", "claim:idle",
                  {"owner": "lead", "status": "parked — behind the card work",
                   "blocked_on": "owner", "needs": "my own attention"})

    board = hub.board(room)
    by_task = {r["task"]: r for r in board["in_progress"]}
    assert by_task["live"]["state"] == "active"
    assert by_task["idle"]["state"] == "parked"
    # ...and the parked card is STILL in progress. Parking changes the field,
    # never the row's membership: that is what "keeps its identity" means.
    assert board["counts"]["in_progress"] == 2


def test_a_terminal_card_states_done(hub, room):
    """`pending_review` rows are terminal, and they are cards too — the same
    contract, so a client renders one surface and not two."""
    hub.store_set(room, "here", "claim:shipped",
                  {"owner": "lead", "status": "done — shipped",
                   "review": "delegate"})

    card = cards(hub, room, "pending_review")["shipped"]
    assert card["state"] == "done"
    assert card["review"] == "delegate"
    assert card["status"] == "done — shipped"


def test_state_never_replaces_the_owners_words(hub, room):
    """The guard on P1 item 7. The owner's `status` is a SENTENCE — prose the
    hub has no business compressing — and the hub's lifecycle word is a
    separate key. A single field would have forced one of the two to be a lie:
    either the badge reads a paragraph, or the hub overwrites what the owner
    said with `active`."""
    prose = "LIVE and UNBLOCKED. Premise check done this chunk. Uncommitted."
    hub.store_set(room, "here", "claim:wordy",
                  {"owner": "lead", "status": prose})

    card = cards(hub, room)["wordy"]
    assert card["status"] == prose      # verbatim, uncompressed, unnormalised
    assert card["state"] == "active"    # the hub's word, in its own field


# -- the human title, and where it came from ----------------------------------

def test_a_seat_written_title_is_served_verbatim_and_labelled(hub, room):
    hub.store_set(room, "here", "claim:msg-296-tui-perf", {
        "owner": "lead",
        "what": "Long bodies typeset twice, the second time mid-read"})

    card = cards(hub, room)["msg-296-tui-perf"]
    assert card["title"] == ("Long bodies typeset twice, the second time "
                             "mid-read")
    assert card["title_source"] == "row"


def test_a_titleless_row_gets_a_sentence_and_says_it_came_from_the_slug(
        hub, room):
    """No renderer can invent one (delegate's P2 line), and a renderer that
    tries falls through to `id` and puts a ULID on the card face. So the hub
    de-hyphenates the key — and LABELS it, because a headline nobody wrote and
    one a seat authored must not be indistinguishable on the wire."""
    hub.store_set(room, "here", "claim:a-list-of-done-things-marks-the-row-done",
                  {"owner": "lead"})

    card = cards(hub, room)["a-list-of-done-things-marks-the-row-done"]
    assert card["title"] == "a list of done things marks the row done"
    assert card["title_source"] == "slug"


def test_the_slug_stays_the_identity_whatever_the_title_says(hub, room):
    """`task` is what a client keys a card on across a lane change and what
    `next` names. The title is display; it must never become the identity."""
    hub.store_set(room, "here", "claim:seam",
                  {"owner": "lead", "title": "Something else entirely"})

    card = cards(hub, room)["seam"]
    assert card["task"] == "seam"
    assert card["title"] == "Something else entirely"


# -- the contract is additive -------------------------------------------------

def test_every_previously_served_key_survives(hub, room):
    """A client written against the old five keys is unbroken. This is the
    only test here that would notice a rename, and a rename is the one way
    this change could break agora-wui without either suite going red."""
    worker = AgentInfo(id="worker", name="worker")
    hub.store_set(room, "here", "claim:seam", {"owner": "lead"})
    # Owner and last writer deliberately differ: they are two facts and the
    # board has always served both.
    hub.store_set(worker, "here", "claim:seam",
                  {"owner": "lead", "next_step": "measure"}, expect_version=1)

    card = cards(hub, room)["seam"]
    assert card["channel"] == "here"
    assert card["task"] == "seam"
    assert card["owner"] == "lead"
    assert card["updated_by"] == "worker"
    assert isinstance(card["updated_at"], float)
    # New, and load-bearing for a store write: a client that reads a card and
    # then writes the row back has the version it must compare-and-swap on.
    assert card["version"] == 2


# -- the abandoned-work signal ------------------------------------------------
#
# The seam's proof (plan:board-redesign): "the board does not open on rows
# owned by a seat laurent stood down." The constraint on it is from the same
# dispatch: retirement served as a FACT, never an age heuristic. A row is not
# abandoned because it is old — it is abandoned because the operator retired
# the seat holding it, and `retired_at` says so exactly.

def _operator(hub, agent_id="op"):
    info, _ = hub.register_agent(agent_id, "Op", True, mission="seat op")
    return info


def test_a_live_owner_is_answered_not_left_blank(hub, room):
    """The trap this shape exists to avoid. Every OTHER field on the card is a
    row field, so `null` correctly reads "nobody filled it in"
    (agora-wui's decision:a-null-on-a-card-reads-not-written-never-none). This
    one the hub can ALWAYS answer, so a null would say "unknown" about a fact
    that is known — the same missing-third-state trap as `waiting_on`."""
    hub.store_set(room, "here", "claim:live", {"owner": "lead"})

    standing = cards(hub, room)["live"]["owner_standing"]
    assert standing == {"known": True, "retired": False,
                        "at": None, "reason": None}


def test_a_retired_owner_carries_the_operators_fact_and_moment(hub, room):
    op = _operator(hub)
    worker = AgentInfo(id="worker", name="worker")
    hub.store_set(worker, "here", "claim:handover", {"owner": "worker"})
    hub.retire_agent(op, "worker", reason="rolled into lead's scope")

    standing = cards(hub, room)["handover"]["owner_standing"]
    assert standing["known"] is True
    assert standing["retired"] is True
    assert standing["reason"] == "rolled into lead's scope"
    assert isinstance(standing["at"], float) and standing["at"] > 0


def test_retirement_with_no_reason_is_retired_with_no_reason(hub, room):
    """`reason` is optional on retire_agent and the empty string is not a
    reason. The card must still say RETIRED — a client that keys the badge off
    the reason would silently drop the signal for every unexplained stand-down.
    """
    op = _operator(hub)
    worker = AgentInfo(id="worker", name="worker")
    hub.store_set(worker, "here", "claim:quiet", {"owner": "worker"})
    hub.retire_agent(op, "worker")

    standing = cards(hub, room)["quiet"]["owner_standing"]
    assert standing["retired"] is True
    assert standing["reason"] is None


def test_an_owner_the_hub_never_registered_is_unknown_not_active(hub, room):
    """`owner` is free text a seat writes into its own row, so a typo is
    reachable. "I cannot answer" must not be served as "checked, they are
    fine" — that is the failure mode of every field this room has argued about
    tonight, and it is the reason `known` is a separate boolean.

    Reachable, not hypothetical: the owner check at write time exempts
    operators outright (`not agent.operator and ...`), so an operator may
    write any owner string at all — and legacy rows predate the check."""
    op = _operator(hub)
    hub.db.add_member("here", "op")
    hub.store_set(op, "here", "claim:typo", {"owner": "wroker"})

    standing = cards(hub, room)["typo"]["owner_standing"]
    assert standing == {"known": False, "retired": False,
                        "at": None, "reason": None}


def test_age_alone_never_produces_the_signal(hub, room):
    """The constraint stated as a test. An ancient row owned by a live seat is
    NOT abandoned, and no threshold anywhere may say otherwise."""
    hub.store_set(room, "here", "claim:ancient", {"owner": "lead"})
    hub.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 86400 * 30 "
        "WHERE channel='here' AND key='claim:ancient'")
    hub.db._conn.commit()

    standing = cards(hub, room)["ancient"]["owner_standing"]
    assert standing["retired"] is False and standing["known"] is True


def test_the_signal_survives_a_non_string_owner(hub, room):
    """A store value is free-form JSON: `owner` can be a dict. The lookup must
    answer "unknown", not raise inside board(). Written as an operator, the
    one caller the owner check exempts."""
    op = _operator(hub)
    hub.db.add_member("here", "op")
    hub.store_set(op, "here", "claim:weird", {"owner": {"seat": "lead"}})

    standing = cards(hub, room)["weird"]["owner_standing"]
    assert standing["known"] is False
