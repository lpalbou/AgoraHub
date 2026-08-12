"""The anti-lurk mechanics (0077-0080), from the 2026-07-13 field failure:
seats ran compliant reception loops — listen, ack, re-arm — while acting on
nothing. Forensics on the live hub found the mechanical gaps these tests pin:
70 asks in 48h named seats only in prose (flagging nobody), answers to one's
own asks were silently ackable, and read-but-unanswered debt was invisible.

- 0077 per-ask addressing: asks[].to flags and pins the named seats.
- 0078 asker-side consumption: an unread, unfollowed answer to your own ask
  is a visible debt.
- 0079 the owed surface: GET /owed ignores read receipts on purpose.
- 0080 lurk visibility: acked_unanswered in the operator overview.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from agora.hub.app import create_app
from agora.hub.presence import _RECEPTION_STALE

ADMIN_KEY = "test-admin"


@pytest.fixture()
def client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY, rate_per_minute=600.0)
    return TestClient(app)


def _register(client, agent_id):
    r = client.post("/agents", headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                    json={"id": agent_id, "mission": f"seat {agent_id}"})
    assert r.status_code == 200
    return r.json()["api_key"]


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture()
def room(client):
    """A channel with three members: asker, named, bystander."""
    keys = {a: _register(client, a) for a in ("asker", "named", "bystander")}
    client.post("/channels", headers=_auth(keys["asker"]),
                json={"name": "canvass", "private": False})
    for a in ("named", "bystander"):
        client.post("/channels/canvass/join", headers=_auth(keys[a]), json={})
    return keys


def _info(service, agent_id):
    from agora.models import AgentInfo
    return AgentInfo(id=agent_id, name=agent_id)


def _post(client, key, **kw):
    payload = {"title": kw.pop("title", "t"), "body": kw.pop("body", "b"), **kw}
    r = client.post("/channels/canvass/messages", headers=_auth(key), json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _inbox(client, key):
    return client.get("/inbox", headers=_auth(key)).json()


# -- 0077: per-ask addressing ---------------------------------------------------


def test_ask_to_validates_membership_cap_and_self(client, room):
    k = room["asker"]
    r = client.post("/channels/canvass/messages", headers=_auth(k), json={
        "title": "t", "body": "b", "status": "open",
        "asks": [{"id": "1", "text": "x", "to": ["ghost"]}]})
    assert r.status_code == 400 and "non-members" in r.json()["detail"]

    r = client.post("/channels/canvass/messages", headers=_auth(k), json={
        "title": "t", "body": "b", "status": "open",
        "asks": [{"id": "1", "text": "x", "to": ["asker"]}]})
    assert r.status_code == 400 and "yourself" in r.json()["detail"]

    r = client.post("/channels/canvass/messages", headers=_auth(k), json={
        "title": "t", "body": "b", "status": "open",
        "asks": [{"id": "1", "text": "x",
                  "to": ["named", "bystander", "named", "bystander"]}]})
    assert r.status_code == 400 and "max 3" in r.json()["detail"]


def test_ask_named_seat_is_flagged_and_pinned_until_its_ask_is_answered(client, room):
    """Miss B made mechanical: a seat named by an ask gets to_me and the pin,
    the bystander does not — and the pin lifts for the named seat the moment
    ITS ask is answered, even while another seat's ask stays open."""
    msg = _post(client, room["asker"], status="open", title="canvass",
                asks=[{"id": "1", "text": "row for named", "to": ["named"]},
                      {"id": "2", "text": "row for bystander", "to": ["bystander"]}])

    env = next(e for e in _inbox(client, room["named"]) if e["id"] == msg["id"])
    assert env["to_me"] is True                      # flagged despite to=[]
    bys = next(e for e in _inbox(client, room["bystander"]) if e["id"] == msg["id"])
    assert bys["to_me"] is True                      # named by ask 2

    # named answers ITS ask and acks: its pin lifts, bystander's stays.
    _post(client, room["named"], status="reply", reply_to=msg["id"],
          answers=["1"], title="ans", body="done")
    client.post("/inbox/ack", headers=_auth(room["named"]),
                json={"cursors": {"canvass": 99}})
    client.post("/inbox/ack", headers=_auth(room["bystander"]),
                json={"cursors": {"canvass": 99}})
    assert not any(e["id"] == msg["id"] for e in _inbox(client, room["named"]))
    assert any(e["id"] == msg["id"] for e in _inbox(client, room["bystander"]))


# -- 0078 + 0079: owed ledgers ----------------------------------------------------


def test_owed_to_answer_ignores_read_receipts(client, room):
    """The lurk case itself: reading and acking an addressed ask does NOT
    clear the debt — only replying (or closure) does."""
    msg = _post(client, room["asker"], status="open", title="do X",
                asks=[{"id": "1", "text": "please do X", "to": ["named"]}])
    # named reads it (receipt) and acks past it — the classic silent lurk.
    client.get(f"/channels/canvass/messages/{msg['id']}", headers=_auth(room["named"]))
    client.post("/inbox/ack", headers=_auth(room["named"]),
                json={"cursors": {"canvass": 99}})

    owed = client.get("/owed", headers=_auth(room["named"])).json()
    assert owed["counts"]["to_answer"] == 1
    row = owed["to_answer"][0]
    assert row["id"] == msg["id"] and row["asks_naming_you"] == ["1"]

    # bystander owes nothing (the ask names only `named`).
    assert client.get("/owed", headers=_auth(room["bystander"])).json()["counts"]["to_answer"] == 0

    # replying clears it.
    _post(client, room["named"], status="reply", reply_to=msg["id"],
          answers=["1"], title="done", body="X done")
    owed = client.get("/owed", headers=_auth(room["named"])).json()
    assert owed["counts"]["to_answer"] == 0


def test_owed_to_consume_tracks_unread_answers_and_clears(client, room):
    """Miss A made mechanical: an answer to your own ask is a visible debt
    until you read it (receipt) or post later in-thread; an authoritative
    close clears everything."""
    msg = _post(client, room["asker"], status="open", title="my question",
                asks=[{"id": "1", "text": "which shape?"}])
    ans = _post(client, room["named"], status="reply", reply_to=msg["id"],
                answers=["1"], title="shape C", body="evidence...")

    owed = client.get("/owed", headers=_auth(room["asker"])).json()
    assert owed["counts"]["to_consume"] == 1
    row = owed["to_consume"][0]
    assert row["answered_by"] == "named" and row["answer_id"] == ans["id"]

    # Reading the ANSWER (the cheapest honest consumption) clears the debt.
    client.get(f"/channels/canvass/messages/{ans['id']}", headers=_auth(room["asker"]))
    owed = client.get("/owed", headers=_auth(room["asker"])).json()
    assert owed["counts"]["to_consume"] == 0

    # A second answer re-creates debt; a later in-thread post by the asker
    # (e.g. the resolved close) clears it without a read receipt.
    ans2 = _post(client, room["bystander"], status="reply", reply_to=msg["id"],
                 answers=["1"], title="also shape C", body="more evidence")
    assert client.get("/owed", headers=_auth(room["asker"])).json()["counts"]["to_consume"] == 1
    _post(client, room["asker"], status="resolved", reply_to=msg["id"],
          title="consumed: shape C it is", body="adopting C")
    assert client.get("/owed", headers=_auth(room["asker"])).json()["counts"]["to_consume"] == 0


def test_owed_to_close_surfaces_discharged_unclosed_threads(client, room, monkeypatch):
    """0116: fully answered own threads still open/blocked surface in to_close
    (advisory); authoritative closure clears them."""
    monkeypatch.setattr("agora.hub.service.TO_CLOSE_MIN_AGE_SECONDS", 0)
    msg = _post(client, room["asker"], status="open", title="pick a shape",
                asks=[{"id": "1", "text": "which shape?"}])
    ans = _post(client, room["named"], status="reply", reply_to=msg["id"],
                answers=["1"], title="shape C", body="evidence")
    client.get(f"/channels/canvass/messages/{ans['id']}", headers=_auth(room["asker"]))

    owed = client.get("/owed", headers=_auth(room["asker"])).json()
    assert owed["counts"]["to_consume"] == 0
    assert owed["counts"]["to_close"] == 1
    row = owed["to_close"][0]
    assert row["seq"] == msg["seq"] and row["answered_by"] == "named"

    _post(client, room["asker"], status="resolved", reply_to=msg["id"],
          title="consumed: shape C", body="adopting C")
    owed = client.get("/owed", headers=_auth(room["asker"])).json()
    assert owed["counts"]["to_close"] == 0


def test_owed_to_close_waits_while_asks_pending(client, room, monkeypatch):
    """0116: partial answers are waiting_on, not to_close."""
    monkeypatch.setattr("agora.hub.service.TO_CLOSE_MIN_AGE_SECONDS", 0)
    msg = _post(client, room["asker"], status="open", title="two asks",
                asks=[{"id": "1", "text": "a", "to": ["named"]},
                      {"id": "2", "text": "b", "to": ["bystander"]}])
    _post(client, room["named"], status="reply", reply_to=msg["id"],
          answers=["1"], title="a done", body="a")
    owed = client.get("/owed", headers=_auth(room["asker"])).json()
    assert owed["counts"]["to_close"] == 0
    assert any(w["ask"] == "2" for w in owed.get("waiting_on", []))


# -- 0080: operator lurk visibility ------------------------------------------------


def test_addressed_obligation_survives_a_bare_read(client, room):
    """The 0080 root fix (watcher audit): read+ack was how lurking seats
    silenced the inbox, status, the stop hook, and the dark watchdog in one
    motion — `read_message` alone must NOT unpin an ADDRESSED obligation.
    Only engaging (a reply) clears it. Bystander economics are unchanged: a
    bystander's read still releases the broadcast pin."""
    msg = _post(client, room["asker"], status="open", title="for named",
                to=["named"], asks=[{"id": "1", "text": "row"}])

    # named reads AND acks — the lurk motion — and stays pinned.
    client.get(f"/channels/canvass/messages/{msg['id']}", headers=_auth(room["named"]))
    client.post("/inbox/ack", headers=_auth(room["named"]),
                json={"cursors": {"canvass": 99}})
    assert any(e["id"] == msg["id"] for e in _inbox(client, room["named"]))

    # Replying (engaging) is what unpins.
    _post(client, room["named"], status="reply", reply_to=msg["id"],
          answers=["1"], title="done", body="answered")
    assert not any(e["id"] == msg["id"] for e in _inbox(client, room["named"]))

    # Broadcast + bystander: a bare read still releases (unchanged economics).
    bmsg = _post(client, room["asker"], status="open", title="broadcast",
                 asks=[{"id": "1", "text": "anyone"}])
    client.get(f"/channels/canvass/messages/{bmsg['id']}",
               headers=_auth(room["bystander"]))
    client.post("/inbox/ack", headers=_auth(room["bystander"]),
                json={"cursors": {"canvass": 199}})
    assert not any(e["id"] == bmsg["id"] for e in _inbox(client, room["bystander"]))


def test_debrief_fixes_envelope_scope_redelivery_and_waiting_on(client, room):
    """The nine-seat debrief round (2026-07-14, dm replies): (a) a to-you
    flag derived from asks must DROP once your ask is discharged (stale flags
    made seats re-verify their own discharges for hours); (b) a read pinned
    obligation re-surfaces headline-only with redelivery=true (full bodies
    were re-sent dozens of times a night); (c) the asker sees per-addressee
    delivery state (acked-past vs not-served) instead of inferring it."""
    msg = _post(client, room["asker"], status="open", title="canvass",
                body="x" * 600,
                asks=[{"id": "1", "text": "for named", "to": ["named"]},
                      {"id": "2", "text": "for bystander", "to": ["bystander"]}])

    env = next(e for e in _inbox(client, room["named"]) if e["id"] == msg["id"])
    assert env["to_me"] is True and env["your_pending_asks"] == ["1"]
    assert env["redelivery"] is False and env["body"] is not None  # addressed inline

    # (b) after reading, the pinned re-surface withholds the body.
    client.get(f"/channels/canvass/messages/{msg['id']}", headers=_auth(room["named"]))
    env = next(e for e in _inbox(client, room["named"]) if e["id"] == msg["id"])
    assert env["redelivery"] is True and env["body"] is None

    # (c) the asker's waiting_on distinguishes served-and-silent from unserved.
    client.post("/inbox/ack", headers=_auth(room["named"]),
                json={"cursors": {"canvass": msg["seq"]}})
    owed = client.get("/owed", headers=_auth(room["asker"])).json()
    states = {(w["seat"], w["state"]) for w in owed["waiting_on"]}
    assert ("named", "acked-past-no-reply") in states
    assert ("bystander", "not-yet-acked") in states

    # (a) named answers its ask: the ask-derived flag and its debt drop while
    # bystander's row stays open (and bystander keeps its flag).
    _post(client, room["named"], status="reply", reply_to=msg["id"],
          answers=["1"], title="mine done", body="done")
    client.post("/inbox/ack", headers=_auth(room["named"]),
                json={"cursors": {"canvass": 99}})
    assert not any(e["id"] == msg["id"] for e in _inbox(client, room["named"]))
    bys = next(e for e in _inbox(client, room["bystander"]) if e["id"] == msg["id"])
    assert bys["to_me"] is True and bys["your_pending_asks"] == ["2"]
    # waiting_on now names only bystander.
    owed = client.get("/owed", headers=_auth(room["asker"])).json()
    assert {w["seat"] for w in owed["waiting_on"]} == {"bystander"}


def test_stewardship_stale_claim_alerts_address_the_delegate(client, room):
    """0084 + 0093: a claim untouched past its channel SLA produces ONE
    coalesced hub-alert ADDRESSED to the reporting delegate. Bounded-debt
    contract (0093): at most one alert stands; an unchanged live set posts
    nothing; touching the claim (the progress receipt) makes the next sweep
    CLOSE the standing alert with the hub's own resolved reply, so the
    delegate's owed row disappears instead of accumulating forever."""
    service = client.app.state.service

    # A claim, then age it past the SLA by backdating the store row.
    key = room["named"]
    client.put("/channels/canvass/store/claim:build-x", headers=_auth(key),
               json={"value": {"owner": "named"}, "expect_version": 0})
    service.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 7200 "
        "WHERE channel='canvass' AND key='claim:build-x'")
    service.db._conn.commit()

    # No reporting delegate yet: sweep stays silent.
    assert service._steward_sweep() == []

    # Grant reporting to bystander; the sweep now alerts, addressed.
    client.put("/admin/delegation", headers={"Authorization": f"Bearer {ADMIN_KEY}"},
               json={"agent_id": "bystander", "powers": ["reporting"]})
    out = service._steward_sweep()
    assert out and out[0].startswith("stale-claims:")
    alerts = service.db.get_messages("hub-alerts", 0, 50)
    alert = next(m for m in reversed(alerts) if "STALE CLAIMS" in m.body)
    assert alert.to == ["bystander"] and alert.status.value == "open"
    assert "canvass/claim:build-x" in alert.body and "owner named" in alert.body
    # The delegate now OWES an answer on the alert.
    owed = client.get("/owed", headers=_auth(room["bystander"])).json()
    assert any(o["id"] == alert.id for o in owed["to_answer"])

    # Unchanged live set: nothing new posted, the ONE alert keeps standing.
    assert service._steward_sweep() == []
    count_before = sum("STALE CLAIMS" in m.body
                       for m in service.db.get_messages("hub-alerts", 0, 100))
    assert count_before == 1

    # Touching the claim row ends the episode: the hub CLOSES its own alert.
    client.put("/channels/canvass/store/claim:build-x", headers=_auth(key),
               json={"value": {"owner": "named", "note": "progress"}})
    assert service._steward_sweep() == ["stale-claims:cleared"]
    replies = service.db.replies_to(alert.id)
    assert any(r.sender == "hub" and r.status.value == "resolved"
               for r in replies)
    # The debt is gone from the delegate's owed ledger.
    owed = client.get("/owed", headers=_auth(room["bystander"])).json()
    assert not any(o["id"] == alert.id for o in owed["to_answer"])
    # And a further sweep with nothing stale posts nothing at all.
    assert service._steward_sweep() == []


def test_stewardship_changed_set_supersedes_bounded_to_one(client, room):
    """0093: when the stale set CHANGES, the old alert is closed (resolved
    reply) and one new alert replaces it — never two standing obligations.
    Restart-safety: the standing alert is found in the channel, so a fresh
    service instance still closes it."""
    service = client.app.state.service
    key = room["named"]
    client.put("/admin/delegation", headers={"Authorization": f"Bearer {ADMIN_KEY}"},
               json={"agent_id": "bystander", "powers": ["reporting"]})

    client.put("/channels/canvass/store/claim:one", headers=_auth(key),
               json={"value": {"owner": "named"}, "expect_version": 0})
    service.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 7200 "
        "WHERE channel='canvass' AND key='claim:one'")
    service.db._conn.commit()
    assert service._steward_sweep() == ["stale-claims:1"]

    # The set grows: a second stale claim appears.
    client.put("/channels/canvass/store/claim:two", headers=_auth(key),
               json={"value": {"owner": "named"}, "expect_version": 0})
    service.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 7200 "
        "WHERE channel='canvass' AND key='claim:two'")
    service.db._conn.commit()
    assert service._steward_sweep() == ["stale-claims:2"]

    msgs = service.db.get_messages("hub-alerts", 0, 100)
    alerts = [m for m in msgs if "STALE CLAIMS" in m.body]
    assert len(alerts) == 2  # history keeps both, but only one STANDS:
    standing = service._standing_steward_alerts()
    assert len(standing) == 1
    assert "claim:two" in standing[0].body
    # The superseded alert carries the hub's closing reply.
    closed = next(m for m in alerts if m.id != standing[0].id)
    assert any(r.sender == "hub" and r.status.value == "resolved"
               for r in service.db.replies_to(closed.id))
    # The delegate owes exactly ONE answer, not one per sweep.
    owed = client.get("/owed", headers=_auth(room["bystander"])).json()
    hub_debts = [o for o in owed["to_answer"] if o["sender"] == "hub"]
    assert len(hub_debts) == 1


def test_stewardship_shrinking_set_does_not_re_ring_the_steward(client, room):
    """THE JANITOR MUST IDLE (live regression, 2026-08-01). Reproduces the
    measured loop exactly: an unclearable RESIDUE (owners offline for days,
    no canvass can ever touch those rows) plus one TRANSIENT claim the
    steward successfully chases. When the transient row was marked done the
    live set shrank, and shrink alone superseded the standing alert and
    minted a FRESH open obligation on the steward about the residue — so
    finishing the work bought the steward its next alert, forever (28 alerts
    in 24h on the live hub, at-test/claim:msg-382 done 18:04:20 -> new alert
    18:07:47, then claim:msg-397-398 done 18:28:34 -> new alert 18:33:06).
    A subset of what the standing alert already names is not news."""
    service = client.app.state.service
    key = room["named"]
    client.put("/admin/delegation", headers={"Authorization": f"Bearer {ADMIN_KEY}"},
               json={"agent_id": "bystander", "powers": ["reporting"]})

    # Residue: claims owned by a seat that never comes back (the live ones
    # had been idle four days behind offline owners). Nothing clears these.
    gone = room["asker"]
    for slug in ("residue-a", "residue-b"):
        client.put(f"/channels/canvass/store/claim:{slug}", headers=_auth(gone),
                   json={"value": {"owner": "asker"}, "expect_version": 0})
    # Transient: the one claim the steward can actually get finished.
    client.put("/channels/canvass/store/claim:transient", headers=_auth(key),
               json={"value": {"owner": "named"}, "expect_version": 0})
    service.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 7200 "
        "WHERE channel='canvass' AND key LIKE 'claim:%'")
    service.db._conn.commit()

    assert service._steward_sweep() == ["stale-claims:3"]
    standing = service._standing_steward_alerts()
    assert len(standing) == 1
    first = standing[0]
    assert "claim:transient" in first.body

    # The steward's chase SUCCEEDS: the owner marks the transient row done,
    # so the live set shrinks to the residue alone.
    client.put("/channels/canvass/store/claim:transient", headers=_auth(key),
               json={"value": {"owner": "named", "status": "done"}})

    # THE REGRESSION: this shrink must post nothing at all. The standing
    # alert already names both residue rows, so there is no new debt to
    # state and no reason to hand the steward a second obligation.
    assert service._steward_sweep() == []
    assert [m.id for m in service._standing_steward_alerts()] == [first.id]
    alerts = [m for m in service.db.get_messages("hub-alerts", 0, 100)
              if "STALE CLAIMS" in m.body]
    assert len(alerts) == 1, "shrink re-alerted: the janitor never idles"
    # No supersede reply was written, so the first alert is still the ONE
    # live obligation and the steward owes exactly one answer.
    owed = client.get("/owed", headers=_auth(room["bystander"])).json()
    hub_debts = [o for o in owed["to_answer"] if o["sender"] == "hub"]
    assert len(hub_debts) == 1 and hub_debts[0]["id"] == first.id

    # Idling is not deafness: genuinely NEW stale work still supersedes.
    client.put("/channels/canvass/store/claim:brand-new", headers=_auth(key),
               json={"value": {"owner": "named"}, "expect_version": 0})
    service.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 7200 "
        "WHERE channel='canvass' AND key='claim:brand-new'")
    service.db._conn.commit()
    assert service._steward_sweep() == ["stale-claims:3"]
    standing = service._standing_steward_alerts()
    assert len(standing) == 1 and standing[0].id != first.id
    assert "claim:brand-new" in standing[0].body
    assert any(r.sender == "hub" and r.status.value == "resolved"
               for r in service.db.replies_to(first.id))

    # And emptying the set still CLOSES the episode outright.
    for slug in ("residue-a", "residue-b"):
        client.put(f"/channels/canvass/store/claim:{slug}", headers=_auth(gone),
                   json={"value": {"owner": "asker", "status": "done"}})
    client.put("/channels/canvass/store/claim:brand-new", headers=_auth(key),
               json={"value": {"owner": "named", "status": "done"}})
    assert service._steward_sweep() == ["stale-claims:cleared"]
    assert service._standing_steward_alerts() == []


def test_terminal_claims_never_go_stale(client, room):
    """Field finding (c2409): the sweep keyed on updated_at alone, so a
    finished claim re-escalated forever and canvass rounds bumped
    timestamps on rows nobody would touch again. Terminal rows — the
    taught {"done": true} AND the observed status="done"/"shipped"
    spellings — never alert, however old; the board agrees (one shared
    predicate)."""
    service = client.app.state.service
    key = room["named"]
    client.put("/admin/delegation", headers={"Authorization": f"Bearer {ADMIN_KEY}"},
               json={"agent_id": "bystander", "powers": ["reporting"]})

    for slug, value in (("done-x", {"owner": "named", "done": True}),
                        ("shipped-y", {"owner": "named", "status": "shipped"}),
                        ("status-done-z", {"owner": "named", "status": "Done"})):
        client.put(f"/channels/canvass/store/claim:{slug}", headers=_auth(key),
                   json={"value": value, "expect_version": 0})
    # A free-text status is NOT terminal — it must still alert when stale.
    client.put("/channels/canvass/store/claim:live-w", headers=_auth(key),
               json={"value": {"owner": "named",
                               "status": "designed; build next session"},
                     "expect_version": 0})
    service.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 7200 "
        "WHERE channel='canvass' AND key LIKE 'claim:%'")
    service.db._conn.commit()

    out = service._steward_sweep()
    assert out == ["stale-claims:1"]
    alerts = service.db.get_messages("hub-alerts", 0, 50)
    alert = next(m for m in reversed(alerts) if "STALE CLAIMS" in m.body)
    assert "claim:live-w" in alert.body
    for terminal in ("done-x", "shipped-y", "status-done-z"):
        assert terminal not in alert.body

    # The board draws the same line: terminal rows are out of in_progress.
    board = client.get("/board", headers=_auth(key)).json()
    tasks = {row["task"] for row in board["in_progress"]}
    assert "live-w" in tasks
    assert tasks.isdisjoint({"done-x", "shipped-y", "status-done-z"})


def test_prose_after_the_state_word_and_parked_claims(client, room):
    """c3349 item 9: owners wrote status='DONE — shipped x, receipt c123'
    and the exact-whole-string match re-alerted rows closed twice. The
    vocabulary keys on the FIRST word now; PARKED rows are deliberately
    idle — no alert — while staying live on the board (unfinished work)."""
    service = client.app.state.service
    key = room["named"]
    client.put("/admin/delegation", headers={"Authorization": f"Bearer {ADMIN_KEY}"},
               json={"agent_id": "bystander", "powers": ["reporting"]})

    for slug, status in (("prose-done", "DONE — shipped xyz, receipt c123"),
                         ("prose-closed", "CLOSED by canvass, twice"),
                         ("parked-a", "PARKED until the gateway wave lands")):
        # A park now carries its tag and its ask (operator ruling
        # 2026-08-06): a park nobody can act on is the black hole this
        # vocabulary exists to abolish. Terminal words need neither.
        value = {"owner": "named", "status": status}
        if slug == "parked-a":
            value |= {"blocked_on": "external",
                      "needs": "the gateway wave to land"}
        client.put(f"/channels/canvass/store/claim:{slug}", headers=_auth(key),
                   json={"value": value, "expect_version": 0})
    # c3363 second axis: the word under the legacy STATE key still counts
    # (a row closed under the wrong key must not nag forever).
    client.put("/channels/canvass/store/claim:state-key", headers=_auth(key),
               json={"value": {"owner": "named", "state": "done, receipt c9"},
                     "expect_version": 0})
    client.put("/channels/canvass/store/claim:still-live", headers=_auth(key),
               json={"value": {"owner": "named", "status": "doneish is not done"},
                     "expect_version": 0})
    service.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 7200 "
        "WHERE channel='canvass' AND key LIKE 'claim:%'")
    service.db._conn.commit()

    out = service._steward_sweep()
    assert out == ["stale-claims:1"]
    alerts = service.db.get_messages("hub-alerts", 0, 50)
    alert = next(m for m in reversed(alerts) if "STALE CLAIMS" in m.body)
    assert "claim:still-live" in alert.body
    for quiet in ("prose-done", "prose-closed", "parked-a", "state-key"):
        assert quiet not in alert.body

    # Board: prose-DONE/CLOSED rows are terminal (out of in_progress);
    # PARKED stays IN progress — parked work is unfinished work.
    board = client.get("/board", headers=_auth(key)).json()
    tasks = {row["task"] for row in board["in_progress"]}
    assert "parked-a" in tasks and "still-live" in tasks
    assert tasks.isdisjoint({"prose-done", "prose-closed"})


def test_fleet_status_gated_to_operators_and_reporting_delegates(client, room):
    """0084: GET /status serves the operator overview to reporting delegates
    (the steward could not see lurk metrics behind the admin key), with
    refusal details redacted for non-operators (they carry private channel
    names and verbatim errors)."""
    r = client.get("/status", headers=_auth(room["named"]))
    assert r.status_code == 403 and "reporting" in r.json()["detail"]

    client.put("/admin/delegation", headers={"Authorization": f"Bearer {ADMIN_KEY}"},
               json={"agent_id": "named", "powers": ["reporting"]})
    r = client.get("/status", headers=_auth(room["named"]))
    assert r.status_code == 200
    data = r.json()
    rows = data["agents"]
    assert "fleet" in data
    assert any(row["agent_id"] == "asker" for row in rows)
    assert all("acked_unanswered" in row and "last_refusal" not in row
               for row in rows)


def test_overview_counts_acked_unanswered(client, room):
    msg = _post(client, room["asker"], status="open", title="for named",
                asks=[{"id": "1", "text": "row", "to": ["named"]}])
    client.post("/inbox/ack", headers=_auth(room["named"]),
                json={"cursors": {"canvass": msg["seq"]}})

    rows = client.get("/admin/status",
                      headers={"Authorization": f"Bearer {ADMIN_KEY}"}).json()["agents"]
    named = next(r for r in rows if r["agent_id"] == "named")
    assert named["acked_unanswered"] == 1 and named["owed_answers"] == 1
    asker = next(r for r in rows if r["agent_id"] == "asker")
    assert asker["acked_unanswered"] == 0


def test_saturated_seats_always_receive_messages(client, room):
    """Operator ruling (2026-07-28): delivery is NEVER refused for recipient
    state. The old 0114 saturation 403 muted the whole fleet toward the
    operator (even a REPLY to the human was refused as 'adding to their
    queue'); saturation is observability now (/status silence_class), not a
    gate. Humans always receive their messages, and so do agents."""
    client.put("/channels/canvass/store/channel:meta",
               json={"value": {"response_sla_minutes": 0.001}},
               headers=_auth(room["asker"]))
    for i in range(6):                       # well past the old gate of 5
        _post(client, room["asker"], status="open", title=f"debt {i}",
              asks=[{"id": "1", "text": "q", "to": ["named"]}])
    time.sleep(0.2)

    # A new open ask, an fyi, and a DM reply to the deeply saturated seat:
    # every one of them delivers.
    r = client.post("/channels/canvass/messages", headers=_auth(room["asker"]),
                    json={"title": "one more", "body": "b", "status": "open",
                          "asks": [{"id": "1", "text": "more?", "to": ["named"]}]})
    assert r.status_code == 200
    r = client.post("/channels/canvass/messages", headers=_auth(room["asker"]),
                    json={"title": "fyi ok", "body": "b", "status": "fyi",
                          "to": ["named"]})
    assert r.status_code == 200
    ask = client.post("/dms/asker/messages", headers=_auth(room["named"]),
                      json={"body": "can you answer?", "status": "open",
                            "asks": [{"id": "1", "text": "answer?"}]}).json()
    r = client.post("/dms/named/messages", headers=_auth(room["asker"]),
                    json={"body": "yes", "status": "reply",
                          "reply_to": ask["id"], "answers": ["1"]})
    assert r.status_code == 200


def test_dark_seats_always_receive_messages(client, room):
    """0107 reshaped by the same ruling: asks and replies to an OFFLINE seat
    deliver (the sender gets an ephemeral non-waking advisory instead of a
    403 — pinned in test_routing); address_dark stays accepted as the
    advisory suppressor for deliberate canvasses."""
    ask = client.post("/dms/asker/messages", headers=_auth(room["named"]),
                      json={"body": "q", "status": "open",
                            "asks": [{"id": "1", "text": "q?"}]}).json()
    client.put("/channels/canvass/store/channel:meta",
               json={"value": {"response_sla_minutes": 0.001}},
               headers=_auth(room["asker"]))
    _post(client, room["asker"], status="open", title="first",
          asks=[{"id": "1", "text": "q", "to": ["named"]}])
    time.sleep(0.2)
    service = client.app.state.service
    service.presence._last_seen.pop("named", None)
    service.presence._connections.pop("named", None)
    service.presence.update("named", "offline")
    service.dark_sweep()
    assert "named" in service._dark_since

    r = client.post("/channels/canvass/messages", headers=_auth(room["asker"]),
                    json={"title": "to dark seat", "body": "b", "status": "open",
                          "asks": [{"id": "1", "text": "more?", "to": ["named"]}]})
    assert r.status_code == 200
    r = client.post("/channels/canvass/messages", headers=_auth(room["asker"]),
                    json={"title": "canvass", "body": "b", "status": "open",
                          "address_dark": True,
                          "asks": [{"id": "1", "text": "canvass", "to": ["named"]}]})
    assert r.status_code == 200
    r = client.post("/dms/named/messages", headers=_auth(room["asker"]),
                    json={"body": "answer", "status": "reply",
                          "reply_to": ask["id"], "answers": ["1"]})
    assert r.status_code == 200


def test_overview_silence_class_routes_sla_breach(client, room):
    """0114: fleet /status exposes silence_class on escalated debts."""
    client.put("/channels/canvass/store/channel:meta",
               json={"value": {"response_sla_minutes": 0.001}},
               headers=_auth(room["asker"]))
    msg = _post(client, room["asker"], status="open", title="for named",
                asks=[{"id": "1", "text": "row", "to": ["named"]}])
    time.sleep(0.2)
    service = client.app.state.service

    rows = client.get("/admin/status",
                      headers={"Authorization": f"Bearer {ADMIN_KEY}"}).json()["agents"]
    named = next(r for r in rows if r["agent_id"] == "named")
    assert named["silence_class"] == "unseen"

    client.post("/inbox/ack", headers=_auth(room["named"]),
                json={"cursors": {"canvass": msg["seq"]}})
    rows = client.get("/admin/status",
                      headers={"Authorization": f"Bearer {ADMIN_KEY}"}).json()["agents"]
    named = next(r for r in rows if r["agent_id"] == "named")
    assert named["silence_class"] == "seen-and-ignored"

    service.presence.touch("named")
    service.presence._last_reception["named"] = (
        time.time() - _RECEPTION_STALE - 100.0)   # anchored, never a magic number
    rows = client.get("/admin/status",
                      headers={"Authorization": f"Bearer {ADMIN_KEY}"}).json()["agents"]
    named = next(r for r in rows if r["agent_id"] == "named")
    assert named["silence_class"] == "deaf"

    service.presence._last_seen.pop("named", None)
    service.presence._connections.pop("named", None)
    service.presence.update("named", "offline")
    rows = client.get("/admin/status",
                      headers={"Authorization": f"Bearer {ADMIN_KEY}"}).json()["agents"]
    named = next(r for r in rows if r["agent_id"] == "named")
    assert named["silence_class"] == "dead"


def test_blocked_claims_are_exempt_like_parked(client, room):
    """The code must match the teaching (2026-08-01). SKILL.md groups
    `blocked` with `parked`/`done` as a row you leave honest where it is, but
    _PARKED_CLAIM_STATUSES omitted it — so 20 blocked rows on the live hub
    were a permanent floor under every stale-claims alert, nagging owners to
    re-answer a question their status already answered."""
    service = client.app.state.service
    key = room["named"]
    client.put("/admin/delegation", headers={"Authorization": f"Bearer {ADMIN_KEY}"},
               json={"agent_id": "bystander", "powers": ["reporting"]})
    client.put("/channels/canvass/store/claim:waiting", headers=_auth(key),
               json={"value": {"owner": "named", "blocked_on": "external", "needs": "the vendor build to land", "status": "blocked on the "
                               "vendor's export"}, "expect_version": 0})
    client.put("/channels/canvass/store/claim:live", headers=_auth(key),
               json={"value": {"owner": "named", "status": "working"},
                     "expect_version": 0})
    service.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 7200 "
        "WHERE channel='canvass' AND key LIKE 'claim:%'")
    service.db._conn.commit()

    assert service._steward_sweep() == ["stale-claims:1"]
    alerts = service.db.get_messages("hub-alerts", 0, 50)
    alert = next(m for m in reversed(alerts) if "STALE CLAIMS" in m.body)
    assert "claim:live" in alert.body
    assert "claim:waiting" not in alert.body
    # Still LIVE work though: parking is not finishing, so the board keeps it
    # in progress — exempt from NAGGING is not the same as done.
    board = client.get("/board", headers=_auth(key)).json()
    assert {r["task"] for r in board["in_progress"]} >= {"waiting", "live"}


def test_steward_bookkeeping_rows_do_not_feed_their_own_sweep(client, room):
    """Stewardship must not become its own backlog. The delegate opens claim
    rows in hub-alerts to track the alerts it is answering; on the live hub
    26 such rows existed, and every one of them aged into the very sweep that
    writes those alerts."""
    service = client.app.state.service
    key = room["named"]
    client.put("/admin/delegation", headers={"Authorization": f"Bearer {ADMIN_KEY}"},
               json={"agent_id": "bystander", "powers": ["reporting"]})
    # The steward's own tracking row, in the alerts channel, gone stale.
    service._ensure_alerts_channel()
    client.put("/channels/hub-alerts/store/claim:msg-999",
               headers=_auth(room["bystander"]),
               json={"value": {"owner": "bystander"}, "expect_version": 0})
    service.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 7200 "
        "WHERE channel='hub-alerts' AND key LIKE 'claim:%'")
    service.db._conn.commit()
    assert service._steward_sweep() == []
    assert not [m for m in service.db.get_messages("hub-alerts", 0, 50)
                if "STALE CLAIMS" in m.body]


def test_watchdogs_only_cite_debt_the_seat_actually_owes(client, room):
    """FALSE-POSITIVE CLASS (live, 2026-08-01). The sweeps filtered a seat's
    INBOX on `escalated` and called the result "obligations this seat is
    holding" — but the inbox shows every escalated row in the seat's rooms,
    including ones addressed to somebody else. at-test#363 was cited as
    rotting debt for six seats that never held it, and the steward burned
    turns canvassing seats about other seats' work."""
    service = client.app.state.service
    # An escalated ask that names ONLY `named`; `bystander` merely sees it.
    msg = _post(client, room["asker"], status="open", title="do X",
                asks=[{"id": "1", "text": "please do X", "to": ["named"]}])
    service.db._conn.execute(
        "UPDATE messages SET created_at = created_at - 86400 WHERE id = ?",
        (msg["id"],))
    service.db._conn.commit()

    # The owner really does hold it; the bystander really does not.
    assert [e.id for e in service._escalated_debts("named")] == [msg["id"]]
    assert service._escalated_debts("bystander") == []
    # And the bystander DOES see it in the inbox — which is exactly why the
    # old `escalated`-only filter smeared them.
    assert any(e.id == msg["id"] and e.escalated
               for e in service.inbox(_info(service, "bystander")))

    # So the lurk leg can name the owner and never the bystander.
    service.presence.mark_reception("named")
    service.presence.mark_reception("bystander")
    for seat in ("named", "bystander"):
        service._lurk_since[seat] = 0.0     # past the confirm window
    alerted: list[str] = []
    for seat in ("named", "bystander"):
        service._lurk_sweep_one(seat, set(), alerted)
    assert alerted == ["named"]


def test_stalled_phase_alert_names_the_steward_and_its_blocking_ask(client, room):
    """THE 17.5-HOUR STALL (2026-08-04). An OPEN phase row whose steward went
    quiet was invisible to every sweep: `_steward_sweep` filters to `claim:`
    keys and the blocked claims beside a stalled phase are exempt as parked.
    Meanwhile the steward's actual blocking question sat in the operator's
    owed pile (index 87 of 144). The phase sweep names BOTH — the stale
    phase and the escalated ask it waits on — to the steward and the
    reporting delegate, with the bounded-debt contract of every other
    standing alert."""
    service = client.app.state.service
    from agora.hub.service import AgentInfo

    # An operator in the room, and a phase stewarded by `named`.
    op_info, op_key_raw = service.register_agent("op", "Op", operator=True, mission="seat op")
    service.join_channel(op_info, "canvass", None)
    # Created by the channel owner (a steward cannot self-nominate on
    # create); every later touch is the named steward's own.
    client.put("/channels/canvass/store/phase:novel", headers=_auth(room["asker"]),
               json={"value": {"current": "packaging", "status": "open",
                               "steward": "named", "next": "verification"},
                     "expect_version": 0})
    # The steward's blocking ask on the operator, already escalated.
    r = client.post("/channels/canvass/messages", headers=_auth(room["named"]),
                    json={"blocked_on": "external", "needs": "the vendor build to land", "status": "blocked", "to": ["op"],
                          "body": "which toolchain may I use for the pdf?"})
    ask_seq = r.json()["seq"]
    service.db._conn.execute(
        "UPDATE messages SET created_at = created_at - 7200 WHERE seq = ? "
        "AND channel='canvass'", (ask_seq,))
    service.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 7200 "
        "WHERE channel='canvass' AND key='phase:novel'")
    service.db._conn.commit()

    # No reporting delegate: the steward alone is still alerted.
    out = service._phase_sweep()
    assert out == ["stalled-phase:1"]
    alerts = service.db.get_messages("hub-alerts", 0, 50)
    alert = next(m for m in reversed(alerts) if "PHASE STALLED" in m.body)
    assert alert.to == ["named"] and alert.status.value == "open"
    assert "canvass/phase:novel" in alert.body
    assert f"canvass#{ask_seq} (to op)" in alert.body     # the blocking ask
    assert "relaunch" not in alert.body.lower()           # decide, not restart

    # Unchanged set: the ONE standing alert is the whole truth.
    assert service._phase_sweep() == []
    n = sum("PHASE STALLED" in m.body
            for m in service.db.get_messages("hub-alerts", 0, 100))
    assert n == 1

    # Touching the phase row is the receipt: the hub closes its own alert.
    client.put("/channels/canvass/store/phase:novel", headers=_auth(room["named"]),
               json={"value": {"current": "packaging", "status": "open",
                               "steward": "named",
                               "note": "waiting parked explicitly"},
                     "expect_version": 1})
    assert service._phase_sweep() == ["stalled-phase:cleared"]
    assert any(r2.sender == "hub" and r2.status.value == "resolved"
               for r2 in service.db.replies_to(alert.id))

    # A COMPLETE phase never alerts, however old.
    service.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 7200 "
        "WHERE channel='canvass' AND key='phase:novel'")
    service.db._conn.commit()
    client.put("/channels/canvass/store/phase:novel", headers=_auth(room["named"]),
               json={"value": {"current": "packaging", "status": "complete",
                               "steward": "named"},
                     "expect_version": 2})
    service.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - 7200 "
        "WHERE channel='canvass' AND key='phase:novel'")
    service.db._conn.commit()
    assert service._phase_sweep() == []


def test_waiting_on_sees_envelope_addressed_asks(client, room):
    """THE BLIND DELEGATE (2026-08-04). `waiting_on` is the asker's radar —
    "who has not delivered" — and it keyed ONLY on a per-ask `to`. The
    obligation ledger has always ALSO honoured the message-level `to`, so a
    delegate that addressed one seat per message and numbered its asks
    inside got a full to_answer row on the WORKER side and an empty radar on
    its own: measured live, five dispatched asks, zero waiting_on rows."""
    service = client.app.state.service
    # Envelope-addressed, asks carry no `to` of their own.
    r = client.post("/channels/canvass/messages", headers=_auth(room["asker"]),
                    json={"status": "open", "to": ["named"],
                          "body": "your slice",
                          "asks": [{"id": "1", "text": "draft it"},
                                   {"id": "2", "text": "check it"}]})
    assert r.status_code == 200
    owed = client.get("/owed", headers=_auth(room["asker"])).json()
    assert {w["seat"] for w in owed["waiting_on"]} == {"named"}, \
        "the asker cannot see who it is waiting on"
    # The worker's own ledger agrees it owes the work — the two halves of
    # one dispatch must never disagree.
    theirs = client.get("/owed", headers=_auth(room["named"])).json()
    assert any(o["seq"] == r.json()["seq"] for o in theirs["to_answer"])

    # A per-ask `to` still WINS over the envelope: an ask aimed at one seat
    # inside a message addressed to another is that seat's, not everyone's.
    r2 = client.post("/channels/canvass/messages", headers=_auth(room["asker"]),
                     json={"status": "open", "to": ["named"], "body": "mixed",
                           "asks": [{"id": "1", "text": "yours",
                                     "to": ["bystander"]}]})
    assert r2.status_code == 200
    owed = client.get("/owed", headers=_auth(room["asker"])).json()
    seats = {w["seat"] for w in owed["waiting_on"] if w["ask"] == "1"}
    assert "bystander" in seats
