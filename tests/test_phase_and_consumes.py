"""Collaboration v2 primitives (agora-0140) — the two the at-test fleet needed.

The 8-seat field test produced two mechanical failures the protocol could
not express:

- PHASE DISORDER (operator, verbatim): "one seat working on v4 while another
  was working on v3. No seat should work on a v4 until v3 is declared
  complete." What the fleet lacked was not a thread — it had plenty — but a
  machine-readable CURRENT-PHASE fact every reception pass reads. `phase:*`
  rows are that fact. They are ADVISORY BY CONSTRUCTION: the hub cannot know
  what a message or an edit "works on", so it never blocks; it makes the
  phase impossible to miss and rings a non-blocking doorbell on writes to the
  paths the row itself registers.

- CEREMONY, O(n²): one seat posted TEN identical "adopted and consumed"
  messages inside one second because the obligation model demands an
  on-the-record consumption per thread and no batch form existed; 26% of all
  253 messages carried zero information. `consumes=[...]` settles N debts
  with ONE message, through the same discharge path a reply uses.

What must not regress: authority is narrow and its refusals TEACH; a phase
row never refuses a post or a write; a consumes ref that settles nothing is
refused by name with nothing posted.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from agora.hub.app import create_app
from agora.listen import qualifies

ADMIN_KEY = "test-admin"


def make_client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0,
                     notify_dir=str(tmp_path / "notify"))
    return TestClient(app)


def register(client: TestClient, agent_id: str,
             operator: bool = False) -> dict[str, str]:
    r = client.post("/agents", json={"id": agent_id, "operator": operator},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_room(client: TestClient, owner: dict, name: str,
              *members: dict) -> None:
    client.post("/channels", json={"name": name, "private": False},
                headers=owner)
    for member in (owner, *members):
        client.post(f"/channels/{name}/join", json={}, headers=member)


def notify_lines(tmp_path: Path, agent_id: str) -> list[dict]:
    p = tmp_path / "notify" / f"{agent_id}-inbox.log"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line]


def set_phase(client, headers, channel, track, value, expect_version=None):
    body: dict = {"value": value}
    if expect_version is not None:
        body["expect_version"] = expect_version
    return client.put(f"/channels/{channel}/store/phase%3A{track}",
                      json=body, headers=headers)


# -- phase rows: authority ------------------------------------------------------

def test_phase_row_is_written_by_the_orchestrator_not_by_any_seat(tmp_path):
    """The operator's instinct made mechanical: 'possibly we just need an
    orchestrator who declares those for the hub channel.' A phase row
    constrains OTHER seats' work, so a seat that could mint one for itself
    could freeze the room. Owner/operator/delegate write it; the refusal
    names who can."""
    client = make_client(tmp_path)
    owner = register(client, "chair")
    writer = register(client, "writer")
    make_room(client, owner, "story", writer)

    refused = set_phase(client, writer, "story", "manuscript",
                        {"current": "v3", "status": "open"})
    assert refused.status_code == 403
    detail = refused.json()["detail"]
    assert "channel owner" in detail and "operator" in detail
    assert "ruling or operational" in detail

    ok = set_phase(client, owner, "story", "manuscript",
                   {"current": "v3", "status": "open", "next": "v4",
                    "steward": "writer", "paths": ["manuscript.md"]})
    assert ok.status_code == 200
    # declared_by/declared_at are HUB-stamped: a phase author is not forgeable.
    row = client.get("/channels/story/store/phase%3Amanuscript",
                     headers=writer).json()
    assert row["value"]["declared_by"] == "chair"
    assert row["value"]["declared_at"] > 0


def test_named_steward_may_declare_the_transition_and_hand_the_track_on(tmp_path):
    """One operator nomination ('that's why I nominated reader as delegate')
    hands a track to a seat, and that seat may hand it on — the steward may
    rewrite `steward`. This is the whole delegation story for phases."""
    client = make_client(tmp_path)
    owner = register(client, "chair")
    reader = register(client, "reader")
    editor = register(client, "editor")
    make_room(client, owner, "story", reader, editor)
    set_phase(client, owner, "story", "manuscript",
              {"current": "v3", "status": "open", "next": "v4",
               "steward": "reader"})

    blocked = set_phase(client, editor, "story", "manuscript",
                        {"current": "v4", "status": "open"})
    assert blocked.status_code == 403
    assert "stewarded by reader" in blocked.json()["detail"]
    assert "ask reader or the operator" in blocked.json()["detail"]

    done = set_phase(client, reader, "story", "manuscript",
                     {"current": "v3", "status": "complete", "next": "v4",
                      "steward": "reader"}, expect_version=1)
    assert done.status_code == 200
    handed = set_phase(client, reader, "story", "manuscript",
                       {"current": "v4", "status": "open", "next": "v5",
                        "steward": "editor"}, expect_version=2)
    assert handed.status_code == 200
    # The handoff is real: reader has no authority on this track anymore.
    assert set_phase(client, reader, "story", "manuscript",
                     {"current": "v5", "status": "open"},
                     expect_version=3).status_code == 403


def test_phase_row_shape_is_checked_at_write_like_a_ruling(tmp_path):
    client = make_client(tmp_path)
    owner = register(client, "chair")
    make_room(client, owner, "story")
    for bad in ({"current": "", "status": "open"},
                {"current": "v3", "status": "shipping"},
                {"current": "v3", "phase": "v3"},
                {"current": "v3", "paths": ["x"] * 17},
                {"current": "v3", "paths": [""]},
                ["not", "an", "object"]):
        r = set_phase(client, owner, "story", "manuscript", bad)
        assert r.status_code == 400, bad
    bare = client.put("/channels/story/store/phase%3A",
                      json={"value": {"current": "v3"}}, headers=owner)
    assert bare.status_code == 400
    assert "phase:<track>" in bare.json()["detail"]


# -- phase rows: surfacing ------------------------------------------------------

def test_phase_leads_the_digest_channel_info_and_the_owed_header(tmp_path):
    """A phase order nobody reads is the phase order that failed. It rides
    the three surfaces every seat touches: the digest (the 'returning after
    a gap' call), channel info (read before your first post), and /owed
    (what check_inbox leads every reception pass with)."""
    client = make_client(tmp_path)
    owner = register(client, "chair")
    writer = register(client, "writer")
    make_room(client, owner, "story", writer)
    set_phase(client, owner, "story", "manuscript",
              {"current": "v3", "status": "open", "next": "v4",
               "steward": "reader", "paths": ["manuscript.md"]})

    digest = client.get("/channels/story/digest", headers=writer).json()
    assert digest["counts"]["phases"] == 1
    line = digest["phase_lines"][0]
    assert "phase:manuscript: v3 OPEN (next: v4)" in line
    assert "do not start v4 work until v3 is declared complete" in line
    assert "steward reader" in line

    info = client.get("/channels/story/info", headers=writer).json()
    assert info["phases"][0]["current"] == "v3"
    assert info["phases"][0]["paths"] == ["manuscript.md"]

    owed = client.get("/owed", headers=writer).json()
    assert [p["key"] for p in owed["phases"]] == ["phase:manuscript"]
    assert owed["phases"][0]["channel"] == "story"

    # A COMPLETE phase stops constraining anyone: it leaves /owed and its
    # digest line flips to "the next phase may begin".
    set_phase(client, owner, "story", "manuscript",
              {"current": "v3", "status": "complete", "next": "v4"},
              expect_version=1)
    assert client.get("/owed", headers=writer).json()["phases"] == []
    digest = client.get("/channels/story/digest", headers=writer).json()
    assert "v3 COMPLETE — v4 may begin" in digest["phase_lines"][0]


def test_registered_path_write_rings_writer_and_steward_and_blocks_nothing(tmp_path):
    """The at-test collision, mechanized: two seats on different versions of
    one artifact is invisible to each of them alone, so BOTH the writer and
    the steward hear it. The write always succeeds — the writer may be fixing
    the current phase, which the hub cannot tell from starting the next one."""
    client = make_client(tmp_path)
    owner = register(client, "chair")
    writer = register(client, "writer")
    reader = register(client, "reader")
    make_room(client, owner, "story", writer, reader)
    set_phase(client, owner, "story", "manuscript",
              {"current": "v3", "status": "open", "next": "v4",
               "steward": "reader", "paths": ["manuscript.md"]})

    r = client.put("/channels/story/fs/manuscript.md",
                   json={"content": "chapter one, take four"}, headers=writer)
    assert r.status_code == 200, "an advisory is never a gate"

    mine = [line for line in notify_lines(tmp_path, "writer")
            if "HUB NOTICE" in str(line.get("preview", ""))]
    assert len(mine) == 1
    # "nothing was blocked" must survive PREVIEW TRUNCATION — a clipped
    # advisory that reads like a refusal teaches the opposite lesson.
    assert mine[0]["preview"].startswith(
        "HUB NOTICE (advisory — nothing was blocked)")
    assert "you wrote manuscript.md" in mine[0]["preview"]
    assert "v3 OPEN" in mine[0]["preview"]
    # The TITLE is the whole headline on a tailer, and the body is clamped to
    # 200 chars — an actionable notice under a bare "hub notice" is one a
    # human scrolls past. `_deliver_doorbell`'s convention is that a caller
    # whose notice is actionable NAMES ITSELF; this one did not until a
    # client author read the hub and said so (agora-tui, commons#28).
    assert mine[0]["title"] == ("hub notice: you wrote manuscript.md while "
                                "phase:manuscript is open")
    # Non-waking by construction: an advisory must never spawn a turn.
    assert not qualifies(mine[0], "writer", important_only=True)
    assert str(mine[0]["id"]).startswith("notice:")

    theirs = [line for line in notify_lines(tmp_path, "reader")
              if "HUB NOTICE" in str(line.get("preview", ""))]
    assert len(theirs) == 1
    assert not qualifies(theirs[0], "reader", important_only=True)
    assert "writer wrote manuscript.md" in theirs[0]["preview"]
    assert "You steward this track" in theirs[0]["preview"]
    assert theirs[0]["title"] == ("hub notice: phase:manuscript is open and "
                                  "writer wrote manuscript.md — you steward it")

    # An UNREGISTERED path, and a COMPLETE phase, are both silent.
    client.put("/channels/story/fs/notes.md", json={"content": "scratch"},
               headers=writer)
    set_phase(client, owner, "story", "manuscript",
              {"current": "v3", "status": "complete", "paths": ["manuscript.md"]},
              expect_version=1)
    client.put("/channels/story/fs/manuscript.md",
               json={"content": "v4 opening"}, headers=writer)
    assert len([line for line in notify_lines(tmp_path, "writer")
                if "HUB NOTICE" in str(line.get("preview", ""))]) == 1


# -- batched consumption --------------------------------------------------------

def _five_debts(client, asker, answerer, channel="story"):
    """Five answered asks the asker now owes consumption for — the exact
    shape that produced TEN identical receipts in one second."""
    refs = []
    for i in range(5):
        root = client.post(f"/channels/{channel}/messages",
                           json={"body": f"drop-in {i}?", "status": "open",
                                 "title": f"drop-in {i}",
                                 "asks": [{"id": "1", "text": "ready?",
                                           "to": ["writer"]}]},
                           headers=asker).json()
        answer = client.post(f"/channels/{channel}/messages",
                             json={"body": f"ready: drop-in {i}",
                                   "status": "reply", "reply_to": root["id"],
                                   "answers": ["1"]},
                             headers=answerer).json()
        refs.append((root, answer))
    return refs


def test_one_message_settles_five_consumption_debts(tmp_path):
    """THE ceremony incident (at-test): ten identical 'adopted and consumed'
    messages in one second, because settling N threads cost N messages. One
    message with consumes=[...] now clears all five, and the transcript
    records WHICH debts it settled."""
    client = make_client(tmp_path)
    asker = register(client, "chair")
    writer = register(client, "writer")
    make_room(client, asker, "story", writer)
    refs = _five_debts(client, asker, writer)

    owed = client.get("/owed", headers=asker).json()
    assert owed["counts"]["to_consume"] == 5

    r = client.post("/channels/story/messages",
                    json={"body": "adopted all five drop-ins; merging tonight",
                          "status": "fyi", "title": "five drop-ins adopted",
                          "consumes": [f"story#{a['seq']}" for _, a in refs]},
                    headers=asker)
    assert r.status_code == 200
    assert len(r.json()["data"]["consumes"]) == 5     # normalized to ids
    assert r.json()["data"]["consumes"][0] == refs[0][1]["id"]

    after = client.get("/owed", headers=asker).json()
    assert after["counts"]["to_consume"] == 0


def test_consumes_accepts_the_thread_root_and_mixed_ref_forms(tmp_path):
    """A seat cites what it has in hand: the answer's channel#seq, a bare
    seq in this room, or the thread ROOT's id (which settles every
    unconsumed answer in it — 'I read the thread' is the honest unit)."""
    client = make_client(tmp_path)
    asker = register(client, "chair")
    writer = register(client, "writer")
    make_room(client, asker, "story", writer)
    refs = _five_debts(client, asker, writer)

    r = client.post("/channels/story/messages",
                    json={"body": "all read", "status": "fyi",
                          "consumes": [refs[0][0]["id"],          # root id
                                       str(refs[1][1]["seq"]),    # bare seq
                                       f"story#{refs[2][1]['seq']}",
                                       refs[3][1]["id"],          # answer id
                                       f"#{refs[4][1]['seq']}"]},
                    headers=asker)
    assert r.status_code == 200
    assert client.get("/owed", headers=asker).json()["counts"]["to_consume"] == 0


def test_consumes_refuses_unowed_refs_by_name_and_posts_nothing(tmp_path):
    """Silently accepting a ref that discharges nothing would recreate the
    failure the field test found: a seat believing it settled a debt that
    stays open. Refuse loudly, name each bad ref, post nothing."""
    client = make_client(tmp_path)
    asker = register(client, "chair")
    writer = register(client, "writer")
    make_room(client, asker, "story", writer)
    refs = _five_debts(client, asker, writer)
    before = len(client.get("/channels/story/messages", headers=asker).json())

    r = client.post("/channels/story/messages",
                    json={"body": "consumed", "status": "fyi",
                          "consumes": [f"story#{refs[0][1]['seq']}",
                                       "story#99999", "01HNOTAREALID"]},
                    headers=asker)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "story#99999" in detail and "01HNOTAREALID" in detail
    assert "/owed" in detail and "nothing was posted" in detail
    # Nothing posted, and the debt that WAS valid is still owed.
    after = len(client.get("/channels/story/messages", headers=asker).json())
    assert after == before
    assert client.get("/owed", headers=asker).json()["counts"]["to_consume"] == 5

    # Someone ELSE's debt is not yours to settle either.
    other = client.post("/channels/story/messages",
                        json={"body": "x", "status": "fyi",
                              "consumes": [f"story#{refs[0][1]['seq']}"]},
                        headers=writer)
    assert other.status_code == 400


def test_consumes_is_capped_and_rejects_empty(tmp_path):
    client = make_client(tmp_path)
    asker = register(client, "chair")
    make_room(client, asker, "story")
    empty = client.post("/channels/story/messages",
                        json={"body": "x", "status": "fyi", "consumes": []},
                        headers=asker)
    assert empty.status_code == 400
    big = client.post("/channels/story/messages",
                      json={"body": "x", "status": "fyi",
                            "consumes": [f"story#{i}" for i in range(40)]},
                      headers=asker)
    assert big.status_code == 400 and "cap is 32" in big.json()["detail"]


def test_steward_is_not_erased_by_omission(tmp_path):
    """A steward updating `status` without restating `steward` must not
    silently resign itself out of its own track (same doctrine as claim
    `owner`). Resigning is an EXPLICIT steward:"" — an act, not a typo."""
    client = make_client(tmp_path)
    owner = register(client, "chair")
    reader = register(client, "reader")
    make_room(client, owner, "story", reader)
    set_phase(client, owner, "story", "manuscript",
              {"current": "v3", "status": "open", "steward": "reader"})

    kept = set_phase(client, reader, "story", "manuscript",
                     {"current": "v3", "status": "complete"}, expect_version=1)
    assert kept.status_code == 200
    row = client.get("/channels/story/store/phase%3Amanuscript",
                     headers=reader).json()
    assert row["value"]["steward"] == "reader"
    # …and the steward can still act on the track it never meant to leave.
    assert set_phase(client, reader, "story", "manuscript",
                     {"current": "v4", "status": "open"},
                     expect_version=2).status_code == 200
    # Explicit resignation IS honored.
    assert set_phase(client, reader, "story", "manuscript",
                     {"current": "v4", "status": "open", "steward": ""},
                     expect_version=3).status_code == 200
    assert set_phase(client, reader, "story", "manuscript",
                     {"current": "v5", "status": "open"},
                     expect_version=4).status_code == 403


def test_consumes_never_becomes_an_existence_oracle(tmp_path):
    """A ref naming a message in a channel the sender cannot read must be
    indistinguishable from a ref naming nothing at all — one refusal for
    both, or consumes becomes a cross-channel probe (the v0.3 IDOR class)."""
    client = make_client(tmp_path)
    asker = register(client, "chair")
    outsider = register(client, "outsider")
    make_room(client, asker, "story")
    make_room(client, outsider, "vault")
    secret = client.post("/channels/vault/messages",
                         json={"body": "internal", "status": "fyi",
                               "title": "vault note"},
                         headers=outsider).json()

    real = client.post("/channels/story/messages",
                       json={"body": "x", "status": "fyi",
                             "consumes": [f"vault#{secret['seq']}"]},
                       headers=asker)
    fake = client.post("/channels/story/messages",
                       json={"body": "x", "status": "fyi",
                             "consumes": ["vault#99999"]},
                       headers=asker)
    assert real.status_code == fake.status_code == 400
    assert (real.json()["detail"].replace(str(secret["seq"]), "N")
            == fake.json()["detail"].replace("99999", "N"))


def test_consumes_dedupes_the_same_debt_cited_twice(tmp_path):
    """One debt cited as its seq AND as its id is ONE settlement; the stored
    record should read as the set of debts settled, not as typing."""
    client = make_client(tmp_path)
    asker = register(client, "chair")
    writer = register(client, "writer")
    make_room(client, asker, "story", writer)
    refs = _five_debts(client, asker, writer)
    answer = refs[0][1]

    r = client.post("/channels/story/messages",
                    json={"body": "read it", "status": "fyi",
                          "consumes": [f"story#{answer['seq']}", answer["id"],
                                       str(answer["seq"])]},
                    headers=asker)
    assert r.status_code == 200
    assert r.json()["data"]["consumes"] == [answer["id"]]
    assert client.get("/owed", headers=asker).json()["counts"]["to_consume"] == 4
