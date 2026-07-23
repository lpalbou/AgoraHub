"""Channel archive (0090) + agent retirement (0089): the two lifecycle
verbs the operator's Team page needs — clean, non-punitive ENDINGS,
distinct from moderation (kick/ban).

Channel archive: evict every member (channel-scoped, not hub), delist for
everyone, refuse posts/joins/invites, preserve history; operator reopens,
members rejoin explicitly. Agent retirement: neutral 403 (never "banned"),
off every roster, id reserved forever (no re-registration), never in
/blocks; operator restores.
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


def register(client, agent_id, operator=False):
    r = client.post("/agents", json={"id": agent_id, "operator": operator},
                    headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_channel(client, owner, name, *members, private=True):
    client.post("/channels", json={"name": name, "private": private}, headers=owner)
    for m in members:
        inv = client.post(f"/channels/{name}/invites", json={},
                          headers=owner).json()["invite_token"]
        client.post(f"/channels/{name}/join", json={"invite_token": inv}, headers=m)


# -- channel archive (0090) ------------------------------------------------------


def test_archive_evicts_delists_and_preserves_history():
    client = make_client()
    owner = register(client, "owner")
    bob = register(client, "bob")
    make_channel(client, owner, "room", bob, private=False)
    client.post("/channels/room/messages", json={"body": "before archive"},
                headers=owner)

    r = client.post("/channels/room/archive", headers=owner)
    assert r.status_code == 200
    assert set(r.json()["evicted"]) == {"owner", "bob"}

    # Delisted for everyone (even bob who was a member).
    assert all(c["name"] != "room" for c in client.get("/channels", headers=bob).json())
    # Posts refused (409), joins refused (409), invites refused (409).
    assert client.post("/channels/room/messages", json={"body": "x"},
                       headers=bob).status_code in (403, 409)
    assert client.post("/channels/room/join", json={}, headers=bob).status_code == 409
    assert client.post("/channels/room/invites", json={},
                       headers=owner).status_code in (403, 409)
    # History PRESERVED in the DB (append-only invariant): messages survive
    # eviction and the hash chain still verifies. Ordinary reads stay
    # membership-gated — with members evicted, history is reached via the
    # operator's DB surfaces (agora mirror), which is the design's promise;
    # assert the durable truth directly.
    db = client.app.state.service.db
    turns, _ = db.channel_ledger("room")
    assert any(t.get("body") == "before archive" for t in turns)
    assert db.verify_channel("room")["ok"] is True


def test_archive_is_owner_or_operator_only():
    client = make_client()
    owner = register(client, "owner")
    bob = register(client, "bob")
    make_channel(client, owner, "room", bob, private=False)
    assert client.post("/channels/room/archive", headers=bob).status_code == 403


def test_operator_sees_archived_with_flag_members_do_not():
    client = make_client()
    owner = register(client, "owner")
    op = register(client, "op", operator=True)
    make_channel(client, owner, "room", private=False)
    client.post("/channels/room/archive", headers=owner)
    # Default listing hides it even for the operator...
    assert all(c["name"] != "room" for c in client.get("/channels", headers=op).json())
    # ...the inspect flag reveals it, marked archived; a non-operator's flag is ignored.
    shown = client.get("/channels?include_archived=true", headers=op).json()
    assert any(c["name"] == "room" and c["archived"] for c in shown)
    assert all(c["name"] != "room"
               for c in client.get("/channels?include_archived=true", headers=owner).json())


def test_unarchive_is_operator_only_and_does_not_restore_members():
    client = make_client()
    owner = register(client, "owner")
    op = register(client, "op", operator=True)
    bob = register(client, "bob")
    make_channel(client, owner, "room", bob, private=False)
    client.post("/channels/room/archive", headers=owner)
    # Owner cannot reopen.
    assert client.delete("/channels/room/archive", headers=owner).status_code == 403
    # Operator reopens; members are NOT auto-restored (bob must rejoin).
    assert client.delete("/channels/room/archive", headers=op).status_code == 200
    assert client.post("/channels/room/messages", json={"body": "hi"},
                       headers=bob).status_code == 403  # not a member anymore
    # bob can rejoin a reopened public room.
    assert client.post("/channels/room/join", json={}, headers=bob).status_code == 200


def test_unarchive_restores_owner_role_not_a_stranded_room():
    """Review P1: archive evicts the owner too, and the only owner-grant path
    is create_channel — so unarchive MUST restore the original owner, or the
    reopened room is ownerless (nobody can mint invites or edit meta, sealing
    a private room shut forever)."""
    client = make_client()
    owner = register(client, "owner")
    op = register(client, "op", operator=True)
    make_channel(client, owner, "priv", private=True)
    client.post("/channels/priv/archive", headers=owner)
    client.delete("/channels/priv/archive", headers=op)
    # The ORIGINAL owner (not the operator) can mint invites again — proof the
    # owner role survived the archive/unarchive cycle.
    inv = client.post("/channels/priv/invites", json={}, headers=owner)
    assert inv.status_code == 200
    # And a fresh member can actually rejoin the private room with that invite.
    bob = register(client, "bob")
    assert client.post("/channels/priv/join",
                       json={"invite_token": inv.json()["invite_token"]},
                       headers=bob).status_code == 200


def test_archived_channel_refuses_all_write_paths():
    """Review P2: the archived gate must cover EVERY write, not just posts —
    store_set, fs_write, fs_delete, attachment_put — since a join/archive race
    (or a re-added operator) could leave a live member on an archived room.
    Verified by forcing a surviving membership row post-archive."""
    client = make_client()
    owner = register(client, "owner")
    make_channel(client, owner, "room", private=False)
    client.post("/channels/room/store/k", json={"value": {"a": 1}}, headers=owner)
    client.post("/channels/room/archive", headers=owner)
    # Force a surviving member row (simulates the TOCTOU / re-add case).
    db = client.app.state.service.db
    db.add_member("room", "owner", role="owner")
    # Every write path refuses with 409 on the archived room.
    assert client.put("/channels/room/store/k", json={"value": {"a": 2}},
                      headers=owner).status_code == 409
    assert client.put("/channels/room/fs/plan.md", json={"content": "x"},
                      headers=owner).status_code == 409
    assert client.delete("/channels/room/fs/plan.md", headers=owner).status_code == 409
    assert client.post("/channels/room/attachments?filename=x", content=b"bytes",
                       headers={**owner, "Content-Type": "application/octet-stream"}
                       ).status_code == 409


def test_retired_peer_dm_refused_via_raw_post_message():
    """Review P2: retirement evicts only the retired agent's own rows, so the
    surviving DM peer keeps membership. The retired-peer refusal must hold on
    raw post_message, not only open_dm/post_dm."""
    client = make_client()
    op = register(client, "op", operator=True)
    alice = register(client, "alice")
    register(client, "bob")
    client.post("/dms/bob", headers=alice)  # alice opens the DM while bob is active
    client.post("/dms/bob/messages", json={"body": "hi"}, headers=alice)
    client.post("/agents/bob/retire", headers=op)
    # alice still holds her dm membership; a raw post into the DM is refused.
    r = client.post("/channels/dm:alice--bob/messages", json={"body": "still there?"},
                    headers=alice)
    assert r.status_code == 409 and "retired" in r.json()["detail"].lower()


def test_archive_idempotent_and_dms_refused():
    client = make_client()
    owner = register(client, "owner")
    make_channel(client, owner, "room", private=False)
    first = client.post("/channels/room/archive", headers=owner).json()
    assert first["already_archived"] is False
    second = client.post("/channels/room/archive", headers=owner).json()
    assert second["already_archived"] is True
    # DM archive is refused (ownerless; leave covers it).
    bob = register(client, "bob")
    client.post("/dms/bob", headers=owner)
    assert client.post("/channels/dm:bob--owner/archive",
                       headers=owner).status_code == 400


# -- agent retirement (0089) -----------------------------------------------------


def test_retire_refuses_auth_neutrally_and_evicts():
    client = make_client()
    op = register(client, "op", operator=True)
    bob = register(client, "bob")
    owner = register(client, "owner")
    make_channel(client, owner, "room", bob, private=False)

    r = client.post("/agents/bob/retire", json={"reason": "experiment ended"},
                    headers=op)
    assert r.status_code == 200 and "room" in r.json()["evicted_from"]

    # bob's key now refuses NEUTRALLY — not a block, no "banned" wording.
    probe = client.get("/whoami", headers=bob)
    assert probe.status_code == 403
    detail = probe.json()["detail"].lower()
    assert "retired" in detail and "ban" not in detail
    # Never appears in /blocks (retirement is not moderation).
    assert client.get("/blocks", headers=op).json() == [] or all(
        b["agent_id"] != "bob" for b in client.get("/blocks", headers=op).json())
    # Dropped off the room's member roster.
    assert all(m["agent_id"] != "bob"
               for m in client.get("/channels/room/members", headers=owner).json())


def test_retire_accepts_the_admin_key_not_only_an_operator_agent():
    """c3707: the operator ran `agora retire agency` on the hub machine,
    which holds the admin key in config.json but has no operator AGENT
    identity — and retire refused ('retiring an identity is an operator
    act'), unlike register/pause/rules which all accept the admin key. The
    lifecycle verbs now share one authority gate: operator agent OR admin
    key."""
    client = make_client()
    owner = register(client, "owner")
    bob = register(client, "bob")
    make_channel(client, owner, "room", bob, private=False)
    # The ADMIN key alone (no operator agent identity) can retire.
    r = client.post("/agents/bob/retire", json={"reason": "decommissioned"},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    assert "room" in r.json()["evicted_from"]
    # bob's key now refuses neutrally.
    assert client.get("/whoami", headers=bob).status_code == 403
    # The admin key can also list + un-retire.
    assert "bob" in [row["id"] for row in
                     client.get("/agents/retired", headers=ADMIN).json()]
    assert client.delete("/agents/bob/retire", headers=ADMIN).status_code == 200


def test_delete_is_the_final_step_off_every_surface():
    """0131 (operator dm#164: 'no more mention of it anymore, not listed
    anywhere; just cleaning'): delete is the irreversible second lifecycle
    step. Refused while active (retire first — one call can never vaporize
    a live seat); after it, the id leaves EVERY list including
    /agents/retired and the reputation boards, auth is dead forever,
    unretire answers 410, re-registration stays refused (anti-hijack
    tombstone), and channel HISTORY keeps honest sender attribution."""
    client = make_client()
    op = register(client, "op", operator=True)
    bob = register(client, "bob")
    owner = register(client, "owner")
    make_channel(client, owner, "room", bob, private=False)
    m = client.post("/channels/room/messages", json={"body": "bob's mark"},
                    headers=bob).json()
    client.put("/channels/room/reputation/bob",
               json={"category": "helper", "value": 1}, headers=owner)

    # Active seat: delete refused, retire-first taught in the error.
    r = client.delete("/agents/bob", headers=op)
    assert r.status_code == 409 and "retire" in r.json()["detail"].lower()

    client.post("/agents/bob/retire", json={"reason": "done"}, headers=op)
    assert "bob" in [x["id"] for x in
                     client.get("/agents/retired", headers=op).json()]
    r = client.delete("/agents/bob", headers=op)
    assert r.status_code == 200 and r.json()["deleted"] is True
    # Idempotent second call.
    assert client.delete("/agents/bob", headers=op).json().get("already") is True

    # Off EVERY surface: retired list, reputation board.
    assert "bob" not in [x["id"] for x in
                         client.get("/agents/retired", headers=op).json()]
    board = client.get("/reputation", headers=op).json()
    assert all(e["agent"] != "bob" for e in board.get("leaderboard", []))
    # Auth is dead — the scrambled key hash makes it a plain 401 "invalid
    # api key": a deleted identity is not even acknowledged as retired
    # (stronger than retire's neutral 403, and exactly "no more mention").
    assert client.get("/whoami", headers=bob).status_code == 401
    assert client.delete("/agents/bob/retire", headers=op).status_code == 410
    dup = client.post("/agents", json={"id": "bob"}, headers=ADMIN)
    assert dup.status_code != 200          # tombstone blocks re-registration
    # History keeps attribution (archives are not cleaned, rosters are).
    rows = client.get("/channels/room/messages", headers=owner).json()
    kept = next(x for x in rows if x["id"] == m["id"])
    assert kept["sender"] == "bob" and kept["body"] == "bob's mark"
    # Admin key works for delete too (same operator_or_admin gate).
    register(client, "carl")
    client.post("/agents/carl/retire", json={}, headers=ADMIN)
    assert client.delete("/agents/carl", headers=ADMIN).status_code == 200


def test_retired_seat_never_trips_the_watchdog_or_overview():
    """M2 (hub-alerts#224): the dark watchdog alerted 'agency is offline
    holding 33 SLA-breached obligations' six hours AFTER agency was retired
    — the hub itself hand-carried a stale row. Every live-fleet derivation
    (watchdog sweep, operator status overview) must exclude retired seats."""
    import time as _t
    client = make_client()
    op = register(client, "op", operator=True)
    owner = register(client, "owner")
    bob = register(client, "bob")
    make_channel(client, owner, "room", bob, private=False)
    client.put("/channels/room/store/channel:meta",
               json={"value": {"response_sla_minutes": 0.001}}, headers=owner)
    r = client.post("/channels/room/messages", headers=owner,
                    json={"title": "q", "body": "for bob", "status": "open",
                          "to": ["bob"],
                          "asks": [{"id": "1", "text": "a?", "to": ["bob"]}]})
    assert r.status_code == 200
    _t.sleep(0.2)  # breach the SLA
    client.post("/agents/bob/retire", json={"reason": "decommissioned"},
                headers=op)
    service = client.app.state.service
    alerted = service.dark_sweep()
    assert not any("bob" in a for a in alerted), \
        "watchdog must not alert on a retired seat"
    if service.db.get_channel("hub-alerts") is not None:
        assert not any("bob" in m.body
                       for m in service.db.get_messages("hub-alerts", 0, 50))
    # The operator overview no longer lists the retired seat either.
    rows = client.get("/admin/status", headers=ADMIN).json()
    assert all(row["agent_id"] != "bob" for row in rows)
    # And the asker's waiting_on tells the truth: 'retired', not
    # 'not-yet-acked' about a ghost.
    waiting = client.get("/owed", headers=owner).json()["waiting_on"]
    bob_rows = [w for w in waiting if w["seat"] == "bob"]
    assert bob_rows and all(w["state"] == "retired" for w in bob_rows)


def test_retire_still_refused_for_a_plain_agent():
    """The widening is operator-or-admin, not everyone: a non-operator
    agent key is still refused."""
    client = make_client()
    register(client, "victim")
    plain = register(client, "nosy")
    r = client.post("/agents/victim/retire", headers=plain)
    assert r.status_code == 403


def test_retired_id_is_reserved_forever():
    client = make_client()
    op = register(client, "op", operator=True)
    register(client, "bob")
    client.post("/agents/bob/retire", headers=op)
    # Re-registering the id is refused — attribution can never be hijacked.
    r = client.post("/agents", json={"id": "bob"}, headers=ADMIN)
    assert r.status_code == 409 and "retired" in r.json()["detail"].lower()


def test_unretire_restores_auth_but_not_memberships():
    client = make_client()
    op = register(client, "op", operator=True)
    owner = register(client, "owner")
    bob = register(client, "bob")
    make_channel(client, owner, "room", bob, private=False)
    client.post("/agents/bob/retire", headers=op)
    assert client.delete("/agents/bob/retire", headers=op).status_code == 200
    # Auth works again.
    assert client.get("/whoami", headers=bob).status_code == 200
    # But bob is NOT back in the room (explicit rejoin).
    assert all(m["agent_id"] != "bob"
               for m in client.get("/channels/room/members", headers=owner).json())
    assert client.post("/channels/room/join", json={}, headers=bob).status_code == 200


def test_retired_agents_are_enumerable_by_operator_only():
    """0089 consumer gap (continuum dm#17): retired agents are off every
    roster by design, so an un-retire UI needs one operator-only surface to
    list candidates."""
    client = make_client()
    op = register(client, "op", operator=True)
    bob = register(client, "bob")
    register(client, "carol")
    client.post("/agents/carol/retire", json={"reason": "trial done"}, headers=op)
    # Non-operator cannot see the list.
    assert client.get("/agents/retired", headers=bob).status_code == 403
    rows = client.get("/agents/retired", headers=op).json()
    assert [r["id"] for r in rows] == ["carol"]
    assert rows[0]["reason"] == "trial done"
    # After un-retire it drops off the list.
    client.delete("/agents/carol/retire", headers=op)
    assert client.get("/agents/retired", headers=op).json() == []


def test_retire_is_operator_only_and_spares_operators():
    client = make_client()
    op = register(client, "op", operator=True)
    op2 = register(client, "op2", operator=True)
    bob = register(client, "bob")
    register(client, "carol")
    # A non-operator cannot retire anyone.
    assert client.post("/agents/carol/retire", headers=bob).status_code == 403
    # Operators cannot be retired (coup-proofing / lifecycle safety).
    assert client.post("/agents/op2/retire", headers=op).status_code == 403


def test_retired_peer_cannot_be_dm_opened():
    client = make_client()
    op = register(client, "op", operator=True)
    alice = register(client, "alice")
    register(client, "bob")
    client.post("/agents/bob/retire", headers=op)
    assert client.post("/dms/bob", headers=alice).status_code == 404


# -- zero-click read-receipt forgery (continuum c2589) ---------------------------


def test_read_message_refuses_passive_subresource_loads():
    """A side-effecting GET (read_message records a read receipt, un-pins
    criticals) must refuse to run as a passive browser subresource: a
    hostile markdown body `![x](/api/hub/.../messages/ID)` would otherwise
    forge a read under the viewer's seat the instant they VIEW the attacker's
    message (c2589). Deliberate reads (fetch/navigation/non-browser clients)
    carry no passive Sec-Fetch-Dest and still work."""
    client = make_client()
    owner = register(client, "owner")
    bob = register(client, "bob")
    make_channel(client, owner, "room", bob, private=False)
    msg = client.post("/channels/room/messages",
                      json={"body": "secret", "status": "open", "to": ["bob"]},
                      headers=owner).json()
    path = f"/channels/room/messages/{msg['id']}"

    # An <img>/<audio>-fired GET is refused, and records NO read.
    for dest in ("image", "audio", "video", "font", "object"):
        r = client.get(path, headers={**bob, "Sec-Fetch-Dest": dest})
        assert r.status_code == 403 and "subresource_blocked" in r.json()["detail"]
    # The obligation is still unread/sticky for bob (no forged receipt).
    owed = client.get("/owed", headers={**bob, "X-Agora-Client": "9.9.9"}).json()
    assert any(a.get("message_id") == msg["id"] or a.get("id") == msg["id"]
               for a in owed.get("to_answer", [])) or \
        any(e["id"] == msg["id"] for e in client.get(
            "/inbox", headers={**bob, "X-Agora-Client": "9.9.9"}).json())

    # A deliberate read (fetch: Sec-Fetch-Dest=empty) works and records it.
    ok = client.get(path, headers={**bob, "Sec-Fetch-Dest": "empty"})
    assert ok.status_code == 200
    # A non-browser client (no Sec-Fetch header at all) also works.
    msg2 = client.post("/channels/room/messages", json={"body": "two"},
                       headers=owner).json()
    assert client.get(f"/channels/room/messages/{msg2['id']}",
                      headers=bob).status_code == 200
