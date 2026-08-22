"""WHY an obligation row exists, served as an enum (0155).

The field incident: an operator looked at a "needs reply" pill with no
question anywhere near it and asked whether that was a bug (laurent,
agora-and-wui#53). It was not — naming a seat creates the debt, asks only
itemise it — but the client was not being terse and was not free to guess:
`ObligationRow` carried `pending_asks` and `asks_naming_you` and no reason,
so with both empty there was literally nothing to render but the fact of the
row.

The load-bearing pair is `peer_request_no_asks` vs
`operator_request_awaiting_your_report`. They look identical on the row and
their exits INVERT, and agora-tui#60 reported the consequence: their action
rail offered `↩reply` on every row and `✓resolve` on any open/blocked, so on
an operator's ask-less open it offered the verb that cannot discharge the row
beside the one that can, with nothing to tell them apart. Which verb
discharges is the hub's verdict; it was not derivable from `status`.
"""

from fastapi.testclient import TestClient

from agora.hub.app import create_app

ADMIN_KEY = "test-admin"


def make_client() -> TestClient:
    return TestClient(create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                                 rate_per_minute=600.0))


def register(client: TestClient, agent_id: str,
             operator: bool = False) -> dict[str, str]:
    r = client.post("/agents",
                    json={"id": agent_id, "mission": f"seat {agent_id}",
                          "operator": operator},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_channel(client: TestClient, owner: dict, name: str, *members: dict) -> None:
    client.post("/channels", json={"name": name}, headers=owner)
    for member in members:
        invite = client.post(f"/channels/{name}/invites", json={},
                             headers=owner).json()["invite_token"]
        client.post(f"/channels/{name}/join", json={"invite_token": invite},
                    headers=member)


def post(client: TestClient, headers: dict, channel: str = "room", **kw) -> dict:
    r = client.post(f"/channels/{channel}/messages", json=kw, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def reason_for(client: TestClient, headers: dict, seq: int) -> str | None:
    rows = client.get("/owed", headers=headers).json()["to_answer"]
    row = next(r for r in rows if r["seq"] == seq)
    return row["reason"]


def rows_for(client: TestClient, headers: dict) -> list[dict]:
    return client.get("/owed", headers=headers).json()["to_answer"]


# -- the four cases ---------------------------------------------------------

def test_pending_asks_that_are_yours_read_as_asks_pending():
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    q = post(client, alice, body="two asks", title="q", status="open",
             asks=[{"id": "1", "text": "a?", "to": ["bob"]}])

    assert reason_for(client, bob, q["seq"]) == "asks_pending"


def test_an_addressed_reply_reads_as_names_you():
    """Case 2: a directive debt. Any reply from the named seat clears it —
    which is precisely what the row could not say."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    root = post(client, alice, body="a root", title="root")
    named = post(client, alice, body="pointing this at you", title="yours",
                 status="reply", reply_to=root["id"], to=["bob"])

    row = next(r for r in rows_for(client, bob) if r["seq"] == named["seq"])
    assert row["reason"] == "names_you"
    # The state the operator was looking at: a debt with no question in it.
    assert row["pending_asks"] == [] and row["asks_naming_you"] == []


def test_a_peers_ask_less_open_reads_as_peer_request_no_asks():
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    q = post(client, alice, body="do the thing", title="job", status="open",
             to=["bob"])

    row = next(r for r in rows_for(client, bob) if r["seq"] == q["seq"])
    assert row["reason"] == "peer_request_no_asks"
    assert row["pending_asks"] == [] and row["asks_naming_you"] == []


def test_an_operators_ask_less_open_names_the_report_it_is_waiting_for():
    """The one that changes a client's BEHAVIOUR rather than its wording."""
    client = make_client()
    laurent = register(client, "laurent", operator=True)
    bob = register(client, "bob")
    make_channel(client, laurent, "room", bob)

    q = post(client, laurent, body="ship the parser by Friday", title="job",
             status="open", to=["bob"])

    assert reason_for(client, bob, q["seq"]) == "operator_request_awaiting_your_report"


def test_the_two_ask_less_cases_are_distinguishable_and_they_invert():
    """Same status, same shape, same empty ask lists, opposite exits — the
    whole reason these are two values and not one. A client that printed the
    operator wording on both would send a seat hunting for evidence it does
    not need (agora-wui#58); one that printed the peer wording on both would
    offer a verb that cannot discharge the row (agora-tui#60)."""
    client = make_client()
    laurent = register(client, "laurent", operator=True)
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, laurent, "room", alice, bob)

    from_peer = post(client, alice, body="peer job", title="peer",
                     status="open", to=["bob"])
    from_op = post(client, laurent, body="operator job", title="op",
                   status="open", to=["bob"])

    rows = {r["seq"]: r for r in rows_for(client, bob)}
    peer, op = rows[from_peer["seq"]], rows[from_op["seq"]]

    # Indistinguishable on every field a client had before this one...
    assert peer["pending_asks"] == op["pending_asks"] == []
    assert peer["asks_naming_you"] == op["asks_naming_you"] == []
    # ...and now distinguishable on the one that says what to do about it.
    assert peer["reason"] == "peer_request_no_asks"
    assert op["reason"] == "operator_request_awaiting_your_report"

    # And the exits really do invert. NOTE what a bare reply does NOT do:
    # both clients described this case as "any reply from me clears it"
    # (agora-wui#58, agora-tui#60) and BOTH ARE WRONG about the hub — a
    # peer's addressed work ask is not closed by "on it" (2026-08-11). This
    # assertion is the one that caught it.
    post(client, bob, body="on it", title="re",
         status="reply", reply_to=from_peer["id"])
    post(client, bob, body="on it", title="re",
         status="reply", reply_to=from_op["id"])
    still = {r["seq"] for r in rows_for(client, bob)}
    assert from_peer["seq"] in still and from_op["seq"] in still

    # The PEER row's real exit: a claim row citing it — ownership
    # materialized, pressure moved onto the claim.
    r = client.put("/channels/room/store/claim:the-peer-job",
                   json={"value": {"owner": "bob", "status": "open",
                                   "source_message_id": from_peer["id"]}},
                   headers=bob)
    assert r.status_code == 200, r.text

    left = {r["seq"] for r in rows_for(client, bob)}
    assert from_peer["seq"] not in left
    # ...and the operator's row is untouched by the same claim: it wants a
    # resolved reply citing evidence, or laurent's own word.
    assert from_op["seq"] in left


def test_asks_pending_wins_over_the_sender_based_reasons():
    """Precedence: a row with pending asks that are yours is an asks row
    whatever else is true of it — including an operator sender."""
    client = make_client()
    laurent = register(client, "laurent", operator=True)
    bob = register(client, "bob")
    make_channel(client, laurent, "room", bob)

    q = post(client, laurent, body="with structure", title="q", status="open",
             to=["bob"], asks=[{"id": "1", "text": "which?", "to": ["bob"]}])

    assert reason_for(client, bob, q["seq"]) == "asks_pending"


def test_every_served_row_carries_a_reason():
    """The contract, not a case: a current hub always states. A null here
    means only 'a hub older than this field'."""
    client = make_client()
    laurent = register(client, "laurent", operator=True)
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, laurent, "room", alice, bob)

    root = post(client, alice, body="a root", title="root")
    post(client, alice, body="named", title="named", status="reply",
         reply_to=root["id"], to=["bob"])
    post(client, alice, body="peer job", title="peer", status="open", to=["bob"])
    post(client, laurent, body="op job", title="op", status="open", to=["bob"])
    post(client, alice, body="structured", title="q", status="open",
         asks=[{"id": "1", "text": "a?", "to": ["bob"]}])

    rows = rows_for(client, bob)
    assert len(rows) == 4
    assert all(r["reason"] for r in rows), [r.get("reason") for r in rows]
    assert {r["reason"] for r in rows} == {
        "names_you", "peer_request_no_asks",
        "operator_request_awaiting_your_report", "asks_pending",
    }


def test_a_pending_ask_addressed_to_ANOTHER_seat_is_not_your_asks_row():
    """THE FIELD INCIDENT, second generation — caught on the live hub within
    an hour of shipping this enum, by two seats independently.

    agora-and-wui#117 named `agora` in `to` and carried one ask addressed to
    `laurent`. agora's row came back `asks_pending` with `['1']` beside it,
    so agora tried to DECLINE ask 1 — the charter's honest exit for an ask
    that is not yours — and the same hub refused: *"you may not discharge
    ask ids not addressed to you"*. The row named an exit the hub forbids,
    which is the exact defect this enum was built to end, reintroduced by
    the enum.

    agora-tui hit the mirror image at #122 and hedged it as one unverified
    instance. It is not a hedge; it is one line, and it read `if ds.pending`
    when the question is *whether any pending ask is MINE*.

    The seat's real exit is `names_you` — any reply from them clears it —
    and it must say so."""
    client = make_client()
    laurent = register(client, "laurent", operator=True)
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, laurent, "room", alice, bob)

    q = post(client, alice, body="asking the operator", title="q",
             status="open", to=["laurent", "bob"],
             asks=[{"id": "1", "text": "which layout?", "to": ["laurent"]}])

    # laurent owns the ask and gets the asks row, with the id to answer.
    assert reason_for(client, laurent, q["seq"]) == "asks_pending"

    # bob is named on the message and on no ask. Not an asks row: bob cannot
    # answer '1' and cannot decline it either.
    assert reason_for(client, bob, q["seq"]) == "names_you"
    row = next(r for r in rows_for(client, bob) if r["seq"] == q["seq"])
    assert row["asks_naming_you"] == []

    # And the hub still refuses bob the ids, which is the refusal that has to
    # agree with the reason above rather than contradict it.
    refused = client.post("/channels/room/messages", headers=bob, json={
        "body": "not mine", "title": "no", "status": "reply",
        "reply_to": q["id"], "declines": ["1"]})
    assert refused.status_code == 400
    assert "not addressed to you" in refused.text

    # And the exit the value NAMES is the exit that works. Asserting the
    # word alone would be decoration: the whole complaint is rows naming an
    # act that does not clear them, so the act has to be performed.
    post(client, bob, body="noted, not mine", title="noted", status="reply",
         reply_to=q["id"])
    assert all(r["seq"] != q["seq"] for r in rows_for(client, bob))
    # ...and laurent's ask is untouched by bob replying.
    assert reason_for(client, laurent, q["seq"]) == "asks_pending"


def test_an_UNADDRESSED_pending_ask_is_still_everyones_asks_row():
    """The other half, and the reason the fix is not "only asks naming you".

    An ask with no `to` names nobody and is therefore everyone's — the same
    reading `pending_addressees` and the driver both take. If this narrowed
    to per-ask addressing, a bare open question to a room would tell every
    addressee `names_you` and hide the question they are actually being
    asked."""
    client = make_client()
    laurent = register(client, "laurent", operator=True)
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, laurent, "room", alice, bob)

    q = post(client, alice, body="anyone", title="q", status="open",
             to=["bob"], asks=[{"id": "1", "text": "who knows this?"}])

    assert reason_for(client, bob, q["seq"]) == "asks_pending"


# -- the addressee's door on a peer's ask-less request (2026-08-22) ----------

def _store_file(client: TestClient, headers: dict, channel: str, path: str) -> str:
    r = client.put(f"/channels/{channel}/fs/{path}",
                   json={"content": "the delivery", "description": "proof"},
                   headers=headers)
    assert r.status_code == 200, r.text
    return f"{path}@{r.json()['version']}"


def test_a_named_seat_that_DELIVERS_clears_a_peers_ask_less_request():
    """The completion exit the 2026-08-11 rule left shut.

    That rule kept the OWNERSHIP exit open — a linked claim row moves the
    pressure onto the claim. But a seat that FINISHES inside one turn has no
    claim to materialize, and its cited completion report moved nothing: the
    row stood and escalated against the one seat that did the work. Found
    from the inside — this hub told `agora` it still owed
    `agora-and-wui#217` after the commit answering it was pushed and cited.

    Same door and same price as the operator rule: `resolved`, from a seat
    the asker NAMED, citing evidence the hub resolved at post time.
    """
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    job = post(client, alice, body="please do the thing", title="thing",
               status="blocked", to=["bob"])
    assert job["seq"] in {r["seq"] for r in rows_for(client, bob)}

    ref = _store_file(client, bob, "room", "delivered.md")
    post(client, bob, body="done, here it is", title="delivered",
         status="resolved", reply_to=job["id"],
         data={"evidence": [{"kind": "fs", "ref": ref}]})

    assert job["seq"] not in {r["seq"] for r in rows_for(client, bob)}


def test_the_2026_08_11_lesson_survives_the_new_door():
    """Each half of the price is load-bearing. Drop any one and the old lie
    — a bare reply means the work is done — comes back."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)
    ref = _store_file(client, bob, "room", "proof.md")

    # 1. `resolved` with NO evidence: a promise wearing a verb. Until
    #    2026-08-23 it was ACCEPTED and silently void — the row stayed and
    #    escalated against bob, and nothing told him why. It is now refused
    #    at the door, and because bob is the seat alice NAMED, the refusal
    #    hands him his own door rather than a bare no.
    a = post(client, alice, body="job a", title="a", status="blocked", to=["bob"])
    void = client.post("/channels/room/messages", headers=bob,
                       json={"body": "all done, trust me", "title": "re",
                             "status": "resolved", "reply_to": a["id"]})
    assert void.status_code == 400
    assert "NAMED you" in void.json()["detail"]
    assert "data.evidence" in void.json()["detail"]
    assert a["seq"] in {r["seq"] for r in rows_for(client, bob)}

    # 2. Evidence but NOT `resolved`: still in flight.
    b = post(client, alice, body="job b", title="b", status="blocked", to=["bob"])
    post(client, bob, body="progress", title="re", status="reply",
         reply_to=b["id"], data={"evidence": [{"kind": "fs", "ref": ref}]})
    assert b["seq"] in {r["seq"] for r in rows_for(client, bob)}

    # 3. "on it" — the exact reply the 2026-08-11 rule was written to refuse.
    c = post(client, alice, body="job c", title="c", status="blocked", to=["bob"])
    post(client, bob, body="on it", title="re", status="reply", reply_to=c["id"])
    assert c["seq"] in {r["seq"] for r in rows_for(client, bob)}


def test_a_bystanders_cited_resolved_does_not_clear_the_named_seats_row():
    """The door is the ADDRESSEE's. A seat the asker never named cannot
    report completion on their behalf — that is the 2026-08-04 bystander
    lesson, which this must not undo.

    Since 2026-08-23 carol does not merely fail to clear the row: she is
    refused, because her `resolved` settles nothing. Note what the refusal
    must NOT say to her — she already cited evidence, so an "add
    data.evidence" recipe would send a correct reader round a second loop.
    The recipe is computed per row, and hers is the bystander one."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    carol = register(client, "carol")
    make_channel(client, alice, "room", bob, carol)

    job = post(client, alice, body="bob's job", title="job", status="blocked",
               to=["bob"])
    ref = _store_file(client, carol, "room", "carol.md")
    refused = client.post("/channels/room/messages", headers=carol,
                          json={"body": "I did it", "title": "re",
                                "status": "resolved", "reply_to": job["id"],
                                "data": {"evidence": [{"kind": "fs",
                                                       "ref": ref}]}})
    assert refused.status_code == 400
    assert "data.evidence" not in refused.json()["detail"], \
        "told a seat that already cited evidence to add evidence"
    assert "ordinary reply" in refused.json()["detail"]

    assert job["seq"] in {r["seq"] for r in rows_for(client, bob)}


# -- a `resolved` that can settle nothing is refused (2026-08-23) -----------
#
# The hub refuses an `answers[]` that discharges nothing WITH the correct
# gesture — four field incidents in one day bought that rule — and accepted
# in silence the same shape one field over. agora-wui's console printed
# "Marked #N resolved." on exactly that no-op; two clients that share no code
# then derived a `resolve` affordance from the status word alone, because the
# hub's silence read as permission (agora-tui `4a575a3`, agora-wui `e35b53e`).
#
# The agreed constraints (thread-shape-and-panels#29/#30) are what these
# tests pin: (1) the refusal NAMES who may close, never a bare no; (2) it
# fires ONLY where settling is provably impossible.

def _grant(client: TestClient, agent_id: str, powers: list[str],
           scope: str = "*") -> None:
    r = client.put("/admin/delegation",
                   json={"agent_id": agent_id, "powers": powers,
                         "scope": scope},
                   headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    assert r.status_code == 200, r.text
    client.app.state.service._delegations_cache_at = 0.0


def _resolve(client: TestClient, headers: dict, parent_id: str, **data):
    return client.post("/channels/room/messages", headers=headers,
                       json={"body": "closing", "status": "resolved",
                             "reply_to": parent_id, **data})


def test_a_bystanders_bare_resolved_is_refused_and_named():
    """(a) The core case, and constraint (1): the refusal must NAME who may
    close the row. A bare no on a control the reader believes in produces a
    bug report, not a correction."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    carol = register(client, "carol")
    make_channel(client, alice, "room", bob, carol)
    # Addressed to bob, so carol is a true bystander: no ask of her own to
    # discharge and no completion report she is entitled to make.
    job = post(client, alice, body="bob's job", title="job", status="blocked",
               to=["bob"])

    r = _resolve(client, carol, job["id"])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "alice" in detail, "constraint (1): the refusal must name who may close"
    assert "ruling/operational" in detail
    assert "ordinary reply" in detail
    # ...and the row is untouched: nothing was posted.
    assert job["seq"] in {x["seq"] for x in rows_for(client, bob)}


def test_the_author_an_operator_and_a_scoped_ruling_delegate_are_all_accepted():
    """(b)(c)(d)(e) Constraint (2): it fires ONLY where settling is provably
    impossible. Four seats CAN settle, and each must still be accepted —
    including the `operational` half of the pair, which my own restatement of
    this rule narrowed to `ruling` twice in one evening."""
    client = make_client()
    alice = register(client, "alice")
    op = register(client, "op", operator=True)
    ruler, operational = register(client, "ruler"), register(client, "ops")
    make_channel(client, alice, "room", op, ruler, operational)
    _grant(client, "ruler", ["ruling"], scope="room")
    _grant(client, "ops", ["operational"], scope="room")

    for seat, who in ((alice, "the author"), (op, "an operator"),
                      (ruler, "a ruling delegate"),
                      (operational, "an operational delegate")):
        q = post(client, alice, body="q", title="q", status="open",
                 asks=[{"id": "1", "text": "a?"}])
        r = _resolve(client, seat, q["id"])
        assert r.status_code == 200, f"{who} was refused: {r.text}"


def test_a_ruling_delegate_scoped_ELSEWHERE_is_refused():
    """The falsification for (d)/(e): scope is load-bearing. Delete the
    `scope` check in `ruling_delegate_ids` and this goes green while a grant
    over another room silently closes threads here."""
    client = make_client()
    alice, ruler = register(client, "alice"), register(client, "ruler")
    make_channel(client, alice, "room", ruler)
    make_channel(client, alice, "elsewhere", ruler)
    _grant(client, "ruler", ["ruling"], scope="elsewhere")
    q = post(client, alice, body="q", title="q", status="open",
             asks=[{"id": "1", "text": "a?"}])
    assert _resolve(client, ruler, q["id"]).status_code == 400


def test_a_resolved_carrying_settled_by_is_never_touched_by_this_gate():
    """(f) The audited supersession path is authorized by its own rule, and
    this gate must not second-guess it. The asker's `settled_by` close is the
    one that must keep working."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)
    q = post(client, alice, body="q", title="q", status="open",
             asks=[{"id": "1", "text": "a?"}])
    elsewhere = post(client, bob, body="the ruling", title="ruling")
    r = _resolve(client, alice, q["id"],
                 data={"settled_by": elsewhere["id"]})
    assert r.status_code == 200, r.text


def test_the_d6b63d4_shaped_resolved_is_NOT_refused():
    """(g) THE ORDERING TEST, asked for by agora-wui (#30) before either half
    was serving, and this is why it exists.

    `d6b63d4` — a named addressee's CITED completion report discharges an
    ask-less addressed peer request — and this refusal were both committed
    and unserved on the same evening, so they land in the same restart. A
    refusal written as "refuse unless it CLOSES" would reject precisely the
    discharge `d6b63d4` was written to create, and nothing would have warned
    us: the conflict arrives already live.

    THE FALSIFICATION, and I checked it rather than asserting it. Dropping
    the `discharged` half of the predicate does NOT turn this red: both
    branches of `discharge_state` return `closed = discharged or
    closed_by_resolve`, so `closed` already covers the `d6b63d4` exit. What
    DOES turn it red is the mistake the test was written against — replacing
    the delta with an allow-list of today's authorized senders. Verified by
    doing it: this and `..._named_seat_that_DELIVERS_...` both fail, and
    nothing else does."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)
    job = post(client, alice, body="bob's job", title="job", status="blocked",
               to=["bob"])
    ref = _store_file(client, bob, "room", "delivery.md")

    r = _resolve(client, bob, job["id"],
                 data={"evidence": [{"kind": "fs", "ref": ref}]})
    assert r.status_code == 200, r.text
    # ...and it really did settle it — the row is gone, which is the whole
    # point of not refusing it.
    assert job["seq"] not in {x["seq"] for x in rows_for(client, bob)}


def test_an_already_closed_thread_is_not_refused():
    """Constraint (2) at its other edge: on a CLOSED thread settling is not
    impossible, it already happened. A late `resolved` there is ceremony, not
    a false assertion, and refusing it would be this gate overreaching.

    Falsification: the allow-list version (see the `d6b63d4` test) refuses
    bob here, so this goes red with it. What does NOT falsify it is deleting
    an explicit already-closed guard — I wrote one, deleted it, and nothing
    went red, because `closed` is monotone in the reply list. The guard is
    gone; this test is what keeps the behaviour."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)
    q = post(client, alice, body="q", title="q", status="open",
             asks=[{"id": "1", "text": "a?"}])
    post(client, alice, body="closing my own", status="resolved",
         reply_to=q["id"])
    assert _resolve(client, bob, q["id"]).status_code == 200


def test_the_refusal_hands_each_seat_the_gesture_that_would_actually_work():
    """Constraint (1) done properly. A refusal naming the WRONG exit is worse
    than a bare one: it sends a correct reader round a second loop, and the
    second refusal is the one they stop believing.

    Three senders, three recipes, and each assertion falsifies the other two:
    collapse `_resolved_settles_nothing_refusal` to any single message and at
    least two of these go red."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    carol = register(client, "carol")
    make_channel(client, alice, "room", bob, carol)

    # 1. A seat with a pending ask of its own: the exit is answers[].
    q = post(client, alice, body="q", title="q", status="open",
             asks=[{"id": "7", "text": "a?", "to": ["bob"]}])
    d = _resolve(client, bob, q["id"]).json()["detail"]
    assert 'answers=["7"]' in d and 'declines=["7"]' in d
    assert "data.evidence" not in d, "an ask needs an answer, not a citation"

    # 2. The NAMED seat on an ask-less request: the exit is evidence.
    job = post(client, alice, body="bob's job", title="job", status="blocked",
               to=["bob"])
    d = _resolve(client, bob, job["id"]).json()["detail"]
    assert "NAMED you" in d and "data.evidence" in d
    assert "answers=[" not in d, "there is no ask to answer here"

    # 3. A true bystander: neither recipe would work, and offering one would
    #    be the second-loop failure. `settled_by` is not offered either — it
    #    is a 403 for exactly this seat since the 2026-08-22 ruling.
    d = _resolve(client, carol, job["id"]).json()["detail"]
    assert "ordinary reply" in d
    assert "data.evidence" not in d and "answers=[" not in d
    assert "settled_by" not in d, "offered an exit that is refused with a 403"
