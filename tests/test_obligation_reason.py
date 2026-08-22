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
