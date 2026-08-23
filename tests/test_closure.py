"""Closure semantics (0062/ADR-0003), addressed-scoped stickiness (0066),
and dark-episode alerts (0067) — each test replays a real field incident
from 2026-07-11/12 (channel commons; see backlog items for the forensics).

The invariant under test: an obligation stays loud exactly where it lives
and exactly until it is settled — never louder, never quieter.
"""

import time

from fastapi.testclient import TestClient

from agora.hub.app import create_app
from agora.hub.presence import _RECEPTION_STALE

ADMIN_KEY = "test-admin"


def make_client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0, dark_watch_seconds=0)
    return TestClient(app)


def register(client: TestClient, agent_id: str, operator: bool = False) -> dict[str, str]:
    r = client.post("/agents", json={"id": agent_id, "mission": f"seat {agent_id}", "operator": operator},
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


def inbox_seqs(client: TestClient, headers: dict) -> list[int]:
    return [e["seq"] for e in client.get("/inbox", headers=headers).json()]


# -- 0062: the asker's resolved reply closes everywhere (the c713 replay) --------

def test_asker_resolved_reply_closes_on_all_surfaces():
    client = make_client()
    flow, memory = register(client, "flow"), register(client, "memory")
    make_channel(client, flow, "room", memory)

    q = post(client, flow, body="two asks", title="q", status="open",
             asks=[{"id": "1", "text": "a?"}, {"id": "2", "text": "b?"}])
    assert q["seq"] in inbox_seqs(client, memory)          # obligation pinned
    # Ack does not clear an open obligation (stickiness intact).
    client.post("/inbox/ack", json={"cursors": {"room": q["seq"]}}, headers=memory)
    assert q["seq"] in inbox_seqs(client, memory)

    # The asker closes their own thread — the c817 gesture, now mechanical.
    post(client, flow, body="ruled elsewhere, closing", title="closed",
         status="resolved", reply_to=q["id"])
    assert q["seq"] not in inbox_seqs(client, memory)      # inbox: gone
    digest = client.get("/channels/room/digest", headers=memory).json()
    assert digest["counts"]["open_questions"] == 0         # digest: decided
    assert any(d["seq"] == q["seq"] and d.get("self_resolved")
               for d in digest["decided"])


def test_a_third_party_cannot_close_someone_elses_thread():
    """This test used to pin the opposite: a `settled_by` pointer let ANY
    member retire ANY thread. That door was shut in front of operator-authored
    requests on 2026-08-06 and left open everywhere else; the operator ruled
    on 2026-08-22 that a question belongs to whoever asked it — they know
    whether it was answered and a bystander does not.

    What a third party should do instead is in the refusal text: post the
    pointer as an ordinary reply. It is just as visible and it obliges the
    asker to read it and close their own question."""
    client = make_client()
    flow, memory, ruling_holder = (register(client, "flow"),
                                   register(client, "memory"),
                                   register(client, "agency"))
    make_channel(client, flow, "room", memory, ruling_holder)
    q = post(client, flow, body="q", title="q", status="open",
             asks=[{"id": "1", "text": "a?"}])
    ruling = post(client, ruling_holder, body="the ruling", title="ruling")

    # A stranger's bare resolved reply does not close — and since 2026-08-23
    # it is not even accepted: it would settle nothing, so the hub refuses it
    # at the door WITH the gesture that works, instead of taking the post and
    # voiding it in silence.
    noop = client.post("/channels/room/messages", headers=memory,
                       json={"body": "closing?", "status": "resolved",
                             "reply_to": q["id"]})
    assert noop.status_code == 400
    assert "flow" in noop.json()["detail"]         # names who may close it
    # ...and what to do INSTEAD, computed for this row: flow's ask names
    # nobody, so `memory` may answer it, and that is the gesture offered.
    assert 'answers=["1"]' in noop.json()["detail"]
    client.post("/inbox/ack", json={"cursors": {"room": 10_000}},
                headers=ruling_holder)
    assert q["seq"] in inbox_seqs(client, ruling_holder)   # sticky despite ack

    # An invalid pointer is still refused loudly, and now so is a VALID one
    # from someone who is neither the asker nor an operator.
    bad = client.post("/channels/room/messages", headers=memory,
                      json={"body": "x", "status": "resolved", "reply_to": q["id"],
                            "data": {"settled_by": "01NOTAREALID"}})
    assert bad.status_code == 400
    refused = client.post("/channels/room/messages", headers=memory,
                          json={"body": "settled by the ruling",
                                "status": "resolved", "reply_to": q["id"],
                                "data": {"settled_by": ruling["id"]}})
    assert refused.status_code == 403
    assert "flow" in refused.json()["detail"]      # names who may close it
    client.post("/inbox/ack", json={"cursors": {"room": 10_000}},
                headers=ruling_holder)
    assert q["seq"] in inbox_seqs(client, ruling_holder)

    # The ASKER closes their own question, and it closes everywhere.
    post(client, flow, body="settled by the ruling", status="resolved",
         reply_to=q["id"], data={"settled_by": ruling["id"]})
    client.post("/inbox/ack", json={"cursors": {"room": 10_000}},
                headers=ruling_holder)
    assert q["seq"] not in inbox_seqs(client, ruling_holder)


def test_operator_resolved_reply_closes():
    client = make_client()
    flow, op, member = (register(client, "flow"),
                        register(client, "op", operator=True),
                        register(client, "member"))
    make_channel(client, flow, "room", op, member)
    q = post(client, flow, body="q", title="q", status="open",
             asks=[{"id": "1", "text": "a?"}])
    post(client, op, body="operator closes", status="resolved", reply_to=q["id"])
    client.post("/inbox/ack", json={"cursors": {"room": 10_000}}, headers=member)
    assert q["seq"] not in inbox_seqs(client, member)


def test_non_delegate_settled_by_on_operator_request_is_refused_not_crashed():
    client = make_client()
    flow, op, member, closer = (register(client, "flow"),
                                register(client, "op", operator=True),
                                register(client, "member"),
                                register(client, "closer"))
    make_channel(client, flow, "room", op, member, closer)
    q = post(client, op, body="operator question", title="q", status="open",
             asks=[{"id": "1", "text": "a?"}])
    ruling = post(client, member, body="settled elsewhere", title="ruling")

    r = client.post("/channels/room/messages", headers=closer,
                    json={"body": "closing with pointer",
                          "status": "resolved",
                          "reply_to": q["id"],
                          "data": {"settled_by": ruling["id"]}})
    assert r.status_code == 403
    assert "reporting delegate" in r.json()["detail"]


# -- 0062: teaching refusals (the c817 / c1113 shapes) ----------------------------

def test_answers_to_own_asks_are_refused_with_the_right_gesture():
    client = make_client()
    flow, memory = register(client, "flow"), register(client, "memory")
    make_channel(client, flow, "room", memory)
    q = post(client, flow, body="q", title="q", status="open",
             asks=[{"id": "2", "text": "b?"}])
    r = client.post("/channels/room/messages", headers=flow,
                    json={"body": "bookkeeping close", "status": "reply",
                          "reply_to": q["id"], "answers": ["2"]})
    assert r.status_code == 400 and "status=resolved" in r.json()["detail"]


def test_answers_on_askless_parent_are_refused():
    client = make_client()
    flow, memory = register(client, "flow"), register(client, "memory")
    make_channel(client, flow, "room", memory)
    plain = post(client, flow, body="no asks here", title="fyi")
    r = client.post("/channels/room/messages", headers=memory,
                    json={"body": "x", "status": "reply",
                          "reply_to": plain["id"], "answers": ["1"]})
    assert r.status_code == 400 and "no asks" in r.json()["detail"]


def test_envelope_carries_has_resolved_reply():
    client = make_client()
    flow, memory, other = (register(client, "flow"), register(client, "memory"),
                           register(client, "other"))
    make_channel(client, flow, "room", memory, other)
    q = post(client, flow, body="q", title="q", status="open",
             asks=[{"id": "1", "text": "a?"}, {"id": "2", "text": "b?"}])
    # A resolved reply that discharges ONE of two asks doesn't close, but the
    # signal shows. (Until 2026-08-23 this used a bystander's BARE resolved;
    # the hub now refuses that outright — see `resolved_settles_nothing`.)
    post(client, memory, body="the first one", status="resolved",
         reply_to=q["id"], answers=["1"])
    env = next(e for e in client.get("/inbox", headers=other).json()
               if e["seq"] == q["seq"])
    assert env["has_resolved_reply"] is True


# -- 0066: addressed-scoped stickiness + reply-records-receipt --------------------

def test_addressed_obligations_pin_only_addressees():
    client = make_client()
    flow, uic, bystander = (register(client, "flow"), register(client, "uic"),
                            register(client, "bystander"))
    make_channel(client, flow, "room", uic, bystander)
    q = post(client, flow, body="for uic", title="q", status="open",
             to=["uic"], asks=[{"id": "1", "text": "a?"}])

    # Both see it once (cursor flow)...
    assert q["seq"] in inbox_seqs(client, uic)
    assert q["seq"] in inbox_seqs(client, bystander)
    # ...but after acking, only the addressee stays pinned.
    for h in (uic, bystander):
        client.post("/inbox/ack", json={"cursors": {"room": q["seq"]}}, headers=h)
    assert q["seq"] in inbox_seqs(client, uic)
    assert q["seq"] not in inbox_seqs(client, bystander)

    # Broadcast obligations keep pinning everyone.
    b = post(client, flow, body="for the room", title="broadcast", status="open",
             asks=[{"id": "1", "text": "anyone?"}])
    client.post("/inbox/ack", json={"cursors": {"room": b["seq"]}}, headers=bystander)
    assert b["seq"] in inbox_seqs(client, bystander)


def test_newcomer_does_not_inherit_addressed_asks():
    client = make_client()
    flow, uic = register(client, "flow"), register(client, "uic")
    make_channel(client, flow, "room", uic)
    q = post(client, flow, body="for uic", title="q", status="open",
             to=["uic"], asks=[{"id": "1", "text": "a?"}])
    late = register(client, "late")
    invite = client.post("/channels/room/invites", json={},
                         headers=flow).json()["invite_token"]
    client.post("/channels/room/join", json={"invite_token": invite}, headers=late)
    assert q["seq"] not in inbox_seqs(client, late)


def test_replying_records_receipt_and_drops_own_pin():
    """Gateway's case (c1101): an addressee who answered from the inlined
    envelope — never calling read_message — must stop being re-pinned."""
    client = make_client()
    flow, uic = register(client, "flow"), register(client, "uic")
    make_channel(client, flow, "room", uic)
    q = post(client, flow, body="two asks for uic", title="q", status="open",
             to=["uic"], asks=[{"id": "1", "text": "a?"}, {"id": "2", "text": "b?"}])
    # Partial answer: obligation NOT discharged globally, but uic replied —
    # the receipt drops uic's own pin.
    post(client, uic, body="answering 1", status="reply", reply_to=q["id"],
         answers=["1"])
    client.post("/inbox/ack", json={"cursors": {"room": 10_000}}, headers=uic)
    assert q["seq"] not in inbox_seqs(client, uic)
    # Still open in the digest (ask 2 pending) — closure was NOT faked.
    digest = client.get("/channels/room/digest", headers=flow).json()
    assert digest["counts"]["open_questions"] == 1


# -- review fixes: smuggling, criticals, privacy, fallbacks ------------------------

def test_settled_by_smuggling_matrix():
    """The supersession pointer must be unusable anywhere but an authoritative
    resolved reply naming a real, OTHER message in the same channel."""
    client = make_client()
    flow, memory = register(client, "flow"), register(client, "memory")
    make_channel(client, flow, "room", memory)
    q = post(client, flow, body="q", title="q", status="open",
             asks=[{"id": "1", "text": "a?"}])
    other_q = post(client, memory, body="elsewhere", title="x")

    def attempt(**kw):
        return client.post("/channels/room/messages", headers=memory, json=kw)

    # On a plain reply (not resolved): refused.
    assert attempt(body="x", status="reply", reply_to=q["id"],
                   data={"settled_by": other_q["id"]}).status_code == 400
    # On a resolved NON-reply: refused.
    assert attempt(body="x", status="resolved",
                   data={"settled_by": other_q["id"]}).status_code == 400
    # Pointing at the question itself: refused (bare claim, review MED-2).
    assert attempt(body="x", status="resolved", reply_to=q["id"],
                   data={"settled_by": q["id"]}).status_code == 400
    # Empty answers list: refused (review LOW-4).
    assert attempt(body="x", status="reply", reply_to=q["id"],
                   answers=[]).status_code == 400


def test_replying_to_a_critical_does_not_unpin_it():
    """Criticals are pinned until deliberately READ — a scripted reply must
    not become a side door around forced attention (review MED-1)."""
    client = make_client()
    op = register(client, "op", operator=True)
    member = register(client, "member")
    make_channel(client, op, "room", member)
    c = post(client, op, body="stop everything", title="crit", critical=True)
    post(client, member, body="acknowledged", status="reply", reply_to=c["id"])
    client.post("/inbox/ack", json={"cursors": {"room": 10_000}}, headers=member)
    assert c["seq"] in inbox_seqs(client, member)      # still pinned
    client.get(f"/channels/room/messages/{c['id']}", headers=member)  # read it
    assert c["seq"] not in inbox_seqs(client, member)  # now cleared


def test_hub_alerts_name_is_reserved_and_channel_private():
    client = make_client()
    agent = register(client, "sneaky")
    squat = client.post("/channels", json={"name": "hub-alerts"}, headers=agent)
    assert squat.status_code == 400 and "reserved" in squat.json()["detail"]

    service = client.app.state.service
    service._ensure_alerts_channel()
    ch = service.db.get_channel("hub-alerts")
    assert ch is not None and ch.private is True


def test_addressee_leaving_reverts_obligation_to_broadcast():
    """An addressed obligation whose only addressee left must not become
    invisible (review MED-3): it falls back to pinning everyone."""
    client = make_client()
    flow, uic, bystander = (register(client, "flow"), register(client, "uic"),
                            register(client, "bystander"))
    make_channel(client, flow, "room", uic, bystander)
    q = post(client, flow, body="for uic", title="q", status="open",
             to=["uic"], asks=[{"id": "1", "text": "a?"}])
    client.post("/inbox/ack", json={"cursors": {"room": 10_000}}, headers=bystander)
    assert q["seq"] not in inbox_seqs(client, bystander)   # scoped away
    client.post("/channels/room/leave", headers=uic)
    assert q["seq"] in inbox_seqs(client, bystander)       # fallback: visible again


# -- 0067: dark-episode operator alerts -------------------------------------------

def test_operator_directive_reply_obliges_the_addressee():
    """0101 (operator: 'a reply, you must answer too'): an operator's
    ADDRESSED reply carrying a directive is an obligation the addressee owes
    — it appears in /owed, pins in the inbox, and clears when the addressee
    engages. Replies normally oblige nobody; this is the narrow operator
    exception so a human order in-thread never silently drops."""
    client = make_client()
    op = register(client, "op", operator=True)
    code = register(client, "code")
    make_channel(client, op, "room", code)
    # code posts a report (fyi), the operator replies with a DIRECTIVE.
    report = post(client, code, body="benchmark done", status="fyi")
    directive = post(client, op, body="redo it properly", status="reply",
                     to=["code"], reply_to=report["id"])

    owed = client.get("/owed", headers=code).json()
    assert any(o["id"] == directive["id"] for o in owed["to_answer"]), \
        "operator directive-reply must be an owed obligation"
    inbox_ids = [e["id"] for e in client.get("/inbox", headers=code).json()]
    assert directive["id"] in inbox_ids  # pinned

    # code engages (replies): the obligation clears.
    post(client, code, body="on it", status="reply", to=["op"],
         reply_to=directive["id"])
    owed = client.get("/owed", headers=code).json()
    assert not any(o["id"] == directive["id"] for o in owed["to_answer"])


def test_peer_reply_to_your_own_message_does_not_oblige_you():
    """0102 consumption exemption: a peer's reply TO YOUR OWN message is
    their answer/commentary coming back to you — your debt is consumption
    (0078), never another reply. This is also the mechanical terminator:
    without it every 'thanks' would oblige a 'you're welcome' forever."""
    client = make_client()
    flow = register(client, "flow")
    code = register(client, "code")
    make_channel(client, flow, "room", code)
    report = post(client, code, body="report", status="fyi")
    peer_reply = post(client, flow, body="nice, also try X", status="reply",
                      to=["code"], reply_to=report["id"])
    owed = client.get("/owed", headers=code).json()
    assert not any(o["id"] == peer_reply["id"] for o in owed["to_answer"])


def test_peer_addressed_reply_elsewhere_obliges_the_named_seat():
    """0102 ('a reply is not mandatory' MUST be false): a peer reply that
    NAMES you — and is not the answer to your own message — is a debt: it
    lands in /owed, pins in the inbox, and clears when YOU engage."""
    client = make_client()
    flow = register(client, "flow")
    code = register(client, "code")
    uic = register(client, "uic")
    make_channel(client, flow, "room", code, uic)
    base = post(client, flow, body="thread root", status="fyi")
    directive = post(client, uic, body="code: please rerun the suite",
                     status="reply", to=["code"], reply_to=base["id"])
    owed = client.get("/owed", headers=code).json()
    assert any(o["id"] == directive["id"] for o in owed["to_answer"])
    assert directive["id"] in [e["id"] for e in
                               client.get("/inbox", headers=code).json()]
    # code engages: the debt clears.
    post(client, code, body="rerun green", status="reply", to=["uic"],
         reply_to=directive["id"])
    owed = client.get("/owed", headers=code).json()
    assert not any(o["id"] == directive["id"] for o in owed["to_answer"])


def test_peer_addressed_fyi_without_tag_does_not_oblige():
    """An untagged peer fyi stays terminal — DMs auto-address every post, so
    plain addressed fyi must still be able to end a thread."""
    client = make_client()
    flow = register(client, "flow")
    code = register(client, "code")
    make_channel(client, flow, "room", code)
    base = post(client, flow, body="root", status="fyi")
    fyi = post(client, code, body="fyi, closing note", status="fyi",
               to=["flow"], reply_to=base["id"])
    owed = client.get("/owed", headers=flow).json()
    assert not any(o["id"] == fyi["id"] for o in owed["to_answer"])


def test_tagged_peer_fyi_is_visible_but_not_owed():
    """A tagged peer fyi targets visibility, not reply debt."""
    client = make_client()
    flow = register(client, "flow")
    code = register(client, "code")
    make_channel(client, flow, "room", code)
    base = post(client, flow, body="root", status="fyi")
    fyi = post(client, code, body="fyi for @flow: please confirm", status="fyi",
               reply_to=base["id"])
    inbox = client.get("/inbox", headers=flow).json()
    assert any(e["id"] == fyi["id"] and e["to_me"] for e in inbox)
    owed = client.get("/owed", headers=flow).json()
    assert not any(o["id"] == fyi["id"] for o in owed["to_answer"])


def test_multi_addressee_directive_each_seat_owes_its_own_engagement():
    """0102 free-rider fix: a directive naming TWO seats stays a debt for
    the silent one after the other replies — engagement is per-addressee,
    not per-thread."""
    client = make_client()
    op = register(client, "op", operator=True)
    code = register(client, "code")
    uic = register(client, "uic")
    make_channel(client, op, "room", code, uic)
    report = post(client, code, body="report", status="fyi")
    directive = post(client, op, body="both of you: verify on your side",
                     status="reply", to=["code", "uic"], reply_to=report["id"])
    # code engages; uic stays silent.
    post(client, code, body="verified mine", status="reply", to=["op"],
         reply_to=directive["id"])
    owed_code = client.get("/owed", headers=code).json()
    owed_uic = client.get("/owed", headers=uic).json()
    assert not any(o["id"] == directive["id"] for o in owed_code["to_answer"])
    assert any(o["id"] == directive["id"] for o in owed_uic["to_answer"]), \
        "another addressee's reply must not clear YOUR debt"
    # And it still pins uic's inbox while code's is clear.
    assert directive["id"] in [e["id"] for e in
                               client.get("/inbox", headers=uic).json()]


def test_operator_addressed_fyi_is_visible_and_owed():
    """An operator's addressed line obliges WHATEVER its status (ruling
    2026-07-19: 'it MUST be'). Humans are allowed to be sloppy about status —
    a directive typed as `fyi` still owes the named seat's engagement, and a
    peer's fyi still obliges nobody (see `_is_addressed_debt`)."""
    client = make_client()
    op = register(client, "op", operator=True)
    code = register(client, "code")
    make_channel(client, op, "room", code)
    note = post(client, op, body="tomorrow: migrate the boards", status="fyi",
                to=["code"])
    inbox = client.get("/inbox", headers=code).json()
    assert any(e["id"] == note["id"] and e["to_me"] for e in inbox)
    owed = client.get("/owed", headers=code).json()
    assert any(o["id"] == note["id"] for o in owed["to_answer"])
    # The addressee's own reply clears it, like any directive debt.
    post(client, code, body="noted — will migrate", status="reply",
         to=["op"], reply_to=note["id"])
    owed = client.get("/owed", headers=code).json()
    assert not any(o["id"] == note["id"] for o in owed["to_answer"])


def test_retired_operator_excluded_from_operator_ids():
    """c3436 HOLE 7 (defense-in-depth): the service guards against retiring
    an operator, but if a retirement ever lands on an operator row,
    list_operator_ids must exclude it — a decommissioned operator must keep
    neither closure authority nor unbounded directive-debt minting."""
    client = make_client()
    register(client, "op", operator=True)
    service = client.app.state.service
    assert "op" in service.db.list_operator_ids()
    service.db.retire_agent("op", "decommissioned")  # direct DB (service 403s this)
    assert "op" not in service.db.list_operator_ids()




def test_directive_debt_cleared_by_authoritative_closure():
    """0102: a resolved reply from someone with closure authority (here the
    directive's own sender) settles the debt without the addressee — the
    thread is closed, nothing is owed into a closed thread."""
    client = make_client()
    op = register(client, "op", operator=True)
    code = register(client, "code")
    make_channel(client, op, "room", code)
    report = post(client, code, body="report", status="fyi")
    directive = post(client, op, body="do X", status="reply",
                     to=["code"], reply_to=report["id"])
    assert any(o["id"] == directive["id"] for o in
               client.get("/owed", headers=code).json()["to_answer"])
    post(client, op, body="superseded, stand down", status="resolved",
         reply_to=directive["id"])
    owed = client.get("/owed", headers=code).json()
    assert not any(o["id"] == directive["id"] for o in owed["to_answer"])


def test_directive_debts_are_epoch_bounded_for_every_sender():
    """0102 hardening (c3379, generalized c3436): a directive posted BEFORE
    this hub learned the directive-debt semantics must not become a debt
    retroactively — for EVERY sender, operator included. The morning after
    0.12.19 seats woke to phantom debts from weeks-old settled traffic; the
    0.12.20 operator carve-out then resurfaced weeks-old and FORGED operator
    DMs, so the operator ruled 'no more surfacing old requests already
    emitted and treated' (dm#42). A debt can never be older than the rule
    that created it; a pre-epoch directive that still matters is re-emitted."""
    client = make_client()
    op = register(client, "op", operator=True)
    flow = register(client, "flow")
    code = register(client, "code")
    make_channel(client, flow, "room", code, op)
    base = post(client, flow, body="root", status="fyi")
    old_peer = post(client, code, body="flow: check this", status="reply",
                    to=["flow"], reply_to=base["id"])
    old_op = post(client, op, body="flow: old directive", status="reply",
                  to=["flow"], reply_to=base["id"])
    # Rewind both posts to before the service's epoch.
    service = client.app.state.service
    service.db._conn.execute(
        "UPDATE messages SET created_at = created_at - 86400 WHERE id IN (?,?)",
        (old_peer["id"], old_op["id"]))
    service.db._conn.commit()
    owed_ids = [o["id"] for o in
                client.get("/owed", headers=flow).json()["to_answer"]]
    assert old_peer["id"] not in owed_ids, "pre-epoch peer reply must not oblige"
    assert old_op["id"] not in owed_ids, \
        "pre-epoch OPERATOR directive must not oblige either (c3436 ruling)"

    # But a POST-epoch operator directive still obliges — the feature works
    # for everything born after the rule.
    fresh_op = post(client, op, body="flow: do this now", status="reply",
                    to=["flow"], reply_to=base["id"])
    owed_ids = [o["id"] for o in
                client.get("/owed", headers=flow).json()["to_answer"]]
    assert fresh_op["id"] in owed_ids, "post-epoch operator directive must oblige"


def test_operator_key_burst_raises_one_alert():
    """0104 (the Jul-14 forgery): 13 DMs in 10s under the operator's cached
    key impersonated the human and nothing flagged it. Machine cadence on a
    human key now raises ONE loud hub-alerts alert per episode; a human-
    paced operator (or any peer burst) never trips it."""
    client = make_client()
    op = register(client, "op", operator=True)
    peer = register(client, "flow")
    make_channel(client, op, "room", peer)
    service = client.app.state.service

    # A peer burst never trips the operator tripwire.
    for i in range(8):
        post(client, peer, body=f"peer {i}", status="fyi")
    assert service.db.get_channel("hub-alerts") is None or not any(
        "OPERATOR-KEY BURST" in m.body
        for m in service.db.get_messages("hub-alerts", 0, 50))

    # Five operator posts: under threshold, silent.
    for i in range(5):
        post(client, op, body=f"op {i}", status="fyi")
    alerts = (service.db.get_messages("hub-alerts", 0, 50)
              if service.db.get_channel("hub-alerts") else [])
    assert not any("OPERATOR-KEY BURST" in m.body for m in alerts)

    # The sixth inside the window trips it — exactly once, even if the
    # burst continues (episode cooldown).
    for i in range(4):
        post(client, op, body=f"blast {i}", status="fyi")
    alerts = service.db.get_messages("hub-alerts", 0, 50)
    hits = [m for m in alerts if "OPERATOR-KEY BURST" in m.body]
    assert len(hits) == 1
    assert "machine cadence" in hits[0].body


def test_directive_debt_escalates_past_sla():
    """0102: an ignored directive rots on the same SLA clock as an
    unanswered question — envelope.escalated flips, which is what feeds
    the deaf/dark watchdogs."""
    client = make_client()
    op = register(client, "op", operator=True)
    code = register(client, "code")
    make_channel(client, op, "room", code)
    client.put("/channels/room/store/channel:meta",
               json={"value": {"response_sla_minutes": 0.001}}, headers=op)
    report = post(client, code, body="report", status="fyi")
    directive = post(client, op, body="do X now", status="reply",
                     to=["code"], reply_to=report["id"])
    time.sleep(0.2)
    env = [e for e in client.get("/inbox", headers=code).json()
           if e["id"] == directive["id"]]
    assert env and env[0]["escalated"] is True


def test_operator_reply_carrying_an_answer_does_not_oblige():
    """0101: an operator reply that DISCHARGES an ask (answers=[...]) is an
    answer, not a directive — it obliges nobody."""
    client = make_client()
    op = register(client, "op", operator=True)
    code = register(client, "code")
    make_channel(client, op, "room", code)
    # code asks the operator; the operator answers.
    ask = post(client, code, body="which model?", title="q", status="open",
               to=["op"], asks=[{"id": "1", "text": "which?"}])
    answer = post(client, op, body="use auto", status="reply", to=["code"],
                  reply_to=ask["id"], answers=["1"])
    owed = client.get("/owed", headers=code).json()
    assert not any(o["id"] == answer["id"] for o in owed["to_answer"])


def test_deaf_sweep_alerts_when_present_seat_stops_arming():
    """0098: a seat that LOOKS present (recent session activity) but whose
    reception loop went silent while it holds escalated addressed work is
    DEAF — it wakes for nothing. The watchdog must alarm it (AGENT DEAF),
    once per episode, distinctly from AGENT DARK (offline)."""
    client = make_client()
    flow = register(client, "flow")
    register(client, "op", operator=True)
    deaf = register(client, "uic")
    make_channel(client, flow, "room", deaf)
    client.put("/channels/room/store/channel:meta",
               json={"value": {"response_sla_minutes": 0.001}}, headers=flow)
    post(client, flow, body="for uic", title="q", status="open", to=["uic"],
         asks=[{"id": "1", "text": "a?"}])
    time.sleep(0.2)  # cross the SLA

    service = client.app.state.service
    # uic LOOKS present: keep its session activity fresh (NOT offline) but
    # make its reception loop stale — it was arming, then the listener died.
    service.presence.touch("uic")
    service.presence._last_reception["uic"] = (
        time.time() - _RECEPTION_STALE - 100.0)   # anchored, never a magic number

    assert service.dark_sweep() == ["uic"]      # DEAF, not DARK
    assert service.dark_sweep() == []           # same episode: no duplicate
    op2 = register(client, "op2", operator=True)
    service.dark_sweep()
    msgs = client.get("/channels/hub-alerts/messages", headers=op2).json()
    assert any("AGENT DEAF: uic" in m["body"] for m in msgs)
    assert any("silence_class=deaf" in m["body"] for m in msgs)
    assert not any("AGENT DARK: uic" in m["body"] for m in msgs)

    # An armed reception loop is NOT deaf: recovery ends the episode.
    service.presence.mark_reception("uic")
    assert service.dark_sweep() == []
    assert "uic" not in service._deaf_since


def test_silence_watchdog_alert_addresses_reporting_steward():
    """0114/0107: DARK/DEAF/LURK alerts tag silence_class and address stewards."""
    client = make_client()
    flow = register(client, "flow")
    op = register(client, "op", operator=True)
    steward = register(client, "steward")
    target = register(client, "uic")
    make_channel(client, flow, "room", target, steward)
    client.put("/admin/delegation",
               json={"agent_id": "steward", "powers": ["reporting"]},
               headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    client.put("/channels/room/store/channel:meta",
               json={"value": {"response_sla_minutes": 0.001}}, headers=flow)
    post(client, flow, body="for uic", title="q", status="open", to=["uic"],
         asks=[{"id": "1", "text": "a?"}])
    time.sleep(0.2)
    service = client.app.state.service
    service.presence.touch("uic")
    service.presence._last_reception["uic"] = (
        time.time() - _RECEPTION_STALE - 100.0)
    service.dark_sweep()
    msgs = client.get("/channels/hub-alerts/messages", headers=op).json()
    deaf = [m for m in msgs if "AGENT DEAF: uic" in m["body"]]
    assert len(deaf) == 1
    assert "silence_class=deaf" in deaf[0]["body"]
    assert "steward" in deaf[0]["to"]


def test_history_rows_carry_viewer_read_state():
    """agora-0130 (the dm#151 burst-skip class): the operator's cursor swept
    46 messages he never opened — including a shipped-feature receipt — and
    no client could tell, because the hub stored reads but served them
    nowhere. History rows now carry the VIEWER's own read receipt:
    `cursor >= seq AND read == False` is the acked-but-never-read badge.
    Read state is viewer-scoped (never leaks) and null on own messages."""
    client = make_client()
    flow, memory = register(client, "flow"), register(client, "memory")
    make_channel(client, flow, "room", memory)
    m1 = post(client, flow, body="receipt you never opened", title="r1")
    m2 = post(client, flow, body="the one you actually read", title="r2")

    # memory deliberately reads ONLY m2, then acks past BOTH (the sweep).
    client.get(f"/channels/room/messages/{m2['id']}", headers=memory)
    client.post("/inbox/ack", json={"cursors": {"room": m2["seq"]}},
                headers=memory)

    rows = {r["seq"]: r for r in
            client.get("/channels/room/messages", headers=memory).json()}
    assert rows[m1["seq"]]["read"] is False      # acked past, never opened
    assert rows[m2["seq"]]["read"] is True       # deliberate read receipt
    # The author's own view: null on own messages (authorship needs no
    # reading), and flow has read nothing of memory's — nothing leaks.
    frows = {r["seq"]: r for r in
             client.get("/channels/room/messages", headers=flow).json()}
    assert frows[m1["seq"]]["read"] is None and frows[m2["seq"]]["read"] is None
    # Viewer isolation: memory's receipts are invisible to a third seat.
    uic = register(client, "uic")
    make_channel(client, flow, "room2")  # uic not in room: fetch as member instead
    invite = client.post("/channels/room/invites", json={},
                         headers=flow).json()["invite_token"]
    client.post("/channels/room/join", json={"invite_token": invite}, headers=uic)
    urows = {r["seq"]: r for r in
             client.get("/channels/room/messages", headers=uic).json()}
    assert urows[m2["seq"]]["read"] is False     # memory's read is not uic's


def test_lurk_sweep_alerts_when_armed_seat_never_triages():
    """RC-3 (2026-07-23 fleet blackout): reception ARMED and heartbeating,
    yet the model never triages — obligations rot UNREAD far past SLA while
    the DEAF leg sees a healthy pulse. For two days every sweep stayed
    silent in exactly this state. The watchdog must name it (AGENT
    LURKING), once per episode, and never fire DEAF for it."""
    client = make_client()
    flow = register(client, "flow")
    register(client, "op", operator=True)
    uic = register(client, "uic")
    make_channel(client, flow, "room", uic)
    client.put("/channels/room/store/channel:meta",
               json={"value": {"response_sla_minutes": 0.001}}, headers=flow)
    ask = post(client, flow, body="for uic", title="q", status="open",
               to=["uic"], asks=[{"id": "1", "text": "a?"}])
    time.sleep(0.3)  # >> 2x the 0.001-minute SLA: well past breach

    service = client.app.state.service
    service.presence.touch("uic")
    service.presence.mark_reception("uic")          # listener heartbeating
    assert service.presence.reception("uic")[0] == "armed"

    # Two-observation rule: the first sweep only RECORDS the candidate (a
    # just-re-armed seat gets a cycle to catch up); it alerts once the
    # state persists past the confirm window.
    assert service.dark_sweep() == []               # candidate recorded
    assert "uic" in service._lurk_since
    service._lurk_since["uic"] -= 601.0             # persist past confirm
    assert service.dark_sweep() == ["uic"]          # LURKING, once
    assert service.dark_sweep() == []               # same episode: silent
    op2 = register(client, "op2", operator=True)
    service.dark_sweep()
    msgs = client.get("/channels/hub-alerts/messages", headers=op2).json()
    assert any("AGENT LURKING: uic" in m["body"] for m in msgs)
    assert not any("AGENT DEAF: uic" in m["body"] for m in msgs)

    # The seat finally answers: the debt clears and the episode ends.
    post(client, uic, body="a!", status="reply", reply_to=ask["id"],
         answers=["1"])
    assert service.dark_sweep() == []
    assert "uic" not in service._lurk_since and "uic" not in service._lurk_alerted


def test_reception_unknown_is_never_alarmed():
    """0098: a seat that never announced a reception heartbeat (drives
    reception another way, or predates the feature) reads 'unknown' — the
    absence of the signal must NOT be treated as deafness."""
    client = make_client()
    flow = register(client, "flow")
    register(client, "op", operator=True)
    quiet = register(client, "uic")
    make_channel(client, flow, "room", quiet)
    client.put("/channels/room/store/channel:meta",
               json={"value": {"response_sla_minutes": 0.001}}, headers=flow)
    post(client, flow, body="for uic", title="q", status="open", to=["uic"],
         asks=[{"id": "1", "text": "a?"}])
    time.sleep(0.2)

    service = client.app.state.service
    service.presence.touch("uic")  # present, but reception NEVER announced
    state, age = service.presence.reception("uic")
    assert state == "unknown" and age is None
    assert service.dark_sweep() == []            # unknown != deaf


def test_reception_marked_by_owed_header():
    """0098: the /owed poll carrying X-Agora-Reception marks the seat armed;
    a plain /owed read does not."""
    client = make_client()
    uic = register(client, "uic")
    # Plain read: no reception mark.
    client.get("/owed", headers=uic)
    assert client.app.state.service.presence.reception("uic")[0] == "unknown"
    # Reception poll: armed.
    client.get("/owed", headers={**uic, "X-Agora-Reception": "arm"})
    assert client.app.state.service.presence.reception("uic")[0] == "armed"


def test_dark_sweep_alerts_operator_once_per_episode():
    client = make_client()
    flow = register(client, "flow")
    register(client, "op", operator=True)
    dark = register(client, "uic")
    make_channel(client, flow, "room", dark)
    # Tiny SLA so the obligation escalates immediately.
    client.put("/channels/room/store/channel:meta",
               json={"value": {"response_sla_minutes": 0.001}}, headers=flow)
    post(client, flow, body="for uic", title="q", status="open", to=["uic"],
         asks=[{"id": "1", "text": "a?"}])
    time.sleep(0.2)  # cross the SLA

    service = client.app.state.service
    # uic's setup calls marked it 'active'; simulate the activity window
    # having passed (the real criterion is presence state, computed from
    # last_seen — dropping the record is equivalent to 10 quiet minutes).
    service.presence._last_seen.pop("uic", None)
    assert service.dark_sweep() == ["uic"]      # first pass alerts
    assert service.dark_sweep() == []           # same episode: no duplicate

    # The alert landed as a system message in hub-alerts; operators are
    # members (added on sweep), so the operator can read it.
    op_headers = register(client, "op2", operator=True)
    service.dark_sweep()  # re-ensures membership for the late operator
    r = client.get("/channels/hub-alerts/messages", headers=op_headers)
    assert r.status_code == 200
    assert any("AGENT DARK: uic" in m["body"] for m in r.json())


def test_escalation_rewake_re_emits_notify_once_per_sla_band(tmp_path):
    """0106: hub re-delivers escalated debts into notify files so listeners
    see the escalated flag; deduped per band; suppressed once DARK owns seat."""
    import json

    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0, dark_watch_seconds=0,
                     notify_dir=str(tmp_path / "notify"))
    client = TestClient(app)
    flow = register(client, "flow")
    uic = register(client, "uic")
    make_channel(client, flow, "room", uic)
    client.put("/channels/room/store/channel:meta",
               json={"value": {"response_sla_minutes": 0.001}}, headers=flow)
    post(client, flow, body="for uic", title="q", status="open", to=["uic"],
         asks=[{"id": "1", "text": "a?"}])
    # Armed + present: not DARK/DEAF — the 0106 target class.
    client.get("/owed", headers={**uic, "X-Agora-Reception": "arm"})
    time.sleep(0.2)

    service = client.app.state.service
    log = tmp_path / "notify" / "uic-inbox.log"
    assert log.exists()
    n_before_sweep = len(log.read_text().strip().split("\n"))
    assert all("escalated" not in json.loads(line).get("flags", "")
               for line in log.read_text().strip().split("\n"))

    assert service._escalation_rewake_sweep() == ["uic"]
    lines = log.read_text().strip().split("\n")
    assert len(lines) == n_before_sweep + 1
    last = json.loads(lines[-1])
    assert "escalated" in last["flags"]

    assert service._escalation_rewake_sweep() == []  # same band: no storm
    assert len(log.read_text().strip().split("\n")) == len(lines)

    # DARK episode suppresses further re-rings (0107 bound). Going dark means
    # BOTH clocks stop: dropping only `_last_seen` while the reception
    # heartbeat still reads 'armed' is the contradiction the 2026-08-03 audit
    # removed (a listening seat cannot be dark), so the seat is silenced
    # properly here.
    service.presence._last_seen.pop("uic", None)
    service.presence._last_reception.pop("uic", None)
    service.dark_sweep()
    assert "uic" in service._dark_since
    assert service._escalation_rewake_sweep() == []


def test_dropped_wake_re_emits_unread_pre_sla_on_armed_seat(tmp_path, monkeypatch):
    """0106 emit≠process: armed + unread pre-SLA debt gets notify re-emit."""
    from agora.hub import service as hub_service

    monkeypatch.setattr(hub_service, "DROPPED_WAKE_REEMIT_SECONDS", 9999.0)
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0, dark_watch_seconds=0,
                     notify_dir=str(tmp_path / "notify"))
    client = TestClient(app)
    flow = register(client, "flow")
    uic = register(client, "uic")
    make_channel(client, flow, "room", uic)
    client.put("/channels/room/store/channel:meta",
               json={"value": {"response_sla_minutes": 60}}, headers=flow)
    post(client, flow, body="for uic", title="q", status="open", to=["uic"],
         asks=[{"id": "1", "text": "a?"}])
    client.get("/owed", headers={**uic, "X-Agora-Reception": "arm"})

    service = client.app.state.service
    log = tmp_path / "notify" / "uic-inbox.log"
    n_before = len(log.read_text().strip().split("\n"))
    assert service._dropped_wake_sweep() == ["uic"]
    assert len(log.read_text().strip().split("\n")) == n_before + 1
    assert service._dropped_wake_sweep() == []  # deduped until interval

    msg_id = client.get("/owed", headers=uic).json()["to_answer"][0]["id"]
    client.get(f"/channels/room/messages/{msg_id}", headers=uic)
    assert service._dropped_wake_sweep() == []


def test_fleet_liveness_sweep_alerts_once_then_recovers(monkeypatch):
    """0110, denominator repaired 2026-08-04: FLEET DARK measures seats the
    hub OBSERVED live vanishing — never a roster's graveyard. The old
    denominator ("every seat ever registered") held the live hub in chronic
    FLEET DARK (7 live / 50 registered), which silently paused the hourly
    desk digest all through the novel-fleet stall. A cold roster no longer
    alarms; a fleet that armed and then vanished still does."""
    from agora.hub import service as hub_service

    monkeypatch.setattr(hub_service, "FLEET_MIN_ELIGIBLE", 3)
    monkeypatch.setattr(hub_service, "FLEET_DARK_CONFIRM_SECONDS", 0.0)
    client = make_client()
    op = register(client, "op", operator=True)
    a = register(client, "a")
    b = register(client, "b")
    c = register(client, "c")
    service = client.app.state.service
    # Never-observed seats are not a fleet: a cold roster raises nothing.
    assert service._fleet_liveness_sweep() == []

    # The hub observes all three live; a healthy fleet raises nothing.
    for h in (a, b, c):
        client.get("/owed", headers={**h, "X-Agora-Reception": "arm"})
    assert service._fleet_liveness_sweep() == []

    # All three vanish inside the signal window: THAT is a collapse.
    for s in ("a", "b", "c"):
        service.presence._last_reception[s] -= 4000.0
        service.presence._last_seen[s] -= 4000.0
    assert service._fleet_liveness_sweep() == ["fleet-dark"]
    assert service._fleet_liveness_sweep() == []

    client.get("/owed", headers={**a, "X-Agora-Reception": "arm"})
    client.get("/owed", headers={**b, "X-Agora-Reception": "arm"})
    assert service._fleet_liveness_sweep() == ["fleet-recovered"]

    msgs = client.get("/channels/hub-alerts/messages", headers=op).json()
    assert any("FLEET DARK" in m["body"] for m in msgs)
    assert any("FLEET RECOVERED" in m["body"] for m in msgs)

    # ...and NOT mirrored into the operator's DM room. A DM is a conversation
    # the human opened; hub notices posted into it displace the thread they
    # are holding, and a system row has no author who can retract it
    # (operator ruling, 2026-08-22). `hub-alerts` is where the hub speaks.
    dm = client.get("/channels/dm:hub--op/messages", headers=op)
    rows = dm.json() if dm.status_code == 200 else []
    assert not [m for m in rows if isinstance(m, dict)
                and "FLEET" in (m.get("body") or "")]


def test_fleet_liveness_snapshot_on_status(monkeypatch):
    """0110: /status carries aggregate fleet liveness alongside agent rows."""
    from agora.hub import service as hub_service

    monkeypatch.setattr(hub_service, "FLEET_MIN_ELIGIBLE", 3)
    client = make_client()
    op = register(client, "op", operator=True)
    seats = [register(client, s) for s in ("a", "b", "c")]
    for h in seats:  # observed live: the denominator counts these (2026-08-04)
        client.get("/owed", headers={**h, "X-Agora-Reception": "arm"})
    r = client.get("/status", headers=op)
    assert r.status_code == 200
    data = r.json()
    assert "fleet" in data and "agents" in data
    fleet = data["fleet"]
    assert fleet["eligible"] >= 3
    assert fleet["live"] <= fleet["eligible"]
    assert "live_fraction" in fleet
    assert "open_claims" in fleet
    assert "report_digest" not in data


def test_retire_report_digest_rows_closes_legacy_hourly_digest_posts():
    """Legacy timer-owned digest rows are retired instead of re-armed."""
    client = make_client()
    op = register(client, "op", operator=True)
    steward = register(client, "steward")
    client.put("/admin/delegation", headers={"Authorization": f"Bearer {ADMIN_KEY}"},
               json={"agent_id": "steward", "powers": ["reporting"]})
    service = client.app.state.service
    legacy = service.db.insert_message(
        "hub-alerts", "hub",
        kind="system", status="open", urgency="inbox",
        title="hourly digest: desk facts",
        body="legacy digest ask",
        data={"report_digest": True},
        reply_to=None, to=["steward"],
    )
    service.db.meta_set(
        "report:steward",
        "{\"period_start\": 1, \"desk_post_id\": \"" + legacy.id + "\"}",
    )
    assert service._retire_report_digest_rows() == ["retired-report-digest"]
    assert service.db.meta_get("report:steward") == ""
    alerts = client.get("/channels/hub-alerts/messages", headers=op).json()
    assert any(m.get("reply_to") == legacy.id for m in alerts)
    assert service._retire_report_digest_rows() == []


def test_the_closure_tightening_does_not_reopen_history():
    """The epoch, and why it exists. Tightening `_closes` without one reopens
    every thread ever closed by a third-party pointer AT ONCE — each instantly
    SLA-breached, waking the fleet on work that finished weeks ago. This repo
    has that scar: the 2026-08-04 operator tightening reopened 132 ask-less
    messages on 23 seats, some 19 days old, which is why four sibling epochs
    already exist in obligations.py."""
    from agora.hub.obligations import _closes
    from agora.models import Kind, Message, Status

    def m(sender, status="open", data=None, ts=100.0):
        return Message(id=f"x{sender}{ts}", channel="c", seq=1, sender=sender,
                       kind=Kind.message, status=Status(status), title="",
                       body="b", data=data or {}, created_at=ts)

    epoch = 1000.0
    asked = m("alice")
    pointer = {"settled_by": "01SOMEMESSAGE"}
    ops, delegates = frozenset({"boss"}), frozenset({"lead"})

    # Closed by a bystander BEFORE the rule changed: still closed, forever.
    assert _closes(asked, m("carol", "resolved", pointer, ts=500),
                   ops, delegates, epoch) is True
    # The same act after the epoch: refused.
    assert _closes(asked, m("carol", "resolved", pointer, ts=2000),
                   ops, delegates, epoch) is False
    # The asker and the operator are unaffected either side of it.
    assert _closes(asked, m("alice", "resolved", ts=2000), ops, delegates, epoch)
    assert _closes(asked, m("boss", "resolved", ts=2000), ops, delegates, epoch)
    # The delegate keeps its door on an OPERATOR's request, with evidence,
    # and only there (2026-08-01: something must settle a commission whose
    # operator has gone quiet).
    cited = {"settled_by": "01SOMEMESSAGE", "evidence": ["shipped it"]}
    assert _closes(m("boss"), m("lead", "resolved", cited, ts=2000),
                   ops, delegates, epoch) is True
    assert _closes(asked, m("lead", "resolved", cited, ts=2000),
                   ops, delegates, epoch) is False


# -- resolving a THREAD, not one message (operator ruling, 2026-08-22) --------

def test_resolving_a_thread_closes_every_obligation_beneath_it():
    """Measured on a live hub: the operator resolved the WebOS commission at
    04:58 and three seats worked it for hours afterwards. Resolution was
    per-MESSAGE — `replies_to` walks direct children only — so the obligations
    the thread had spawned were never touched."""
    client = make_client()
    boss = register(client, "boss", operator=True)
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, boss, "room", alice, bob)

    root = post(client, boss, body="build the thing", title="task", status="open")
    q1 = post(client, alice, body="which parser?", status="open",
              reply_to=root["id"], to=["bob"])
    post(client, bob, body="and which codec?", status="open",
         reply_to=q1["id"], to=["alice"])
    assert client.get("/owed", headers=alice).json()["to_answer"]
    assert client.get("/owed", headers=bob).json()["to_answer"]

    out = client.post(f"/channels/room/messages/{root['id']}/resolve_thread",
                      json={}, headers=boss)
    assert out.status_code == 200, out.text
    assert len(out.json()["closed"]) == 3
    assert not client.get("/owed", headers=alice).json()["to_answer"]
    assert not client.get("/owed", headers=bob).json()["to_answer"]


def test_a_thread_resolve_reports_claim_rows_and_never_touches_them():
    """The refused temptation. The hub cannot know whether work already done
    should be thrown away, and `store_set` will not write another seat's row
    anyway — but returning success while the claims still read `active` is
    the same lie the operator hit from the other direction."""
    client = make_client()
    boss = register(client, "boss", operator=True)
    alice = register(client, "alice")
    make_channel(client, boss, "room", alice)
    root = post(client, boss, body="build it", title="task", status="open")
    client.put("/channels/room/store/claim:thing", headers=alice,
               json={"value": {"owner": "alice", "status": "active",
                               "source_message_id": root["id"]}})

    out = client.post(f"/channels/room/messages/{root['id']}/resolve_thread",
                      json={}, headers=boss).json()
    assert [c["key"] for c in out["claims"]] == ["claim:thing"]
    row = client.get("/channels/room/store/claim:thing", headers=alice).json()
    assert row["value"]["status"] == "active", "the hub rewrote someone's claim"


def test_a_peer_cannot_close_a_thread_holding_other_seats_questions():
    """Authority is the single-message rule applied to every node, and the
    refusal is all-or-nothing: a partial close reports success over a thread
    that is still alive."""
    client = make_client()
    boss = register(client, "boss", operator=True)
    alice, bob = register(client, "alice"), register(client, "bob")
    make_channel(client, boss, "room", alice, bob)
    root = post(client, alice, body="mine", title="mine", status="open")
    post(client, bob, body="bob's question", status="open", reply_to=root["id"],
         to=["alice"])

    r = client.post(f"/channels/room/messages/{root['id']}/resolve_thread",
                    json={}, headers=alice)
    assert r.status_code == 403 and "bob" in r.json()["detail"]
    # NOTHING closed: bob's question is still owed by alice.
    assert client.get("/owed", headers=alice).json()["to_answer"]
    # The operator can.
    assert client.post(f"/channels/room/messages/{root['id']}/resolve_thread",
                       json={}, headers=boss).status_code == 200


def test_board_and_owed_agree_about_who_is_behind():
    """One fact, one answer. `/owed` releases an addressee who has engaged and
    has no pending ask naming them; `board` re-derived the same question and
    did not, so a seat that had answered sat on the operator's board as
    ESCALATED while `/owed` said it owed nothing — the louder surface being
    the wrong one."""
    client = make_client()
    op = register(client, "op", operator=True)
    a = register(client, "a")
    b = register(client, "b")
    make_channel(client, op, "room", a, b)

    m = post(client, op, body="two lanes", title="mandate", status="open",
             to=["a", "b"],
             asks=[{"id": "1", "text": "a?", "to": ["a"]},
                   {"id": "2", "text": "b?", "to": ["b"]}])
    # `a` answers its own ask; `b` has not answered yet.
    post(client, a, body="acknowledged", status="reply", reply_to=m["id"],
         answers=["1"])

    def pending(headers):
        return {(r["channel"], r["seq"])
                for r in client.get("/board", headers=headers).json()["pending_on_me"]}

    def owed(headers):
        return {(r["channel"], r["seq"])
                for r in client.get("/owed", headers=headers).json()["to_answer"]}

    assert owed(a) == pending(a) == set(), "a answered and is still listed"
    # ...and the seat that has NOT answered is still on both.
    assert ("room", m["seq"]) in owed(b)
    assert ("room", m["seq"]) in pending(b)


def test_a_row_says_whether_THIS_reader_may_close_it():
    """THE SECOND CLOSURE QUESTION (agora-tui, thread-shape-and-panels#189).

    `closed` answers *is this settled*. Nothing answered *may I settle it* —
    so both clients were about to derive a resolve-verb gate from delegation
    scopes read once at load: an authority verdict computed client-side, from
    a grant the hub can narrow or revoke afterwards, with no oracle to catch
    the drift until an operator hits a refusal. The quiet direction is the
    dangerous one — a verb HIDDEN from a seat the hub would have allowed
    suppresses a real act and nobody sees the error.

    Three values, because the reporting delegate's door is real but priced.
    """
    client = make_client()
    op = register(client, "op", operator=True)
    asker, other = register(client, "asker"), register(client, "other")
    make_channel(client, asker, "room", op, other)

    q = post(client, asker, body="q", title="q", status="open",
             asks=[{"id": "1", "text": "a?", "to": ["other"]}])

    def row(who):
        return next(m for m in
                    client.get("/channels/room/messages", headers=who).json()
                    if m["id"] == q["id"])

    assert row(asker)["may_close"] == "yes"     # the asker, always
    assert row(op)["may_close"] == "yes"        # the operator, always
    assert row(other)["may_close"] == "no"      # a peer: not yours to close

    # ...and once it IS closed the field makes no statement, rather than
    # saying "no" — a closed row has no verb to offer and null is the hub's
    # word for silence.
    post(client, asker, body="closing my own", status="resolved",
         reply_to=q["id"])
    assert row(asker)["closed"] is True
    assert row(asker)["may_close"] is None


def test_may_close_follows_the_REAL_ladder_when_authority_is_granted():
    """The field must track the rules rather than restate them: a delegation
    granted AFTER the message was posted flips the verb, with no republish
    and no client reload — which is exactly the staleness a client-side
    derivation could not see."""
    client = make_client()
    op = register(client, "op", operator=True)
    asker, deputy = register(client, "asker"), register(client, "deputy")
    make_channel(client, asker, "room", op, deputy)
    q = post(client, asker, body="q", title="q", status="open")

    def verb(who):
        return next(m for m in
                    client.get("/channels/room/messages", headers=who).json()
                    if m["id"] == q["id"])["may_close"]

    assert verb(deputy) == "no"

    r = client.put("/admin/delegation",
                   json={"agent_id": "deputy", "powers": ["ruling"],
                         "scope": "room"},
                   headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    assert r.status_code == 200, r.text
    assert verb(deputy) == "yes", "a ruling delegate scoped here may close"

    # The negative twin: a grant scoped ELSEWHERE must not leak in. Without
    # this, "yes" could be coming from the mere existence of a delegation.
    other = register(client, "elsewhere-deputy")
    client.post(f"/channels/room/join",
                json={"invite_token": client.post(
                    "/channels/room/invites", json={},
                    headers=asker).json()["invite_token"]}, headers=other)
    client.put("/admin/delegation",
               json={"agent_id": "elsewhere-deputy", "powers": ["ruling"],
                     "scope": "some-other-room"},
               headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    assert verb(other) == "no"


def test_the_reporting_delegate_gets_its_own_word_not_a_bare_yes():
    """`with_evidence` exists because both collapses lie. "no" would hide the
    one exit that seat has on an operator's commission; "yes" would offer a
    bare `resolved` the hub refuses as settling nothing — the accepted-but-
    discharges-nothing shape measured live at agora-wui's six rows."""
    client = make_client()
    op = register(client, "op", operator=True)
    lead = register(client, "lead")
    make_channel(client, op, "room", lead)
    client.put("/admin/delegation",
               json={"agent_id": "lead", "powers": ["reporting"],
                     "scope": "*"},
               headers={"Authorization": f"Bearer {ADMIN_KEY}"})

    commission = post(client, op, body="build it", title="build it",
                      status="open")

    def verb(who):
        return next(m for m in
                    client.get("/channels/room/messages", headers=who).json()
                    if m["id"] == commission["id"])["may_close"]

    assert verb(lead) == "with_evidence"
    assert verb(op) == "yes"

    # And the word is not decorative: the bare `resolved` it warns against is
    # actually refused, so a client that rendered a plain verb here would be
    # offering a button the hub rejects.
    r = client.post("/channels/room/messages",
                    json={"body": "delivered", "status": "resolved",
                          "reply_to": commission["id"]}, headers=lead)
    assert r.status_code == 400, r.text
    assert "evidence" in r.text

    # ...AND THE PREDICTION IS TIED TO THE OUTCOME (agora-tui #192: "a guard
    # whose every input is a constant cannot be surprised"). `_may_close`
    # probes with a HAND-SHAPED evidence ref — a constant standing in for
    # what `_validate_evidence` stamps on a real citation. Nothing made the
    # two agree: if real refs stopped looking like that, the field would
    # quietly answer with the wrong branch and every assertion above would
    # stay green, because they all read the same constant.
    #
    # So the cited report is actually POSTED, through the real evidence
    # validator, and the row must then be closed. Prediction and outcome in
    # one test: `with_evidence` is only true if evidence really does close it.
    client.put("/channels/room/store/decision:built",
               json={"value": {"what": "the thing"}}, headers=lead)
    landmark = post(client, lead, body="here is the artifact", title="built")
    done = client.post(
        "/channels/room/messages",
        json={"body": "delivered", "status": "resolved",
              "reply_to": commission["id"],
              "data": {"settled_by": landmark["id"],
                       "evidence": [{"kind": "store", "ref": "decision:built"}]}},
        headers=lead)
    assert done.status_code == 200, done.text

    row = next(m for m in
               client.get("/channels/room/messages", headers=op).json()
               if m["id"] == commission["id"])
    assert row["closed"] is True, "the priced door did not actually open"
    assert row["closed_by"] == "lead"
    assert row["may_close"] is None      # closed: no verb left to offer


def test_a_history_row_carries_the_verdict_not_just_the_shape():
    """agora-wui, agora-and-wui#9: a client could render "you owe this" but
    never "this is done" — the only authoritative signal was a row's ABSENCE
    from /owed, and a message shown from another room had to have its replies
    fetched or say nothing.

    `has_resolved_reply` is not a substitute: a `resolved` reply can set it
    while closing nothing, so a client rendering "settled" from that flag
    states something the hub did not say.

    THE SHAPE THAT SETS IT MOVED (2026-08-23). This used to use a pure
    bystander's bare `resolved` — which the hub now refuses outright, because
    it settles nothing (`resolved_settles_nothing`). The invariant is
    untouched and so is the reason it matters; what demonstrates it is now a
    PARTIAL discharge, which is the shape a client is most likely to
    misrender: a real answer, really accepted, and the question still open."""
    client = make_client()
    op = register(client, "op", operator=True)
    asker, answerer = register(client, "asker"), register(client, "answerer")
    make_channel(client, asker, "room", op, answerer)

    q = post(client, asker, body="q", title="q", status="open",
             asks=[{"id": "1", "text": "a?", "to": ["answerer"]},
                   {"id": "2", "text": "b?", "to": ["op"]}])

    def row():
        return next(m for m in
                    client.get("/channels/room/messages", headers=asker).json()
                    if m["id"] == q["id"])

    assert row()["closed"] is False and row()["closed_by"] is None

    # One ask of two, answered on a `resolved`: the flag goes true, the
    # verdict does not.
    post(client, answerer, body="mine is done", status="resolved",
         reply_to=q["id"], answers=["1"])
    assert row()["has_resolved_reply"] is True
    assert row()["closed"] is False, "one of two asks closed the question"

    # The operator's word does close it, and the row names who.
    post(client, op, body="ruled", status="resolved", reply_to=q["id"])
    assert row()["closed"] is True
    assert row()["closed_by"] == "op"
