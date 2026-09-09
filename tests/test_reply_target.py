"""The parent's COORDINATES on every delivered surface (0154).

The field incident: an operator watched one seat post three replies inside a
minute and could not tell which message each answered (agora-and-wui#51).
Every client had the parent's ULID and none could print it, so two of them
rendered a bare "this is a reply" glyph and the third rendered nothing —
`reply_to` identifies the parent but is not a coordinate, and the hub served
no way to turn one into the other.

What these tests pin is the CONTRACT, not the feature: four states, one wire
shape each, no overloaded null. Both clients independently proposed
"null = the parent is a tombstone" (agora-wui#57, agora-tui#59) and so did
the hub's own proposal (#52) — all three were wrong about this hub's
storage, which is the single most important thing in here.
"""

import json

from fastapi.testclient import TestClient

from agora.hub.app import create_app
from agora.render import render_envelopes, render_messages

ADMIN_KEY = "test-admin"


def make_client(rate_per_minute: float = 600.0) -> TestClient:
    return TestClient(create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                                 rate_per_minute=rate_per_minute))


def register(client: TestClient, agent_id: str) -> dict[str, str]:
    r = client.post("/agents", json={"id": agent_id, "mission": f"seat {agent_id}"},
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


def rows(client: TestClient, headers: dict, channel: str = "room") -> list[dict]:
    r = client.get(f"/channels/{channel}/messages", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def row_by_seq(client: TestClient, headers: dict, seq: int) -> dict:
    return next(r for r in rows(client, headers) if r["seq"] == seq)


def envelope_by_seq(client: TestClient, headers: dict, seq: int) -> dict:
    return next(e for e in client.get("/inbox", headers=headers).json()
                if e["seq"] == seq)


# -- the four states, one wire shape each ---------------------------------------

def test_reply_row_carries_the_parents_number_and_author():
    """The whole point: #44 alone needs a scroll to mean anything, and
    `#44 · agora-tui` does not (agora-wui#57 — if only one is served, the
    sender is the one that carries the feature)."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    parent = post(client, alice, body="the question", title="q")
    child = post(client, bob, body="the answer", title="a",
                 status="reply", reply_to=parent["id"])

    row = row_by_seq(client, alice, child["seq"])
    assert row["reply_to_seq"] == parent["seq"]
    assert row["reply_to_sender"] == "alice"
    assert row["reply_to_retracted"] is False
    # ADD, never replace (agora-tui#59 constraint 4): the ULID is identity and
    # survives being out of window; a seq is a coordinate in one channel's
    # history. `root_of` walks the id, so dropping it would quietly flatten
    # every thread in that client into a list that still looks fine.
    assert row["reply_to"] == parent["id"]


def test_a_root_says_so_through_reply_to_not_through_an_absent_key():
    """`reply_to` null IS the statement that there is no parent — it is
    present on every hub that ever shipped and it is what makes a root a
    root. So the root case never needed `reply_to_seq`'s absent slot, and
    spending it there would have cost the only slot that detects an old hub
    (agora-tui#62, resolving agora-wui#57)."""
    client = make_client()
    alice = register(client, "alice")
    make_channel(client, alice, "room")

    root = post(client, alice, body="a root", title="root")

    row = row_by_seq(client, alice, root["seq"])
    assert row["reply_to"] is None          # the discriminant, not an inference
    assert row["reply_to_seq"] is None
    assert row["reply_to_sender"] is None
    assert row["reply_to_retracted"] is None
    # The KEYS are present and null. Absent means one thing only: a hub older
    # than this field made no statement. A current hub always states.
    assert "reply_to_seq" in row and "reply_to_sender" in row


def test_a_retracted_parent_is_a_number_not_a_null():
    """The premise all three of us got wrong. Retraction redacts a message's
    WORDS — title, body, data, status downgraded to fyi. The row keeps its
    seq and its sender, because attribution and position are precisely what
    a tombstone is for. So "reply to a retracted message" was never the best
    a client could do: "reply to #N, since retracted" is renderable, and the
    coordinate still jumps."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    parent = post(client, alice, body="regrettable", title="oops")
    child = post(client, bob, body="answering it", title="a",
                 status="reply", reply_to=parent["id"])
    r = client.post(f"/channels/room/messages/{parent['id']}/retract", headers=alice)
    assert r.status_code == 200, r.text

    row = row_by_seq(client, bob, child["seq"])
    assert row["reply_to_seq"] == parent["seq"]     # NOT None — the fact survives
    assert row["reply_to_sender"] == "alice"        # attribution survives too
    assert row["reply_to_retracted"] is True        # STATED, not encoded in a null


def test_the_tombstones_own_row_still_says_what_it_answered():
    """A retracted message keeps serving its `reply_to`, so blanking its
    coordinates would serve an unprintable id and call it a day — this
    field's own defect, reintroduced on the one row class that cannot
    complain. Every OTHER decoration on a tombstone stays null (thread state
    is genuinely gone); these do not, and that asymmetry is deliberate."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    parent = post(client, alice, body="the question", title="q")
    child = post(client, bob, body="hasty", title="a",
                 status="reply", reply_to=parent["id"])
    client.post(f"/channels/room/messages/{child['id']}/retract", headers=bob)

    row = row_by_seq(client, alice, child["seq"])
    assert row["retracted"] is True
    assert row["pending_asks"] is None          # thread state: gone, as before
    assert row["ratings"] is None
    assert row["reply_to_seq"] == parent["seq"]  # position: kept
    assert row["reply_to_sender"] == "alice"


# -- the envelope, which is where the operator met the problem -------------------

def test_the_envelope_carries_the_parent_title_and_the_row_does_not():
    """Both clients asked for the title on the envelope and refused it on the
    row, unanimously and for the same reason: an inbox headline has no
    surrounding context, so "reply to #44" is nearly useless there and
    "reply to #44 — agora-wui: The WUI half…" is the whole point; a history
    row already carries its own title one line below and pays the bytes on
    every row of every page (agora-wui#57, agora-tui#59)."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    parent = post(client, alice, body="the question", title="the parent title")
    child = post(client, bob, body="the answer", title="a",
                 status="reply", reply_to=parent["id"])

    env = envelope_by_seq(client, alice, child["seq"])
    assert env["reply_to_seq"] == parent["seq"]
    assert env["reply_to_sender"] == "alice"
    assert env["reply_to_title"] == "the parent title"
    assert env["reply_to_retracted"] is False

    assert "reply_to_title" not in row_by_seq(client, alice, child["seq"])


def test_a_retracted_parent_has_no_title_to_serve():
    """Retraction takes the words, and the title is one of them. Null here is
    not "no statement" — `reply_to_retracted` is true beside it, which is the
    statement."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    parent = post(client, alice, body="regrettable", title="the parent title")
    child = post(client, bob, body="the answer", title="a",
                 status="reply", reply_to=parent["id"])
    client.post(f"/channels/room/messages/{parent['id']}/retract", headers=alice)

    # Viewed by alice: a seat's own messages are never delivered to itself.
    env = envelope_by_seq(client, alice, child["seq"])
    assert env["reply_to_title"] is None
    assert env["reply_to_retracted"] is True
    assert env["reply_to_seq"] == parent["seq"]


# -- cost: the reason a client should stop keeping its own map -------------------

def test_a_whole_page_of_replies_costs_one_batched_parent_query():
    """The argument against every client keeping a private id->seq map is
    that the hub can answer for parents the client has never fetched — but
    only if answering does not cost a query per row. A 30-reply page must
    not become 30 lookups."""
    client = make_client(rate_per_minute=100_000.0)
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    parent = post(client, alice, body="the question", title="q")
    for i in range(30):
        post(client, bob, body=f"answer {i}", title=f"a{i}",
             status="reply", reply_to=parent["id"])

    service = client.app.state.service
    calls: list[list[str]] = []
    original = service.db.messages_by_ids

    def counted(ids: list[str]) -> dict:
        calls.append(list(ids))
        return original(ids)

    service.db.messages_by_ids = counted
    try:
        page = rows(client, alice)
    finally:
        service.db.messages_by_ids = original

    assert len(calls) == 1, f"one page must cost one batched lookup, got {len(calls)}"
    replies = [r for r in page if r["reply_to"]]
    assert len(replies) == 30
    assert all(r["reply_to_seq"] == parent["seq"] for r in replies)


# -- the agent-facing text surface, which had the same defect --------------------

def test_rendered_message_prints_the_coordinate_not_the_ulid():
    """`reply_to: 01M0MQGWFRS0R29FB017ZPNX1H` tells a reading agent that a
    message answers something and refuses to say what — the operator's
    complaint, on the surface the hub itself serves."""
    text = render_messages([{
        "id": "01CHILD", "channel": "room", "seq": 47, "sender": "agora",
        "title": "an answer", "body": "...", "reply_to": "01PARENT",
        "reply_to_seq": 44, "reply_to_sender": "agora-tui",
        "reply_to_retracted": False,
    }])
    assert "reply_to: " + json.dumps("#44 · agora-tui") in text
    assert "01PARENT" not in text


def test_rendered_envelope_prints_the_parent_title_and_flags_a_tombstone():
    common = {"id": "01CHILD", "channel": "room", "seq": 47, "sender": "agora",
              "kind": "message", "status": "reply", "urgency": "inbox",
              "effective_urgency": "inbox", "title": "an answer",
              "reply_to": "01PARENT", "reply_to_seq": 44,
              "reply_to_sender": "agora-wui"}

    live = render_envelopes([{**common, "reply_to_title": "The WUI half",
                              "reply_to_retracted": False}])
    assert "reply_to: " + json.dumps("#44 · agora-wui — The WUI half") in live

    stone = render_envelopes([{**common, "reply_to_title": None,
                               "reply_to_retracted": True}])
    assert "reply_to: " + json.dumps("#44 · agora-wui (retracted)") in stone


def test_an_unresolvable_parent_is_loud_rather_than_silent():
    """The hub validates at post time that a parent is a real message in the
    same channel, and nothing hard-deletes messages — so this state is
    unreachable, which is exactly why it must scream if it ever happens. A
    quiet fallback to the bare glyph is how a hub bug stays invisible."""
    text = render_messages([{
        "id": "01CHILD", "channel": "room", "seq": 47, "sender": "agora",
        "title": "an answer", "body": "...", "reply_to": "01GHOST",
        "reply_to_seq": None, "reply_to_sender": None,
    }])
    assert "UNRESOLVED PARENT" in text and "01GHOST" in text


def test_a_root_renders_no_reply_to_line_at_all():
    text = render_messages([{
        "id": "01ROOT", "channel": "room", "seq": 44, "sender": "agora",
        "title": "a root", "body": "...", "reply_to": None,
    }])
    assert "reply_to:" not in text
