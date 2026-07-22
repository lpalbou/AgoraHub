"""One reputation system (agora-0122, operator ruling dm#111): a ± on a
message IS reputation input about its sender.

What must hold (from the two adversarial design reviews):
- ONE standing rating per (message, rater): PUT flips, DELETE withdraws,
  nothing ever stacks (the NULL-hole class is closed by the NOT NULL PK).
- Counting rule (operator dm#134): voting is per message (mechanics);
  the SCORE counts each colleague once per category (net-sign collapse).
  Honesty = visible raters count + per-message idempotency + budget.
- Gates: no self-rating, no system/retracted/foreign-channel targets,
  budget-limited writes.
- Lifecycle parity: leave, kick and retire all clear the rater's ratings
  (the kick door previously stranded votes — fixed in the same change).
- Board shape (0123): one score + per-category breakdown {score,up,down,raters}.
- Migration: OPERATOR reaction rows convert once (meta-guarded); agent rows
  and withdrawn/self/system rows never do.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agora.db import Database
from agora.hub.app import create_app

ADMIN_KEY = "test-admin-ratings"


@pytest.fixture()
def client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY, rate_per_minute=600.0)
    return TestClient(app)


def register(client: TestClient, agent_id: str, operator: bool = False) -> dict:
    r = client.post("/agents", json={"id": agent_id, "operator": operator},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_room(client, owner_h, members: dict[str, dict]) -> None:
    assert client.post("/channels", json={"name": "room"}, headers=owner_h).status_code == 200
    for mid, mh in members.items():
        t = client.post("/channels/room/invites", json={"agent_id": mid},
                        headers=owner_h).json()["invite_token"]
        assert client.post("/channels/room/join", json={"invite_token": t},
                           headers=mh).status_code == 200


def post(client, h, body="hello") -> dict:
    r = client.post("/channels/room/messages", json={"body": body}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_rating_toggles_flips_and_never_stacks(client):
    alice, bob = register(client, "alice"), register(client, "bob")
    make_room(client, alice, {"bob": bob})
    m = post(client, alice)
    path = f"/channels/room/messages/{m['id']}/rating"
    assert client.put(path, json={"value": 1}, headers=bob).status_code == 200
    assert client.put(path, json={"value": 1}, headers=bob).status_code == 200
    row = client.get(f"/channels/room/messages/by-seq/{m['seq']}", headers=bob).json()
    assert row["ratings"] == {"up": 1, "down": 0, "mine": 1}   # no stacking
    # Flip replaces.
    client.put(path, json={"value": -1}, headers=bob)
    row = client.get(f"/channels/room/messages/by-seq/{m['seq']}", headers=bob).json()
    assert row["ratings"] == {"up": 0, "down": 1, "mine": -1}
    # The sender sees the tally but mine=0 (they cannot rate themselves).
    row = client.get(f"/channels/room/messages/by-seq/{m['seq']}", headers=alice).json()
    assert row["ratings"] == {"up": 0, "down": 1, "mine": 0}
    # Withdraw clears.
    assert client.delete(path, headers=bob).json()["removed"] == 1
    row = client.get(f"/channels/room/messages/by-seq/{m['seq']}", headers=bob).json()
    assert row["ratings"] == {"up": 0, "down": 0, "mine": 0}


def test_rating_gates(client):
    alice, bob = register(client, "alice"), register(client, "bob")
    make_room(client, alice, {"bob": bob})
    m = post(client, alice)
    path = f"/channels/room/messages/{m['id']}/rating"
    # Self-rating refused.
    assert client.put(path, json={"value": 1}, headers=alice).status_code == 400
    # Bad values refused (strict int, and only +-1).
    assert client.put(path, json={"value": 2}, headers=bob).status_code == 400
    assert client.put(path, json={"value": "1"}, headers=bob).status_code == 422
    # System rows carry no accountable author.
    sysrow = client.get("/channels/room/messages", params={"since": 0},
                        headers=bob).json()[0]
    assert sysrow["kind"] == "system"
    assert client.put(f"/channels/room/messages/{sysrow['id']}/rating",
                      json={"value": 1}, headers=bob).status_code == 400
    # Retracted rows are tombstones.
    m2 = post(client, alice, body="soon gone")
    client.post(f"/channels/room/messages/{m2['id']}/retract", headers=alice)
    assert client.put(f"/channels/room/messages/{m2['id']}/rating",
                      json={"value": -1}, headers=bob).status_code == 409
    # Channel binding: the same id through another room 404s.
    client.post("/channels", json={"name": "other"}, headers=bob)
    assert client.put(f"/channels/other/messages/{m['id']}/rating",
                      json={"value": 1}, headers=bob).status_code == 404


def test_score_counts_colleagues_while_votes_are_per_message(client):
    """The operator's FINAL rule (dm#134: 'i meant the MECHANICS!!! 10
    messages = UP TO 10 votes'): casting is per message — one standing
    vote per (rater, message), flip/withdraw free — but the SCORE counts
    each colleague once per category (net sign). Five pleased thumbs from
    bob = one voice; the adversary-measured DM pair-farm (30 points from
    one rater) is structurally impossible."""
    alice, bob = register(client, "alice"), register(client, "bob")
    carol = register(client, "carol")
    make_room(client, alice, {"bob": bob, "carol": carol})
    # bob rates FIVE of alice's messages up: five votes, ONE voice.
    for i in range(5):
        m = post(client, alice, body=f"msg {i}")
        client.put(f"/channels/room/messages/{m['id']}/rating",
                   json={"value": 1}, headers=bob)
    m = post(client, alice, body="carol's turn")
    client.put(f"/channels/room/messages/{m['id']}/rating",
               json={"value": -1}, headers=carol)
    board = client.get("/channels/room/reputation", headers=alice).json()
    entry = next(e for e in board["leaderboard"] if e["target"] == "alice")
    assert entry["breakdown"]["general"] == {"score": 0, "up": 1, "down": 1,
                                             "raters": 2}
    assert entry["raters"] == 2 and entry["score"] == 0
    # Hub-wide: same collapse.
    hub = client.get("/reputation", headers=alice).json()
    entry = next(e for e in hub["leaderboard"] if e["target"] == "alice")
    assert entry["breakdown"]["general"]["score"] == 0
    # The per-message TALLY still shows all five standings (mechanics
    # intact): the row decoration is where per-message truth lives.
    row = client.get(f"/channels/room/messages/by-seq/{m['seq']}",
                     headers=alice).json()
    assert row["ratings"] == {"up": 0, "down": 1, "mine": 0}


def test_hub_axis_opinions_stay_one_voice_per_colleague(client):
    """The other half of the rule: a categorized OPINION (trust/...) is a
    standing judgment, not a per-action signal — hub-wide it counts once
    per colleague however many channels repeat it (the measured 0094
    channel-farm stays closed). Within one channel the primary key already
    guarantees one standing vote per rater."""
    alice, bob = register(client, "alice"), register(client, "bob")
    make_room(client, alice, {"bob": bob})
    # bob states the same trust opinion about alice in three rooms.
    for name in ("r1", "r2"):
        client.post("/channels", json={"name": name}, headers=alice)
        t = client.post(f"/channels/{name}/invites", json={"agent_id": "bob"},
                        headers=alice).json()["invite_token"]
        client.post(f"/channels/{name}/join", json={"invite_token": t},
                    headers=bob)
        client.put(f"/channels/{name}/reputation/alice",
                   json={"axis": "trust", "value": 1}, headers=bob)
    client.put("/channels/room/reputation/alice",
               json={"axis": "trust", "value": 1}, headers=bob)
    hub = client.get("/reputation", headers=alice).json()
    entry = next(e for e in hub["leaderboard"] if e["target"] == "alice")
    assert entry["breakdown"]["trust"] == {"score": 1, "up": 1, "down": 0,
                                           "raters": 1}
    assert entry["score"] == 1


def test_dm_ratings_count_hub_wide_per_operator_ruling(client):
    """Operator ruling dm#118 (2026-07-22, 'yes' to include): DM-channel
    message ratings COUNT toward public standing — excluding them was
    exactly what made the operator's -1s invisible. The privacy fold holds:
    the hub board reports counts, never the DM channel name."""
    alice, bob = register(client, "alice"), register(client, "bob")
    client.post("/dms/bob", headers=alice)
    dm = "dm:alice--bob"
    r = client.post(f"/channels/{dm}/messages", json={"body": "dm work"},
                    headers=alice)
    assert r.status_code == 200
    m = r.json()
    assert client.put(f"/channels/{dm}/messages/{m['id']}/rating",
                      json={"value": -1}, headers=bob).status_code == 200
    hub = client.get("/reputation", headers=alice).json()
    entry = next(e for e in hub["leaderboard"] if e["target"] == "alice")
    assert entry["breakdown"]["general"] == {"score": -1, "up": 0, "down": 1, "raters": 1}
    assert entry["raters"] == 1 and entry["score"] == -1
    # Privacy fold: no dm channel name anywhere in the hub-wide payload.
    assert "dm:" not in json.dumps(hub)
    # The DM channel's own board still shows it locally to its members.
    local = client.get(f"/channels/{dm}/reputation", headers=alice).json()
    entry = next(e for e in local["leaderboard"] if e["target"] == "alice")
    assert entry["breakdown"].get("general", {"score":0,"up":0,"down":0})["down"] == 1


def test_lifecycle_clears_ratings_through_every_door(client):
    alice, bob = register(client, "alice"), register(client, "bob")
    carol = register(client, "carol")
    make_room(client, alice, {"bob": bob, "carol": carol})
    m = post(client, alice)
    path = f"/channels/room/messages/{m['id']}/rating"
    client.put(path, json={"value": -1}, headers=bob)
    client.put(path, json={"value": -1}, headers=carol)
    # Leave clears the leaver's rating.
    client.post("/channels/room/leave", headers=bob)
    row = client.get(f"/channels/room/messages/by-seq/{m['seq']}", headers=alice).json()
    assert row["ratings"]["down"] == 1  # carol's stands, bob's cleared
    # Kick clears the kicked rater's rating (the previously-stranded door).
    assert client.post("/channels/room/blocks",
                       json={"agent": "carol", "seconds": 60},
                       headers=alice).status_code == 200
    row = client.get(f"/channels/room/messages/by-seq/{m['seq']}", headers=alice).json()
    assert row["ratings"] == {"up": 0, "down": 0, "mine": 0}


def test_rating_budget_bounds_write_churn():
    # Own app with a wide-open MESSAGE limiter so only the RATING budget
    # (the thing under test) can be the limiting factor.
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=100000.0)
    client = TestClient(app)
    alice, bob = register(client, "alice"), register(client, "bob")
    make_room(client, alice, {"bob": bob})
    ids = [post(client, alice, body=f"m{i}")["id"] for i in range(31)]
    codes = [client.put(f"/channels/room/messages/{i}/rating",
                        json={"value": 1}, headers=bob).status_code for i in ids]
    assert codes.count(200) == 30 and codes[-1] == 429


def test_migration_converts_operator_reactions_once():
    db = Database(":memory:")
    db.register_agent("op", "op", "k-op", operator=True)
    db.register_agent("flow", "flow", "k-flow")
    db.register_agent("peer", "peer", "k-peer")
    db.create_channel("room", False, "op")
    db.add_member("room", "flow")
    db.add_member("room", "peer")
    m = db.insert_message("room", "flow", kind="message", status="fyi",
                          urgency="inbox", title="", body="work", data=None,
                          reply_to=None, critical=False, downgraded=False, to=[])
    own = db.insert_message("room", "op", kind="message", status="fyi",
                            urgency="inbox", title="", body="mine", data=None,
                            reply_to=None, critical=False, downgraded=False, to=[])
    # Operator down on flow's message; peer (non-operator) down too; operator
    # reaction on his OWN message; and a withdrawn (empty) row.
    db.store_set("room", f"reactions:{m.id}",
                 {"up": [], "down": ["op", "peer"]}, "op")
    db.store_set("room", f"reactions:{own.id}", {"up": ["op"], "down": []}, "op")
    db.store_set("room", "reactions:01WITHDRAWN000000000000000",
                 {"up": [], "down": []}, "op")
    # Simulate a PRE-0122 database: such a db has no migration marker (the
    # marker is only written by 0122+ inits — a fresh db writes it at
    # creation, correctly, because there is nothing to migrate there).
    db._conn.execute("DELETE FROM meta WHERE key = 'reactions_migrated'")
    db._conn.commit()
    # Re-open the SAME database content: migration runs at Database.__init__,
    # so serialize this in-memory db into a file-backed one via backup API.
    import sqlite3
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    dst = sqlite3.connect(path)
    db._conn.backup(dst)
    dst.commit(); dst.close()
    db.close()

    reopened = Database(path)
    rows = reopened.ratings_for_messages([m.id, own.id])
    got = rows.get(m.id, [])
    # ONLY the operator's reaction on ANOTHER seat's message converted.
    assert len(got) == 1 and got[0]["rater"] == "op" and got[0]["value"] == -1
    assert own.id not in rows          # self-reaction skipped
    # Meta guard: withdrawing then re-opening must NOT resurrect the rating.
    reopened.rating_clear(m.id, "op")
    reopened.close()
    reopened2 = Database(path)
    assert reopened2.ratings_for_messages([m.id]) == {}
    reopened2.close()
