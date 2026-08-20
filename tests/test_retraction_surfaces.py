"""Retraction is REDACTION EVERYWHERE (0097), proven surface by surface.

test_retraction.py proves the verb; this file is the adversarial sweep that
proves the *promise*: after a retraction, the message's distinctive words are
unreachable through EVERY agent-facing read the hub serves. The words are
planted as unique nonce tokens and hunted in the serialized JSON of each
response, so a leak through a field nobody thought to assert on still fails.

Kept deliberately blunt: one fixture builds a room with a doomed message
carrying words in title, body, ask text and attachment description; every
surface below then gets swept. Adding a read surface to the hub without
adding it here is how the promise rots.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from agora.hub.app import create_app

BODY = "zorblattbody"
TITLE = "zorblatttitle"
ASK = "zorblattask"
FILE = "zorblattfile"
TOKENS = (BODY, TITLE, ASK, FILE)

ADMIN_KEY = "test-admin"
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}


def leaks(obj) -> list[str]:
    """Every nonce token found anywhere in the serialized payload."""
    blob = json.dumps(obj, default=str).lower()
    return [t for t in TOKENS if t in blob]


def register(client, agent_id, operator=False):
    r = client.post("/agents", json={"id": agent_id, "operator": operator},
                    headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


@pytest.fixture()
def world(tmp_path):
    """A room with one doomed message (retracted by an OPERATOR, not its
    author — the operator-override path the console exposes), one live reply,
    a rating and a work reference, plus a hub-written notify dir."""
    notify = tempfile.mkdtemp(dir=str(tmp_path))
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=6000.0, dark_watch_seconds=0,
                     notify_dir=notify)
    client = TestClient(app)
    ka = register(client, "alice")
    kb = register(client, "bob")
    kop = register(client, "op", operator=True)
    client.post("/channels", json={"name": "room", "private": False}, headers=ka)
    client.post("/channels/room/join", json={}, headers=kb)
    client.post("/channels/room/join", json={}, headers=kop)

    blob = client.post("/channels/room/attachments",
                       params={"filename": f"{FILE}.txt"},
                       headers=ka, content=b"hi")
    assert blob.status_code == 200, blob.text
    doomed = client.post("/channels/room/messages", headers=ka, json={
        "title": f"{TITLE} plan",
        "body": f"{BODY} the deprecated approach work:agora-0001",
        "status": "open",
        "urgency": "interrupt",
        "asks": [{"id": "1", "text": f"{ASK} please confirm", "to": ["bob"]}],
        "attachments": [{"id": blob.json()["id"], "filename": f"{FILE}.txt"}],
    })
    assert doomed.status_code == 200, doomed.text
    doomed = doomed.json()
    reply = client.post("/channels/room/messages", headers=kb, json={
        "title": "re", "body": "acknowledged", "status": "reply",
        "reply_to": doomed["id"], "answers": ["1"]}).json()
    client.put(f"/channels/room/messages/{doomed['id']}/rating",
               headers=kb, json={"vote": "up"})

    # Pre-condition: the words ARE findable before the retraction, so a
    # "clean" verdict below cannot be an artifact of a broken index.
    pre = client.get("/search", params={"q": BODY}, headers=kb).json()
    assert leaks(pre), "search never indexed the doomed words — test is blind"

    r = client.post(f"/channels/room/messages/{doomed['id']}/retract",
                    headers=kop)
    assert r.status_code == 200, r.text
    assert r.json()["retracted"] is True
    return {"client": client, "ka": ka, "kb": kb, "kop": kop,
            "doomed": doomed, "reply": reply, "notify": notify}


# -- the sweep -----------------------------------------------------------------

def _surfaces(w) -> list[tuple[str, str, dict, dict]]:
    """(name, path, params, headers) for every agent-facing GET."""
    kb, kop, ka = w["kb"], w["kop"], w["ka"]
    mid, seq = w["doomed"]["id"], w["doomed"]["seq"]
    rid = w["reply"]["id"]
    return [
        ("messages", "/channels/room/messages", {}, kb),
        ("messages?since=0", "/channels/room/messages", {"since": 0}, kb),
        ("messages?since=backfill", "/channels/room/messages",
         {"since": max(seq - 1, 0)}, kb),
        ("messages?sort=votes", "/channels/room/messages", {"sort": "votes"}, kb),
        ("messages/by-seq", f"/channels/room/messages/by-seq/{seq}", {}, kb),
        ("read_message(doomed)", f"/channels/room/messages/{mid}", {}, kb),
        ("read_message(reply→ancestors)", f"/channels/room/messages/{rid}", {}, kop),
        ("inbox(bob)", "/inbox", {}, kb),
        ("inbox(op)", "/inbox", {}, kop),
        ("owed(bob)", "/owed", {}, kb),
        ("owed(alice)", "/owed", {}, ka),
        ("desk", "/desk", {}, kop),
        ("board(bob)", "/board", {}, kb),
        ("board(op)", "/board", {}, kop),
        ("digest", "/channels/room/digest", {}, kb),
        ("search q=body", "/search", {"q": BODY}, kb),
        ("search q=title", "/search", {"q": TITLE}, kb),
        ("search q=ask", "/search", {"q": ASK}, kb),
        ("search q=file", "/search", {"q": FILE}, kb),
        ("search mode=lexical", "/search", {"q": BODY, "mode": "lexical"}, kb),
        ("search rated=up (browse)", "/search", {"rated": "up"}, kb),
        ("search ref=work:agora-0001", "/search",
         {"ref": "work:agora-0001", "rated": "any"}, kb),
        ("ledger", "/channels/room/ledger", {}, kb),
        ("ledger?verify=false", "/channels/room/ledger", {"verify": "false"}, kb),
        ("channel work rows", "/channels/room/work", {}, kb),
        ("work activity", "/work/agora-0001", {}, kb),
        ("store", "/channels/room/store", {}, kb),
        ("fs", "/channels/room/fs", {}, kb),
        ("channel info", "/channels/room/info", {}, kb),
        ("ratings", f"/channels/room/messages/{mid}/ratings", {}, kb),
        ("status", "/status", {}, kop),
        ("stats/activity", "/stats/activity", {}, kb),
        ("supervise", "/supervise", {}, kop),
        ("channels", "/channels", {}, kb),
    ]


def test_no_agent_facing_read_serves_retracted_words(world):
    client = world["client"]
    dirty = {}
    for name, path, params, headers in _surfaces(world):
        r = client.get(path, params=params, headers=headers)
        assert r.status_code in (200, 403, 404), f"{name}: {r.status_code} {r.text}"
        try:
            payload = r.json()
        except ValueError:                       # pragma: no cover - defensive
            payload = r.text
        found = leaks(payload)
        if found:
            dirty[name] = found
    assert not dirty, f"retracted words still served by: {dirty}"


def test_admin_surfaces_serve_no_retracted_words(world):
    """Admin-key reads are still *agent*-reachable in practice (the operator's
    console runs them); the words must be gone there too."""
    client = world["client"]
    dirty = {}
    for name, path in [("admin/status", "/admin/status"),
                       ("admin/doctor", "/admin/doctor"),
                       ("admin/noise", "/admin/noise"),
                       ("admin/search/drift", "/admin/search/drift")]:
        r = client.get(path, headers=ADMIN)
        found = leaks(r.json() if r.headers.get("content-type", "").startswith(
            "application/json") else r.text)
        if found:
            dirty[name] = found
    assert not dirty, f"retracted words still served by: {dirty}"


def test_websocket_reconnect_backlog_serves_a_tombstone(world):
    """The catch-up path a reconnecting agent takes: subscribe with since=0
    replays history — the retracted turn must arrive redacted."""
    client, kb = world["client"], world["kb"]
    token = kb["Authorization"].split()[1]
    # Bound the drain by what the hub can possibly replay to bob (his own
    # posts are never echoed) so a missing frame fails instead of hanging.
    history = client.get("/channels/room/messages", headers=kb).json()
    expected = sum(1 for m in history if m["sender"] != "bob")
    frames = []
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["room"],
                      "since": {"room": 0}})
        assert ws.receive_json()["type"] == "subscribed"
        for _ in range(expected):
            frames.append(ws.receive_json())
    envelopes = [f["envelope"] for f in frames if f.get("type") == "envelope"]
    tombstone = next(e for e in envelopes if e["id"] == world["doomed"]["id"])
    assert tombstone["retracted"] is True
    assert not leaks(frames), f"ws backlog leaked: {leaks(frames)}"


def test_websocket_live_retraction_push_carries_no_words(world):
    """The retraction broadcast itself: subscribers are told to redact in
    place, and the payload that tells them must not re-ship the words."""
    client, kb, ka = world["client"], world["kb"], world["ka"]
    token = kb["Authorization"].split()[1]
    head = client.get("/channels/room/messages", headers=kb).json()[-1]["seq"]
    with client.websocket_connect(f"/ws?token={token}") as ws:
        # since = current head: subscribe with an empty backlog, so every
        # frame below is a LIVE push and nothing can block on drain order.
        ws.send_json({"type": "subscribe", "channels": ["room"],
                      "since": {"room": head}})
        assert ws.receive_json()["type"] == "subscribed"
        fresh = client.post("/channels/room/messages", headers=ka, json={
            "title": f"{TITLE} again", "body": f"{BODY} again"}).json()
        live = ws.receive_json()
        assert leaks(live), "precondition: the live push carried the words"
        client.post(f"/channels/room/messages/{fresh['id']}/retract", headers=ka)
        # A sentinel posted AFTER the retraction turns "the tombstone never
        # arrives" into a failure instead of a hang: whichever frame lands
        # first tells the truth about whether the wake survived the pump.
        sentinel = client.post("/channels/room/messages", headers=ka, json={
            "title": "sentinel", "body": "sentinel"}).json()
        pushed = ws.receive_json()
    assert pushed["envelope"]["id"] != sentinel["id"], (
        "the retraction wake never reached the live subscriber — it holds "
        "the original words with nothing telling it to redact")
    assert pushed["envelope"]["id"] == fresh["id"]
    assert pushed["envelope"]["retracted"] is True
    assert not leaks(pushed), f"retraction push leaked: {leaks(pushed)}"


def test_notify_log_retraction_line_carries_no_preview(world):
    """`<seat>-inbox.log` is a hub-written liveness tail. The retraction line
    the hub appends must be a tombstone: `agora listen` tails from the END,
    so this line is what a listener sees after the fact."""
    lines = []
    for name in os.listdir(world["notify"]):
        with open(os.path.join(world["notify"], name)) as fh:
            for raw in fh:
                row = json.loads(raw)
                if row["id"] == world["doomed"]["id"]:
                    lines.append(row)
    assert lines, "no notify line for the doomed message"
    last = lines[-1]
    assert last["retracted"] is True
    assert not leaks(last), f"notify tombstone leaked: {leaks(last)}"


def test_search_index_row_is_purged_not_just_filtered(world):
    """Match-then-redact is an oracle: a hit count alone would confirm the
    words. The FTS doc must be gone, not merely filtered at serve time."""
    client, kb = world["client"], world["kb"]
    for token in TOKENS:
        report = client.get("/search", params={"q": token}, headers=kb).json()
        hits = [h for group in report.get("groups", [])
                for h in group.get("hits", [])
                if h.get("id") == world["doomed"]["id"]]
        assert not hits, f"q={token} still matches the retracted message"


def test_retracted_message_stays_out_of_a_rebuilt_search_index(world):
    """An operator rebuilding the index must not resurrect the words: the
    rebuild source has to honor the retraction filter."""
    client, kb = world["client"], world["kb"]
    r = client.post("/admin/search/rebuild", headers=ADMIN)
    assert r.status_code == 200, r.text
    report = client.get("/search", params={"q": BODY}, headers=kb).json()
    assert not leaks(report), f"rebuild resurrected: {leaks(report)}"


def test_ledger_serves_a_tombstone_and_still_verifies(world):
    """The verbatim ledger is a read surface too. It must serve the tombstone
    (P0: it served the original bytes to any member until this sweep) while
    the chain stays intact — retraction is presentation, never a rewrite."""
    client, kb = world["client"], world["kb"]
    led = client.get("/channels/room/ledger", headers=kb).json()
    turn = next(t for t in led["turns"] if t["id"] == world["doomed"]["id"])
    assert turn["retracted"] is True
    assert turn["body"] == "[retracted by op]"
    assert turn["title"] == "" and turn["data"] is None and turn["status"] == "fyi"
    assert turn["hash"], "the chain link survives the redaction"
    # The hub verifies from the ORIGINAL bytes it still stores.
    assert led["verified"] is True and led["broken_at"] is None


def test_standalone_verifier_links_through_a_redacted_turn(world):
    """scripts/verify_ledger.py is the third-party check. A retracted turn is
    link-only; every other turn must still be recomputed and verified, so a
    real tamper elsewhere is still caught."""
    import importlib.util
    import pathlib

    here = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "verify_ledger.py"
    spec = importlib.util.spec_from_file_location("verify_ledger", here)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    client, kb = world["client"], world["kb"]
    led = client.get("/channels/room/ledger", headers=kb).json()
    result = mod.verify(led)
    assert result["ok"], result
    assert result["redacted"] == 1

    # Tamper with a LIVE turn: still caught, so the link-through is narrow.
    live = next(t for t in led["turns"] if t["id"] == world["reply"]["id"])
    live["body"] = "tampered"
    assert not mod.verify(led)["ok"]


def test_author_socket_learns_of_an_operator_retraction(world):
    """The self-skip must not hide an operator's retraction from the seat
    that wrote the words: alice's client would otherwise keep rendering
    them with nothing ever telling it to redact."""
    client, ka, kop = world["client"], world["ka"], world["kop"]
    token = ka["Authorization"].split()[1]
    head = client.get("/channels/room/messages", headers=ka).json()[-1]["seq"]
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["room"],
                      "since": {"room": head}})
        assert ws.receive_json()["type"] == "subscribed"
        mine = client.post("/channels/room/messages", headers=ka, json={
            "title": f"{TITLE} mine", "body": f"{BODY} mine"}).json()
        client.post(f"/channels/room/messages/{mine['id']}/retract", headers=kop)
        sentinel = client.post("/channels/room/messages", headers=kop, json={
            "title": "sentinel", "body": "sentinel"}).json()
        pushed = ws.receive_json()
    assert pushed["envelope"]["id"] != sentinel["id"], (
        "alice never learned her own message was retracted by the operator")
    assert pushed["envelope"]["id"] == mine["id"]
    assert pushed["envelope"]["retracted"] is True
    assert not leaks(pushed)


def test_attachment_bytes_die_with_the_last_message_that_carried_them(world):
    """Retraction drops the attachment REF, so no surface hands a new reader
    the blob id — but an agent that read the message first memorized it. A
    blob whose every referencing message is retracted stops serving."""
    client, kb, ka = world["client"], world["kb"], world["ka"]
    blob_id = None
    # Recover the id the way a reader would have before the retraction: from
    # the attachment upload in the fixture, replayed by content address.
    up = client.post("/channels/room/attachments",
                     params={"filename": f"{FILE}.txt"}, headers=ka, content=b"hi")
    blob_id = up.json()["id"]
    r = client.get(f"/channels/room/attachments/{blob_id}", headers=kb)
    assert r.status_code == 404, "the retracted message's file still serves bytes"
    assert "retracted" in r.text


def test_a_live_message_keeps_its_shared_attachment_alive(world):
    """Blobs are content-addressed and shared: retracting ONE message that
    cites a file must not vaporize it for a message that still cites it."""
    client, ka, kb, kop = world["client"], world["ka"], world["kb"], world["kop"]
    up = client.post("/channels/room/attachments",
                     params={"filename": "shared.txt"}, headers=ka, content=b"shared bytes")
    blob_id = up.json()["id"]
    doomed = client.post("/channels/room/messages", headers=ka, json={
        "title": "one", "body": "one", "attachments": [{"id": blob_id}]}).json()
    client.post("/channels/room/messages", headers=ka, json={
        "title": "two", "body": "two", "attachments": [{"id": blob_id}]})
    client.post(f"/channels/room/messages/{doomed['id']}/retract", headers=kop)
    r = client.get(f"/channels/room/attachments/{blob_id}", headers=kb)
    assert r.status_code == 200 and r.content == b"shared bytes"


def test_an_unposted_upload_still_serves(world):
    """The compose flow: an uploaded blob no message references yet has
    nothing to unsay, so the refusal must not swallow it."""
    client, ka = world["client"], world["ka"]
    up = client.post("/channels/room/attachments",
                     params={"filename": "draft.txt"}, headers=ka, content=b"draft bytes")
    r = client.get(f"/channels/room/attachments/{up.json()['id']}", headers=ka)
    assert r.status_code == 200 and r.content == b"draft bytes"
