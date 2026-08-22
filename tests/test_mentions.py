"""@mention addressing at post time (agora-0105)."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from agora.hub.app import create_app
from agora.mentions import (body_for_mention_scan, parse_mention_candidates,
                            parse_mentions, resolve_mentions)

ADMIN_KEY = "test-admin"


def make_client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0,
                     notify_dir=str(tmp_path / "notify"))
    return TestClient(app)


def register(client: TestClient, agent_id: str, operator: bool = False) -> dict[str, str]:
    r = client.post("/agents", json={"id": agent_id, "operator": operator},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_channel(client: TestClient, owner: dict, name: str, *members: dict) -> None:
    client.post("/channels", json={"name": name}, headers=owner)
    for member in members:
        invite = client.post(f"/channels/{name}/invites", json={},
                             headers=owner).json()["invite_token"]
        client.post(f"/channels/{name}/join", json={"invite_token": invite},
                    headers=member)


def test_parse_mentions_skips_nonce_fenced_quotes():
    body = (
        "Please comply \u27e6AGORA:abc:quote\u27e7\n"
        "laurent RULED @ghost must stop\u27e6/AGORA:abc\u27e7 "
        "and work with @agora now."
    )
    assert parse_mentions(body) == ["agora"]
    assert "@ghost" not in body_for_mention_scan(body)


def notify_lines(tmp_path: Path, agent_id: str) -> list[dict]:
    p = tmp_path / "notify" / f"{agent_id}-inbox.log"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line]


# -- vfs references vs mentions: seat-identity precedence (operator ruling) ----
# @folder/file.md and @channel:folder/file.md are vfs references, but a token
# that exactly matches a REGISTERED seat id is a mention, always. The parser
# reports shape; the hub decides against the registry.


def test_parse_mention_candidates_reports_shape():
    assert parse_mention_candidates(
        "see @plans/q3.md and @laurent: hi and @flow") == [
        ("plans", True), ("laurent", True), ("flow", False)]
    # One plain occurrence anywhere clears the flag: the author demonstrably
    # meant the seat.
    assert parse_mention_candidates("@plans/q3.md then @plans") == [
        ("plans", False)]


def test_parse_mentions_is_the_registry_free_safe_subset():
    # Text-only callers have no registry, so path-like tokens are dropped:
    # a vfs reference can never mint a seat out of text alone.
    assert parse_mentions("roadmap lives in @plans/q3.md now") == []
    assert parse_mentions("logo at @agora-wui-work:assets/logo.png") == []


def test_mention_adjacent_to_vfs_reference_still_counts():
    assert parse_mentions("@laurent see @plans/q3.md") == ["laurent"]


def test_punctuation_after_mention_does_not_eat_it():
    # Only '/' and ':' mark the path shape.
    assert parse_mentions("ship it @seat, thanks") == ["seat"]
    assert parse_mentions("ship it @seat") == ["seat"]
    # A sentence-final dot is PUNCTUATION, not part of the id. Ids end in
    # [a-z0-9] (`agent_id._AGENT_ID_RE`), so `seat.` can never be a seat —
    # and reading it as one failed twice over: the member named was not
    # addressed, and the hub warned the author about a non-member that does
    # not exist. Measured on a live notify file (commons#54): `@tui.` was
    # one of five false mention notices out of seven.
    assert parse_mentions("ship it @seat. next") == ["seat"]
    assert parse_mentions("cc @seat!") == ["seat"]


def test_quoted_block_exclusion_still_holds_around_vfs_refs():
    body = (
        "⟦AGORA:xy:quote⟧@ghost said read @plans/q3.md"
        "⟦/AGORA:xy⟧ @agora please check @plans/q3.md"
    )
    assert parse_mentions(body) == ["agora"]


def test_resolve_mentions_seat_identity_precedence():
    body = "see @plans/q3.md and @laurent: hi and @ghost"
    # No seat registered: path-like tokens vanish silently; plain ones
    # keep their long-standing outsider warning.
    assert resolve_mentions(body, set(), set()) == ([], ["ghost"])
    # A registered seat wins over the path reading, in or out of the room.
    assert resolve_mentions(body, {"laurent", "plans"},
                            {"laurent", "plans"}) == (
        ["plans", "laurent"], ["ghost"])
    assert resolve_mentions(body, {"laurent"}, {"laurent", "plans"}) == (
        ["laurent"], ["plans", "ghost"])


def test_colon_or_slash_after_registered_handle_still_obliges(tmp_path):
    # The regression the operator caught: '@laurent: please review' is
    # colon-after-handle prose, not a vfs reference — laurent is registered,
    # so the seat wins.
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    laurent = register(client, "laurent")
    make_channel(client, op, "room", laurent)

    for body in ("@laurent: please review this", "@laurent/ done"):
        r = client.post("/channels/room/messages", json={
            "body": body, "title": "review", "status": "open"}, headers=op)
        assert r.status_code == 200, r.text
        msg = r.json()
        assert msg["to"] == ["laurent"], body
        owed = client.get("/owed", headers=laurent).json()
        assert any(row["id"] == msg["id"] for row in owed["to_answer"]), body


def test_vfs_ref_with_no_registered_seat_is_silent(tmp_path):
    # No seat 'plans' or 'chan' exists: nothing is mentioned, nothing is
    # obliged, and the sender gets NO outsider doorbell — path tokens must
    # not generate 'names no seat' noise.
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    other = register(client, "other")
    make_channel(client, op, "room", other)

    r = client.post("/channels/room/messages", json={
        "body": "roadmap in @plans/q3.md, logo at @chan:assets/x.png",
        "title": "directive", "status": "open"}, headers=op)
    assert r.status_code == 200, r.text
    assert not r.json()["to"]
    warned = [line for line in notify_lines(tmp_path, "op")
              if "you wrote" in str(line.get("preview", ""))]
    assert warned == []


def test_registered_seat_wins_over_vfs_reference(tmp_path):
    # Seat-identity precedence, pinned as DELIBERATE: a path-shaped token
    # whose id exactly matches a registered seat is a mention — so a channel
    # named like a seat cannot be @-referenced cross-channel; rename the
    # channel or reference the file from inside it.
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    plans = register(client, "plans")
    make_channel(client, op, "room", plans)

    r = client.post("/channels/room/messages", json={
        "body": "the roadmap is in @plans/q3.md — read before Friday",
        "title": "directive", "status": "open"}, headers=op)
    assert r.status_code == 200, r.text
    msg = r.json()
    assert msg["to"] == ["plans"]
    owed = client.get("/owed", headers=plans).json()
    assert any(row["id"] == msg["id"] for row in owed["to_answer"])


def test_registered_nonmember_in_path_shape_still_warns(tmp_path):
    # Outsider-mention warnings must keep firing for REAL seats, even when
    # the token is path-shaped: the registry, not room membership, decides.
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    other = register(client, "other")
    register(client, "plans")            # registered, NOT in the room
    make_channel(client, op, "room", other)

    r = client.post("/channels/room/messages", json={
        "body": "see @plans/q3.md", "title": "fyi", "status": "fyi"},
        headers=op)
    assert r.status_code == 200, r.text
    assert not r.json()["to"]
    warned = [line for line in notify_lines(tmp_path, "op")
              if "you wrote @plans" in str(line.get("preview", ""))]
    assert warned


def test_operator_ask_text_mention_populates_per_ask_to(tmp_path):
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    agora = register(client, "agora")
    make_channel(client, op, "room", agora)

    r = client.post("/channels/room/messages", json={
        "body": "please answer",
        "title": "q",
        "status": "open",
        "asks": [{"id": "1", "text": "@agora confirm the plan?"}],
    }, headers=op)
    assert r.status_code == 200, r.text
    msg = r.json()
    assert msg["data"]["asks"][0]["to"] == ["agora"]

    owed = client.get("/owed", headers=agora).json()
    assert any(row["asks_naming_you"] == ["1"] for row in owed["to_answer"])


def test_peer_ask_text_mention_populates_per_ask_to(tmp_path):
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    peer = register(client, "peer")
    agora = register(client, "agora")
    make_channel(client, op, "room", peer, agora)

    r = client.post("/channels/room/messages", json={
        "body": "please answer",
        "title": "q",
        "status": "open",
        "asks": [{"id": "1", "text": "@agora confirm the plan?"}],
    }, headers=peer)
    assert r.status_code == 200, r.text
    msg = r.json()
    assert msg["data"]["asks"][0]["to"] == ["agora"]

    owed = client.get("/owed", headers=agora).json()
    assert any(row["asks_naming_you"] == ["1"] for row in owed["to_answer"])


def test_operator_body_mention_populates_to(tmp_path):
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    agora = register(client, "agora")
    flow = register(client, "flow")
    make_channel(client, op, "room", agora, flow)

    r = client.post("/channels/room/messages", json={
        "body": "work with @agora and @flow on this",
        "title": "directive",
        "status": "open",
    }, headers=op)
    assert r.status_code == 200, r.text
    msg = r.json()
    assert sorted(msg["to"]) == ["agora", "flow"]

    owed = client.get("/owed", headers=agora).json()
    assert any(row["id"] == msg["id"] for row in owed["to_answer"])


def test_peer_body_mention_populates_to_without_owing_reply(tmp_path):
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    peer = register(client, "peer")
    target = register(client, "agora")
    make_channel(client, op, "room", peer, target)

    r = client.post("/channels/room/messages", json={
        "body": "thread update for @agora — please review",
        "title": "fyi",
        "status": "fyi",
    }, headers=peer)
    assert r.status_code == 200
    msg = r.json()
    assert msg["to"] == ["agora"]

    owed = client.get("/owed", headers=target).json()
    assert not any(row["id"] == msg["id"] for row in owed.get("to_answer", []))


def test_code_is_quoted_text_never_speech():
    """commons#54/#56: a bug report listing false mentions inside a fenced
    table ADDRESSED the human named in that table, and the hub then fired a
    routing nudge for a three-seat room nobody asked for. Code is evidence
    an author is showing you, never an act of addressing."""
    fenced = "results:\n```\n| webos#33 | @laurent | TRUE |\n```\nthat is all"
    assert parse_mentions(fenced) == []
    assert parse_mentions("the literal string `@laurent` in prose") == []
    # ...and the fence must not deafen the free text around it.
    assert parse_mentions(fenced + " @laurent see above") == ["laurent"]


def test_a_mention_starts_a_word_so_citations_are_not_seats():
    """The skill teaches `key@version` store citations, so the hub was
    minting a mention of `1` every time an agent followed its own
    documented convention (three in one day, commons#54). Same rule kills
    email addresses, which are the other half of that finding."""
    assert parse_mentions("per decision:notice-delivery-state@1") == []
    assert parse_mentions("audit/annotations.md@1 is the file") == []
    assert parse_mentions("mail bob@agora.local about it") == []
    # The word-start rule must not cost a real mention any ground.
    assert parse_mentions("(@laurent) and [@seat]") == ["laurent", "seat"]
    assert parse_mentions("line one\n@laurent") == ["laurent"]
