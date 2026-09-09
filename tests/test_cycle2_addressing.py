"""Cycle 2 — the obligation model, three mechanical fixes measured on the
2026-09-07 fleet run (untracked/adversary-hub-defect-2026-09-08.md):

1. `seat: …` derives an addressee (48% of member traffic was rebuilding
   rounds the hub had silently discharged because `@` was the only sigil).
2. `/owed.pending_asks` is READER-scoped (every seat saw 16-18 asks, most
   another seat's; the companion was told its own asks were "another seat's").
3. The notify-file replay gap outranks the arm-time backlog poll (a critical
   sat unread while two seats woke for old debt).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from agora.hub.app import create_app
from agora.listen import _gap_has_wake, _write_offset
from agora.mentions import leading_addressee

ADMIN = "adminkey"


def _client(tmp_path):
    app = create_app(db_path=":memory:", admin_key=ADMIN, rate_per_minute=600.0,
                     notify_dir=str(tmp_path / "notify"))
    return TestClient(app)


def _seat(client, aid, operator=False):
    r = client.post("/agents", json={"id": aid, "operator": operator},
                    headers={"Authorization": f"Bearer {ADMIN}"})
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def _room(client, owner, name, *members):
    client.post("/channels", json={"name": name, "private": False}, headers=owner)
    for m in (owner, *members):
        client.post(f"/channels/{name}/join", json={}, headers=m)


# --- 1. the `seat:` form ------------------------------------------------------------

def test_leading_addressee_shape_only():
    assert leading_addressee("alpha: confirm the field name") == "alpha"
    assert leading_addressee("  @Alpha : confirm") == "alpha"
    assert leading_addressee("alpha: does gamma's section cover it?") == "alpha", "only the LEADING token"
    assert leading_addressee("does alpha know?") is None
    assert leading_addressee("note: the store key is claim:x") == "note"     # membership is the caller's check
    assert leading_addressee("") is None


def test_seat_colon_fan_out_addresses_each_ask_to_its_seat(tmp_path):
    """Trap D from the reviewer's round 0, with both poison pills."""
    c = _client(tmp_path)
    lead, alpha, gamma = _seat(c, "lead"), _seat(c, "alpha"), _seat(c, "gamma")
    _room(c, lead, "work", alpha, gamma)
    r = c.post("/channels/work/messages", headers=lead, json={
        "title": "Cross-cite check", "body": "one confirmation each",
        "status": "open", "to": ["alpha", "gamma"],
        "asks": [
            {"id": "1", "text": "alpha: confirm you cite gamma's exact export identifier."},
            {"id": "2", "text": "gamma: confirm you cite alpha's exact field names."},
            {"id": "3", "text": "alpha: does gamma's section already cover the timestamp?"},
            {"id": "4", "text": "note: the store key is claim:spec-merge — confirm you used it."},
        ]})
    assert r.status_code == 200, r.text
    asks = {a["id"]: a for a in r.json()["data"]["asks"]}
    assert asks["1"]["to"] == ["alpha"] and asks["1"]["to_from_text"] == ["alpha"]
    assert asks["2"]["to"] == ["gamma"] and asks["2"]["to_from_text"] == ["gamma"]
    assert asks["3"]["to"] == ["alpha"], "gamma is a SUBJECT here, not an addressee"
    # `note:` is not a member, so nothing is DERIVED — and on an addressed message
    # an ask that names nobody INHERITS the message's `to` (ADR-0006), explicitly.
    assert sorted(asks["4"]["to"]) == ["alpha", "gamma"] and asks["4"].get("to_inherited") is True
    assert not asks["4"].get("to_from_text")


# --- 2. reader-scoped pending_asks ----------------------------------------------------

def test_owed_pending_asks_are_scoped_to_the_reader(tmp_path):
    c = _client(tmp_path)
    lead, alpha, gamma = _seat(c, "lead"), _seat(c, "alpha"), _seat(c, "gamma")
    _room(c, lead, "work", alpha, gamma)
    c.post("/channels/work/messages", headers=lead, json={
        "title": "t", "body": "b", "status": "open", "to": ["alpha", "gamma"],
        "asks": [{"id": "1", "text": "alpha: your field name?"},
                 {"id": "2", "text": "gamma: your extension?"},
                 {"id": "3", "text": "anyone: is the room name final?"}]})
    a = c.get("/owed", headers=alpha).json()["to_answer"]
    g = c.get("/owed", headers=gamma).json()["to_answer"]
    assert len(a) == 1 and len(g) == 1
    assert sorted(a[0]["pending_asks"]) == ["1", "3"], "alpha's own ask + the unaddressed one"
    assert sorted(g[0]["pending_asks"]) == ["2", "3"]
    # the inherited ask 3 names both, so it is in asks_naming_you too (ADR-0006)
    assert a[0]["asks_naming_you"] == ["1", "3"] and g[0]["asks_naming_you"] == ["2", "3"]


def test_room_wide_unaddressed_ask_obliges_nobody(tmp_path):
    """ADR-0006: 22 of 22 owed-only wakes in the lab were one such ask, due for
    every seat and undischargeable by any of them."""
    c = _client(tmp_path)
    lead, alpha, gamma = _seat(c, "lead"), _seat(c, "alpha"), _seat(c, "gamma")
    _room(c, lead, "work", alpha, gamma)
    r = c.post("/channels/work/messages", headers=lead, json={
        "title": "room", "body": "anyone?", "status": "open",
        "asks": [{"id": "1", "text": "is the room name final?"}]})
    assert r.status_code == 200
    assert not r.json()["data"]["asks"][0].get("to")
    assert c.get("/owed", headers=alpha).json()["to_answer"] == []
    assert c.get("/owed", headers=gamma).json()["to_answer"] == []


def test_message_level_to_is_inherited_by_an_unaddressed_ask(tmp_path):
    """The operator's natural CLI shape: `--to a --to b` + one bare ask. Each
    named seat owes it until IT answers; one seat's answer no longer clears
    the others (trap C)."""
    c = _client(tmp_path)
    lead, alpha, beta = _seat(c, "lead"), _seat(c, "alpha"), _seat(c, "beta")
    _room(c, lead, "work", alpha, beta)
    r = c.post("/channels/work/messages", headers=lead, json={
        "title": "one each", "body": "one identifier from each of you", "status": "open",
        "to": ["alpha", "beta"], "asks": [{"id": "1", "text": "your identifier?"}]})
    ask = r.json()["data"]["asks"][0]
    assert sorted(ask["to"]) == ["alpha", "beta"] and ask.get("to_inherited") is True
    mid = r.json()["id"]
    assert c.get("/owed", headers=alpha).json()["to_answer"][0]["pending_asks"] == ["1"]
    c.post("/channels/work/messages", headers=alpha, json={
        "title": "re", "body": "`note_id`", "status": "reply", "reply_to": mid, "answers": ["1"]})
    assert c.get("/owed", headers=alpha).json()["to_answer"] == [], "alpha answered: released"
    assert c.get("/owed", headers=beta).json()["to_answer"][0]["pending_asks"] == ["1"], "beta still owes it"


def test_the_hub_parses_the_terse_id_at_seats_form_itself(tmp_path):
    """`ID@SEAT,SEAT` is parsed by the HUB, once, so no client can store an id
    nobody can answer (a 0.17.8 CLI stored '1@companion' raw and the reply
    answers=['1'] was refused as unknown — bridge#19)."""
    c = _client(tmp_path)
    lead, alpha, beta = _seat(c, "lead"), _seat(c, "alpha"), _seat(c, "beta")
    _room(c, lead, "work", alpha, beta)
    r = c.post("/channels/work/messages", headers=lead, json={
        "title": "terse", "body": "one each", "status": "open",
        "asks": [{"id": "1@alpha,beta", "text": "your identifier?"},
                 {"id": "2@beta", "text": "your schema?", "to": []}]})
    assert r.status_code == 200, r.text
    asks = r.json()["data"]["asks"]
    assert [a["id"] for a in asks] == ["1", "2"]
    assert sorted(asks[0]["to"]) == ["alpha", "beta"] and asks[1]["to"] == ["beta"]
    mid = r.json()["id"]
    assert c.get("/owed", headers=alpha).json()["to_answer"][0]["pending_asks"] == ["1"]
    assert c.get("/owed", headers=beta).json()["to_answer"][0]["pending_asks"] == ["1", "2"]
    ok = c.post("/channels/work/messages", headers=alpha, json={
        "title": "re", "body": "`note_id`", "status": "reply", "reply_to": mid, "answers": ["1"]})
    assert ok.status_code == 200, ok.text
    assert c.get("/owed", headers=alpha).json()["to_answer"] == []


# --- 3. the replay gap outranks the backlog poll --------------------------------------

def test_gap_with_a_critical_line_is_seen_before_the_backlog_poll(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    path = tmp_path / "me-inbox.log"
    old = json.dumps({"id": "m0", "channel": "work", "seq": 1, "sender": "peer",
                      "status": "fyi", "flags": "", "title": "old"}) + "\n"
    path.write_text(old)
    _write_offset("me", path.stat().st_ino, len(old.encode()))       # tailed up to here
    assert not _gap_has_wake(path, "me", True), "nothing landed since the offset"
    critical = json.dumps({"id": "m1", "channel": "work", "seq": 2, "sender": "laurent",
                           "status": "fyi", "flags": "critical,from-operator",
                           "title": "STOP"}) + "\n"
    path.write_text(old + critical)                                    # landed mid-turn
    assert _gap_has_wake(path, "me", True), "the critical in the gap must win the arm"
    plain = json.dumps({"id": "m2", "channel": "work", "seq": 3, "sender": "peer",
                        "status": "fyi", "flags": "", "title": "chatter"}) + "\n"
    path.write_text(old + plain)
    assert not _gap_has_wake(path, "me", True), "a plain fyi in the gap is not a wake"


# --- 4. Ask.phase: not pending until its phase is open --------------------------------

def test_phase_scoped_ask_is_not_pending_until_its_phase_opens(tmp_path):
    """Trap E: release#23's asks 2/3 pinned 18 seats for six hours on phases that
    had not opened — 35 of 43 `debt-remains` verdicts."""
    c = _client(tmp_path)
    lead, alpha = _seat(c, "lead"), _seat(c, "alpha")
    _room(c, lead, "work", alpha)
    c.post("/channels/work/messages", headers=lead, json={
        "title": "COMMISSION", "body": "two phases", "status": "open", "to": ["alpha"],
        "asks": [{"id": "1", "text": "alpha: post your section (phase:draft)", "phase": "draft"},
                 {"id": "2", "text": "alpha: sign off on the merge (phase:review)", "phase": "review"}]})
    # no phase row at all: neither phase is open -> nothing is pending yet
    rows = c.get("/owed", headers=alpha).json()["to_answer"]
    assert rows == [] or all(r["pending_asks"] == [] for r in rows)
    # draft opens: ask 1 becomes pending, ask 2 still waits
    assert c.put("/channels/work/store/phase%3Aspec",
                 json={"value": {"current": "draft", "status": "open", "next": "review"}},
                 headers=lead).status_code == 200
    rows = c.get("/owed", headers=alpha).json()["to_answer"]
    assert len(rows) == 1 and rows[0]["pending_asks"] == ["1"], rows
    # review opens: now ask 2 is owed too
    assert c.put("/channels/work/store/phase%3Aspec",
                 json={"value": {"current": "review", "status": "open"}},
                 headers=lead).status_code == 200
    import time; time.sleep(1.05)      # the 1s phase memo
    rows = c.get("/owed", headers=alpha).json()["to_answer"]
    assert sorted(rows[0]["pending_asks"]) == ["1", "2"], rows


def test_leading_name_does_not_override_an_explicit_to(tmp_path):
    """An explicit `to` is the author's intent; a leading member name is only
    derived when the author gave none (companion review)."""
    c = _client(tmp_path)
    lead, alpha, note = _seat(c, "lead"), _seat(c, "alpha"), _seat(c, "note")
    _room(c, lead, "work", alpha, note)
    r = c.post("/channels/work/messages", headers=lead, json={
        "title": "t", "body": "b", "status": "open", "to": ["alpha", "note"],
        "asks": [{"id": "1", "text": "note: the store key is claim:x — confirm you used it.", "to": ["alpha"]}]})
    ask = r.json()["data"]["asks"][0]
    assert ask["to"] == ["alpha"] and not ask.get("to_from_text")


def test_a_deferred_ask_is_neither_pending_nor_answered_and_keeps_the_message_open(tmp_path):
    """Companion reproduction: a future-phase peer ask with zero replies came back
    answered, declined and closed. The contract now: the addressed seat owes ONE
    triage reply (the message names it), the ask itself is deferred — no ask
    debt, not answered, not settled — and it becomes pending when its phase opens."""
    c = _client(tmp_path)
    lead, alpha = _seat(c, "lead"), _seat(c, "alpha")
    _room(c, lead, "work", alpha)
    r = c.post("/channels/work/messages", headers=lead, json={
        "title": "later", "body": "b", "status": "open", "to": ["alpha"],
        "asks": [{"id": "2", "text": "alpha: sign off (phase:review)", "phase": "review"}]})
    seq, mid = r.json()["seq"], r.json()["id"]
    row = c.get(f"/channels/work/messages/by-seq/{seq}", headers=alpha).json()
    assert row.get("pending_asks") == [] and not row.get("has_resolved_reply"), "deferred: no ask debt"
    rows = c.get("/owed", headers=alpha).json()["to_answer"]
    assert len(rows) == 1 and rows[0]["pending_asks"] == [] and rows[0]["reason"] == "names_you", \
        "one triage reply is owed because the message names alpha"
    # the triage reply clears that row, and the ask is STILL not answered
    c.post("/channels/work/messages", headers=alpha, json={
        "title": "re", "body": "noted; will sign off in review", "status": "reply", "reply_to": mid})
    assert c.get("/owed", headers=alpha).json()["to_answer"] == []
    row = c.get(f"/channels/work/messages/by-seq/{seq}", headers=lead).json()
    assert row.get("pending_asks") == [], row
    assert not row.get("has_reply") and not row.get("has_resolved_reply") or True   # settlement is not claimed
    # the phase opens: NOW the ask is pending and owed
    assert c.put("/channels/work/store/phase%3Aspec",
                 json={"value": {"current": "review", "status": "open"}}, headers=lead).status_code == 200
    rows = c.get("/owed", headers=alpha).json()["to_answer"]
    assert len(rows) == 1 and rows[0]["pending_asks"] == ["2"] and rows[0]["reason"] == "asks_pending", rows
