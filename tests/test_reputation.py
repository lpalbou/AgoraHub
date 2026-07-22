"""Reputation (0094): peer ±1 on four fixed axes, per-channel scores that
sum to the hub score, leaderboards at both levels, full attribution.

The anti-gaming contract under test: identity-bound votes (auth is the
rater), ONE live vote per (rater, target, axis, channel) with
revision-in-place (never stacking), self-votes refused, membership required
on both sides, notes bounded, archived channels read-only, and the audit
surface (votes-for) naming who stands where on whom.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agora.hub.app import create_app

ADMIN_KEY = "test-admin"
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}


def make_client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0, dark_watch_seconds=0)
    return TestClient(app)


def register(client, agent_id):
    r = client.post("/agents", json={"id": agent_id}, headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def setup_room(client, name="workroom", *, private=False):
    """Three members (alpha owner, beta, gamma) + outsider not in the room."""
    keys = {a: register(client, a) for a in ("alpha", "beta", "gamma", "outsider")}
    client.post("/channels", json={"name": name, "private": private},
                headers=keys["alpha"])
    for a in ("beta", "gamma"):
        client.post(f"/channels/{name}/join", json={}, headers=keys[a])
    return keys


def rate(client, key, target, axis="trust", value=1, note="", channel="workroom"):
    return client.put(f"/channels/{channel}/reputation/{target}",
                      json={"axis": axis, "value": value, "note": note},
                      headers=key)


# -- casting: validation walls ------------------------------------------------


def test_vote_requires_valid_axis_value_and_no_self():
    client = make_client()
    k = setup_room(client)
    r = rate(client, k["alpha"], "beta", axis="charisma")
    assert r.status_code == 400 and "axis must be one of" in r.text
    r = rate(client, k["alpha"], "beta", value=5)
    assert r.status_code == 400 and "+1 or -1" in r.text
    r = rate(client, k["alpha"], "beta", value=0)
    assert r.status_code == 400
    r = rate(client, k["alpha"], "alpha")
    assert r.status_code == 400 and "self-votes are refused" in r.text


def test_vote_requires_shared_membership_both_sides():
    client = make_client()
    k = setup_room(client)
    # Rater outside the channel: refused at the membership wall.
    r = rate(client, k["outsider"], "beta")
    assert r.status_code == 403
    # Target outside the channel: refused with the teaching text.
    r = rate(client, k["alpha"], "outsider")
    assert r.status_code == 400 and "not a member" in r.text
    # Unregistered target: 404.
    r = rate(client, k["alpha"], "ghost")
    assert r.status_code == 404


def test_note_bounded_at_280():
    client = make_client()
    k = setup_room(client)
    r = rate(client, k["alpha"], "beta", note="x" * 281)
    assert r.status_code == 413
    r = rate(client, k["alpha"], "beta", note="x" * 280)
    assert r.status_code == 200


# -- the one-live-vote invariant ----------------------------------------------


def test_revision_replaces_never_stacks():
    client = make_client()
    k = setup_room(client)
    assert rate(client, k["alpha"], "beta", value=1).status_code == 200
    assert rate(client, k["alpha"], "beta", value=1).status_code == 200
    assert rate(client, k["alpha"], "beta", value=1).status_code == 200
    board = client.get("/channels/workroom/reputation",
                       headers=k["alpha"]).json()
    row = next(r for r in board["leaderboard"] if r["target"] == "beta")
    # Three casts, ONE ballot: the score is +1, not +3.
    assert row["score"] == 1 and row["breakdown"]["trust"]["score"] == 1
    assert row["raters"] == 1

    # Flipping the vote replaces it in place: -1, not 0-sum history.
    assert rate(client, k["alpha"], "beta", value=-1,
                note="claimed shipped; was not").status_code == 200
    board = client.get("/channels/workroom/reputation",
                       headers=k["alpha"]).json()
    row = next(r for r in board["leaderboard"] if r["target"] == "beta")
    assert row["score"] == -1
    assert row["breakdown"]["trust"] == {"score": -1, "up": 0, "down": 1, "raters": 1}


def test_withdraw_vote():
    client = make_client()
    k = setup_room(client)
    rate(client, k["alpha"], "beta", axis="trust", value=1)
    rate(client, k["alpha"], "beta", axis="helper", value=1)
    r = client.delete("/channels/workroom/reputation/beta?axis=trust",
                      headers=k["alpha"])
    assert r.json()["removed"] == 1
    r = client.delete("/channels/workroom/reputation/beta",
                      headers=k["alpha"])
    assert r.json()["removed"] == 1  # the remaining helper vote
    board = client.get("/channels/workroom/reputation",
                       headers=k["alpha"]).json()
    assert board["leaderboard"] == []


# -- scores, leaderboards, and the hub sum -------------------------------------


def test_channel_leaderboard_shape_and_order():
    client = make_client()
    k = setup_room(client)
    rate(client, k["alpha"], "beta", axis="trust", value=1)
    rate(client, k["gamma"], "beta", axis="thorough", value=1)
    rate(client, k["alpha"], "gamma", axis="helper", value=-1)
    board = client.get("/channels/workroom/reputation",
                       headers=k["beta"]).json()
    assert board["channel"] == "workroom"
    assert board["categories"] == ["general", "trust", "wisdom", "thorough", "helper"]
    lb = board["leaderboard"]
    assert [r["target"] for r in lb] == ["beta", "gamma"]  # +2 before -1
    beta = lb[0]
    assert beta["score"] == 2 and beta["raters"] == 2
    assert beta["breakdown"]["trust"] == {"score": 1, "up": 1, "down": 0, "raters": 1}
    assert beta["breakdown"]["thorough"] == {"score": 1, "up": 1, "down": 0, "raters": 1}


def test_hub_reputation_is_sum_over_channels():
    client = make_client()
    k = setup_room(client)
    # Second shared room with its own votes.
    client.post("/channels", json={"name": "lab", "private": False},
                headers=k["alpha"])
    client.post("/channels/lab/join", json={}, headers=k["beta"])
    rate(client, k["alpha"], "beta", axis="trust", value=1)
    rate(client, k["gamma"], "beta", axis="trust", value=1)
    rate(client, k["alpha"], "beta", axis="trust", value=1, channel="lab")

    hub = client.get("/reputation", headers=k["outsider"]).json()
    assert hub["channel"] is None
    row = next(r for r in hub["leaderboard"] if r["target"] == "beta")
    # DISTINCT VOUCHERS (0094 hardening), not the channel sum: alpha voted
    # +trust in BOTH workroom and lab but counts ONCE (+1); gamma once (+1).
    # Total = 2 vouchers across 2 channels, not 3 channel-votes.
    assert row["score"] == 2 and row["channels"] == 2
    assert row["breakdown"]["trust"]["up"] == 2

    # Channel boards stay membership-gated; the outsider reads only hub.
    r = client.get("/channels/workroom/reputation", headers=k["outsider"])
    assert r.status_code == 403


def test_votes_audit_surface_names_raters_and_whys():
    client = make_client()
    k = setup_room(client)
    rate(client, k["alpha"], "beta", axis="trust", value=1,
         note="receipts matched claims all week")
    rate(client, k["gamma"], "beta", axis="trust", value=-1,
         note="two stale version claims")
    votes = client.get("/channels/workroom/reputation/beta/votes",
                       headers=k["beta"]).json()
    assert {v["rater"] for v in votes} == {"alpha", "gamma"}
    assert all(v["note"] for v in votes)
    # Non-members cannot read the audit surface.
    r = client.get("/channels/workroom/reputation/beta/votes",
                   headers=k["outsider"])
    assert r.status_code == 403


# -- lifecycle interactions -----------------------------------------------------


def test_hub_score_counts_distinct_vouchers_not_channel_farms():
    """0094 hardening (adversary V2): the hub total is DISTINCT VOUCHERS,
    not a sum over channels — so a colluding pair cannot pump a score by
    farming self-created channels. One rater across many shared channels
    counts ONCE per axis; DM channels never add weight."""
    client = make_client()
    k = setup_room(client)
    # alpha rates beta +trust in the shared room AND in five more shared
    # channels — the naive sum would be +6; distinct-voucher is +1.
    rate(client, k["alpha"], "beta", axis="trust", value=1)
    for i in range(5):
        name = f"farm{i}"
        client.post("/channels", json={"name": name, "private": False},
                    headers=k["alpha"])
        client.post(f"/channels/{name}/join", json={}, headers=k["beta"])
        rate(client, k["alpha"], "beta", axis="trust", value=1, channel=name)
    # A unilateral DM vote must not add weight either.
    client.post("/dms/beta", json={}, headers=k["alpha"])
    rate(client, k["alpha"], "beta", axis="trust", value=1,
         channel="dm:alpha--beta")

    hub = client.get("/reputation", headers=k["gamma"]).json()
    beta = next(r for r in hub["leaderboard"] if r["target"] == "beta")
    assert beta["score"] == 1                       # ONE voucher, not 6 or 7
    assert beta["breakdown"]["trust"] == {"score": 1, "up": 1, "down": 0, "raters": 1}
    assert beta["raters"] == 1

    # A SECOND independent rater is what raises the score.
    rate(client, k["gamma"], "beta", axis="trust", value=1)
    hub = client.get("/reputation", headers=k["gamma"]).json()
    beta = next(r for r in hub["leaderboard"] if r["target"] == "beta")
    assert beta["score"] == 2 and beta["raters"] == 2


def test_retiring_a_rater_withdraws_its_votes():
    """0094 hardening (adversary V5): a decommissioned seat must not keep
    voting weight. Retiring the RATER clears its votes; votes ABOUT a
    still-active target are untouched."""
    client = make_client()
    k = setup_room(client)
    rate(client, k["alpha"], "beta", axis="trust", value=1)
    rate(client, k["gamma"], "beta", axis="trust", value=1)
    op = register(client, "operator-y")
    client.app.state.service.db._conn.execute(
        "UPDATE agents SET operator = 1 WHERE id = 'operator-y'")
    client.app.state.service.db._conn.commit()
    r = client.post("/agents/alpha/retire", headers=op, json={})
    assert r.status_code == 200, r.text
    board = client.get("/channels/workroom/reputation",
                       headers=k["gamma"]).json()
    beta = next(r for r in board["leaderboard"] if r["target"] == "beta")
    assert beta["score"] == 1 and beta["raters"] == 1   # alpha's vote gone
    votes = client.get("/channels/workroom/reputation/beta/votes",
                       headers=k["gamma"]).json()
    assert {v["rater"] for v in votes} == {"gamma"}


def test_value_rejects_non_integer_at_boundary():
    """0094 hardening (adversary V1): StrictInt rejects JSON true/1.0/'1'
    so the audit trail carries only real integer ballots."""
    client = make_client()
    k = setup_room(client)
    for bad in (True, 1.5, "1"):
        r = client.put("/channels/workroom/reputation/beta",
                       json={"axis": "trust", "value": bad},
                       headers=k["alpha"])
        assert r.status_code == 422, (bad, r.text)


def test_note_is_sanitized():
    """0094 hardening (adversary V6): control chars/ANSI/newlines stripped
    so a note can't spoof a CLI board or poison a log."""
    client = make_client()
    k = setup_room(client)
    rate(client, k["alpha"], "beta", axis="trust", value=1,
         note="line1\nline2\x1b[31mred\x00")
    votes = client.get("/channels/workroom/reputation/beta/votes",
                       headers=k["alpha"]).json()
    note = votes[0]["note"]
    assert "\n" not in note and "\x1b" not in note and "\x00" not in note
    assert "line1" in note and "red" in note


def test_dm_exclusion_is_case_sensitive():
    """0094 hardening (adversary F1): the hub excludes real DM channels
    (dm:a--b) but NOT a legitimate public channel whose name merely starts
    with 'DM:' — the creation guard is case-sensitive, so the exclusion
    must be too (GLOB, not case-insensitive LIKE), or such a channel's
    votes silently vanish from the hub score."""
    client = make_client()
    k = setup_room(client)
    # A public channel named with an uppercase 'DM:' prefix is legal.
    r = client.post("/channels", json={"name": "DM:project", "private": False},
                    headers=k["alpha"])
    assert r.status_code == 200, r.text
    client.post("/channels/DM:project/join", json={}, headers=k["beta"])
    rate(client, k["alpha"], "beta", axis="trust", value=1, channel="DM:project")
    hub = client.get("/reputation", headers=k["gamma"]).json()
    # The 'DM:project' vote COUNTS (it is not a real DM).
    assert any(r["target"] == "beta" for r in hub["leaderboard"])
    beta = next(r for r in hub["leaderboard"] if r["target"] == "beta")
    assert beta["breakdown"]["trust"]["score"] == 1


def test_leaving_withdraws_your_votes_keeps_votes_about_you():
    """0094 hardening (adversary F2): a rater can't drive-by downvote then
    leave, stranding a vote neither they nor the target can remove. Leaving
    withdraws the leaver's OWN votes; votes ABOUT the leaver stay."""
    client = make_client()
    k = setup_room(client)
    rate(client, k["gamma"], "beta", axis="trust", value=-1, note="hit and run")
    rate(client, k["alpha"], "gamma", axis="helper", value=1)  # about gamma
    # gamma leaves the channel.
    r = client.post("/channels/workroom/leave", json={}, headers=k["gamma"])
    assert r.status_code == 200, r.text
    board = client.get("/channels/workroom/reputation",
                       headers=k["alpha"]).json()
    targets = {row["target"]: row for row in board["leaderboard"]}
    # gamma's drive-by downvote on beta is GONE.
    assert "beta" not in targets or targets["beta"]["score"] == 0
    # But alpha's vote ABOUT gamma survives gamma's departure.
    assert targets.get("gamma", {}).get("score") == 1


def test_unrate_is_pause_gated():
    """0094 hardening (adversary F3): the board is shared state — a hub
    stand-down freezes withdrawals just as it freezes casting."""
    client = make_client()
    k = setup_room(client)
    rate(client, k["alpha"], "beta", axis="trust", value=1)
    # Pause requires the admin key (never an agent/operator seat key).
    client.put("/admin/pause", headers=ADMIN, json={"reason": "stand down"})
    r = client.delete("/channels/workroom/reputation/beta", headers=k["alpha"])
    assert r.status_code == 423  # paused: no board mutation, cast or withdraw


def test_net_zero_target_stays_visible_with_split():
    """0094 hardening (adversary F4): a controversial target must not read
    as unrated. A target whose vouchers net to zero still appears, with the
    up/down split showing the disagreement."""
    client = make_client()
    k = setup_room(client)
    # Second room so alpha can hold opposite signs across channels.
    client.post("/channels", json={"name": "lab", "private": False},
                headers=k["alpha"])
    client.post("/channels/lab/join", json={}, headers=k["beta"])
    rate(client, k["alpha"], "beta", axis="trust", value=1)
    rate(client, k["gamma"], "beta", axis="trust", value=-1)
    hub = client.get("/reputation", headers=k["gamma"]).json()
    beta = next(r for r in hub["leaderboard"] if r["target"] == "beta")
    # Net zero, but PRESENT and showing the +1/-1 controversy, not hidden.
    assert beta["breakdown"]["trust"] == {"score": 0, "up": 1, "down": 1, "raters": 2}
    assert beta["raters"] == 2


def test_archived_channel_refuses_new_votes_keeps_board_readable():
    client = make_client()
    k = setup_room(client)
    rate(client, k["alpha"], "beta", value=1)
    op = register(client, "operator-x")
    client.app.state.service.db._conn.execute(
        "UPDATE agents SET operator = 1 WHERE id = 'operator-x'")
    client.app.state.service.db._conn.commit()
    r = client.post("/channels/workroom/archive", headers=op, json={})
    assert r.status_code == 200, r.text
    r = rate(client, k["alpha"], "beta", value=-1)
    assert r.status_code in (403, 409)  # archived: no new judgment written
    # Hub-wide board still carries the history.
    hub = client.get("/reputation", headers=k["alpha"]).json()
    assert any(row["target"] == "beta" for row in hub["leaderboard"])
