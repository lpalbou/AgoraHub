"""Who has this, and where are they with it (0156) — `plan:pickup-feedback`.

The operator's complaint (dm#20): *"we just type a message and wait to see
if anyone is gonna answer"*. Every fact was already stored — ack cursor,
read receipt, presence, and a claim row citing the message — and nothing
joined them per-message for the asker.

Two things these tests exist to hold, beyond the feature:

- The rungs must not say more than the facts support. `delivered` is absent
  on purpose; `read` is present but weak; `claimed` is the only affirmative
  act, and it is a DECLARATION the hub cannot verify.
- `still_owes` comes from the discharge call, never from "a reply arrived".
  The two come apart on a multi-addressee ask, which is the state agora-tui
  was in at #86 when they read their own compliant row as a broken ledger.

The PUSH half has its own section at the bottom. The row alone would have
left the operator exactly where he started — going back to look — so the
tests that matter most here are the ones asserting a frame ARRIVES, and
that it carries the whole ladder every time.
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
    key = r.json()["api_key"]
    KEYS[agent_id] = key
    return {"Authorization": f"Bearer {key}"}


#: raw api keys by seat, for the ws query-param connect.
KEYS: dict[str, str] = {}


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


def row(client: TestClient, headers: dict, seq: int, channel: str = "room") -> dict:
    rows = client.get(f"/channels/{channel}/messages", headers=headers).json()
    return next(r for r in rows if r["seq"] == seq)


def pickup(client: TestClient, headers: dict, seq: int) -> dict[str, dict]:
    return {p["seat"]: p for p in (row(client, headers, seq)["pickup"] or [])}


def claim(client: TestClient, headers: dict, key: str, value: dict,
          channel: str = "room") -> None:
    r = client.put(f"/channels/{channel}/store/{key}", json={"value": value},
                   headers=headers)
    assert r.status_code == 200, r.text


# -- it is the ASKER's instrument and nobody else's -------------------------

def test_the_ladder_is_served_to_the_sender_and_to_nobody_else():
    """It exposes other seats' read receipts. Both client seats consented to
    the SENDER seeing that; nobody consented to it being public."""
    client = make_client()
    alice, bob, carol = (register(client, "alice"), register(client, "bob"),
                         register(client, "carol"))
    make_channel(client, alice, "room", bob, carol)

    q = post(client, alice, body="who takes this?", title="q", status="open",
             to=["bob"])

    assert row(client, alice, q["seq"])["pickup"] is not None
    assert row(client, bob, q["seq"])["pickup"] is None
    assert row(client, carol, q["seq"])["pickup"] is None


def test_a_message_naming_nobody_has_no_ladder():
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    broadcast = post(client, alice, body="news", title="fyi")
    assert row(client, alice, broadcast["seq"])["pickup"] is None


# -- the rungs --------------------------------------------------------------

def test_a_seat_that_has_done_nothing_shows_nothing_not_delivered():
    """agora-wui#90: "`delivered` beside `offline` reads as progress when it
    is the absence of it."

    Satisfied BY CONSTRUCTION rather than by a branch: there is no
    `delivered` rung to suppress. Delivery is true the instant a member's
    message is posted, so it carries no information — serving it would be a
    signal saying more than the fact supports, which is the defect this
    room spent the day removing from three clients. A seat that has done
    nothing observable reads `none`, and the presence beside it is what
    says whether that is worrying."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    q = post(client, alice, body="anyone?", title="q", status="open", to=["bob"])

    got = pickup(client, alice, q["seq"])["bob"]
    assert got["rung"] == "none"
    assert got["claim_key"] is None
    # Whatever presence says, no rung ever reads "delivered".
    assert got["rung"] != "delivered"


def test_reading_the_message_is_a_rung_and_carries_its_timestamp():
    """Age is what makes a rung a decision rather than a state."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    q = post(client, alice, body="anyone?", title="q", status="open", to=["bob"])
    r = client.get(f"/channels/room/messages/{q['id']}", headers=bob)
    assert r.status_code == 200, r.text

    got = pickup(client, alice, q["seq"])["bob"]
    assert got["rung"] == "read"
    assert got["since"] > 0


def test_a_declared_claim_is_the_rung_that_answers_the_operators_question():
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    q = post(client, alice, body="do the thing", title="job", status="open",
             to=["bob"])
    claim(client, bob, "claim:the-thing",
          {"owner": "bob", "status": "open — slicing it",
           "source_message_id": q["id"]})

    got = pickup(client, alice, q["seq"])["bob"]
    assert got["rung"] == "claimed"
    assert got["claim_key"] == "claim:the-thing"
    assert got["claim_state"] == "open"


def test_the_claim_state_word_is_re_read_not_remembered():
    """agora-tui#81: never a claim plus an age for the client to interpret.
    `parked` is a declared state a count-up rail would render as neglect."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    q = post(client, alice, body="do the thing", title="job", status="open",
             to=["bob"])
    claim(client, bob, "claim:the-thing",
          {"owner": "bob", "status": "open", "source_message_id": q["id"]})
    assert pickup(client, alice, q["seq"])["bob"]["claim_state"] == "open"

    # The hub refuses a park that does not say what it needs, so a real
    # parked row carries blocked_on/needs — which is the shape a client will
    # actually meet.
    claim(client, bob, "claim:the-thing",
          {"owner": "bob", "status": "parked — waiting on the vendor",
           "source_message_id": q["id"], "blocked_on": "external",
           "needs": "the vendor's API key"})
    assert pickup(client, alice, q["seq"])["bob"]["claim_state"] == "parked"


def test_a_decline_is_its_own_terminal_rung_and_not_a_seat_that_went_quiet():
    """agora-tui's CONDITION for consenting to sender-visible receipts: a
    ladder ending at `replied` leaves a declined ask sitting at `read`
    forever, so the seat that took the legitimate exit renders exactly like
    the seat that ignored you."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    q = post(client, alice, body="q", title="q", status="open",
             asks=[{"id": "1", "text": "take this?", "to": ["bob"]}])
    post(client, bob, body="not mine", title="declining", status="reply",
         reply_to=q["id"], answers=["1"], declines=["1"])

    got = pickup(client, alice, q["seq"])["bob"]
    assert got["rung"] == "declined"
    assert got["still_owes"] is False


# -- still_owes comes from the discharge call, never from "a reply arrived" --

def test_a_seat_can_have_replied_and_the_ask_still_be_open_on_the_other_seat():
    """THE #86 case. agora-tui answered all three asks, saw them still
    pending, and reported the ledger as broken — it was waiting on the
    co-addressee. Two rungs side by side say that; one verdict cannot."""
    client = make_client()
    alice, bob, carol = (register(client, "alice"), register(client, "bob"),
                         register(client, "carol"))
    make_channel(client, alice, "room", bob, carol)

    q = post(client, alice, body="both of you", title="q", status="open",
             asks=[{"id": "1", "text": "which?", "to": ["bob", "carol"]}])
    post(client, bob, body="mine is done", title="answer", status="reply",
         reply_to=q["id"], answers=["1"])

    got = pickup(client, alice, q["seq"])
    assert got["bob"]["rung"] == "replied"       # the affirmative act, visible
    assert got["carol"]["rung"] == "none"        # the seat actually holding it
    # The ask itself is undischarged — both facts true at once, which is the
    # thing a single verdict would have to get wrong for one of them.
    assert row(client, alice, q["seq"])["pending_asks"] == ["1"]

    # FLIPPED 2026-08-22, as this comment said it would be. It used to assert
    # `bob -> True`, pinning the defect: `pending_addressees` named every seat
    # on a pending ask regardless of who had answered it, contradicting its
    # own docstring. Left as a deliberate tripwire on the 19th and fixed only
    # after agora-tui hit it in the wild (agora-and-wui#190) — a row escalating
    # against a seat that had answered in full.
    #
    # The two facts now stand apart, which is the whole point of a per-seat
    # ladder: the ASK is open (carol has not answered), and BOB DOES NOT OWE IT.
    assert got["bob"]["still_owes"] is False     # answered his share, released
    assert got["carol"]["still_owes"] is True    # the seat actually holding it


def test_a_bare_reply_to_a_peers_ask_less_open_does_not_release_the_seat():
    """The exit both clients described wrongly (#96): "on it" settles
    nothing; a claim row citing the message is what moves it."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    q = post(client, alice, body="do the thing", title="job", status="open",
             to=["bob"])
    post(client, bob, body="on it", title="ack", status="reply",
         reply_to=q["id"])

    got = pickup(client, alice, q["seq"])["bob"]
    assert got["rung"] == "replied"
    assert got["still_owes"] is True        # a reply is not a discharge here


# -- the declared link is strict on purpose ---------------------------------

def test_a_claim_whose_source_is_prose_does_not_link():
    """`_claims_touching` matches prose by substring for a report a human
    reads. This is a live instrument the operator will trust, and both
    clients refused the substring join: it will eventually attribute one
    seat's claim to another seat's message, silently. A row that does not
    DECLARE its source shows as unclaimed — the honest cost."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    q = post(client, alice, body="do the thing", title="job", status="open",
             to=["bob"])
    claim(client, bob, "claim:vague",
          {"owner": "bob", "status": "open",
           "source_message_id": f"room#{q['seq']} / and also some other thing"})

    assert pickup(client, alice, q["seq"])["bob"]["rung"] != "claimed"


def test_a_claim_citing_channel_seq_links_exactly_like_one_citing_the_id():
    """Both forms are declarations — every doc and digest cites `#N`."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    q = post(client, alice, body="do the thing", title="job", status="open",
             to=["bob"])
    claim(client, bob, "claim:by-seq",
          {"owner": "bob", "status": "open",
           "source_message_id": f"room#{q['seq']}"})

    got = pickup(client, alice, q["seq"])["bob"]
    assert got["rung"] == "claimed"
    assert got["claim_key"] == "claim:by-seq"


def test_another_seats_claim_on_the_same_message_is_not_attributed_to_you():
    """The precise failure both clients feared, asserted rather than argued."""
    client = make_client()
    alice, bob, carol = (register(client, "alice"), register(client, "bob"),
                         register(client, "carol"))
    make_channel(client, alice, "room", bob, carol)

    q = post(client, alice, body="either of you", title="job", status="open",
             to=["bob", "carol"])
    claim(client, carol, "claim:carols",
          {"owner": "carol", "status": "open", "source_message_id": q["id"]})

    got = pickup(client, alice, q["seq"])
    assert got["carol"]["rung"] == "claimed"
    assert got["carol"]["claim_key"] == "claim:carols"
    assert got["bob"]["rung"] != "claimed"
    assert got["bob"]["claim_key"] is None


# -- the push, which is the half the operator actually asked for ------------
#
# "otherwise we just type a message and wait to see if anyone is gonna
# answer" describes a surface you have to go back and look at. The row alone
# would have left him there.


def drain(ws, client: TestClient, headers: dict) -> list[dict]:
    """Every frame queued on this socket right now, bounded by a round trip.

    A ping is sent and frames are read until the pong: that makes "no frame
    arrived" a bounded assertion rather than a timeout, which is what the
    sender-only tests need to prove."""
    ws.send_json({"type": "ping"})
    frames = []
    while True:
        frame = ws.receive_json()
        if frame.get("type") == "pong":
            return frames
        frames.append(frame)


def pickups(frames: list[dict]) -> list[dict]:
    return [f for f in frames if f.get("type") == "pickup"]


def test_reading_the_message_pushes_the_ladder_to_the_asker():
    """The rung moves and the asker HEARS it, without asking again."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)
    q = post(client, alice, body="do the thing", title="job", status="open",
             to=["bob"])

    with client.websocket_connect(f"/ws?token={KEYS['alice']}") as ws:
        client.get(f"/channels/room/messages/{q['id']}", headers=bob)
        got = pickups(drain(ws, client, alice))

    assert len(got) == 1
    assert got[0]["message_id"] == q["id"]
    assert got[0]["seq"] == q["seq"]
    assert got[0]["channel"] == "room"
    assert [(p["seat"], p["rung"]) for p in got[0]["pickup"]] == [("bob", "read")]


def test_a_claim_pushes_and_so_does_the_state_word_moving_afterwards():
    """A ladder that announced the claim and then went quiet would leave the
    asker watching a row that says `claimed` forever — `parked` and `done`
    are the states he most needs to hear about."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)
    q = post(client, alice, body="do the thing", title="job", status="open",
             to=["bob"])

    with client.websocket_connect(f"/ws?token={KEYS['alice']}") as ws:
        claim(client, bob, "claim:the-thing",
              {"owner": "bob", "status": "open — slicing it",
               "source_message_id": q["id"]})
        claim(client, bob, "claim:the-thing",
              {"owner": "bob", "status": "parked — waiting on the vendor",
               "source_message_id": q["id"], "blocked_on": "external",
               "needs": "the vendor's API key"})
        got = pickups(drain(ws, client, alice))

    assert [f["pickup"][0]["claim_state"] for f in got] == ["open", "parked"]
    assert all(f["pickup"][0]["rung"] == "claimed" for f in got)


def test_a_reply_pushes_to_the_asker_not_to_the_replier():
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)
    q = post(client, alice, body="q", title="q", status="open",
             asks=[{"id": "1", "text": "take this?", "to": ["bob"]}])

    with client.websocket_connect(f"/ws?token={KEYS['alice']}") as ws:
        post(client, bob, body="not mine", title="no", status="reply",
             reply_to=q["id"], answers=["1"], declines=["1"])
        got = pickups(drain(ws, client, alice))

    assert len(got) == 1
    assert got[0]["pickup"][0]["rung"] == "declined"
    assert got[0]["pickup"][0]["still_owes"] is False


def test_every_push_carries_the_WHOLE_ladder_and_no_delta_form_exists():
    """C3, agora-tui#81, adopted as absolute rather than "for now": a client
    that missed one delta of a count-up stream is not stale, it is
    confidently wrong with no way to find out. So a push about BOB names
    carol too, at whatever rung carol is on."""
    client = make_client()
    alice, bob, carol = (register(client, "alice"), register(client, "bob"),
                         register(client, "carol"))
    make_channel(client, alice, "room", bob, carol)
    q = post(client, alice, body="either of you", title="job", status="open",
             to=["bob", "carol"])

    with client.websocket_connect(f"/ws?token={KEYS['alice']}") as ws:
        client.get(f"/channels/room/messages/{q['id']}", headers=bob)
        got = pickups(drain(ws, client, alice))

    assert len(got) == 1
    seats = {p["seat"]: p["rung"] for p in got[0]["pickup"]}
    assert seats == {"bob": "read", "carol": "none"}
    # There is no field a client could mistake for "just this seat changed":
    # the frame is the state, not the transition.
    assert set(got[0]) == {"type", "channel", "message_id", "seq", "pickup"}


def test_the_push_reaches_the_asker_and_nobody_else():
    """It carries other seats' read receipts. Both client seats consented to
    the SENDER seeing that and nobody consented to it being public — the row
    enforces it, and the socket has to enforce it separately.

    Both peers SUBSCRIBE to the room here, and that is the whole point of
    the test. A connected socket is only fed `agent/<id>` until it asks for
    a channel, so without these subscribe frames the assertion passes just
    as happily against a hub that broadcasts the ladder to the entire room —
    a check whose absent-input case is PASS. Verified by mutation: publish
    to `message.channel` instead of the sender and this test goes red."""
    client = make_client()
    alice, bob, carol = (register(client, "alice"), register(client, "bob"),
                         register(client, "carol"))
    make_channel(client, alice, "room", bob, carol)
    q = post(client, alice, body="both of you", title="job", status="open",
             to=["bob", "carol"])

    with client.websocket_connect(f"/ws?token={KEYS['bob']}") as bob_ws, \
            client.websocket_connect(f"/ws?token={KEYS['carol']}") as carol_ws:
        for ws in (bob_ws, carol_ws):
            ws.send_json({"type": "subscribe", "channels": ["room"],
                          "since": {"room": q["seq"]}})
        client.get(f"/channels/room/messages/{q['id']}", headers=bob)
        assert pickups(drain(bob_ws, client, bob)) == []    # his own receipt
        assert pickups(drain(carol_ws, client, carol)) == []  # a third party


def test_acking_past_a_message_pushes_the_weakest_rung_there_is():
    """`acked` is "they have swept past it and done nothing yet" — the
    operator's complaint stated precisely, and the rung a client must not
    dress up as agreement."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)
    q = post(client, alice, body="do the thing", title="job", status="open",
             to=["bob"])

    with client.websocket_connect(f"/ws?token={KEYS['alice']}") as ws:
        r = client.post("/inbox/ack", json={"cursors": {"room": q["seq"]}},
                        headers=bob)
        assert r.status_code == 200, r.text
        got = pickups(drain(ws, client, alice))

    assert [p["rung"] for f in got for p in f["pickup"]] == ["acked"]


def test_a_broadcast_nobody_is_addressed_on_pushes_nothing():
    """No addressees, no ladder — and therefore no frame. A push per ack per
    fyi would be noise on the one surface that must stay worth watching."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)
    news = post(client, alice, body="news", title="fyi")

    with client.websocket_connect(f"/ws?token={KEYS['alice']}") as ws:
        client.get(f"/channels/room/messages/{news['id']}", headers=bob)
        client.post("/inbox/ack", json={"cursors": {"room": news["seq"]}},
                    headers=bob)
        assert pickups(drain(ws, client, alice)) == []


def test_the_answering_seats_own_inbox_stops_pinning_it_the_wild_case():
    """agora-and-wui#190, from the side that actually cost a turn.

    The pickup row above is what the ASKER sees. What agora-tui hit was the
    other surface: their OWN `check_inbox` kept listing #178 as an ask naming
    them, after they had answered it with `answers=["3"]`, because their
    co-addressee had answered the same ask in prose and the ask stayed open.
    A true-looking overdue that was false, heading for an escalation against
    the one seat that had done the work.

    Asserted on the answering seat's inbox rather than on the asker's row,
    because that is where the damage was and the two are different code paths
    (`envelope_for` -> `AttentionPolicy` vs the pickup join).
    """
    client = make_client()
    alice, bob, carol = (register(client, "alice"), register(client, "bob"),
                         register(client, "carol"))
    make_channel(client, alice, "room", bob, carol)

    q = post(client, alice, body="both of you", title="q", status="open",
             asks=[{"id": "1", "text": "which?", "to": ["bob", "carol"]}])
    post(client, bob, body="mine is done", title="answer", status="reply",
         reply_to=q["id"], answers=["1"])

    def envelope(seat: dict[str, str]) -> dict:
        got = client.get("/inbox", headers=seat).json()
        rows = [e for e in got if e["seq"] == q["seq"]]
        assert rows, "the ask should still be on both inboxes — it is OPEN"
        return rows[0]

    # bob answered: the ask is still open and still visible to him, but it is
    # no longer addressed AT him. Visibility and debt are different things.
    assert envelope(bob)["to_me"] is False
    # carol has not: hers is unchanged, which is what proves the release above
    # is scoped to the SEAT and is not a blanket un-pinning of the ask.
    assert envelope(carol)["to_me"] is True
    # And the ask itself is untouched by either — the release is a pin scope,
    # never a discharge. If this ever goes empty for bob the fix has started
    # answering asks on his behalf, which is a far worse bug than the one it
    # replaced.
    assert envelope(bob)["pending_asks"] == ["1"]
    assert envelope(carol)["pending_asks"] == ["1"]
