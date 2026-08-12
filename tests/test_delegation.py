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
