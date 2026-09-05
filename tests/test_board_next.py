"""The board's `next` column: what a waiting row is REALLY waiting for.

laurent asked for a board answering `request / tasks / ongoing / next / who
/ when` (dm#67, dm#72). Four of those were derivable; `next` was not, and it
was the only column with no owner (delegate, commons#426).

The input is the `waiting_on` edge, ruled the carrier of a row-to-row
dependency at `decision:a-row-dependency-goes-in-waiting-on-not-in-prose`.
The design question the column actually has came from the first three rows
ever converted to that edge (tui, commons#429): all three point at
`claim:msg-296-streaming-long-text`, which waits on no ROW at all — it is
`blocked_on: seat`, `needs_from: agora-tui`. A `next` that answered "another
row" would stop one hop short of the only useful answer in the population it
was built for. So the resolution walks the chain and crosses edge types at
the head, and `test_next_crosses_from_the_row_edge_to_the_seat_edge` is that
exact shape.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agora.db import Database
from agora.hub.app import create_app
from agora.hub.service import HubService
from agora.models import AgentInfo


@pytest.fixture()
def hub() -> HubService:
    return HubService(Database(":memory:"), rate_per_minute=600.0)


def seat(hub: HubService, agent_id: str, *, operator: bool = False) -> AgentInfo:
    info, _ = hub.register_agent(agent_id, agent_id.title(), operator,
                                 mission=f"seat {agent_id}")
    return info


@pytest.fixture()
def rooms(hub):
    """Two rooms and two seats, as in the field: the waiter sits in `here`
    and the row it waits on lives in `there`."""
    lead, worker = seat(hub, "lead"), seat(hub, "worker")
    for room in ("here", "there"):
        hub.create_channel(lead, room, True)
        hub.db.add_member(room, "worker")
    return lead, worker


def park(hub, agent, channel, key, **fields):
    """A parked claim row. `status`/`blocked_on`/`needs` default to a valid
    park so each test states only the field it is about."""
    value = {"owner": agent.id, "status": "parked",
             "blocked_on": "decision", "needs": "a ruling", **fields}
    return hub.store_set(agent, channel, key, value)


def nexts(hub, agent):
    return {r["task"]: r for r in hub.board(agent)["next"]}


# -- the shape on the wire, which is a client contract ------------------------

def test_next_is_an_array_and_counted(hub, rooms):
    """agora-wui measured (commons#430) that their drawer promotes any
    array-valued key it does not know to a column, and drops every other
    shape SILENTLY — no column, no note, nothing for a reader to notice. A
    map keyed by task would have been invisible in the client and green in
    both suites. So the array is the contract, not the preference."""
    lead, worker = rooms
    hub.store_set(worker, "there", "claim:head", {"owner": "worker"})
    park(hub, lead, "here", "claim:waiter", blocked_on="row",
         needs="the head row", waiting_on={"channel": "there",
                                           "key": "claim:head"})

    board = hub.board(lead)
    assert isinstance(board["next"], list)
    assert board["counts"]["next"] == 1
    assert board["next"][0]["channel"] == "here"
    assert board["next"][0]["task"] == "waiter"


def test_next_reaches_the_wire_over_http():
    """The bucket is served, not merely computed — agora-wui's live probe
    reads it off `/board` and names any bucket it does not know."""
    app = create_app(db_path=":memory:", admin_key="k", rate_per_minute=600.0,
                     dark_watch_seconds=0)
    client = TestClient(app)
    admin = {"Authorization": "Bearer k"}
    key = client.post("/agents", json={"id": "solo", "mission": "m"},
                      headers=admin).json()["api_key"]
    head = {"Authorization": f"Bearer {key}"}
    client.post("/channels", json={"name": "room"}, headers=head)
    client.put("/channels/room/store/claim:head",
               json={"value": {"owner": "solo"}}, headers=head)
    client.put("/channels/room/store/claim:waiter",
               json={"value": {"owner": "solo", "status": "parked",
                               "blocked_on": "row", "needs": "the head",
                               "waiting_on": {"key": "claim:head"}}},
               headers=head)

    board = client.get("/board", headers=head).json()
    assert isinstance(board["next"], list) and len(board["next"]) == 1
    assert board["counts"]["next"] == 1


# -- resolution: the chain, and the edge type it ends on ----------------------

def test_next_crosses_from_the_row_edge_to_the_seat_edge(hub, rooms):
    """THE FIELD CASE (tui, commons#429). Three rows wait on a row that
    waits on a SEAT. The useful answer is the seat, one hop past where the
    row edge stops."""
    lead, worker = rooms
    park(hub, worker, "there", "claim:perf", blocked_on="seat",
         needs_from="lead", needs="a profile of one notch at 1600 rows")
    park(hub, lead, "here", "claim:inks", blocked_on="row",
         needs="the perf row", waiting_on={"channel": "there",
                                           "key": "claim:perf"})

    row = nexts(hub, lead)["inks"]
    assert row["kind"] == "seat"
    assert row["who"] == "lead"
    assert "a profile of one notch at 1600 rows" in row["what"]
    assert row["head"] == {"channel": "there", "key": "claim:perf"}
    assert row["owner_can_act"] is False


def test_the_chain_is_walked_to_its_head_and_reported(hub, rooms):
    """Two hops. The answer is the seat at the END, and the route is shown
    so a reader can check the hub's arithmetic rather than trust it."""
    lead, worker = rooms
    park(hub, worker, "there", "claim:c", blocked_on="seat",
         needs_from="lead", needs="c's real blocker")
    park(hub, worker, "there", "claim:b", blocked_on="row", needs="c",
         waiting_on={"channel": "there", "key": "claim:c"})
    park(hub, lead, "here", "claim:a", blocked_on="row", needs="b",
         waiting_on={"channel": "there", "key": "claim:b"})

    row = nexts(hub, lead)["a"]
    assert row["chain"] == ["there/claim:b", "there/claim:c"]
    assert row["kind"] == "seat" and row["who"] == "lead"
    assert "c's real blocker" in row["what"]


def test_a_cycle_is_named_and_does_not_hang(hub, rooms):
    """A waits on B waits on A. The hub says so; it does not walk forever
    and it does not report patience."""
    lead, _ = rooms
    hub.store_set(lead, "here", "claim:b", {"owner": "lead"})
    park(hub, lead, "here", "claim:a", blocked_on="row", needs="b",
         waiting_on={"key": "claim:b"})
    park(hub, lead, "here", "claim:b", blocked_on="row", needs="a",
         waiting_on={"key": "claim:a"})

    row = nexts(hub, lead)["a"]
    assert row["kind"] == "cycle"
    assert row["owner_can_act"] is True
    assert "wait on each other" in row["what"]


def test_a_long_chain_stops_walking_and_says_it_stopped(hub, rooms):
    """The cap is for the honest long chain — it must be visible, never a
    silent truncation that reads like a resolved answer."""
    lead, _ = rooms
    depth = HubService._NEXT_MAX_HOPS + 3
    hub.store_set(lead, "here", f"claim:r{depth}", {"owner": "lead"})
    for i in range(depth - 1, -1, -1):
        park(hub, lead, "here", f"claim:r{i}", blocked_on="row",
             needs=f"r{i + 1}", waiting_on={"key": f"claim:r{i + 1}"})

    row = nexts(hub, lead)["r0"]
    assert row["kind"] == "deep"
    assert "stopped walking" in row["what"]


# -- the four the row's own owner must fix ------------------------------------

def test_a_finished_dependency_reports_the_wait_as_over(hub, rooms):
    """The 2026-08-06 incident in board form: the dependency finished and
    the waiter kept looking patient. `owner_can_act` is the whole point."""
    lead, worker = rooms
    hub.store_set(worker, "there", "claim:head", {"owner": "worker"})
    park(hub, lead, "here", "claim:waiter", blocked_on="row", needs="the head",
         waiting_on={"channel": "there", "key": "claim:head"})
    hub.store_set(worker, "there", "claim:head",
                  {"owner": "worker", "status": "done"})

    row = nexts(hub, lead)["waiter"]
    assert row["kind"] == "done"
    assert row["owner_can_act"] is True
    assert row["who"] == "lead"


def test_a_vanished_dependency_is_reported_not_swallowed(hub, rooms):
    lead, worker = rooms
    hub.store_set(worker, "there", "claim:head", {"owner": "worker"})
    park(hub, lead, "here", "claim:waiter", blocked_on="row", needs="the head",
         waiting_on={"channel": "there", "key": "claim:head"})
    hub.db._conn.execute("DELETE FROM store WHERE channel = ? AND key = ?",
                         ("there", "claim:head"))
    hub.db._conn.commit()

    row = nexts(hub, lead)["waiter"]
    assert row["kind"] == "gone"
    assert row["owner_can_act"] is True


def test_a_target_the_VIEWER_cannot_read_is_named_without_its_state(hub, rooms):
    """`waiting_on` guarantees the WAITER can read the target — never the
    board's viewer. The pointer is already in the row the viewer is reading,
    so naming it is not a leak; reporting the target's status would be."""
    lead, worker = rooms
    park(hub, worker, "there", "claim:secret", blocked_on="seat",
         needs_from="lead", needs="a thing said only in there")
    park(hub, lead, "here", "claim:waiter", blocked_on="row",
         needs="the secret row",
         waiting_on={"channel": "there", "key": "claim:secret"})
    hub.db.remove_member("there", "lead")

    row = nexts(hub, lead)["waiter"]
    assert row["kind"] == "unreadable"
    assert row["head"] is None and row["chain"] == []
    assert "a thing said only in there" not in row["what"]
    assert row["who"] is None


# -- the head's own blocker, one branch each ----------------------------------

def test_an_operator_blocker_names_the_operator(hub, rooms):
    lead, worker = rooms
    laurent = seat(hub, "laurent", operator=True)
    hub.db.add_member("there", laurent.id)
    park(hub, worker, "there", "claim:gate", blocked_on="operator",
         needs="a decision only he can make")
    park(hub, lead, "here", "claim:waiter", blocked_on="row", needs="the gate",
         waiting_on={"channel": "there", "key": "claim:gate"})

    row = nexts(hub, lead)["waiter"]
    assert row["kind"] == "operator" and row["who"] == "laurent"


def test_an_owner_blocker_says_only_its_owner_can_raise_it(hub, rooms):
    """`owner` means NOT BLOCKED — its holder ranked other work above it.
    It must never read as someone else's debt."""
    lead, worker = rooms
    park(hub, worker, "there", "claim:later", blocked_on="owner",
         needs="a turn I have not given it")
    park(hub, lead, "here", "claim:waiter", blocked_on="row", needs="later",
         waiting_on={"channel": "there", "key": "claim:later"})

    row = nexts(hub, lead)["waiter"]
    assert row["kind"] == "owner" and row["who"] == "worker"
    assert "only they can raise it" in row["what"]


def test_a_head_that_is_MOVING_reports_its_owner_and_next_step(hub, rooms):
    """The commonest chain in a working fleet: the row you wait on is not
    blocked at all, someone is doing it. Its `next_step` is the answer."""
    lead, worker = rooms
    hub.store_set(worker, "there", "claim:head",
                  {"owner": "worker", "status": "in progress",
                   "next_step": "land the migration, then hand it over"})
    park(hub, lead, "here", "claim:waiter", blocked_on="row", needs="the head",
         waiting_on={"channel": "there", "key": "claim:head"})

    row = nexts(hub, lead)["waiter"]
    assert row["kind"] == "working" and row["who"] == "worker"
    assert "land the migration" in row["what"]


def test_a_park_with_no_readable_blocker_asks_its_owner(hub, rooms):
    """Park validation is TRANSITION-only, so rows parked before the rule
    (and the rows tui retagged, which dropped a false `blocked_on` and kept
    only the edge) carry no tag. The hub says that instead of guessing."""
    lead, worker = rooms
    park(hub, worker, "there", "claim:old")          # a valid park first...
    hub.store_set(worker, "there", "claim:old",      # ...then the tag drops
                  {"owner": "worker", "status": "parked — deliberately idle"})
    park(hub, lead, "here", "claim:waiter", blocked_on="row", needs="the old row",
         waiting_on={"channel": "there", "key": "claim:old"})

    row = nexts(hub, lead)["waiter"]
    assert row["kind"] == "untagged" and row["who"] == "worker"


# -- what is NOT in the column ------------------------------------------------

def test_a_wait_on_an_ACT_outside_the_hub_is_served_not_blanked(hub, rooms):
    """agora-tui's third case (commons#437 §5): their CI row waits on a
    crates.io PUBLICATION. The hub refused `waiting_on` for it and was right
    to — an act is not a row. A column reading only the row edge would have
    rendered that as a blank, so the row's own tag is read instead."""
    lead, _ = rooms
    park(hub, lead, "here", "claim:waits-on-a-publish", blocked_on="external",
         needs="a package published to a registry")

    row = nexts(hub, lead)["waits-on-a-publish"]
    assert row["kind"] == "external"
    assert row["waiting_on"] is None and row["chain"] == []
    assert row["head"] == {"channel": "here", "key": "claim:waits-on-a-publish"}
    assert "published to a registry" in row["what"]


def test_an_act_outside_the_hub_can_name_the_hand_that_performs_it(hub, rooms):
    """agora-tui refused to retag their row quietly (commons#465) and the
    refusal found the hole: `external` was TRUE of it and dropped the actor,
    `operator` kept the actor and called an act a decision. `needs_from`
    already carries WHO and was never read here."""
    lead, worker = rooms
    park(hub, lead, "here", "claim:ci-cannot-resolve", blocked_on="external",
         needs_from="worker", needs="0.6.0 published to crates.io")

    row = nexts(hub, lead)["ci-cannot-resolve"]
    assert row["kind"] == "external"          # the tag still says WHAT
    assert row["who"] == "worker"             # and now it says WHO
    assert "worker must do it outside the hub" in row["what"]


def test_the_shape_agora_tuis_row_ACTUALLY_carries_today(hub, rooms):
    """The row that motivated the `external` branch is tagged `operator`,
    and my commons#453 said it resolved as `external`. It does not, and this
    pins what it really does so the claim cannot drift again: an act only
    the operator can perform, named as theirs."""
    lead, _ = rooms
    laurent = seat(hub, "laurent", operator=True)
    hub.db.add_member("here", laurent.id)
    park(hub, lead, "here", "claim:tagged-operator-for-an-act",
         blocked_on="operator",
         needs="laurent to publish abstracttui 0.6.0 to crates.io")

    row = nexts(hub, lead)["tagged-operator-for-an-act"]
    assert row["kind"] == "operator" and row["who"] == "laurent"
    assert "must decide" in row["what"]


def test_a_moving_row_with_no_edge_is_not_in_next(hub, rooms):
    """`next` answers "what is this WAITING for". A row nobody is waiting on
    is not waiting: its next step is its own, and it is already in
    `in_progress` under its owner."""
    lead, _ = rooms
    hub.store_set(lead, "here", "claim:moving",
                  {"owner": "lead", "status": "in progress",
                   "next_step": "keep going"})

    assert nexts(hub, lead) == {}


def test_a_finished_waiter_leaves_the_column_with_its_claim(hub, rooms):
    """`next` explains live work. A row that is over explains nothing."""
    lead, worker = rooms
    hub.store_set(worker, "there", "claim:head", {"owner": "worker"})
    park(hub, lead, "here", "claim:waiter", blocked_on="row", needs="the head",
         waiting_on={"channel": "there", "key": "claim:head"})
    assert "waiter" in nexts(hub, lead)

    hub.store_set(lead, "here", "claim:waiter",
                  {"owner": "lead", "status": "done",
                   "waiting_on": {"channel": "there", "key": "claim:head"}})
    assert nexts(hub, lead) == {}


def test_in_progress_keeps_the_rows_next_explains(hub, rooms):
    """Additive, deliberately: `next` is an explanation of `in_progress`
    rows, not a partition of them. Nothing moves buckets here — the
    narrowing of `in_progress` is a separate, deploy-gated change
    (`decision:board-partitions-moving-from-parked`)."""
    lead, worker = rooms
    hub.store_set(worker, "there", "claim:head", {"owner": "worker"})
    park(hub, lead, "here", "claim:waiter", blocked_on="row", needs="the head",
         waiting_on={"channel": "there", "key": "claim:head"})

    board = hub.board(lead)
    assert {"here": "waiter"}.items() <= {
        r["channel"]: r["task"] for r in board["in_progress"]}.items()


# -- ordering and the moved flag ----------------------------------------------

def test_rows_whose_owner_can_act_sort_first(hub, rooms):
    lead, worker = rooms
    hub.store_set(worker, "there", "claim:finished", {"owner": "worker"})
    park(hub, worker, "there", "claim:blocked-on-me", blocked_on="seat",
         needs_from="lead", needs="something from lead")
    park(hub, lead, "here", "claim:a-still-waiting", blocked_on="row",
         needs="the blocked row",
         waiting_on={"channel": "there", "key": "claim:blocked-on-me"})
    park(hub, lead, "here", "claim:z-can-move", blocked_on="row",
         needs="the finished row",
         waiting_on={"channel": "there", "key": "claim:finished"})
    hub.store_set(worker, "there", "claim:finished",
                  {"owner": "worker", "status": "shipped"})

    order = [r["task"] for r in hub.board(lead)["next"]]
    # `blocked-on-me` is a parked row in its own right and serves itself;
    # only the ordering of the two waiters is under test here.
    assert order.index("z-can-move") < order.index("a-still-waiting")
    assert order[0] == "z-can-move"


def test_moved_flags_a_dependency_that_advanced_past_the_stamp(hub, rooms):
    """`at_version` is stamped at declaration so 'has it moved?' is a fact.
    The board says it moved; what that MEANS stays the waiting seat's call."""
    lead, worker = rooms
    park(hub, worker, "there", "claim:head", blocked_on="seat",
         needs_from="lead", needs="round one")
    park(hub, lead, "here", "claim:waiter", blocked_on="row", needs="the head",
         waiting_on={"channel": "there", "key": "claim:head"})
    assert nexts(hub, lead)["waiter"]["moved"] is False

    park(hub, worker, "there", "claim:head", blocked_on="seat",
         needs_from="lead", needs="round two, a different ask")
    assert nexts(hub, lead)["waiter"]["moved"] is True


# -- the terminal render ------------------------------------------------------

def test_agora_board_prints_the_next_section(monkeypatch, capsys):
    """`agora board` is the surface laurent reads. A column served and not
    rendered is the same to him as a column that does not exist."""
    import asyncio

    from agora import cli

    payload = {
        "viewer": "lead", "pending_on_me": [], "queue": [], "proposals": [],
        "in_progress": [{"channel": "here", "task": "inks", "owner": "lead"}],
        "next": [{"channel": "here", "task": "inks", "owner": "lead",
                  "waiting_on": {"channel": "there", "key": "claim:perf",
                                 "at_version": 11},
                  "chain": ["there/claim:perf", "there/claim:profile"],
                  "head": {"channel": "there", "key": "claim:profile"},
                  "moved": True, "kind": "seat", "who": "agora-tui",
                  "what": "agora-tui must move `claim:profile`: a profile of "
                          "one notch", "owner_can_act": False}],
        "pending_review": [], "done": [],
        "counts": {"pending_on_me": 0, "queue": 0, "proposals": 0,
                   "in_progress": 1, "next": 1, "pending_review": 0,
                   "done_shown": 0, "done_total": 0},
    }

    class _Stub:
        async def board(self):
            return payload

    monkeypatch.setattr(cli, "_run_agent_cmd",
                        lambda args, go: asyncio.run(go(_Stub(), args)))
    cli.cmd_board(object())

    out = capsys.readouterr().out
    assert "1 waiting on another row" in out
    assert "## next" in out
    assert "here inks — seat [agora-tui] MOVED" in out
    assert "via there/claim:perf -> there/claim:profile" in out


def test_the_render_survives_a_hub_that_does_not_serve_next(monkeypatch, capsys):
    """A CLI newer than the hub it points at (the live hub runs an installed
    snapshot, not this tree). The section is absent, not a traceback."""
    import asyncio

    from agora import cli

    payload = {"viewer": "lead", "pending_on_me": [], "queue": [],
               "proposals": [], "in_progress": [], "pending_review": [],
               "done": [],
               "counts": {"pending_on_me": 0, "queue": 0, "proposals": 0,
                          "in_progress": 0, "pending_review": 0,
                          "done_shown": 0, "done_total": 0}}

    class _Stub:
        async def board(self):
            return payload

    monkeypatch.setattr(cli, "_run_agent_cmd",
                        lambda args, go: asyncio.run(go(_Stub(), args)))
    cli.cmd_board(object())

    out = capsys.readouterr().out
    assert "## next" not in out
    assert "0 waiting on another row" in out
