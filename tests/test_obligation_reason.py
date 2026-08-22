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
