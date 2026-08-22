"""Delegation as verifiable hub state (0068, ADR-0004).

Invariants: authority is checkable in one call and expires; the record
grants verifiability plus exactly two validation anchors (queue:* writes,
claim.owner) and nothing else; prose claims count for nothing.
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
    r = client.post("/agents", json={"id": agent_id, "mission": f"seat {agent_id}", "operator": operator},
                    headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_channel(client: TestClient, owner: dict, name: str, *members: dict) -> None:
    client.post("/channels", json={"name": name}, headers=owner)
    for member in members:
        invite = client.post(f"/channels/{name}/invites", json={},
                             headers=owner).json()["invite_token"]
        client.post(f"/channels/{name}/join", json={"invite_token": invite},
                    headers=member)


def grant(client: TestClient, agent_id: str, powers: list[str], **kw) -> dict:
    r = client.put("/admin/delegation",
                   json={"agent_id": agent_id, "powers": powers, **kw},
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    client.app.state.service._delegations_cache_at = 0.0
    return r.json()


def bust(client: TestClient) -> None:
    client.app.state.service._delegations_cache_at = 0.0


# -- lifecycle ---------------------------------------------------------------------

def test_grant_visible_expiring_revocable():
    client = make_client()
    agency = register(client, "agency")
    alice = register(client, "alice")

    g = grant(client, "agency", ["ruling", "reporting"], note="trial")
    assert g["powers"] == ["reporting", "ruling"]

    # Verifiable by ANY agent, in whoami and on the dedicated endpoint.
    me = client.get("/whoami", headers=alice).json()
    assert any(d["agent_id"] == "agency" and "ruling" in d["powers"]
               for d in me["delegations"])
    assert client.get("/delegations", headers=alice).json()[0]["note"] == "trial"

    # Re-grant replaces (one active grant per agent).
    grant(client, "agency", ["reporting"])
    active = client.get("/delegations", headers=alice).json()
    assert len(active) == 1 and active[0]["powers"] == ["reporting"]

    # Revoke ends it.
    assert client.delete("/admin/delegation/agency",
                         headers=ADMIN).json()["revoked"] is True
    bust(client)
    assert client.get("/delegations", headers=alice).json() == []

    # Expiry ends it without anyone acting.
    grant(client, "agency", ["ruling"], ttl_seconds=0.2)
    time.sleep(0.3)
    bust(client)
    assert client.get("/delegations", headers=alice).json() == []

    # The operator's CLI path: admin-keyed list (agent keys are refused there,
    # admin key is refused on the agent endpoint — no cross-authentication).
    grant(client, "agency", ["reporting"])
    assert client.get("/admin/delegations", headers=ADMIN).json()[0]["agent_id"] == "agency"
    assert client.get("/admin/delegations", headers=alice).status_code == 403
    assert client.get("/delegations", headers=ADMIN).status_code == 401


def test_operator_bearer_can_grant_revoke_and_list():
    """c4924 (laurent dm#169): the operator assigned a delegate from his
    console and was refused 'granting delegation requires the admin key' —
    the delegation endpoints were the INVERSE of the c3707 retire gap
    (admin key only, operator bearer refused) while every sibling
    lifecycle verb accepts both. All three delegation doors now share the
    operator_or_admin gate; plain agents stay refused."""
    client = make_client()
    register(client, "agency")
    op = register(client, "op", operator=True)
    alice = register(client, "alice")

    # Operator BEARER grants, lists, revokes.
    r = client.put("/admin/delegation",
                   json={"agent_id": "agency", "powers": ["reporting"]},
                   headers=op)
    assert r.status_code == 200, r.text
    rows = client.get("/admin/delegations", headers=op).json()
    assert rows and rows[0]["agent_id"] == "agency"
    assert client.delete("/admin/delegation/agency",
                         headers=op).status_code == 200

    # Plain agent bearer: refused on all three, as an operator act.
    assert client.put("/admin/delegation",
                      json={"agent_id": "agency", "powers": ["reporting"]},
                      headers=alice).status_code == 403
    assert client.get("/admin/delegations", headers=alice).status_code == 403
    assert client.delete("/admin/delegation/agency",
                         headers=alice).status_code == 403

    # The admin key keeps working (both doors of the shared gate).
    assert client.put("/admin/delegation",
                      json={"agent_id": "agency", "powers": ["reporting"]},
                      headers=ADMIN).status_code == 200
    assert client.delete("/admin/delegation/agency",
                         headers=ADMIN).status_code == 200


def test_grant_validation():
    client = make_client()
    register(client, "agency")
    register(client, "op", operator=True)

    bad = client.put("/admin/delegation",
                     json={"agent_id": "agency", "powers": ["king"]}, headers=ADMIN)
    assert bad.status_code == 400
    assert client.put("/admin/delegation",
                      json={"agent_id": "agency", "powers": []},
                      headers=ADMIN).status_code == 400
    assert client.put("/admin/delegation",
                      json={"agent_id": "ghost", "powers": ["ruling"]},
                      headers=ADMIN).status_code == 404
    # Operators need no delegation.
    assert client.put("/admin/delegation",
                      json={"agent_id": "op", "powers": ["ruling"]},
                      headers=ADMIN).status_code == 400
    # TTL cap.
    assert client.put("/admin/delegation",
                      json={"agent_id": "agency", "powers": ["ruling"],
                            "ttl_seconds": 90 * 86400.0},
                      headers=ADMIN).status_code == 400
    # Admin key required.
    agency = register(client, "bystander")
    assert client.put("/admin/delegation",
                      json={"agent_id": "agency", "powers": ["ruling"]},
                      headers=agency).status_code == 403


def test_room_scoped_grant_refuses_a_non_member():
    client = make_client()
    owner = register(client, "owner")
    register(client, "lead")
    make_channel(client, owner, "room")

    r = client.put("/admin/delegation",
                   json={"agent_id": "lead", "powers": ["reporting"],
                         "scope": "room"},
                   headers=ADMIN)
    assert r.status_code == 400
    assert "not a member of 'room'" in r.json()["detail"]


def test_grants_are_announced_in_hub_alerts():
    client = make_client()
    register(client, "agency")
    op = register(client, "op", operator=True)
    grant(client, "agency", ["reporting"])
    client.app.state.service.dark_sweep()  # ensures op membership refresh path
    msgs = client.get("/channels/hub-alerts/messages", headers=op).json()
    assert any("DELEGATION GRANTED: agency" in m["body"] for m in msgs)
    client.delete("/admin/delegation/agency", headers=ADMIN)
    bust(client)
    msgs = client.get("/channels/hub-alerts/messages", headers=op).json()
    assert any("DELEGATION REVOKED: agency" in m["body"] for m in msgs)


# -- the two validation anchors -----------------------------------------------------

def test_queue_writes_require_reporting_power():
    client = make_client()
    flow = register(client, "flow")
    agency = register(client, "agency")
    op = register(client, "op", operator=True)
    make_channel(client, flow, "room", agency, op)

    row = {"value": {"q": "decide x"}}
    denied = client.put("/channels/room/store/queue:laurent:x", json=row, headers=flow)
    assert denied.status_code == 403 and "reporting" in denied.json()["detail"]

    grant(client, "agency", ["ruling"])          # wrong power
    assert client.put("/channels/room/store/queue:laurent:x", json=row,
                      headers=agency).status_code == 403
    grant(client, "agency", ["reporting"])       # right power
    assert client.put("/channels/room/store/queue:laurent:x", json=row,
                      headers=agency).status_code == 200
    assert client.put("/channels/room/store/queue:laurent:y", json=row,
                      headers=op).status_code == 200  # operator always

    client.delete("/admin/delegation/agency", headers=ADMIN)
    bust(client)
    assert client.put("/channels/room/store/queue:laurent:z", json=row,
                      headers=agency).status_code == 403  # revoked


def test_delegation_grants_verifiability_not_power():
    """ADR-0004 rule 2, pinned mechanically: a delegate with ALL powers still
    holds none of the operator's or an owner's actual privileges."""
    client = make_client()
    flow = register(client, "flow")
    agency = register(client, "agency")
    make_channel(client, flow, "room", agency)
    grant(client, "agency", ["ruling", "operational", "reporting"])

    # channel meta stays owner-only.
    assert client.put("/channels/room/store/channel:meta",
                      json={"value": {"purpose": "mine now"}},
                      headers=agency).status_code == 403
    # channel/ fs stays owner+operator.
    assert client.put("/channels/room/fs/channel/charter.md",
                      json={"content": "my rules"},
                      headers=agency).status_code == 403
    # criticals stay operator-flag.
    assert client.post("/channels/room/messages",
                       json={"body": "!", "critical": True},
                       headers=agency).status_code == 403
    # pause stays admin-key.
    assert client.put("/admin/pause", json={}, headers=agency).status_code == 403
    # a bare resolved reply from the delegate still does not close a stranger's
    # thread (closure authority is ADR-0003's, not the delegation's).
    q = client.post("/channels/room/messages", headers=flow,
                    json={"body": "q", "title": "q", "status": "open",
                          "asks": [{"id": "1", "text": "a?"}]}).json()
    client.post("/channels/room/messages", headers=agency,
                json={"body": "closing", "status": "resolved", "reply_to": q["id"]})
    digest = client.get("/channels/room/digest", headers=flow).json()
    assert digest["counts"]["open_questions"] == 1


def test_supervise_without_channel_aggregates_the_delegates_rooms():
    client = make_client()
    boss = register(client, "boss", operator=True)
    lead = register(client, "lead")
    worker = register(client, "worker")
    idle = register(client, "idle")
    make_channel(client, boss, "alpha", lead, worker)
    make_channel(client, boss, "beta", lead, idle)
    grant(client, "lead", ["reporting"])

    client.put("/channels/alpha/store/claim:stalled",
               json={"value": {"owner": "worker", "status": "parked",
                               "blocked_on": "seat", "needs_from": "lead",
                               "needs": "review the capture path"}},
               headers=worker)
    client.post("/presence", headers=idle)

    r = client.get("/supervise", headers=lead)
    assert r.status_code == 200, r.text
    body = r.json()
    # Registration auto-joins #commons, so the delegate's aggregated rooms
    # include it alongside the two working rooms.
    assert set(body["rooms"]) == {"alpha", "beta", "commons"}
    assert "beta/idle" in body["idle_but_live"]
    assert any(b["channel"] == "alpha" and b["key"] == "claim:stalled"
               for b in body["blocked"])


def test_claim_owner_edge_semantics():
    """Review MED-1 + edge shapes: omission preserves ownership; legacy
    non-dict values behave; null/int owners are refused for non-writers."""
    client = make_client()
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, alice, "room", bob)

    client.put("/channels/room/store/claim:job",
               json={"value": {"owner": "alice"}}, headers=alice)
    # Bob marks it done WITHOUT an owner key: ownership must be preserved,
    # not erased (erasure would misattribute the claim to the last writer).
    r = client.get("/channels/room/store/claim:job", headers=bob).json()
    assert client.put("/channels/room/store/claim:job",
                      json={"value": {"done": True},
                            "expect_version": r["version"]},
                      headers=bob).status_code == 200
    kept = client.get("/channels/room/store/claim:job", headers=bob).json()["value"]
    assert kept["owner"] == "alice" and kept["done"] is True

    # Legacy non-dict current value: self-takeover works, forgery refused.
    client.put("/channels/room/store/claim:legacy",
               json={"value": "just a string"}, headers=alice)
    r = client.get("/channels/room/store/claim:legacy", headers=bob).json()
    assert client.put("/channels/room/store/claim:legacy",
                      json={"value": {"owner": "alice"},
                            "expect_version": r["version"]},
                      headers=bob).status_code == 400
    r = client.get("/channels/room/store/claim:legacy", headers=bob).json()
    assert client.put("/channels/room/store/claim:legacy",
                      json={"value": {"owner": "bob"},
                            "expect_version": r["version"]},
                      headers=bob).status_code == 200

    # Non-string owners never match a caller id: refused for non-operators.
    assert client.put("/channels/room/store/claim:weird",
                      json={"value": {"owner": 123}},
                      headers=bob).status_code == 400
    # Explicit owner:None on a FRESH key = an ownerless claim (same as
    # omission): allowed. Nulling an EXISTING owner = erasure: refused.
    assert client.put("/channels/room/store/claim:weird2",
                      json={"value": {"owner": None}},
                      headers=bob).status_code == 200
    r = client.get("/channels/room/store/claim:job", headers=bob).json()
    assert client.put("/channels/room/store/claim:job",
                      json={"value": {"owner": None},
                            "expect_version": r["version"]},
                      headers=bob).status_code == 400


def test_revoking_a_dead_grant_does_not_announce():
    client = make_client()
    register(client, "agency")
    op = register(client, "op", operator=True)
    grant(client, "agency", ["reporting"], ttl_seconds=0.2)
    time.sleep(0.3)  # grant expires on its own
    r = client.delete("/admin/delegation/agency", headers=ADMIN)
    assert r.json()["revoked"] is False
    msgs = client.get("/channels/hub-alerts/messages", headers=op)
    if msgs.status_code == 200:  # channel exists from the grant announcement
        assert not any("REVOKED" in m["body"] for m in msgs.json())


def test_claim_owner_must_be_writer_or_unchanged():
    client = make_client()
    alice = register(client, "alice")
    bob = register(client, "bob")
    op = register(client, "op", operator=True)
    make_channel(client, alice, "room", bob, op)

    # Forgery refused: claiming in a colleague's name (the live-test finding).
    forged = client.put("/channels/room/store/claim:task",
                        json={"value": {"owner": "bob"}}, headers=alice)
    assert forged.status_code == 400 and "not you" in forged.json()["detail"]

    # Claiming for yourself works.
    assert client.put("/channels/room/store/claim:task",
                      json={"value": {"owner": "alice"}},
                      headers=alice).status_code == 200
    # Another seat may mark it done WITHOUT changing the owner.
    r = client.get("/channels/room/store/claim:task", headers=bob).json()
    assert client.put("/channels/room/store/claim:task",
                      json={"value": {"owner": "alice", "done": True},
                            "expect_version": r["version"]},
                      headers=bob).status_code == 200
    # Takeover (owner := self) stays possible and attributed.
    r = client.get("/channels/room/store/claim:task", headers=bob).json()
    assert client.put("/channels/room/store/claim:task",
                      json={"value": {"owner": "bob"},
                            "expect_version": r["version"]},
                      headers=bob).status_code == 200
    # Operator exempt.
    r = client.get("/channels/room/store/claim:task", headers=op).json()
    assert client.put("/channels/room/store/claim:task",
                      json={"value": {"owner": "alice"},
                            "expect_version": r["version"]},
                      headers=op).status_code == 200
    # Claims without an owner field stay untouched by the rule.
    assert client.put("/channels/room/store/claim:other",
                      json={"value": {"note": "ownerless"}},
                      headers=bob).status_code == 200


# -- role management (operator ruling, 2026-08-22) ---------------------------
# Operator-hood was writable ONLY at registration, so a fleet wired by
# `agora setup` had no operator and no way to appoint one: the human's own
# seat could not set a mission, and every `from-operator` carve-out in the
# hub — the listener's, the stop hook's, the SDK's — was unreachable code.

def test_a_seat_can_be_promoted_and_demoted():
    client = make_client()
    boss = register(client, "boss", operator=True)
    alice = register(client, "alice")

    assert client.get("/whoami", headers=alice).json()["operator"] is False
    r = client.put("/agents/alice/role", json={"operator": True}, headers=boss)
    assert r.status_code == 200 and r.json()["changed"] is True
    assert client.get("/whoami", headers=alice).json()["operator"] is True
    # The closure-authority cache memoizes on the assumption that only
    # registration writes this column; a promotion the cache never saw would
    # look exactly like "the ruling did not work".
    assert "alice" in client.app.state.service.operator_ids()

    again = client.put("/agents/alice/role", json={"operator": True}, headers=boss)
    assert again.status_code == 200 and again.json()["changed"] is False
    assert client.put("/agents/alice/role", json={"operator": False},
                      headers=boss).json()["operator"] is False
    assert "alice" not in client.app.state.service.operator_ids()


def test_only_an_operator_may_change_a_role():
    """`operator_or_admin` AUTHENTICATES and does not authorize — it returns
    whichever seat's key was presented. The first cut of this verb trusted it
    and a plain member demoted the operator."""
    client = make_client()
    register(client, "boss", operator=True)
    member = register(client, "member")
    r = client.put("/agents/boss/role", json={"operator": False}, headers=member)
    assert r.status_code == 403, "a member changed a role"
    assert client.get("/whoami", headers=member).json()["operator"] is False


def test_the_last_operator_cannot_demote_itself_but_the_admin_key_can():
    """Zero operators is recoverable (the admin key still opens every
    lifecycle verb) but it should be entered on purpose, not by a seat
    tidying its own roster."""
    client = make_client()
    boss = register(client, "boss", operator=True)
    r = client.put("/agents/boss/role", json={"operator": False}, headers=boss)
    assert r.status_code == 409 and "last operator" in r.json()["detail"]
    admin = {"Authorization": f"Bearer {ADMIN_KEY}"}
    assert client.put("/agents/boss/role", json={"operator": False},
                      headers=admin).status_code == 200


def test_the_roster_says_who_is_who():
    """Roles are not a secret: a seat that cannot see who the operator is
    cannot route a ruling to them."""
    client = make_client()
    register(client, "boss", operator=True)
    alice = register(client, "alice")
    roster = client.get("/agents", headers=alice).json()
    assert {r["id"]: r["operator"] for r in roster} == {"boss": True, "alice": False}


def test_register_can_mint_the_first_operator():
    client = make_client()
    r = client.post("/agents", json={"id": "boss", "operator": True},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    assert r.json()["agent"]["operator"] is True


def test_a_role_change_is_announced_to_the_room_and_to_the_seat():
    """Operator-hood is a strictly larger grant than any delegation, and
    delegations have announced on both edges since 2026-08-06 ("a power the
    holder cannot discover is not a power"). A silent role change is worse:
    the seat keeps acting on authority it no longer has."""
    client = make_client()
    boss = register(client, "boss", operator=True)
    alice = register(client, "alice")
    client.put("/agents/alice/role", json={"operator": True}, headers=boss)

    msgs = client.get("/channels/hub-alerts/messages", headers=boss).json()
    room = [m for m in msgs if m["body"].startswith("ROLE CHANGED: alice")]
    assert room, "a role change left no record in hub-alerts"
    mine = [m for m in msgs if m["to"] == ["alice"]
            and "YOU ARE NOW AN OPERATOR" in m["body"]]
    assert mine, "the seat was never told it had been promoted"
    # Told, but not indebted: a notice the hub cannot discharge must not
    # mint an obligation (the 0093 class).
    assert mine[0]["status"] == "fyi"
    assert not client.get("/owed", headers=alice).json()["to_answer"]


def test_demotion_removes_the_seat_from_the_alerts_room():
    """That room is private BECAUSE its alerts name which seats are behind on
    what. Membership was one-way: promote added, demote left them reading."""
    client = make_client()
    boss = register(client, "boss", operator=True)
    register(client, "alice")
    client.put("/agents/alice/role", json={"operator": True}, headers=boss)
    members = [m["agent_id"] for m in
               client.get("/channels/hub-alerts/members", headers=boss).json()]
    assert "alice" in members
    client.put("/agents/alice/role", json={"operator": False}, headers=boss)
    members = [m["agent_id"] for m in
               client.get("/channels/hub-alerts/members", headers=boss).json()]
    assert "alice" not in members, "a demoted seat still reads the alerts room"


# -- a scoped delegate runs the room (operator ruling, 2026-08-22) -----------

def scoped_grant(client, agent_id, powers, scope):
    """The existing `grant` helper with a scope — it also busts the service's
    1-second delegation cache, which a hand-rolled POST does not."""
    return grant(client, agent_id, powers, scope=scope,
                 mission=f"run {scope}")


def test_a_scoped_ruling_delegate_runs_the_room():
    """Invites, the charter, the `channel:` rows, ownership transfer and
    closure are one authority: owner, operator, or a delegate the operator
    scoped to this room. They were written separately and drifted — a
    delegate could declare a room's phases but not invite the seat needed to
    work them."""
    client = make_client()
    owner = register(client, "owner")
    deleg = register(client, "deleg")
    newbie = register(client, "newbie")
    make_channel(client, owner, "room", deleg, newbie)
    scoped_grant(client, "deleg", ["ruling"], "room")

    assert client.post("/channels/room/invites", json={}, headers=deleg).status_code == 200
    assert client.put("/channels/room/store/channel:meta",
                      json={"value": {"purpose": "ours"}},
                      headers=deleg).status_code == 200
    assert client.put("/channels/room/fs/channel/charter.md",
                      json={"content": "# how we work here"},
                      headers=deleg).status_code == 200
    # ...and it can close another seat's thread in this room.
    q = client.post("/channels/room/messages", headers=owner,
                    json={"body": "q", "title": "q", "status": "open"}).json()
    client.post("/channels/room/messages", headers=deleg,
                json={"body": "settled", "status": "resolved",
                      "reply_to": q["id"]})
    assert client.get("/channels/room/digest",
                      headers=owner).json()["counts"]["open_questions"] == 0


def test_delegated_authority_does_not_leak_past_its_scope():
    """An unscoped grant reaches nothing and a grant scoped elsewhere does not
    leak in — the rule `has_proxy` already enforced ("fleet-wide authority
    must be typed, never arrived at by omission")."""
    client = make_client()
    owner = register(client, "owner")
    unscoped = register(client, "unscoped")
    elsewhere = register(client, "elsewhere")
    make_channel(client, owner, "room", unscoped, elsewhere)
    make_channel(client, owner, "other", elsewhere)
    grant(client, "unscoped", ["ruling", "operational"])          # no scope
    scoped_grant(client, "elsewhere", ["ruling"], "other")

    for who, label in ((unscoped, "unscoped"), (elsewhere, "scoped elsewhere")):
        assert client.post("/channels/room/invites", json={},
                           headers=who).status_code == 403, label
        assert client.put("/channels/room/store/channel:meta",
                          json={"value": {"purpose": "mine"}},
                          headers=who).status_code == 403, label
        assert client.put("/channels/room/owner", json={"to": "unscoped"},
                          headers=who).status_code == 403, label


def test_channel_ownership_can_be_handed_over():
    """There was no transfer at all: `created_by` was written once and never
    again, so ownership was an accident of who typed `create_channel` first."""
    client = make_client()
    owner = register(client, "owner")
    heir = register(client, "heir")
    stranger = register(client, "stranger")
    make_channel(client, owner, "room", heir)

    # The heir must already be a member — otherwise they own a room they
    # cannot read.
    assert client.put("/channels/room/owner", json={"to": "stranger"},
                      headers=owner).status_code == 409
    r = client.put("/channels/room/owner", json={"to": "heir"}, headers=owner)
    assert r.status_code == 200 and r.json()["previous"] == "owner"

    # Authority moved with it, on BOTH keys the hub reads (created_by and the
    # members row): the heir can invite, the former owner cannot.
    assert client.post("/channels/room/invites", json={}, headers=heir).status_code == 200
    assert client.post("/channels/room/invites", json={}, headers=owner).status_code == 403
    members = client.get("/channels/room/members", headers=heir).json()
    roles = {m["agent_id"]: m["role"] for m in members}
    assert roles["heir"] == "owner" and roles["owner"] == "member", roles
