"""Closure semantics (0062/ADR-0003), addressed-scoped stickiness (0066),
and dark-episode alerts (0067) — each test replays a real field incident
from 2026-07-11/12 (channel commons; see backlog items for the forensics).

The invariant under test: an obligation stays loud exactly where it lives
and exactly until it is settled — never louder, never quieter.
"""

import time

from fastapi.testclient import TestClient

from agora.hub.app import create_app

ADMIN_KEY = "test-admin"


def make_client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0, dark_watch_seconds=0)
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


def test_third_party_resolved_needs_settled_by_pointer():
    client = make_client()
    flow, memory, ruling_holder = (register(client, "flow"),
                                   register(client, "memory"),
                                   register(client, "agency"))
    make_channel(client, flow, "room", memory, ruling_holder)
    q = post(client, flow, body="q", title="q", status="open",
             asks=[{"id": "1", "text": "a?"}])
    ruling = post(client, ruling_holder, body="the ruling", title="ruling")

    # A stranger's bare resolved reply does NOT close (needs the audit pointer).
    post(client, memory, body="closing?", status="resolved", reply_to=q["id"])
    client.post("/inbox/ack", json={"cursors": {"room": 10_000}},
                headers=ruling_holder)
    assert q["seq"] in inbox_seqs(client, ruling_holder)   # sticky despite ack

    # An invalid pointer is refused loudly.
    bad = client.post("/channels/room/messages", headers=memory,
                      json={"body": "x", "status": "resolved", "reply_to": q["id"],
                            "data": {"settled_by": "01NOTAREALID"}})
    assert bad.status_code == 400

    # With a valid pointer naming the settling message, it closes everywhere.
    post(client, memory, body="settled by the ruling", status="resolved",
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
             asks=[{"id": "1", "text": "a?"}])
    # A non-authoritative resolved reply doesn't close, but the signal shows.
    post(client, memory, body="fyi resolved elsewhere", status="resolved",
         reply_to=q["id"])
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


def test_peer_addressed_fyi_never_obliges():
    """0102: peer fyi is the terminal gesture — DMs auto-address every post,
    so without a non-obliging status no DM thread could ever end."""
    client = make_client()
    flow = register(client, "flow")
    code = register(client, "code")
    make_channel(client, flow, "room", code)
    base = post(client, flow, body="root", status="fyi")
    fyi = post(client, code, body="fyi, closing note", status="fyi",
               to=["flow"], reply_to=base["id"])
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


def test_operator_addressed_fyi_obliges_too():
    """0102 widening: operator words oblige whatever status the composer
    picked — fyi included. Human words are few and never chatter."""
    client = make_client()
    op = register(client, "op", operator=True)
    code = register(client, "code")
    make_channel(client, op, "room", code)
    note = post(client, op, body="tomorrow: migrate the boards", status="fyi",
                to=["code"])
    owed = client.get("/owed", headers=code).json()
    assert any(o["id"] == note["id"] for o in owed["to_answer"])


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
    service.presence._last_reception["uic"] = time.time() - 1000.0  # > 900s

    assert service.dark_sweep() == ["uic"]      # DEAF, not DARK
    assert service.dark_sweep() == []           # same episode: no duplicate
    op2 = register(client, "op2", operator=True)
    service.dark_sweep()
    msgs = client.get("/channels/hub-alerts/messages", headers=op2).json()
    assert any("AGENT DEAF: uic" in m["body"] for m in msgs)
    assert not any("AGENT DARK: uic" in m["body"] for m in msgs)

    # An armed reception loop is NOT deaf: recovery ends the episode.
    service.presence.mark_reception("uic")
    assert service.dark_sweep() == []
    assert "uic" not in service._deaf_since


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

    # Episode ends when the seat's overdue work clears: the asker closes the
    # thread, so uic no longer holds an escalated obligation.
    q_id = next(m["id"] for m in client.get("/channels/room/messages",
                                            headers=flow).json()
                if m["status"] == "open")
    post(client, flow, body="closing", status="resolved", reply_to=q_id)
    assert service.dark_sweep() == []
    assert "uic" not in service._dark_since
