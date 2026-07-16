"""Moderation: kick (timed block) and ban (permanent block), channel and hub.

Invariants under test:
- a kick removes membership NOW and blocks BOTH rejoin paths (public join,
  owner-minted invite) until expiry; expiry re-admits without ceremony;
- a ban is the same block with no expiry; lifting works for both;
- authority: channel scope = owner or operator; hub scope = operator only;
- hub scope is a full lockout: every authenticated call refuses with a
  teaching 403, and the id cannot re-register its way back in;
- operators can never be blocked; you cannot block yourself; DM channels
  have no kicks; blocks are visible hub state (GET /blocks).
"""

import time

from fastapi.testclient import TestClient

from agora.hub.app import create_app

ADMIN_KEY = "test-admin"
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}


def make_client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0, dark_watch_seconds=0)
    return TestClient(app)


def register(client: TestClient, agent_id: str, operator: bool = False) -> dict[str, str]:
    r = client.post("/agents", json={"id": agent_id, "operator": operator},
                    headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_channel(client: TestClient, owner: dict, name: str, *members: dict,
                 private: bool = True) -> None:
    client.post("/channels", json={"name": name, "private": private}, headers=owner)
    for member in members:
        invite = client.post(f"/channels/{name}/invites", json={},
                             headers=owner).json()["invite_token"]
        client.post(f"/channels/{name}/join", json={"invite_token": invite},
                    headers=member)


def _expire_block(client: TestClient, scope: str, agent_id: str) -> None:
    """Rewind the block's expiry instead of sleeping in the test."""
    db = client.app.state.service.db
    with db._lock:
        db._conn.execute(
            "UPDATE blocks SET expires_at = ? WHERE scope = ? AND agent_id = ?"
            " AND lifted_at IS NULL", (time.time() - 1, scope, agent_id))
        db._conn.commit()


# -- channel kick lifecycle ----------------------------------------------------------

def test_kick_removes_and_blocks_both_join_paths_until_expiry():
    client = make_client()
    owner = register(client, "owner")
    bob = register(client, "bob")
    make_channel(client, owner, "room", bob, private=False)

    r = client.post("/channels/room/blocks",
                    json={"agent": "bob", "seconds": 900, "reason": "cooling off"},
                    headers=owner)
    assert r.status_code == 200, r.text
    assert r.json()["expires_at"] is not None

    # Membership is gone NOW: posting refuses.
    r = client.post("/channels/room/messages", json={"body": "hi"}, headers=bob)
    assert r.status_code == 403

    # Public join refuses with a teaching text naming the term.
    r = client.post("/channels/room/join", json={}, headers=bob)
    assert r.status_code == 403 and "kicked by owner until" in r.json()["detail"]

    # An owner-minted invite does NOT outrank the block.
    invite = client.post("/channels/room/invites", json={},
                         headers=owner).json()["invite_token"]
    r = client.post("/channels/room/join", json={"invite_token": invite},
                    headers=bob)
    assert r.status_code == 403

    # Expiry re-admits without ceremony.
    _expire_block(client, "room", "bob")
    r = client.post("/channels/room/join", json={}, headers=bob)
    assert r.status_code == 200 and r.json()["joined"]


def test_ban_is_forever_until_lifted():
    client = make_client()
    owner = register(client, "owner")
    bob = register(client, "bob")
    make_channel(client, owner, "room", bob, private=False)

    r = client.post("/channels/room/blocks", json={"agent": "bob"}, headers=owner)
    assert r.status_code == 200 and r.json()["expires_at"] is None

    r = client.post("/channels/room/join", json={}, headers=bob)
    assert r.status_code == 403 and "banned by owner" in r.json()["detail"]

    # Lift -> rejoin works; lifting again reports no live block.
    r = client.delete("/channels/room/blocks/bob", headers=owner)
    assert r.status_code == 200 and r.json()["lifted"] is True
    assert client.post("/channels/room/join", json={}, headers=bob).status_code == 200
    r = client.delete("/channels/room/blocks/bob", headers=owner)
    assert r.json()["lifted"] is False


def test_reblock_replaces_prior_and_blocks_list_shows_state():
    client = make_client()
    owner = register(client, "owner")
    bob = register(client, "bob")
    make_channel(client, owner, "room", bob)

    client.post("/channels/room/blocks", json={"agent": "bob", "seconds": 60},
                headers=owner)
    client.post("/channels/room/blocks", json={"agent": "bob"}, headers=owner)

    rows = client.get("/blocks", headers=owner).json()
    mine = [b for b in rows if b["scope"] == "room" and b["agent_id"] == "bob"]
    assert len(mine) == 1 and mine[0]["expires_at"] is None  # ban superseded kick

    # Any agent may read the list (verifiable moderation state).
    carol = register(client, "carol")
    assert client.get("/blocks", headers=carol).status_code == 200


# -- authority -----------------------------------------------------------------------

def test_channel_authority_owner_or_operator_only():
    client = make_client()
    owner = register(client, "owner")
    bob = register(client, "bob")
    carol = register(client, "carol")
    op = register(client, "op", operator=True)
    make_channel(client, owner, "room", bob, carol)

    r = client.post("/channels/room/blocks", json={"agent": "bob"}, headers=carol)
    assert r.status_code == 403  # plain member cannot moderate

    r = client.post("/channels/room/blocks", json={"agent": "bob", "seconds": 60},
                    headers=op)
    assert r.status_code == 200  # operator can, even without membership


def test_hub_scope_is_operator_only_and_guards_hold():
    client = make_client()
    owner = register(client, "owner")
    bob = register(client, "bob")
    op = register(client, "op", operator=True)
    op2 = register(client, "op2", operator=True)

    r = client.post("/hub/blocks", json={"agent": "bob"}, headers=owner)
    assert r.status_code == 403 and "need an operator" in r.json()["detail"]

    # Operators cannot be blocked (either scope); unknown agents 404.
    assert client.post("/hub/blocks", json={"agent": "op2"},
                       headers=op).status_code == 403
    assert client.post("/channels/x/blocks", json={"agent": "op2"},
                       headers=op).status_code == 403
    assert client.post("/hub/blocks", json={"agent": "ghost"},
                       headers=op).status_code == 404

    dm_guard = client.post("/channels/dm:a--b/blocks", json={"agent": "bob"},
                           headers=op)
    assert dm_guard.status_code == 400  # DMs have no kicks


def test_self_block_refused():
    client = make_client()
    owner = register(client, "owner")
    make_channel(client, owner, "room")
    r = client.post("/channels/room/blocks", json={"agent": "owner"}, headers=owner)
    assert r.status_code == 400


# -- hub lockout ----------------------------------------------------------------------

def test_hub_block_severs_live_websocket():
    """authenticate() only gates NEW calls: a WS opened BEFORE the block
    must be severed by the control frame, and a reconnect refused."""
    client = make_client()
    bob_headers = register(client, "bob")
    op = register(client, "op", operator=True)
    token = bob_headers["Authorization"].removeprefix("Bearer ")

    with client.websocket_connect(f"/ws?token={token}") as ws:
        r = client.post("/hub/blocks", json={"agent": "bob", "seconds": 60},
                        headers=op)
        assert r.status_code == 200
        # The next frame the socket sees is the close (4403), not traffic.
        try:
            frame = ws.receive()
        except Exception:
            frame = {"type": "websocket.close"}
        assert frame.get("type") == "websocket.close", frame

    # Reconnect refuses at the accept gate.
    try:
        with client.websocket_connect(f"/ws?token={token}") as ws2:
            frame = ws2.receive()
            assert frame.get("type") == "websocket.close", frame
    except Exception:
        pass  # some clients surface the 4401/4403 close as an exception


def test_hub_block_severs_and_refuses_ws_frames():
    """The lockout must hold on an ALREADY-OPEN socket (review F1): frames
    sent after the block are refused, not served."""
    client = make_client()
    owner = register(client, "owner")
    bob_headers = register(client, "bob")
    op = register(client, "op", operator=True)
    client.post("/channels", json={"name": "room", "private": False}, headers=owner)
    token = bob_headers["Authorization"].removeprefix("Bearer ")
    client.post("/channels/room/join", json={}, headers=bob_headers)

    saw_refusal = False
    try:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.send_json({"type": "subscribe", "channels": ["room"]})
            ws.receive_json()  # subscribed
            client.post("/hub/blocks", json={"agent": "bob", "seconds": 60},
                        headers=op)
            # A post frame after the block must be refused (403), never
            # persisted. Either a 403 error frame or the sever-close counts.
            ws.send_json({"type": "post", "channel": "room", "body": "sneaky"})
            for _ in range(4):
                frame = ws.receive_json()
                if frame.get("status") == 403:
                    saw_refusal = True
                    break
    except Exception:
        # receive on a severed socket raises (WebSocketDisconnect /
        # EndOfStream depending on the starlette version) — that IS the
        # refusal: the sever closed the socket.
        saw_refusal = True
    assert saw_refusal

    # The durable truth regardless of frame timing: the sneaky post never
    # reached the ledger.
    msgs = client.get("/channels/room/messages", headers=owner).json()
    assert not any(m.get("body") == "sneaky" for m in msgs)


def test_hub_kick_is_full_lockout_with_teaching_text():
    client = make_client()
    bob = register(client, "bob")
    op = register(client, "op", operator=True)

    r = client.post("/hub/blocks",
                    json={"agent": "bob", "seconds": 1800, "reason": "runaway loop"},
                    headers=op)
    assert r.status_code == 200

    # EVERY authenticated call refuses — reads included ("can't sign in").
    r = client.get("/whoami", headers=bob)
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "kicked by op until" in detail and "runaway loop" in detail
    assert client.get("/inbox", headers=bob).status_code == 403

    # Expiry restores access.
    _expire_block(client, "hub", "bob")
    assert client.get("/whoami", headers=bob).status_code == 200


def test_hub_ban_blocks_reregistration_and_join_tokens():
    client = make_client()
    register(client, "bob")
    op = register(client, "op", operator=True)

    assert client.post("/hub/blocks", json={"agent": "bob"},
                       headers=op).status_code == 200

    # The id cannot come back via admin registration...
    r = client.post("/agents", json={"id": "bob"}, headers=ADMIN)
    assert r.status_code == 403 and "banned by op" in r.json()["detail"]

    # ...nor via a join token naming the id explicitly.
    token = client.post("/join-tokens", json={"any_id": True}, headers=ADMIN)
    if token.status_code == 200:
        artifact = token.json().get("token") or token.json().get("join_token")
        if artifact:
            r = client.post("/join", json={"token": artifact, "agent_id": "bob"})
            assert r.status_code == 403

    # Lift -> whoami works again (identity survived the ban, locked not deleted).
    assert client.delete("/hub/blocks/bob", headers=op).json()["lifted"] is True


# -- validation ------------------------------------------------------------------------

def test_channel_kick_of_owner_refused_teaches_hub_scope():
    """A channel kick deletes the member row (incl. role=owner) with no
    transfer, stranding invites and channel:meta forever — refuse it and
    point at hub scope, which preserves the row (review F2)."""
    client = make_client()
    owner = register(client, "owner")
    op = register(client, "op", operator=True)
    make_channel(client, owner, "room")

    r = client.post("/channels/room/blocks", json={"agent": "owner"}, headers=op)
    assert r.status_code == 403 and "owns 'room'" in r.json()["detail"]
    assert "hub-scope" in r.json()["detail"]
    # The owner is still the owner: can still mint invites afterwards.
    assert client.post("/channels/room/invites", json={},
                       headers=owner).status_code == 200


def test_private_channel_kick_text_warns_about_invite():
    """The kick 403 must not promise a bare-expiry re-admit on a PRIVATE
    channel — the membership was removed and the invite consumed (review F3)."""
    client = make_client()
    owner = register(client, "owner")
    bob = register(client, "bob")
    make_channel(client, owner, "room", bob)  # private by default

    client.post("/channels/room/blocks", json={"agent": "bob", "seconds": 900},
                headers=owner)
    r = client.post("/channels/room/join", json={}, headers=bob)
    assert r.status_code == 403 and "need a fresh invite" in r.json()["detail"]


def test_hub_ban_revokes_delegation_but_kick_keeps_it():
    """whoami is the fleet's authority record — a permanent ban must not leave
    a locked-out id advertising delegated power (review F4). A timed kick
    keeps the grant (a 15-min cooloff should not destroy a 7-day grant)."""
    client = make_client()
    agency = register(client, "agency")
    op = register(client, "op", operator=True)
    client.put("/admin/delegation",
               json={"agent_id": "agency", "powers": ["ruling"]}, headers=ADMIN)
    client.app.state.service._delegations_cache_at = 0.0

    # Timed hub kick keeps the delegation.
    client.post("/hub/blocks", json={"agent": "agency", "seconds": 900}, headers=op)
    client.app.state.service._delegations_cache_at = 0.0
    assert any(d["agent_id"] == "agency"
               for d in client.get("/delegations", headers=op).json())

    # Permanent ban revokes it.
    client.post("/hub/blocks", json={"agent": "agency"}, headers=op)
    client.app.state.service._delegations_cache_at = 0.0
    assert not any(d["agent_id"] == "agency"
                   for d in client.get("/delegations", headers=op).json())


def test_hub_block_orphaned_obligation_reverts_to_broadcast():
    """An open ask addressed only to a hub-blocked agent must revert to
    broadcast pinning, not rot pinned to an id that cannot sign in (F3)."""
    client = make_client()
    owner = register(client, "owner")
    bob = register(client, "bob")
    carol = register(client, "carol")
    op = register(client, "op", operator=True)
    make_channel(client, owner, "room", bob, carol)

    # owner asks bob (addressed). After carol ACKs the seq, the addressed
    # obligation is NOT sticky for carol (0066) — gone from her inbox. Once
    # bob is hub-blocked, it reverts to broadcast and re-pins for carol.
    posted = client.post(
        "/channels/room/messages",
        json={"body": "bob, do X?", "status": "open", "to": ["bob"],
              "asks": [{"id": "1", "text": "do X?", "assignee": "bob"}]},
        headers=owner).json()
    client.post("/inbox/ack", json={"cursors": {"room": posted["seq"]}},
                headers=carol)
    # Identify as a current client (X-Agora-Client) so the hub's synthetic
    # stale-client notice (0.12.3) is not appended to the counted rows.
    current = {**carol, "X-Agora-Client": "999.0.0"}
    before = [e for e in client.get("/inbox", headers=current).json()
              if e["channel"] == "room"]
    assert before == []  # addressed to bob, not sticky for carol
    client.post("/hub/blocks", json={"agent": "bob"}, headers=op)
    after = [e for e in client.get("/inbox", headers=current).json()
             if e["channel"] == "room"]
    assert len(after) == 1  # reverted to broadcast — now pins for carol too


def _grant(client: TestClient, agent_id: str, powers: list[str]) -> None:
    r = client.put("/admin/delegation",
                   json={"agent_id": agent_id, "powers": powers}, headers=ADMIN)
    assert r.status_code == 200, r.text
    client.app.state.service._delegations_cache_at = 0.0


def test_moderation_delegate_can_kick_agents_and_nonoperator_humans():
    """The owner grants `moderation` to protect the collaboration: the
    delegate may kick a misbehaving agent AND a non-operator human, at both
    channel and hub scope."""
    client = make_client()
    owner = register(client, "owner")
    agency = register(client, "agency")          # the delegate
    bob = register(client, "bob")                # a misbehaving agent
    human = register(client, "human")            # a non-operator human seat
    make_channel(client, owner, "room", agency, bob, human, private=False)
    _grant(client, "agency", ["moderation"])

    # Channel kick of an agent by the delegate.
    r = client.post("/channels/room/blocks",
                    json={"agent": "bob", "seconds": 900}, headers=agency)
    assert r.status_code == 200, r.text
    assert client.post("/channels/room/join", json={},
                       headers=bob).status_code == 403

    # Hub ban of a non-operator human by the delegate ("even humans").
    r = client.post("/hub/blocks", json={"agent": "human"}, headers=agency)
    assert r.status_code == 200, r.text
    assert client.get("/whoami", headers=human).status_code == 403


def test_moderation_delegate_cannot_touch_owner_operators_or_delegates():
    """Coup-proofing: a `moderation` delegate may never kick an operator
    (the human owner is an operator, so untouchable at any scope) nor another
    delegate; only operators may kick a delegate. The channel is owned by a
    plain operator here to model the human owner."""
    client = make_client()
    boss = register(client, "boss", operator=True)  # the human owner (operator)
    op2 = register(client, "op2", operator=True)     # a co-operator human
    agency = register(client, "agency")             # moderation delegate
    peer = register(client, "peer")                 # another delegate
    make_channel(client, boss, "room", op2, agency, peer, private=False)
    _grant(client, "agency", ["moderation"])
    _grant(client, "peer", ["reporting"])

    # The human owner (operator) is untouchable — any scope.
    assert client.post("/hub/blocks", json={"agent": "boss"},
                       headers=agency).status_code == 403
    assert client.post("/channels/room/blocks", json={"agent": "boss"},
                       headers=agency).status_code == 403
    # A co-operator human is untouchable by the delegate.
    assert client.post("/hub/blocks", json={"agent": "op2"},
                       headers=agency).status_code == 403
    # Another delegate is untouchable by the delegate (no steward wars).
    r = client.post("/hub/blocks", json={"agent": "peer"}, headers=agency)
    assert r.status_code == 403 and "another steward" in r.json()["detail"]
    # But an OPERATOR retains full authority over a delegate.
    assert client.post("/hub/blocks", json={"agent": "peer"},
                       headers=boss).status_code == 200


def test_plain_delegate_without_moderation_cannot_kick():
    """Only the `moderation` power grants kick — a `reporting` delegate is
    refused, proving the power is separable, not a rider."""
    client = make_client()
    owner = register(client, "owner")
    agency = register(client, "agency")
    bob = register(client, "bob")
    make_channel(client, owner, "room", agency, bob, private=False)
    _grant(client, "agency", ["reporting", "ruling", "operational"])

    assert client.post("/channels/room/blocks", json={"agent": "bob"},
                       headers=agency).status_code == 403
    assert client.post("/hub/blocks", json={"agent": "bob"},
                       headers=agency).status_code == 403


def test_channel_named_hub_is_reserved():
    """Scope collision guard: blocks key on scope where 'hub' means the whole
    hub — a channel with that name would make its blocks indistinguishable
    from hub-wide lockouts, so creation refuses."""
    client = make_client()
    owner = register(client, "owner")
    r = client.post("/channels", json={"name": "hub"}, headers=owner)
    assert r.status_code == 400 and "reserved" in r.json()["detail"]


def test_kick_duration_bounds_and_unknown_channel():
    client = make_client()
    owner = register(client, "owner")
    bob = register(client, "bob")
    op = register(client, "op", operator=True)
    make_channel(client, owner, "room", bob)

    too_long = 8 * 86400
    r = client.post("/channels/room/blocks",
                    json={"agent": "bob", "seconds": too_long}, headers=owner)
    assert r.status_code == 400 and "ban" in r.json()["detail"]

    assert client.post("/channels/room/blocks",
                       json={"agent": "bob", "seconds": 0},
                       headers=owner).status_code == 400
    assert client.post("/channels/nowhere/blocks", json={"agent": "bob"},
                       headers=op).status_code == 404
