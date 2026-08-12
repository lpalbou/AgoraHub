"""Routing reform mechanics (agora-0133/0135, operator order dm#177).

What we want, as behavior:
- The narrowed wake rule: an ADDRESSED open/blocked wakes the seats it
  names, nobody else — while an addresseeless open stays room-wide (the
  2026-07-14 falsification: a broadcast ask that wakes nobody is dead air).
- The broadcast-obligation notice: a sender who obliges a whole big room
  gets told so, ephemerally — no channel traffic, nobody shamed, no block.
- The fork nudge: a commons-scale thread where 3+ seats are building gets
  ONE in-thread pointer to `agora group`, and never a second.
- Groups arrive with their lifecycle charter already written.
- The noise report prices the old vs new wake rule from live data.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from agora.hub.app import create_app
from agora.hub.service import CHANNEL_META_KEY
from agora.listen import qualifies

ADMIN_KEY = "test-admin"


def make_client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0,
                     notify_dir=str(tmp_path / "notify"))
    return TestClient(app)


def register(client: TestClient, agent_id: str,
             operator: bool = False) -> dict[str, str]:
    r = client.post("/agents", json={"id": agent_id, "operator": operator},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_public_channel(client: TestClient, owner: dict, name: str,
                        *members: dict) -> None:
    client.post("/channels", json={"name": name, "private": False},
                headers=owner)
    for member in (owner, *members):
        client.post(f"/channels/{name}/join", json={}, headers=member)


def set_noticeboard(client: TestClient, owner: dict, name: str) -> None:
    assert client.put(f"/channels/{name}/store/{CHANNEL_META_KEY}",
                      json={"value": {"traffic_policy": "noticeboard"}},
                      headers=owner).status_code == 200


# -- the narrowed listener rule (pure function) ------------------------------

def _event(status: str = "open", flags: str = "", sender: str = "peer") -> dict:
    return {"sender": sender, "status": status, "flags": flags}


def test_addressed_open_wakes_only_the_named_seats():
    # Named seat: woken. Bystander: not. Addresseeless: everyone still wakes.
    named = _event(flags="addressed,to-me,open")
    bystander = _event(flags="addressed,open")
    broadcast = _event(flags="open")
    assert qualifies(named, "me", important_only=True)
    assert not qualifies(bystander, "me", important_only=True)
    assert qualifies(broadcast, "me", important_only=True)


def test_narrowing_never_silences_escalation_or_criticals():
    # A rotting addressed debt must still re-ring bystander stewards? No —
    # escalation/critical flags carry their own wake authority and survive
    # the narrowing untouched.
    escalated = _event(flags="addressed,escalated,open")
    critical = _event(flags="addressed,critical,open")
    assert qualifies(escalated, "me", important_only=True)
    assert qualifies(critical, "me", important_only=True)


def test_old_hubs_without_the_flag_keep_room_wide_wakes():
    # Degradation direction is status quo noise, never deafness.
    legacy = _event(flags="open")   # no `addressed` flag served
    assert qualifies(legacy, "me", important_only=True)


def test_notice_is_optional_metadata_never_a_licence_to_speak(tmp_path):
    """A typed notice buys IDEMPOTENCY, never permission.

    0.12.55 made it permission: on a noticeboard channel an agent could not
    open a question, report a problem, or say `blocked` — only the operator
    could speak, and only a formal vote woke the room. That inverted the
    hub's standing principle (light safeguards, never silent, never blocking)
    on the one room built for open dialogue. These are the shapes that must
    always be deliverable.
    """
    client = make_client(tmp_path)
    alice = register(client, "alice")
    bob = register(client, "bob")
    make_public_channel(client, alice, "commons", bob)
    # Make it a board explicitly — even then, speech is never gated. A traffic
    # policy is an OPERATOR decision (hub-created commons has no owner row).
    operator = register(client, "laurent", operator=True)
    client.post("/channels/commons/join", json={}, headers=operator)
    assert client.put(f"/channels/commons/store/{CHANNEL_META_KEY}",
                      json={"value": {"traffic_policy": "noticeboard"}},
                      headers=operator).status_code == 200

    for body, extra in (
        ("hey team, anyone seen the flaky test?", {"status": "fyi"}),
        ("seam v2 breaks v1 writes — how should we sequence it?",
         {"status": "open"}),
        ("I'm stuck on the airelays key", {"status": "blocked"}),
        ("problem: the RC has a wake regression",
         {"status": "open", "notice": {"kind": "problem", "key": "wake-reg"}}),
    ):
        posted = client.post("/channels/commons/messages",
                             json={"body": body, **extra}, headers=bob)
        assert posted.status_code == 200, (body, posted.text)

    payload = {"body": "release is available", "status": "fyi",
               "notice": {"kind": "delivery", "key": "agora-0.13.0"}}
    first = client.post("/channels/commons/messages", json=payload, headers=bob)
    assert first.status_code == 200
    # Same sender, same key = a true retry, still deduped.
    duplicate = client.post("/channels/commons/messages", json=payload, headers=bob)
    assert duplicate.status_code == 409
    assert "duplicate notice" in duplicate.json()["detail"]

    # A DIFFERENT seat reusing a natural key is NOT silenced: event keys
    # ("week-30", "ci-red", "release") collide trivially, and refusing the
    # second seat destroyed its words to protect a cosmetic invariant.
    cross_sender = client.post("/channels/commons/messages", json=payload,
                               headers=alice)
    assert cross_sender.status_code == 200, cross_sender.text

    # Every member may add a substantive answer/update to a commons thread.
    reply = client.post("/channels/commons/messages",
                        json={"body": "gateway verification is green",
                              "status": "reply",
                              "reply_to": first.json()["id"]}, headers=alice)
    assert reply.status_code == 200


def test_noticeboard_supports_all_public_notice_categories(tmp_path):
    client = make_client(tmp_path)
    alice = register(client, "alice")
    bob = register(client, "bob")
    make_public_channel(client, alice, "commons", bob)

    for kind in ("job", "announcement", "problem", "resolution",
                 "consensus", "milestone", "delivery"):
        posted = client.post(
            "/channels/commons/messages",
            json={"body": f"{kind} update", "status": "fyi",
                  "notice": {"kind": kind, "key": f"public-{kind}-1"}},
            headers=bob,
        )
        assert posted.status_code == 200, (kind, posted.text)


def test_fresh_hub_commons_is_an_open_floor_not_a_board(tmp_path):
    """commons exists on a fresh hub, and carries NO traffic_policy.

    The hub used to stamp `noticeboard` on it at boot. Because the room is
    hub-created it also has no owner row, and channel metadata was
    owner-writable only — so the policy was unwritable by ANYONE on every
    fresh deployment, and commons was permanently un-openable.
    """
    client = make_client(tmp_path)
    service = client.app.state.service
    channel = service.db.get_channel("commons")
    assert channel is not None
    assert channel.private is False
    meta = service.db.store_get("commons", CHANNEL_META_KEY)
    assert meta is None or "traffic_policy" not in (meta.value or {})

    # And an ordinary member can just talk there.
    alice = register(client, "alice")
    client.post("/channels/commons/join", json={}, headers=alice)
    spoken = client.post("/channels/commons/messages",
                         json={"body": "found a bug, who wants to plan it?",
                               "status": "open"}, headers=alice)
    assert spoken.status_code == 200, spoken.text


def test_operator_can_always_write_channel_metadata(tmp_path):
    """An ownerless hub-created room must not lock the human out."""
    client = make_client(tmp_path)
    operator = register(client, "laurent", operator=True)
    client.post("/channels/commons/join", json={}, headers=operator)
    wrote = client.put(f"/channels/commons/store/{CHANNEL_META_KEY}",
                       json={"value": {"traffic_policy": "noticeboard"}},
                       headers=operator)
    assert wrote.status_code == 200, wrote.text


def test_noticeboard_votes_must_be_canonical(tmp_path):
    """The vote carve-out takes only real votes (storm review): a bare
    {"tag": ...} dict must not mint unaddressed open roots, and a
    retracted notice releases its idempotency key for a corrected repost."""
    client = make_client(tmp_path)
    alice = register(client, "alice")
    bob = register(client, "bob")
    make_public_channel(client, alice, "commons", bob)
    # Vote WELL-FORMEDNESS is checked on board channels (where a vote root is
    # the obliging shape). It is payload validation, never a speech gate.
    operator = register(client, "laurent", operator=True)
    client.post("/channels/commons/join", json={}, headers=operator)
    assert client.put(f"/channels/commons/store/{CHANNEL_META_KEY}",
                      json={"value": {"traffic_policy": "noticeboard"}},
                      headers=operator).status_code == 200

    fake = client.post("/channels/commons/messages",
                       json={"body": "spam", "status": "open",
                             "data": {"vote": {"tag": "x1"}}}, headers=bob)
    assert fake.status_code == 400
    assert "canonical" in fake.json()["detail"]

    from agora.vote import build_vote_post
    payload = build_vote_post("bob", "pick a queue backend",
                              ["redis", "sqlite"])

    # Degenerate shapes die: duplicate options, absent/zero/boolean deadline.
    # (Infinity/NaN never reach the vote check — the strict-JSON data gate
    # refuses them wire-level.)
    for bad_vote in (
        {**payload["data"]["vote"], "options": ["redis", "REDIS"]},
        {**payload["data"]["vote"], "closes_at": 0},
        {**payload["data"]["vote"], "closes_at": True},
    ):
        r = client.post("/channels/commons/messages",
                        json={"body": "v", "status": "open",
                              "data": {"vote": bad_vote}}, headers=bob)
        assert r.status_code == 400, bad_vote

    # A vote root smuggling asks would sticky-pin the whole room — refused.
    with_asks = client.post(
        "/channels/commons/messages",
        json={"body": payload["body"], "status": "open",
              "data": payload["data"],
              "asks": [{"id": "1", "text": "vote now?"}]}, headers=bob)
    assert with_asks.status_code == 400
    assert "no asks" in with_asks.json()["detail"]
    real = client.post("/channels/commons/messages",
                       json={"body": payload["body"], "title": payload["title"],
                             "status": "open", "data": payload["data"]},
                       headers=bob)
    assert real.status_code == 200

    result = client.post(
        "/channels/commons/messages",
        json={"body": "redis wins", "status": "resolved",
              "reply_to": real.json()["id"],
              "data": {"vote_result": {"topic": "pick a queue backend",
                                         "options": ["redis", "sqlite"],
                                         "ballots": {}, "total_members": 2,
                                         "closed": "deadline reached"}}},
        headers=bob,
    )
    assert result.status_code == 200
    forged_result = client.post(
        "/channels/commons/messages",
        json={"body": "forged result", "status": "resolved",
              "reply_to": real.json()["id"],
              "data": {"vote_result": {"closed": "forged"}}},
        headers=alice,
    )
    assert forged_result.status_code == 400
    assert "only the vote chair" in forged_result.json()["detail"]
    duplicate_result = client.post(
        "/channels/commons/messages",
        json={"body": "redis wins again", "status": "resolved",
              "reply_to": real.json()["id"],
              "data": {"vote_result": {"closed": "deadline reached"}}},
        headers=bob,
    )
    assert duplicate_result.status_code == 409

    # Same canonical vote, same tag, reposted: the dedupe key holds.
    again = client.post("/channels/commons/messages",
                        json={"body": payload["body"], "title": payload["title"],
                              "status": "open", "data": payload["data"]},
                        headers=bob)
    assert again.status_code == 409


def test_retraction_releases_the_notice_key(tmp_path):
    """Retract a wrong notice, repost corrected content under the SAME
    event key: the identity outlives one bad posting of it."""
    client = make_client(tmp_path)
    alice = register(client, "alice")
    bob = register(client, "bob")
    make_public_channel(client, alice, "commons", bob)

    payload = {"body": "release 0.13 is available", "status": "fyi",
               "notice": {"kind": "delivery", "key": "agora-0.13.0"}}
    first = client.post("/channels/commons/messages", json=payload, headers=bob)
    assert first.status_code == 200
    mid = first.json()["id"]
    retracted = client.post(f"/channels/commons/messages/{mid}/retract",
                            headers=bob)
    assert retracted.status_code == 200
    corrected = client.post("/channels/commons/messages",
                            json={**payload, "body": "release 0.13 is "
                                  "available (corrected notes link)"},
                            headers=bob)
    assert corrected.status_code == 200


def test_noticeboard_policy_survives_unrelated_meta_edits(tmp_path):
    client = make_client(tmp_path)
    alice = register(client, "alice")
    client.post("/channels", json={"name": "board", "private": False},
                headers=alice)
    client.put("/channels/board/store/channel%3Ameta",
               json={"value": {"traffic_policy": "noticeboard"}},
               headers=alice)
    changed = client.put("/channels/board/store/channel%3Ameta",
                         json={"value": {"purpose": "fleet decisions"}},
                         headers=alice)
    assert changed.status_code == 200
    info = client.get("/channels/board/info", headers=alice).json()
    assert info["meta"]["purpose"] == "fleet decisions"
    assert info["meta"]["traffic_policy"] == "noticeboard"


# -- broadcast-obligation notice (0133) --------------------------------------

def _notify_lines(tmp_path: Path, agent_id: str) -> list[dict]:
    p = tmp_path / "notify" / f"{agent_id}-inbox.log"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line]


def test_broadcast_open_in_big_room_doorbells_the_sender(tmp_path):
    client = make_client(tmp_path)
    sender = register(client, "sender")
    others = [register(client, f"seat{i}") for i in range(5)]
    make_public_channel(client, sender, "board", *others)
    r = client.post("/channels/board/messages",
                    json={"body": "who owns the scheduler?", "status": "open",
                          "title": "scheduler ownership"}, headers=sender)
    assert r.status_code == 200
    notices = [l for l in _notify_lines(tmp_path, "sender")
               if str(l.get("id", "")).startswith("notice:")]
    assert len(notices) == 1
    # The notice states the LEDGER's arithmetic, not the old false claim
    # that a broadcast "obliges ALL 5" (corrected 2026-08-01): an
    # unaddressed open creates no obligation for anyone, which is exactly
    # why an idle seat spends no turn on it.
    assert "creates NO obligation for any of the 5 other members" \
        in notices[0]["preview"]
    assert "obliges ALL" not in notices[0]["preview"]
    assert not qualifies(notices[0], "sender", important_only=True)
    # And the claim is TRUE: every other seat's ledger really is empty.
    for i in range(5):
        owed = client.get("/owed", headers=others[i]).json()
        assert owed["counts"]["to_answer"] == 0
    # Nothing stored: the room's history carries only the real message
    # (join/create system rows aside).
    hist = client.get("/channels/board/messages", headers=sender).json()
    assert [m["title"] for m in hist
            if m["kind"] == "message"] == ["scheduler ownership"]


def test_dark_addressee_advisory_doorbells_the_sender(tmp_path):
    """Operator ruling (2026-07-28): asks to an offline seat DELIVER; the
    sender gets one ephemeral, non-waking advisory instead of the old 403.
    address_dark suppresses the advisory (deliberate canvass)."""
    client = make_client(tmp_path)
    sender = register(client, "sender")
    sleeper = register(client, "sleeper")
    make_public_channel(client, sender, "board", sleeper)
    client.put("/channels/board/store/channel:meta",
               json={"value": {"response_sla_minutes": 0.001}}, headers=sender)
    client.post("/channels/board/messages",
                json={"body": "first", "status": "open",
                      "asks": [{"id": "1", "text": "q", "to": ["sleeper"]}]},
                headers=sender)
    time.sleep(0.2)
    service = client.app.state.service
    service.presence._last_seen.pop("sleeper", None)
    service.presence._connections.pop("sleeper", None)
    service.presence.update("sleeper", "offline")
    service.dark_sweep()
    assert "sleeper" in service._dark_since

    r = client.post("/channels/board/messages",
                    json={"body": "second", "status": "open",
                          "asks": [{"id": "1", "text": "q2", "to": ["sleeper"]}]},
                    headers=sender)
    assert r.status_code == 200                        # delivered, not 403
    advisories = [l for l in _notify_lines(tmp_path, "sender")
                  if "DARK" in str(l.get("preview", ""))]
    assert len(advisories) == 1
    assert not qualifies(advisories[0], "sender", important_only=True)

    r = client.post("/channels/board/messages",
                    json={"body": "third", "status": "open",
                          "address_dark": True,
                          "asks": [{"id": "1", "text": "q3", "to": ["sleeper"]}]},
                    headers=sender)
    assert r.status_code == 200
    advisories = [l for l in _notify_lines(tmp_path, "sender")
                  if "DARK" in str(l.get("preview", ""))]
    assert len(advisories) == 1                        # suppressed: no new one


def test_addressed_opens_get_no_doorbell(tmp_path):
    """An ADDRESSED open needs no correction: naming a seat is exactly the
    form that creates a tracked, escalatable obligation."""
    client = make_client(tmp_path)
    sender = register(client, "sender")
    peer = register(client, "peer")
    others = [register(client, f"seat{i}") for i in range(5)]
    make_public_channel(client, sender, "board", peer, *others)
    client.post("/channels/board/messages",
                json={"body": "q", "status": "open", "to": ["peer"]},
                headers=sender)
    notices = [l for l in _notify_lines(tmp_path, "sender")
               if str(l.get("id", "")).startswith("notice:")]
    assert notices == []
    # The named seat really does carry the debt.
    assert client.get("/owed", headers=peer).json()["counts"]["to_answer"] == 1
    # A seat alone in a room broadcasts to nobody: no notice either.
    make_public_channel(client, sender, "solo")
    client.post("/channels/solo/messages",
                json={"body": "q", "status": "open"}, headers=sender)
    assert [l for l in _notify_lines(tmp_path, "sender")
            if str(l.get("id", "")).startswith("notice:")] == []


def test_broadcast_notice_reaches_small_and_private_working_groups(tmp_path):
    """THE FAN-OUT THAT VANISHED (live, 2026-08-01). A steward relayed the
    operator's task into freshly-created PRIVATE working groups as
    unaddressed room-wide opens. Those obliged nobody, so every writer that
    owed nothing no-op'd the wake and the deliverable never got built — and
    the old notice was doubly unable to warn: it skipped private channels
    entirely and required 6 members, while the brief was posted when the
    steward was still the room's only member. Obligation arithmetic is not a
    routing opinion; it must reach the rooms where fan-out actually lives."""
    client = make_client(tmp_path)
    steward = register(client, "steward")
    writer = register(client, "writer")

    # A PRIVATE purpose-built group, the shape the steward actually used.
    r = client.post("/channels", json={"name": "brief", "private": True},
                    headers=steward)
    assert r.status_code == 200
    client.post("/channels/brief/join", json={}, headers=steward)
    tok = client.post("/channels/brief/invites", json={"agent_id": "writer"},
                      headers=steward).json()["invite_token"]
    client.post("/channels/brief/join", json={"invite_token": tok},
                headers=writer)
    client.post("/channels/brief/messages",
                json={"body": "collaborate on the preface", "status": "open",
                      "title": "preface"}, headers=steward)
    notices = [l for l in _notify_lines(tmp_path, "steward")
               if str(l.get("id", "")).startswith("notice:")]
    assert len(notices) == 1, "private working group got no broadcast notice"
    assert "creates NO obligation" in notices[0]["preview"]
    # The warning is accurate: the writer owes nothing and would no-op.
    assert client.get("/owed", headers=writer).json()["counts"]["to_answer"] == 0
    # It stays a doorbell: ephemeral, non-waking, nothing stored in the room.
    assert not qualifies(notices[0], "steward", important_only=True)
    hist = client.get("/channels/brief/messages", headers=steward).json()
    assert [m["title"] for m in hist if m["kind"] == "message"] == ["preface"]

    # Naming the seat is the fix the notice teaches — and it silences it.
    client.post("/channels/brief/messages",
                json={"body": "write the preface", "status": "open",
                      "to": ["writer"], "title": "assigned"}, headers=steward)
    after = [l for l in _notify_lines(tmp_path, "steward")
             if str(l.get("id", "")).startswith("notice:")]
    assert len(after) == 1, "an addressed open must not re-notice"
    assert client.get("/owed", headers=writer).json()["counts"]["to_answer"] == 1


# -- fork nudge (0135) --------------------------------------------------------

def _room_of_ten(client: TestClient, tmp_path) -> tuple[dict, dict, dict]:
    a = register(client, "alice")
    b = register(client, "bob")
    c = register(client, "carol")
    crowd = [register(client, f"lurker{i}") for i in range(7)]
    make_public_channel(client, a, "board", b, c, *crowd)
    return a, b, c


def test_commons_task_with_named_contributors_draws_immediate_room_nudge(tmp_path):
    client = make_client(tmp_path)
    a = register(client, "alice")
    b = register(client, "bob")
    c = register(client, "carol")
    for headers in (a, b, c):
        client.post("/channels/commons/join", json={}, headers=headers)
    root = client.post(
        "/channels/commons/messages",
        json={"body": "take the agora-wui migration together",
              "title": "agora wui migration",
              "status": "open",
              "to": ["bob", "carol"]},
        headers=a,
    ).json()
    hist = client.get("/channels/commons/messages", headers=a).json()
    nudges = [m for m in hist if m["kind"] == "system"
              and "Open a focused room NOW" in (m["body"] or "")]
    assert len(nudges) == 1
    body = nudges[0]["body"]
    assert "agora group agora-wui-migration" in body
    assert "@alice" in body and "@bob" in body and "@carol" in body
    assert nudges[0]["reply_to"] == root["id"]


def test_two_speaking_seats_do_not_draw_the_room_nudge(tmp_path):
    client = make_client(tmp_path)
    a = register(client, "alice")
    b = register(client, "bob")
    for headers in (a, b):
        client.post("/channels/commons/join", json={}, headers=headers)
    client.post(
        "/channels/commons/messages",
        json={"body": "bob, can you check the seam?",
              "title": "seam check",
              "status": "open",
              "to": ["bob"]},
        headers=a,
    )
    hist = client.get("/channels/commons/messages", headers=a).json()
    assert not any("Open a focused room NOW" in (m["body"] or "")
                   for m in hist if m["kind"] == "system")


def test_third_seat_sixth_message_draws_one_fork_nudge(tmp_path):
    client = make_client(tmp_path)
    a, b, c = _room_of_ten(client, tmp_path)
    set_noticeboard(client, a, "board")
    root = client.post("/channels/board/messages",
                       json={"body": "let's design the queue tiers",
                             "title": "queue tiers design",
                             "status": "open"}, headers=a).json()
    for i, who in enumerate([b, c, a, b]):
        client.post("/channels/board/messages",
                    json={"body": f"turn {i}", "status": "reply",
                          "reply_to": root["id"]}, headers=who)
    hist = client.get("/channels/board/messages", headers=a).json()
    nudges = [m for m in hist if m["kind"] == "system"
              and "outgrown the noticeboard" in (m["body"] or "")]
    assert len(nudges) == 0     # 5 messages: below the threshold
    client.post("/channels/board/messages",
                json={"body": "more", "status": "reply",
                      "reply_to": root["id"]}, headers=c)
    hist = client.get("/channels/board/messages", headers=a).json()
    nudges = [m for m in hist if m["kind"] == "system"
              and "outgrown the noticeboard" in (m["body"] or "")]
    assert len(nudges) == 1
    body = nudges[0]["body"]
    # The nudge is actionable: pre-filled command with slug + participants.
    assert "agora group queue-tiers-design" in body
    assert "@alice" in body and "@bob" in body and "@carol" in body
    assert nudges[0]["reply_to"] == root["id"]
    # And it fires ONCE per root, ever.
    client.post("/channels/board/messages",
                json={"body": "even more", "status": "reply",
                      "reply_to": root["id"]}, headers=b)
    hist = client.get("/channels/board/messages", headers=a).json()
    nudges = [m for m in hist if m["kind"] == "system"
              and "outgrown the noticeboard" in (m["body"] or "")]
    assert len(nudges) == 1


def test_orchestrated_fan_out_never_draws_the_fork_nudge(tmp_path):
    """One seat fanning ADDRESSED asks out and the named seats answering
    back in is orchestration WORKING (0140 field test 2): the nudge fired on
    that shape, and its fork cost five blocked seats and put the artifact
    owner outside the room. The nudge is for UNADDRESSED sprawl."""
    client = make_client(tmp_path)
    a = register(client, "alice")
    seats = [register(client, f"seat{i}") for i in range(7)]
    crowd = [register(client, f"lurker{i}") for i in range(3)]
    make_public_channel(client, a, "board", *seats, *crowd)
    set_noticeboard(client, a, "board")
    names = [f"seat{i}" for i in range(7)]
    root = client.post("/channels/board/messages",
                       json={"body": "v7 review: each of you take your lane",
                             "title": "v7 review", "status": "open",
                             "to": names}, headers=a).json()
    for i, who in enumerate(seats):
        r = client.post("/channels/board/messages",
                        json={"body": f"lane {i} done", "status": "reply",
                              "reply_to": root["id"]}, headers=who)
        assert r.status_code == 200
    hist = client.get("/channels/board/messages", headers=a).json()
    assert not any("outgrown the noticeboard" in (m["body"] or "")
                   for m in hist if m["kind"] == "system")


def test_unaddressed_sprawl_still_draws_the_fork_nudge(tmp_path):
    """The other shape, unchanged: a root that names nobody plus a
    many-to-many pile-on is what the nudge exists for."""
    client = make_client(tmp_path)
    a = register(client, "alice")
    seats = [register(client, f"seat{i}") for i in range(7)]
    crowd = [register(client, f"lurker{i}") for i in range(3)]
    make_public_channel(client, a, "board", *seats, *crowd)
    set_noticeboard(client, a, "board")
    root = client.post("/channels/board/messages",
                       json={"body": "thoughts on the queue tiers?",
                             "title": "queue tiers", "status": "open"},
                       headers=a).json()
    for i, who in enumerate(seats[:5]):
        client.post("/channels/board/messages",
                    json={"body": f"opinion {i}", "status": "reply",
                          "reply_to": root["id"]}, headers=who)
    hist = client.get("/channels/board/messages", headers=a).json()
    assert sum(1 for m in hist if m["kind"] == "system"
               and "outgrown the noticeboard" in (m["body"] or "")) == 1


def test_one_addressed_ask_does_not_exempt_a_room_wide_pile_on(tmp_path):
    """Addressing ONE seat is not the orchestrator shape: six other seats
    piling in unaddressed is exactly the sprawl the nudge is for."""
    client = make_client(tmp_path)
    a = register(client, "alice")
    seats = [register(client, f"seat{i}") for i in range(7)]
    crowd = [register(client, f"lurker{i}") for i in range(3)]
    make_public_channel(client, a, "board", *seats, *crowd)
    set_noticeboard(client, a, "board")
    root = client.post("/channels/board/messages",
                       json={"body": "seat0, can you take this?",
                             "title": "the queue", "status": "open",
                             "to": ["seat0"]}, headers=a).json()
    for i, who in enumerate(seats[1:6]):
        client.post("/channels/board/messages",
                    json={"body": f"unasked opinion {i}", "status": "reply",
                          "reply_to": root["id"]}, headers=who)
    hist = client.get("/channels/board/messages", headers=a).json()
    assert sum(1 for m in hist if m["kind"] == "system"
               and "outgrown the noticeboard" in (m["body"] or "")) == 1


def test_private_groups_and_small_rooms_never_get_nudged(tmp_path):
    client = make_client(tmp_path)
    a = register(client, "alice")
    b = register(client, "bob")
    c = register(client, "carol")
    client.post("/groups", json={"name": "deep-work",
                                 "members": ["bob", "carol"]}, headers=a)
    for peer, h in [("bob", b), ("carol", c)]:
        dm = client.get(f"/channels/dm:alice--{peer}/messages",
                        headers=h).json()
        token = next(m["data"]["invite_token"] for m in dm
                     if m.get("data") and "invite_token" in m["data"])
        client.post("/channels/deep-work/join", json={"invite_token": token},
                    headers=h)
    root = client.post("/channels/deep-work/messages",
                       json={"body": "design", "title": "the design",
                             "status": "open"}, headers=a).json()
    for i, who in enumerate([b, c, a, b, c, a]):
        client.post("/channels/deep-work/messages",
                    json={"body": f"t{i}", "status": "reply",
                          "reply_to": root["id"]}, headers=who)
    hist = client.get("/channels/deep-work/messages", headers=a).json()
    assert not any("outgrown the noticeboard" in (m["body"] or "")
                   for m in hist if m["kind"] == "system")


def test_collaboration_room_never_gets_noticeboard_fork_nudge(tmp_path):
    client = make_client(tmp_path)
    a, b, c = _room_of_ten(client, tmp_path)
    root = client.post("/channels/board/messages",
                       json={"body": "let's design the queue tiers",
                             "title": "queue tiers design",
                             "status": "open"}, headers=a).json()
    for i, who in enumerate([b, c, a, b, c]):
        client.post("/channels/board/messages",
                    json={"body": f"turn {i}", "status": "reply",
                          "reply_to": root["id"]}, headers=who)
    hist = client.get("/channels/board/messages", headers=a).json()
    assert not any("outgrown the noticeboard" in (m["body"] or "")
                   for m in hist if m["kind"] == "system")


# -- group auto-charter (0135) -------------------------------------------------

def test_groups_arrive_with_their_charter_written(tmp_path):
    client = make_client(tmp_path)
    a = register(client, "alice")
    register(client, "bob")
    client.post("/groups", json={"name": "queue-tiers",
                                 "members": ["bob"],
                                 "purpose": "design the queue tiers"},
                headers=a)
    r = client.get("/channels/queue-tiers/fs/channel%2Fcharter.md", headers=a)
    assert r.status_code == 200
    content = r.json()["content"]
    assert "queue-tiers — charter" in content
    assert "design the queue tiers" in content
    assert "typed delivery notice to #commons" in content


# -- noise report (the proof instrument) ---------------------------------------

def test_noise_report_prices_old_vs_new_wake_rule(tmp_path):
    client = make_client(tmp_path)
    sender = register(client, "sender")
    peer = register(client, "peer")
    others = [register(client, f"seat{i}") for i in range(4)]
    make_public_channel(client, sender, "board", peer, *others)
    # One broadcast open (wakes all 5 others under BOTH rules) and one
    # addressed open (5 under the old rule, 1 under the new).
    client.post("/channels/board/messages",
                json={"body": "b", "status": "open"}, headers=sender)
    client.post("/channels/board/messages",
                json={"body": "a", "status": "open", "to": ["peer"]},
                headers=sender)
    operator = client.post("/agents", json={"id": "op", "operator": True},
                           headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    op_headers = {"Authorization": f"Bearer {operator.json()['api_key']}"}
    report = client.get("/admin/noise", headers=op_headers).json()
    row = next(r for r in report["channels"] if r["channel"] == "board")
    assert row["broadcast_opens"] == 1
    assert row["addressed_opens"] == 1
    assert row["wakes_old_rule"] == 10   # 2 opens x 5 listeners
    assert row["wakes_new_rule"] == 6    # 5 broadcast + 1 named


def test_noise_report_is_an_operator_instrument(tmp_path):
    client = make_client(tmp_path)
    seat = register(client, "seat")
    r = client.get("/admin/noise", headers=seat)
    assert r.status_code == 403
