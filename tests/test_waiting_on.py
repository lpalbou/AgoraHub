"""A parked claim's dependency is state the hub watches, not prose.

THE INCIDENT (measured, 2026-08-06). Seven driven seats built Milestone 1 of
an operator commission and then sat idle for hours on finished work.

  14:55:13  rt2-lead parks `rtype-open/claim:phase-1-m1-dispatch`
            cadence_minutes=60
            next_step: "Resume when `claim:m1-engine-boot-manifest` changes
                        with captured browser boot-proof evidence"
  14:58:56  rt2-engine writes `rtype-phase-1-m1-dispatch/
            claim:m1-engine-boot-manifest` v6 -> "done ... next hand
            QA/critic the evidence bundle"

The dependency completed 3 minutes 43 seconds after the wait was declared,
carrying exactly the evidence named. Nothing woke. Four failures stacked:

  1. `store_set` rings nobody — only messages wake seats.
  2. "Resume when X changes" was PROSE in `next_step`; nothing read it.
  3. `_claim_due_sweep` discarded the owner's OWN declared cadence because
     the row was also parked — a silent fallback.
  4. The driver classes `parked` as terminal, so the row was a one-way door.

Plus a fifth, which is why nobody saw it from outside: the delegate had put
the real work in a private room its operator was not in.

These tests pin all five.
"""

from __future__ import annotations

import pytest

from agora.db import Database
from agora.hub.service import HubError, HubService
from agora.models import AgentInfo, PostMessage, Status


@pytest.fixture()
def hub() -> HubService:
    return HubService(Database(":memory:"), rate_per_minute=600.0)


def seat(hub: HubService, agent_id: str, *, operator: bool = False) -> AgentInfo:
    info, _ = hub.register_agent(agent_id, agent_id.title(), operator,
                                 mission=f"seat {agent_id}")
    return info


@pytest.fixture()
def rooms(hub):
    """Two rooms, as in the incident: the lead waits in A on a row in B."""
    lead, worker = seat(hub, "lead"), seat(hub, "worker")
    for room in ("open-room", "dispatch-room"):
        hub.create_channel(lead, room, True)
        hub.join_channel_direct(worker, room) if hasattr(
            hub, "join_channel_direct") else hub.db.add_member(room, "worker")
    hub.store_set(worker, "dispatch-room", "claim:boot-manifest",
                  {"owner": "worker", "status": "in progress"})
    return lead, worker


# -- 1. the declaration is validated, never taken on faith -----------------

def test_waiting_on_a_row_that_does_not_exist_is_REFUSED(hub, rooms):
    """The delegate's park named a row that, in ITS room, did not exist.
    A wait on a phantom is a permanent park the hub cannot tell from
    finished work."""
    lead, _ = rooms
    with pytest.raises(HubError) as exc:
        hub.store_set(lead, "open-room", "claim:dispatch",
                      {"owner": "lead", "status": "parked",
                       "waiting_on": {"channel": "dispatch-room",
                                      "key": "claim:nope"}})
    assert exc.value.status_code == 404
    assert "does not exist" in exc.value.detail
    # ...and nothing was written: a refused write is a no-op.
    assert hub.db.store_get("open-room", "claim:dispatch") is None


def test_a_claim_cannot_wait_on_itself(hub, rooms):
    lead, _ = rooms
    with pytest.raises(HubError, match="cannot wait on itself"):
        hub.store_set(lead, "open-room", "claim:dispatch",
                      {"owner": "lead", "status": "parked",
                       "waiting_on": {"key": "claim:dispatch"}})


def test_the_target_version_is_stamped_at_declaration(hub, rooms):
    """'Has it moved?' must be a fact, not a judgement."""
    lead, _ = rooms
    hub.store_set(lead, "open-room", "claim:dispatch",
                  {"owner": "lead", "status": "parked",
                   "blocked_on": "seat", "needs_from": "worker",
                   "needs": "worker's boot manifest",
                   "waiting_on": {"channel": "dispatch-room",
                                  "key": "claim:boot-manifest"}})
    dep = hub.db.store_get("open-room", "claim:dispatch").value["waiting_on"]
    assert dep == {"channel": "dispatch-room", "key": "claim:boot-manifest",
                   "at_version": 1}


def test_waiting_on_refuses_a_target_the_waiting_seat_cannot_read(hub, rooms):
    """A parked claim may not subscribe itself to a hidden row and later leak
    that room/key back into another channel as a wake."""
    lead, _ = rooms
    hub.db.remove_member("dispatch-room", "lead")
    with pytest.raises(HubError) as exc:
        hub.store_set(lead, "open-room", "claim:dispatch",
                      {"owner": "lead", "status": "parked",
                       "blocked_on": "seat", "needs_from": "worker",
                       "needs": "worker's boot manifest",
                       "waiting_on": {"channel": "dispatch-room",
                                      "key": "claim:boot-manifest"}})
    assert exc.value.status_code == 400
    assert "not a member of 'dispatch-room'" in exc.value.detail


# -- 2. the sweep rings when the dependency moves --------------------------

def _bodies(hub, channel="open-room"):
    return [m.body for m in hub.db.get_messages(channel, limit=50)]


def test_the_sweep_rings_the_waiter_when_the_dependency_moves(hub, rooms):
    """THE INCIDENT, replayed. It must not be silent this time."""
    lead, worker = rooms
    hub.store_set(lead, "open-room", "claim:dispatch",
                  {"owner": "lead", "status": "parked waiting on worker",
                   "blocked_on": "seat", "needs_from": "worker",
                   "needs": "worker's boot manifest",
                   "waiting_on": {"channel": "dispatch-room",
                                  "key": "claim:boot-manifest"}})
    assert hub._waiting_on_sweep() == []          # nothing has moved yet

    hub.store_set(worker, "dispatch-room", "claim:boot-manifest",
                  {"owner": "worker", "status": "done, evidence captured"})

    assert hub._waiting_on_sweep() == ["waiting-on:open-room/claim:dispatch"]
    rung = [b for b in _bodies(hub) if "claim:boot-manifest" in b]
    assert rung, "the waiting seat was never told"
    assert "v1 -> v2" in rung[0] and "status done" in rung[0]
    # It is ADDRESSED: a broadcast unpins on a bare read and decays.
    alert = [m for m in hub.db.get_messages("open-room", limit=50)
             if "claim:boot-manifest" in m.body][0]
    assert alert.to == ["lead"] and alert.status.value == "open"


def test_the_same_change_is_not_rung_twice(hub, rooms):
    """One alert per real change. A surface that repeats itself is ignored."""
    lead, worker = rooms
    hub.store_set(lead, "open-room", "claim:dispatch",
                  {"owner": "lead", "status": "parked",
                   "blocked_on": "seat", "needs_from": "worker",
                   "needs": "worker's boot manifest",
                   "waiting_on": {"channel": "dispatch-room",
                                  "key": "claim:boot-manifest"}})
    hub.store_set(worker, "dispatch-room", "claim:boot-manifest",
                  {"owner": "worker", "status": "done"})
    hub._waiting_on_sweep()
    before = len(hub.db.get_messages("open-room", limit=99))
    hub._waiting_on_sweep()
    assert len(hub.db.get_messages("open-room", limit=99)) == before


def test_a_vanished_dependency_is_SAID_not_swallowed(hub, rooms):
    """Waiting on a deleted row is the worst park available: it will never
    resume and it looks exactly like finished work."""
    lead, worker = rooms
    hub.store_set(lead, "open-room", "claim:dispatch",
                  {"owner": "lead", "status": "parked",
                   "blocked_on": "seat", "needs_from": "worker",
                   "needs": "worker's boot manifest",
                   "waiting_on": {"channel": "dispatch-room",
                                  "key": "claim:boot-manifest"}})
    hub.db._conn.execute("DELETE FROM store WHERE channel = ? AND key = ?",
                         ("dispatch-room", "claim:boot-manifest"))
    hub.db._conn.commit()
    assert hub._waiting_on_sweep() == ["waiting-on:open-room/claim:dispatch"]
    assert any("no longer exists" in b for b in _bodies(hub))


def test_a_wait_who_loses_target_membership_is_alerted_without_a_leak(hub, rooms):
    """If the waiting seat can no longer read the target room, the wake must
    say that fact without repeating the hidden room/key into another room."""
    lead, _ = rooms
    hub.store_set(lead, "open-room", "claim:dispatch",
                  {"owner": "lead", "status": "parked",
                   "blocked_on": "seat", "needs_from": "worker",
                   "needs": "worker's boot manifest",
                   "waiting_on": {"channel": "dispatch-room",
                                  "key": "claim:boot-manifest"}})
    hub.db.remove_member("dispatch-room", "lead")

    assert hub._waiting_on_sweep() == ["waiting-on:open-room/claim:dispatch"]
    alert = [m for m in hub.db.get_messages("open-room", limit=50)
             if m.to == ["lead"]][-1]
    assert "can no longer read" in alert.body
    assert "dispatch-room" not in alert.body
    assert "claim:boot-manifest" not in alert.body


def test_a_parked_row_without_a_declaration_still_rings_nobody(hub, rooms):
    """The feature is opt-in. A plain park must not become a new nag."""
    lead, _ = rooms
    hub.store_set(lead, "open-room", "claim:dispatch",
                  {"owner": "lead", "blocked_on": "external", "needs": "the vendor build to land", "status": "parked"})
    assert hub._waiting_on_sweep() == []


# -- 3. the owner's OWN declared cadence survives a park -------------------

def test_a_park_does_not_cancel_the_cadence_its_owner_declared(hub, rooms):
    """`cadence_minutes` is the owner saying "remind ME". Dropping it because
    they also parked is the hub declining to do the thing its author asked
    for, without saying so. In the incident this was the difference between
    a 40-minute stall and a 56-minute self-wake.

    `_steward_sweep` KEEPS its parked exemption — that one is a third party
    re-asking a question the status already answered. A self-declared
    reminder is not a third party."""
    lead, _ = rooms
    hub.store_set(lead, "open-room", "claim:dispatch",
                  {"owner": "lead", "cadence_minutes": 60,
                   "blocked_on": "external", "needs": "the vendor build to land", "status": "parked waiting on worker",
                   "next_step": "Resume when claim:boot-manifest changes"})
    row = hub.db.store_get("open-room", "claim:dispatch")
    hub.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 7200 "
        "WHERE channel='open-room' AND key='claim:dispatch'")
    hub.db._conn.commit()
    assert row is not None

    assert hub._claim_due_sweep(), "the owner's declared cadence was discarded"
    # ...while the third-party staleness nag stays correctly silent.
    assert hub._steward_sweep() == []


def test_a_done_row_is_still_exempt_from_the_cadence_ping(hub, rooms):
    """Finished work has nothing to re-check."""
    lead, _ = rooms
    hub.store_set(lead, "open-room", "claim:dispatch",
                  {"owner": "lead", "cadence_minutes": 60, "status": "done"})
    hub.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 7200 "
        "WHERE channel='open-room' AND key='claim:dispatch'")
    hub.db._conn.commit()
    assert hub._claim_due_sweep() == []


# -- 4. parked work counts as unfinished -----------------------------------

def test_parked_work_counts_as_open_in_the_fleet_snapshot(hub, rooms):
    """The hourly digest told the delegate "7/7 live" while its own room was
    stalled, because parked rows were counted as closed."""
    lead, _ = rooms
    hub.store_set(lead, "open-room", "claim:dispatch",
                  {"owner": "lead", "blocked_on": "external", "needs": "the vendor build to land", "status": "parked waiting on worker"})
    assert hub._fleet_open_claims_count() >= 1


# -- 5. a delegate may not work where its operator cannot look -------------

def test_a_delegates_new_room_invites_the_operator(hub):
    """The delegate split the real work into a private room and invited only
    the workers. For three hours the operator's board showed a dead channel
    while a milestone shipped next door — and the room's own creation notice
    was posted inside the room nobody could read."""
    boss = seat(hub, "boss", operator=True)
    lead, worker = seat(hub, "lead"), seat(hub, "worker")
    hub.set_delegation("lead", ["reporting"], ttl_seconds=86400.0)

    out = hub.create_group(lead, "sub-room", ["worker"],
                           purpose="focused dispatch")
    invited = set(out.get("invited") or [])
    assert "worker" in invited
    assert "boss" in invited, "the operator was left out of its delegate's room"


def test_a_plain_seats_room_does_not_drag_the_operator_in(hub):
    """Only a DELEGATE's workroom carries the operator. An ordinary seat's
    private room is its own business."""
    seat(hub, "boss", operator=True)
    plain, worker = seat(hub, "plain"), seat(hub, "worker")
    out = hub.create_group(plain, "private-room", ["worker"], purpose="ours")
    assert "boss" not in set(out.get("invited") or [])


# -- a scoped delegation means something -----------------------------------

def test_a_room_scoped_delegate_is_not_conscripted_into_other_rooms(hub):
    """`scope` was decoration: the CLI printed it, one call site read it, and
    the stale-claim sweep built ONE fleet-wide steward set and reported every
    room's stale work to all of them.

    Measured 2026-08-06: `rt2-lead`, scoped to `rtype-open`, spent its last
    four work chunks and every subsequent wake canvassing stale claims for
    unrelated seats — 15 housekeeping posts against 5 on the operator's
    commission. The chore firehose the hub generated starved the request the
    hub exists to serve."""
    boss = seat(hub, "boss", operator=True)
    lead, worker = seat(hub, "lead"), seat(hub, "worker")
    for room in ("mine", "elsewhere"):
        hub.create_channel(boss, room, True)
        for who in ("lead", "worker"):
            hub.db.add_member(room, who)
    hub.set_delegation("lead", ["reporting"], ttl_seconds=86400.0, scope="mine")

    # Stale work in a room the grant does NOT reach: not this delegate's.
    hub.store_set(worker, "elsewhere", "claim:theirs", {"owner": "worker"})
    hub.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 999999 "
        "WHERE channel='elsewhere'")
    hub.db._conn.commit()
    assert hub._steward_sweep() == []

    # Stale work in its OWN room still reaches it.
    hub.store_set(worker, "mine", "claim:ours", {"owner": "worker"})
    hub.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 999999 WHERE channel='mine'")
    hub.db._conn.commit()
    assert hub._steward_sweep()
    alert = hub.db.get_messages(hub.DARK_ALERTS_CHANNEL, limit=50)[-1]
    assert alert.to == ["lead"]


def test_an_unscoped_delegation_still_stewards_the_whole_hub(hub):
    """A grant with no scope, or `*`, is fleet-wide as before — the operator
    who wants a janitor can still have one."""
    boss = seat(hub, "boss", operator=True)
    seat(hub, "lead"); worker = seat(hub, "worker")
    hub.create_channel(boss, "anywhere", True)
    for who in ("lead", "worker"):
        hub.db.add_member("anywhere", who)
    hub.set_delegation("lead", ["reporting"], ttl_seconds=86400.0)
    hub.store_set(worker, "anywhere", "claim:x", {"owner": "worker"})
    hub.db._conn.execute("UPDATE store SET updated_at = updated_at - 999999")
    hub.db._conn.commit()
    assert hub._steward_sweep()


# -- a park must say what it needs, and the delegate must see it -----------

def test_a_bare_park_is_refused_and_names_both_fields(hub, rooms):
    """OPERATOR RULING 2026-08-06: "the agent should state on its channel why
    it is parking and what it needs to continue... a park should have a tag
    and an ask of what it needs to continue."

    A bare `parked` was a black hole: no sweep read it, its own driver
    treated it as finished, and the room never heard it."""
    lead, _ = rooms
    with pytest.raises(HubError) as exc:
        hub.store_set(lead, "open-room", "claim:dispatch",
                      {"owner": "lead", "status": "parked pending the export"})
    assert exc.value.status_code == 400
    assert "blocked_on" in exc.value.detail and "needs" in exc.value.detail
    assert "rings nobody" in exc.value.detail       # says it out loud too
    assert hub.db.store_get("open-room", "claim:dispatch") is None


def test_the_blocker_tag_vocabulary_is_closed(hub, rooms):
    """Closed so the delegate's board can GROUP. "three seats waiting on me,
    one on a build" is a picture; free text is a pile."""
    lead, _ = rooms
    with pytest.raises(HubError, match="not a blocker kind"):
        hub.store_set(lead, "open-room", "claim:dispatch",
                      {"owner": "lead", "status": "parked",
                       "blocked_on": "vibes", "needs": "something"})


def test_a_row_parked_before_the_rule_still_updates(hub, rooms):
    """Only the TRANSITION into park is gated — semantics changes must not
    rewrite the past."""
    lead, _ = rooms
    hub.db.store_set("open-room", "claim:legacy",
                     {"owner": "lead", "status": "parked"}, "lead", None)
    hub.store_set(lead, "open-room", "claim:legacy",
                  {"owner": "lead", "status": "parked, chasing the vendor"})
    assert hub.db.store_get("open-room", "claim:legacy").version == 2


def test_supervise_carries_every_blocker_with_who_can_end_it(hub, rooms):
    """`blockers()` was deleted 2026-08-07: zero callers, zero live calls,
    and its one distinctive feature — grouping by `blocked_on` — was inert
    (35 of 39 live parked rows were untagged). `supervise` carries the same
    rows AND says whether your powers let you end each one, which is the
    part a delegate actually needs."""
    lead, worker = rooms
    hub.set_delegation("lead", ["reporting"], ttl_seconds=86400.0,
                       scope="open-room")
    hub.store_set(lead, "open-room", "claim:a",
                  {"owner": "lead", "status": "parked", "blocked_on": "operator",
                   "needs": "laurent to pick the art direction"})
    view = hub.supervise(lead, "open-room")
    row = [b for b in view["blocked"] if b["key"] == "claim:a"][0]
    assert row["blocked_on"] == "operator"
    assert row["needs"].startswith("laurent to pick")
    assert row["you_can_act"] is False          # no proxy in this grant
    assert view["needs_the_operator"] == ["claim:a"]

def test_untagged_legacy_parks_are_named_not_hidden(hub, rooms):
    """Rows parked before the rule are the ones nobody can act on. That is a
    reason to surface them, not to bury them."""
    lead, _ = rooms
    hub.set_delegation('lead', ['reporting'], ttl_seconds=86400.0, scope='open-room')
    hub.db.store_set("open-room", "claim:old",
                     {"owner": "lead", "status": "parked"}, "lead", None)
    assert [b for b in hub.supervise(lead, 'open-room')['blocked']
             if b['blocked_on'] == 'untagged'][0]['key'] == "claim:old"


def test_finished_work_is_not_a_blocker(hub, rooms):
    lead, _ = rooms
    hub.set_delegation('lead', ['reporting'], ttl_seconds=86400.0, scope='open-room')
    hub.store_set(lead, "open-room", "claim:done", {"owner": "lead",
                                                    "status": "done"})
    assert hub.supervise(lead, 'open-room')['blocked'] == []


# -- a power the holder cannot discover is not a power ---------------------

def test_granting_a_power_tells_the_seat_that_gained_it(hub):
    """OPERATOR RULING 2026-08-06: "IF the delegate has been given sufficient
    power to act on behalf of the user, and the user is not connected to the
    hub, then the delegate MUST act on behalf of the user."

    It cannot act on a power nobody told it about. Measured: `g4-lead` opened
    a gate asking the absent operator to ratify a plan, was granted `proxy`
    an hour later — the power to answer its own gate — and sat blocked for 64
    more minutes, because the grant went to `hub-alerts` (which it was not in)
    and to `whoami` (which only a fresh session reads). The hub told every
    seat except the one whose powers changed."""
    boss = seat(hub, "boss", operator=True)
    seat(hub, "lead")
    hub.create_channel(boss, "rtype", True)
    hub.db.add_member("rtype", "lead")
    hub.set_delegation("lead", ["reporting", "proxy"], ttl_seconds=86400.0,
                       scope="rtype")

    told = [m for m in hub.db.get_messages(hub.DARK_ALERTS_CHANNEL, limit=50)
            if m.to == ["lead"]]
    assert told, "the seat whose powers changed was not told"
    body = told[-1].body
    assert "YOUR POWERS CHANGED" in body
    assert "proxy" in body and "#rtype" in body
    # It names the ACT the power unlocks, not just the word.
    assert "gated acts are YOURS" in body
    assert "waiting for someone who is not here is not caution" in body


def test_the_power_notice_wakes_but_owes_nothing(hub):
    """An addressed system fyi carries `to-me`, so it wakes a driven
    listener — and kind=system is not a directive debt, so the hub does not
    mint a row it can never discharge (the 0093 class: 8 undischargeable
    rows on one delegate)."""
    from agora.models import Status

    boss = seat(hub, "boss", operator=True)
    lead = seat(hub, "lead")
    hub.set_delegation("lead", ["reporting"], ttl_seconds=86400.0)
    told = [m for m in hub.db.get_messages(hub.DARK_ALERTS_CHANNEL, limit=50)
            if m.to == ["lead"]][-1]
    assert told.status == Status.fyi
    assert hub.owed(lead).counts.to_answer == 0


def test_a_proxy_holder_answers_a_gate_for_an_absent_owner(hub):
    """`proxy` exists to be the owner's hand while the owner is away, and it
    is scope-typed so it cannot leak past its room. Refusing the holder here
    made the power useless at the only moment it is for."""
    boss = seat(hub, "boss", operator=True)
    lead, worker = seat(hub, "lead"), seat(hub, "worker")
    hub.create_channel(boss, "room", True)
    for who in ("lead", "worker"):
        hub.db.add_member("room", who)
    hub.set_delegation("lead", ["proxy"], ttl_seconds=86400.0, scope="room")

    hub.store_set(lead, "room", "gate:ratify",
                  {"owner": "boss", "asked_by": "lead", "status": "asked",
                   "q": "which shape?"})
    # The owner is OUT OF CONTACT — that is the whole precondition. Proxy is
    # the owner's hand while they are away, never a second vote while they
    # are here (the conditional was inverted until 2026-08-07).
    assert hub._out_of_contact("boss")   # never touched the hub
    # The proxy holder rules on the absent owner's behalf.
    hub.store_set(lead, "room", "gate:ratify",
                  {"owner": "boss", "asked_by": "lead", "status": "granted",
                   "q": "which shape?", "acts": ["decision"]})
    assert hub.db.store_get("room", "gate:ratify").value["status"] == "granted"

    # A seat with no proxy still cannot.
    with pytest.raises(HubError, match="can answer this"):
        hub.store_set(worker, "room", "gate:ratify",
                      {"owner": "boss", "asked_by": "lead", "status": "denied",
                       "q": "which shape?", "acts": ["decision"]})


# -- the hub must DELIVER what it knows, not merely store it ---------------

def test_a_block_on_a_seat_must_name_the_seat(hub, rooms):
    """MEASURED IN `rtype-g4`, 2026-08-07. Two rows parked `blocked_on: seat`
    with `needs` reading "g4-engine must add an auditable same-run capture
    path". The hub had validated that field, so it KNEW who was blocking
    whom — and told nobody. `g4-engine` sat armed and idle in the same room
    while two rows named it. `/blockers` showed it perfectly, to whoever
    thought to pull it, which was no one.

    Same ruling as 2026-08-01 in another costume: an ask addressed to nobody
    obliges nobody. A block naming its unblocker only in prose is the same
    thing."""
    lead, _ = rooms
    with pytest.raises(HubError) as exc:
        hub.store_set(lead, "open-room", "claim:x",
                      {"owner": "lead", "status": "blocked",
                       "blocked_on": "seat",
                       "needs": "worker must add the capture path"})
    assert exc.value.status_code == 400
    assert "needs_from" in exc.value.detail
    assert "obliges nobody" in exc.value.detail


def test_the_named_seat_is_TOLD_it_is_the_blocker(hub, rooms):
    """The whole point. Storing who can unblock you and not delivering it is
    the failure this exists to end."""
    lead, worker = rooms
    hub.store_set(lead, "open-room", "claim:x",
                  {"owner": "lead", "status": "blocked", "blocked_on": "seat",
                   "needs_from": "worker",
                   "needs": "an auditable same-run capture path"})
    rung = [m for m in hub.db.get_messages("open-room", limit=50)
            if m.to == ["worker"]]
    assert rung, "the seat that could unblock it was never told"
    assert "YOU ARE THE BLOCKER" in rung[-1].body
    assert "auditable same-run capture path" in rung[-1].body
    assert rung[-1].status.value == "open"      # a real, dischargeable debt
    assert hub.owed(worker).counts.to_answer >= 1


def test_the_blocker_is_not_rung_twice_for_the_same_version(hub, rooms):
    lead, _ = rooms
    row = {"owner": "lead", "status": "blocked", "blocked_on": "seat",
           "needs_from": "worker", "needs": "the capture path"}
    hub.store_set(lead, "open-room", "claim:x", row)
    before = len(hub.db.get_messages("open-room", limit=99))
    hub.store_set(lead, "open-room", "claim:x", dict(row, note="same block"))
    after = hub.db.get_messages("open-room", limit=99)
    assert len([m for m in after if "YOU ARE THE BLOCKER" in m.body]) == 1
    assert len(after) == before                                  # no new ring
    assert hub.db.store_get("open-room", "claim:x").version == 2  # row updated

    # A genuinely DIFFERENT ask rings again — dedupe is on the block, not
    # the row, so an edited note is silent and a changed need is not.
    hub.store_set(lead, "open-room", "claim:x",
                  dict(row, needs="the capture path AND a frame dump"))
    assert len([m for m in hub.db.get_messages("open-room", limit=99)
                if "YOU ARE THE BLOCKER" in m.body]) == 2


def test_you_cannot_be_blocked_on_yourself_or_a_stranger(hub, rooms):
    lead, _ = rooms
    with pytest.raises(HubError, match="blocked on itself"):
        hub.store_set(lead, "open-room", "claim:x",
                      {"owner": "lead", "status": "blocked",
                       "blocked_on": "seat", "needs_from": "lead",
                       "needs": "me"})
    with pytest.raises(HubError, match="not a member"):
        hub.store_set(lead, "open-room", "claim:y",
                      {"owner": "lead", "status": "blocked",
                       "blocked_on": "seat", "needs_from": "ghost",
                       "needs": "someone who is not here"})


def test_a_block_written_before_the_hook_is_still_delivered(hub, rooms):
    """THE GENERAL DEFECT (2026-08-07). Every delivery mechanism on this hub
    was write-triggered, so a coordination fact was lost for every row
    already written and for every seat that was down when it happened.

    Measured: two rows in `rtype-g4` named `g4-engine` as their blocker
    before the write hook shipped. The room sat dead for twenty minutes
    after the fix deployed, because nothing re-reads state.

    State the hub can derive must be deliverable FROM the state."""
    lead, worker = rooms
    # A block written straight to the DB — the hook never ran.
    hub.db.store_set("open-room", "claim:legacy",
                     {"owner": "lead", "status": "blocked",
                      "blocked_on": "seat", "needs_from": "worker",
                      "needs": "an auditable same-run capture path"},
                     "lead", None)
    assert not [m for m in hub.db.get_messages("open-room", limit=50)
                if m.to == ["worker"]]

    assert hub._blocking_sweep() == ["blocking:open-room/claim:legacy"]
    rung = [m for m in hub.db.get_messages("open-room", limit=50)
            if m.to == ["worker"]]
    assert rung and "YOU ARE THE BLOCKER" in rung[-1].body
    assert "auditable same-run capture path" in rung[-1].body


def test_the_sweep_and_the_write_hook_share_one_dedupe(hub, rooms):
    """Belt and braces must not mean told twice: the fast path and the
    durable path key on the same block."""
    lead, _ = rooms
    hub.store_set(lead, "open-room", "claim:x",
                  {"owner": "lead", "status": "blocked", "blocked_on": "seat",
                   "needs_from": "worker", "needs": "the capture path"})
    assert hub._blocking_sweep() == []      # the write hook already told them
    assert len([m for m in hub.db.get_messages("open-room", limit=50)
                if "YOU ARE THE BLOCKER" in m.body]) == 1


def test_a_resolved_block_stops_being_swept(hub, rooms):
    lead, _ = rooms
    hub.db.store_set("open-room", "claim:x",
                     {"owner": "lead", "status": "blocked",
                      "blocked_on": "seat", "needs_from": "worker",
                      "needs": "x"}, "lead", None)
    assert hub._blocking_sweep()
    hub.store_set(lead, "open-room", "claim:x",
                  {"owner": "lead", "status": "done"})
    assert hub._blocking_sweep() == []


def test_an_undeliverable_block_is_reported_to_its_owner(hub, rooms):
    """SILENT INABILITY IS A SILENT FALLBACK (2026-08-07).

    A row saying `blocked_on: seat` with no `needs_from` cannot be
    delivered — the seat is named only in prose, and reading prose to guess
    who is meant is the mind-reading gate. But doing nothing is not the
    alternative.

    Measured in `rtype-g4`: two rows said `blocked_on: seat`, unset
    `needs_from`, and `needs` reading "g4-engine must add an auditable
    same-run capture path". Both predated the field. The hub could see the
    block, could not address it, and said nothing for thirty minutes while
    seven seats sat idle. Every fix that requires a new field leaves a
    cohort of rows behind; the hub must at least tell their owner it is
    holding something it cannot deliver."""
    lead, _ = rooms
    hub.db.store_set("open-room", "claim:legacy",
                     {"owner": "lead", "status": "blocked",
                      "blocked_on": "seat",
                      "needs": "worker must add the capture path"},
                     "lead", None)

    assert hub._blocking_sweep() == ["undeliverable-block:open-room/claim:legacy"]
    told = [m for m in hub.db.get_messages("open-room", limit=50)
            if m.to == ["lead"]]
    assert told, "the owner was never told its block reaches nobody"
    body = told[-1].body
    assert "UNDELIVERABLE BLOCK" in body
    assert "nobody is coming" in body
    assert "worker must add the capture path" in body   # quotes their words
    assert "needs_from" in body                          # and names the fix

    # Saying it once is enough until the block changes.
    assert hub._blocking_sweep() == []

    # Naming the seat converts it into a real delivery.
    hub.store_set(lead, "open-room", "claim:legacy",
                  {"owner": "lead", "status": "blocked", "blocked_on": "seat",
                   "needs_from": "worker",
                   "needs": "worker must add the capture path"})
    rung = [m for m in hub.db.get_messages("open-room", limit=50)
            if m.to == ["worker"] and "YOU ARE THE BLOCKER" in m.body]
    assert rung


def test_the_blocked_owner_is_told_when_its_blocker_answers(hub, rooms):
    """A DELIVERY MECHANISM MUST BE SYMMETRIC (2026-08-07).

    Measured: `g4-qa` parked blocked on `g4-engine`. The hub told
    `g4-engine`, which did the work and announced it `status=resolved,
    to=[]` — unaddressed, so `g4-qa` owed nothing and never woke. The hub
    knew exactly who was waiting and never looked back down the wire. 118
    messages, 7/7 seats idle, on work that was finished.

    The hub reports the fact and lifts nothing: only the owner can decide
    that its own block is over."""
    lead, worker = rooms
    hub.store_set(lead, "open-room", "claim:x",
                  {"owner": "lead", "status": "blocked", "blocked_on": "seat",
                   "needs_from": "worker", "needs": "the capture path"})
    alert = [m for m in hub.db.get_messages("open-room", limit=50)
             if m.to == ["worker"] and "YOU ARE THE BLOCKER" in m.body][-1]

    # Nothing has been answered yet: the owner is not pestered.
    assert hub._blocking_sweep() == []
    assert not [m for m in hub.db.get_messages("open-room", limit=50)
                if m.to == ["lead"] and "YOUR BLOCKER ANSWERED" in m.body]

    # The blocker answers the hub's own addressed alert.
    hub.post_message(worker, "open-room", PostMessage(
        status=Status.reply, body="capture path landed", reply_to=alert.id))

    hub._blocking_sweep()
    back = [m for m in hub.db.get_messages("open-room", limit=50)
            if m.to == ["lead"] and "YOUR BLOCKER ANSWERED" in m.body]
    assert back, "the blocked owner was never told its blocker had spoken"
    assert "only you can lift it" in back[-1].body
    # The hub did NOT lift the block itself.
    assert hub.db.store_get("open-room", "claim:x").value["status"] == "blocked"

    # Said once per answer, not once per sweep.
    hub._blocking_sweep()
    assert len([m for m in hub.db.get_messages("open-room", limit=50)
                if "YOUR BLOCKER ANSWERED" in m.body]) == 1


# -- the delegate supervises; the powers decide the moves ------------------

def test_supervise_reports_idle_seats_blockers_and_what_you_may_do(hub):
    """OPERATOR RULING 2026-08-07: "the delegate is in essence more a
    SUPERVISOR than a doer; it helps others ensure they can accomplish the
    tasks they were meant to... the amount of things it would do is still
    conditioned by the power granted."

    The hub computes the picture; the delegate decides. And what it may do
    is conditioned by what it holds — a supervisor that reports moves the
    hub would refuse is worse than silent."""
    boss = seat(hub, "boss", operator=True)
    lead = seat(hub, "lead"); worker = seat(hub, "worker"); idler = seat(hub, "idler")
    hub.create_channel(boss, "room", True)
    for who in ("lead", "worker", "idler"):
        hub.db.add_member("room", who)
    hub.set_delegation("lead", ["reporting"], ttl_seconds=86400.0, scope="room")
    for who in ("lead", "worker", "idler"):
        hub.presence.touch(who)          # they are all awake and reachable

    hub.store_set(worker, "room", "claim:a",
                  {"owner": "worker", "status": "blocked", "blocked_on": "seat",
                   "needs_from": "idler", "needs": "the capture path"})
    hub.store_set(lead, "room", "claim:b",
                  {"owner": "lead", "status": "parked",
                   "blocked_on": "operator", "needs": "which art direction"})

    view = hub.supervise(lead, "room")
    assert view["your_powers"] == ["reporting"]
    # `idler` is alive and holds nothing — the thing a supervisor must see.
    assert "idler" in view["idle_but_live"]
    by_key = {b["key"]: b for b in view["blocked"]}
    # A named seat is always chaseable.
    assert by_key["claim:a"]["you_can_act"] is True
    assert "idler" in by_key["claim:a"]["move"]
    # Without `proxy`, an owner-blocked row is NOT yours.
    assert by_key["claim:b"]["you_can_act"] is False
    assert "no `proxy`" in by_key["claim:b"]["move"]
    assert view["needs_the_operator"] == ["claim:b"]


def test_proxy_changes_what_the_supervisor_may_do(hub):
    """Same stalled room, different powers, different available moves."""
    boss = seat(hub, "boss", operator=True)
    lead = seat(hub, "lead")
    hub.create_channel(boss, "room", True)
    hub.db.add_member("room", "lead")
    hub.set_delegation("lead", ["reporting", "proxy"], ttl_seconds=86400.0,
                       scope="room")
    hub.store_set(lead, "room", "claim:b",
                  {"owner": "lead", "status": "parked",
                   "blocked_on": "operator", "needs": "which art direction"})
    b = hub.supervise(lead, "room")["blocked"][0]
    assert b["you_can_act"] is True
    assert "proxy" in b["move"]
    assert hub.supervise(lead, "room")["needs_the_operator"] == []


def test_supervise_is_the_delegates_view_only(hub):
    boss = seat(hub, "boss", operator=True)
    plain = seat(hub, "plain")
    hub.create_channel(boss, "room", True)
    hub.db.add_member("room", "plain")
    with pytest.raises(HubError, match="delegate's view"):
        hub.supervise(plain, "room")


def test_supervise_names_what_each_idle_seat_is_FOR(hub):
    """OPERATOR RULING 2026-08-07: "it must be smart at leveraging the
    resources at its disposal (the various agents and their specific
    expertises)... he knows which agents are here and have been idle for some
    time AND WHY."

    This view emitted a list of idle seats with no statement of what any of
    them was for, so a delegate had to guess who to hand the work to — while
    `mission` sat in the member list unread."""
    boss = seat(hub, "boss", operator=True)
    lead = seat(hub, "lead")
    hub.register_agent("artist", "Artist", False,
                       mission="You hold the VISUAL perspective.")
    hub.create_channel(boss, "room", True)
    for who in ("lead", "artist"):
        hub.db.add_member("room", who)
        hub.presence.touch(who)
    hub.set_delegation("lead", ["reporting"], ttl_seconds=86400.0, scope="room")

    view = hub.supervise(lead, "room")
    artist = [s for s in view["seats"] if s["seat"] == "artist"][0]
    assert artist["holds_nothing"] is True
    assert "VISUAL perspective" in artist["mission"]   # who to hand it to
    assert "reception" in artist                       # and whether it can hear


def test_supervise_honours_the_scope_of_the_grant(hub):
    """It flattened powers across every grant and ignored scope, while the
    act it promises goes through `has_proxy(agent, channel)` which does not.
    A delegate scoped to one room was told "decide it yourself under proxy"
    about a row in another — a move the hub then refuses with 403."""
    boss = seat(hub, "boss", operator=True)
    lead = seat(hub, "lead")
    for room in ("mine", "elsewhere"):
        hub.create_channel(boss, room, True)
        hub.db.add_member(room, "lead")
    hub.set_delegation("lead", ["reporting", "proxy"], ttl_seconds=86400.0,
                       scope="mine")
    hub.store_set(lead, "elsewhere", "claim:x",
                  {"owner": "lead", "status": "parked",
                   "blocked_on": "operator", "needs": "a call"})

    # In the room its grant does NOT reach, it is an ordinary member — and
    # the view refuses rather than reporting powers it cannot spend there.
    with pytest.raises(HubError, match="delegate's view"):
        hub.supervise(lead, "elsewhere")
    # In its own room the same seat gets the full picture.
    assert "proxy" in hub.supervise(lead, "mine")["your_powers"]


def test_supervise_carries_the_rooms_open_phase(hub):
    """"he knows if and when it can reactivate them (eg a task completion was
    missing to move on to next phase)." Reported beside the idle list, never
    joined — which task a phase needs is the delegate's judgement."""
    boss = seat(hub, "boss", operator=True)
    lead = seat(hub, "lead")
    hub.create_channel(boss, "room", True)
    hub.db.add_member("room", "lead")
    hub.set_delegation("lead", ["reporting", "operational"],
                       ttl_seconds=86400.0, scope="room")
    hub.store_set(lead, "room", "phase:build",
                  {"current": "M0", "status": "open", "next": "M1",
                   "steward": "lead"})
    ph = hub.supervise(lead, "room")["open_phases"]
    assert ph and ph[0]["current"] == "M0" and ph[0]["next"] == "M1"
