"""ADR-0005 at the HUB: the two sites that COMPUTE `due` (reviewer round 1 §8:
"hardcode due=True at both sites and all 13 tests stay green"). These go red
if either site is hardcoded, and they pin the POSITIVE half of criterion (d):
a waiting row is still served and still reaches the seat's next inbox."""

from __future__ import annotations

from fastapi.testclient import TestClient

from agora.hub.app import create_app

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


def _rows(client, hdr):
    return client.get("/owed", headers=hdr).json()["to_answer"]


def test_operator_addressed_fyi_is_owed_but_not_due_and_still_delivered(tmp_path):
    c = _client(tmp_path)
    op, beta = _seat(c, "laurent", operator=True), _seat(c, "beta")
    _room(c, op, "work", beta)
    r = c.post("/channels/work/messages", headers=op, json={
        "title": "FYI: heading style", "body": "sentence case, pick it up whenever",
        "status": "fyi", "to": ["beta"]})
    assert r.status_code == 200
    rows = _rows(c, beta)
    assert len(rows) == 1 and rows[0]["due"] is False, rows          # owed, waits
    # the POSITIVE half: it is still on the ledger AND in the seat's inbox
    inbox = c.get("/inbox", headers=beta).json()
    ids = {m.get("id") for m in (inbox if isinstance(inbox, list) else inbox.get("messages", inbox.get("items", [])))}
    assert r.json()["id"] in ids, "a waiting row must still reach the next inbox"


def test_operator_critical_fyi_is_due(tmp_path):
    c = _client(tmp_path)
    op, beta = _seat(c, "laurent", operator=True), _seat(c, "beta")
    _room(c, op, "work", beta)
    c.post("/channels/work/messages", headers=op, json={
        "title": "STOP", "body": "halt", "status": "fyi", "to": ["beta"], "critical": True})
    rows = _rows(c, beta)
    assert len(rows) == 1 and rows[0]["due"] is True


def test_critical_broadcast_is_every_members_due_debt(tmp_path):
    """ADR-0006 §4: a critical with to=[] needs the owed signature as a second
    delivery path for a seat that missed the notify line mid-turn."""
    c = _client(tmp_path)
    op, alpha, beta = _seat(c, "laurent", operator=True), _seat(c, "alpha"), _seat(c, "beta")
    _room(c, op, "work", alpha, beta)
    c.post("/channels/work/messages", headers=op, json={
        "title": "STOP: confirm you are awake", "body": "halt", "status": "fyi", "critical": True})
    for hdr in (alpha, beta):
        rows = _rows(c, hdr)
        assert len(rows) == 1 and rows[0]["due"] is True and rows[0]["title"].startswith("STOP"), rows


def test_addressed_open_is_due_and_peer_open_with_no_asks_is_due(tmp_path):
    c = _client(tmp_path)
    op, alice, bob = _seat(c, "laurent", operator=True), _seat(c, "alice"), _seat(c, "bob")
    _room(c, op, "work", alice, bob)
    c.post("/channels/work/messages", headers=op, json={
        "title": "one thing", "body": "do it", "status": "open", "to": ["bob"],
        "asks": [{"id": "1", "text": "bob: the literal?"}]})
    c.post("/channels/work/messages", headers=alice, json={
        "title": "please fix", "body": "the retry loop drops the 429 body", "status": "open", "to": ["bob"]})
    rows = _rows(c, bob)
    assert len(rows) == 2 and all(r["due"] is True for r in rows), rows
    assert {r["reason"] for r in rows} >= {"peer_request_no_asks"}, "the review's counter-example stays DUE"


def test_a_consume_row_is_due_only_while_the_seats_claim_is_blocked(tmp_path):
    """An answer to the seat's OWN ask waits for its next real turn — unless
    the seat has said, in its live claim row, that it is BLOCKED: then the
    answer it asked for is exactly what must wake it (reviewer Round 2, Q1a)."""
    from fastapi.testclient import TestClient
    from agora.hub.app import create_app
    admin = "adminkey"
    c = TestClient(create_app(db_path=":memory:", admin_key=admin, rate_per_minute=600.0,
                              notify_dir=str(tmp_path / "notify")))
    def seat(aid):
        r = c.post("/agents", json={"id": aid, "operator": False}, headers={"Authorization": f"Bearer {admin}"})
        return {"Authorization": f"Bearer {r.json()['api_key']}"}
    gamma, alpha = seat("gamma"), seat("alpha")
    c.post("/channels", json={"name": "work", "private": False}, headers=gamma)
    for h in (gamma, alpha):
        c.post("/channels/work/join", json={}, headers=h)
    q = c.post("/channels/work/messages", headers=gamma, json={
        "title": "field names?", "body": "alpha: your field names", "status": "open",
        "asks": [{"id": "1", "text": "field names", "to": ["alpha"]}]}).json()
    c.post("/channels/work/messages", headers=alpha, json={
        "title": "re", "body": "`note_id`, `title`", "status": "reply", "reply_to": q["id"], "answers": ["1"]})
    rows = c.get("/owed", headers=gamma).json()["to_consume"]
    assert len(rows) == 1 and rows[0]["due"] is False, "an answer waits by default"
    c.put("/channels/work/store/claim:gamma-section", headers=gamma, json={"value": {
        "owner": "gamma", "status": "blocked: need alpha's field names", "next_step": "write", "source": "work#1",
        "blocked_on": "seat", "needs": "alpha's field names", "needs_from": "alpha"}})
    rows = c.get("/owed", headers=gamma).json()["to_consume"]
    assert rows[0]["due"] is True, "blocked on it: due now"
    c.put("/channels/work/store/claim:gamma-section", headers=gamma, json={"value": {
        "owner": "gamma", "status": "active", "next_step": "write", "source": "work#1"}})
    assert c.get("/owed", headers=gamma).json()["to_consume"][0]["due"] is False


def _hub(tmp_path):
    from fastapi.testclient import TestClient
    from agora.hub.app import create_app
    admin = "adminkey"
    c = TestClient(create_app(db_path=":memory:", admin_key=admin, rate_per_minute=600.0,
                              notify_dir=str(tmp_path / "notify")))
    def seat(aid, operator=False):
        r = c.post("/agents", json={"id": aid, "operator": operator}, headers={"Authorization": f"Bearer {admin}"})
        return {"Authorization": f"Bearer {r.json()['api_key']}"}
    return c, seat


def test_a_fact_row_is_owned_by_its_first_writer_and_its_writes_are_on_the_record(tmp_path):
    """Fleet review F6 (2026-09-09), confirmed by the reviewer: `fact:` rows were
    writable by any member and a store write left no trace in the channel."""
    c, seat = _hub(tmp_path)
    wake, remove, laurent = seat("wakeeconomics"), seat("remove"), seat("laurent", operator=True)
    c.post("/channels", json={"name": "task-1", "private": False}, headers=laurent)
    for h in (wake, remove):
        c.post("/channels/task-1/join", json={}, headers=h)
    r = c.put("/channels/task-1/store/fact:driven-turn-cost", headers=wake, json={"value": {"tokens": 4800}})
    assert r.status_code == 200, r.text
    r = c.put("/channels/task-1/store/fact:driven-turn-cost", headers=remove, json={"value": {"tokens": 1}})
    assert r.status_code == 403 and "owned by its first writer 'wakeeconomics'" in r.text
    assert c.put("/channels/task-1/store/fact:driven-turn-cost", headers=wake, json={"value": {"tokens": 4900}}).status_code == 200
    assert c.put("/channels/task-1/store/fact:driven-turn-cost", headers=laurent, json={"value": {"tokens": 5000}}).status_code == 200, "channel authority may correct it"
    audits = [m for m in c.get("/channels/task-1/messages", headers=laurent).json() if str(m.get("title", "")).startswith("store:set fact:")]
    assert len(audits) == 3 and audits[0]["sender"] == "wakeeconomics" and audits[-1]["sender"] == "laurent"


def test_a_channel_file_is_co_edited_knowingly_or_not_at_all(tmp_path):
    """Fleet review F3 (2026-09-09), confirmed by the reviewer: any member could
    overwrite another seat's deliverable. Now: the last writer owns it; another
    seat passes the version it read (compare-and-swap) or has channel authority."""
    c, seat = _hub(tmp_path)
    alpha, beta, laurent = seat("alpha"), seat("beta"), seat("laurent", operator=True)
    c.post("/channels", json={"name": "task-1", "private": False}, headers=laurent)
    for h in (alpha, beta):
        c.post("/channels/task-1/join", json={}, headers=h)
    r = c.put("/channels/task-1/fs/shared/alpha.md", headers=alpha, json={"content": "# alpha v1"})
    assert r.status_code == 200, r.text
    r = c.put("/channels/task-1/fs/shared/alpha.md", headers=beta, json={"content": "# beta clobbers"})
    assert r.status_code == 403 and "written by 'alpha'" in r.text and "expect_version=1" in r.text
    r = c.put("/channels/task-1/fs/shared/alpha.md", headers=beta, json={"content": "# beta edits knowingly", "expect_version": 1})
    assert r.status_code == 200, r.text
    assert c.put("/channels/task-1/fs/shared/alpha.md", headers=laurent, json={"content": "# operator"}).status_code == 200
    assert c.put("/channels/task-1/fs/shared/alpha.md", headers=alpha, json={"content": "# alpha again, blind"}).status_code == 403, "alpha is no longer the last writer"


def test_an_addressed_open_whose_asks_name_others_is_read_only_for_the_rest(tmp_path):
    """20-seat run (2026-09-09): a manager's open to nine seats with one ask
    addressed to `gateway` made eight seats wonder whether they must decline.
    The envelope now says whose the asks are."""
    c, seat = _hub(tmp_path)
    mgr, gateway, flow = seat("mgr-apps"), seat("gateway"), seat("flow")
    c.post("/channels", json={"name": "task-1", "private": False}, headers=mgr)
    for h in (gateway, flow):
        c.post("/channels/task-1/join", json={}, headers=h)
    c.post("/channels/task-1/messages", headers=mgr, json={
        "title": "routing entity's seam to gateway", "body": "gateway: endpoints?", "status": "open",
        "to": ["gateway", "flow"], "asks": [{"id": "1", "text": "which endpoints?", "to": ["gateway"]}]})
    g = [e for e in c.get("/inbox", headers=gateway).json() if e["status"] == "open"][0]
    f = [e for e in c.get("/inbox", headers=flow).json() if e["status"] == "open"][0]
    assert g["asks_yours"] == ["1"] and g["asks_others"] is False
    assert f["asks_yours"] == [] and f["asks_others"] is True and f["to_me"] is True
    assert c.get("/owed", headers=flow).json()["to_answer"] == [], "flow owes a read, not an answer"
    assert c.get("/owed", headers=gateway).json()["to_answer"][0]["pending_asks"] == ["1"]
