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
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agora.hub.app import create_app
from agora.listen import qualifies

ADMIN_KEY = "test-admin"


def make_client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0,
                     notify_dir=str(tmp_path / "notify"))
    return TestClient(app)


def register(client: TestClient, agent_id: str) -> dict[str, str]:
    r = client.post("/agents", json={"id": agent_id},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_public_channel(client: TestClient, owner: dict, name: str,
                        *members: dict) -> None:
    client.post("/channels", json={"name": name, "private": False},
                headers=owner)
    for member in members:
        client.post(f"/channels/{name}/join", json={}, headers=member)


# -- the narrowed listener rule (pure function) ------------------------------

def _event(status: str = "open", flags: str = "", sender: str = "peer") -> dict:
    return {"from": sender, "status": status, "flags": flags}


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
    assert "obliges ALL 5" in notices[0]["preview"]
    # Nothing stored: the room's history carries only the real message
    # (join/create system rows aside).
    hist = client.get("/channels/board/messages", headers=sender).json()
    assert [m["title"] for m in hist
            if m["kind"] == "message"] == ["scheduler ownership"]


def test_addressed_or_small_room_opens_get_no_doorbell(tmp_path):
    client = make_client(tmp_path)
    sender = register(client, "sender")
    peer = register(client, "peer")
    others = [register(client, f"seat{i}") for i in range(5)]
    make_public_channel(client, sender, "board", peer, *others)
    make_public_channel(client, sender, "duo", peer)
    # Addressed in the big room: the named seat carries it, no notice.
    client.post("/channels/board/messages",
                json={"body": "q", "status": "open", "to": ["peer"]},
                headers=sender)
    # Broadcast in a tiny room: below the teaching threshold.
    client.post("/channels/duo/messages",
                json={"body": "q", "status": "open"}, headers=sender)
    notices = [l for l in _notify_lines(tmp_path, "sender")
               if str(l.get("id", "")).startswith("notice:")]
    assert notices == []


# -- fork nudge (0135) --------------------------------------------------------

def _room_of_ten(client: TestClient, tmp_path) -> tuple[dict, dict, dict]:
    a = register(client, "alice")
    b = register(client, "bob")
    c = register(client, "carol")
    crowd = [register(client, f"lurker{i}") for i in range(7)]
    make_public_channel(client, a, "board", b, c, *crowd)
    return a, b, c


def test_third_seat_sixth_message_draws_one_fork_nudge(tmp_path):
    client = make_client(tmp_path)
    a, b, c = _room_of_ten(client, tmp_path)
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
    assert "receipt to #commons" in content


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
