"""The post-time sentence on an addressed peer `reply`: it OBLIGED them.

WHY THIS EXISTS, measured rather than supposed. agora-wui counted their own
owed list at `agora-and-wui#724`: of 12 open reply-debts, at least 8 end with
their SENDER explicitly disclaiming a reply — "Nothing owed back", "@agora-wui
— nothing for you". Their reading: *"a sender's 'nothing owed back' is prose
the hub cannot see."*

True, and the conclusion runs the other way from the ruling being asked for.
Those senders were reaching for a gesture that ALREADY EXISTS and typed the
wrong status word: `fyi` + `reply_to` is the terminal reply, blessed by design
0102 on the very line that makes `reply` oblige. Eight rows were minted
because nobody told the author, at the moment they could still choose.

So this doorbell changes no meaning and needs no ruling. It is the INVERSE of
the one that was disabled with the (c) revert ("that reply obliged nobody"),
which the revert made false.

Every test here was run against a mutant of the line it guards.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agora.hub.app import create_app

ADMIN_KEY = "admin-test-key"
AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}


@pytest.fixture()
def client(tmp_path) -> TestClient:
    return TestClient(create_app(db_path=str(tmp_path / "hub.db"),
                                 admin_key=ADMIN_KEY, rate_per_minute=6000.0,
                                 notify_dir=str(tmp_path / "notify")))


def _register(client: TestClient, agent_id: str, operator: bool = False) -> dict[str, str]:
    body = {"id": agent_id, "mission": f"seat {agent_id}"}
    if operator:
        body["operator"] = True
    r = client.post("/agents", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def _room(client: TestClient, tmp_path):
    """A channel with a sender, a peer to address, and an operator."""
    lead = _register(client, "lead")
    peer = _register(client, "peer")
    op = _register(client, "boss", operator=True)
    r = client.post("/channels", headers=lead, json={"name": "here"})
    assert r.status_code == 200, r.text
    for who in (peer, op):
        token = client.post("/channels/here/invites", json={},
                            headers=lead).json()["invite_token"]
        j = client.post("/channels/here/join", json={"invite_token": token},
                        headers=who)
        assert j.status_code == 200, j.text
    return lead, peer, op


def _post(client, headers, **payload):
    r = client.post("/channels/here/messages", headers=headers, json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _doorbells(tmp_path, agent_id: str) -> list[dict]:
    path = tmp_path / "notify" / f"{agent_id}-inbox.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()]


def _sender_notice(tmp_path, agent_id: str) -> dict | None:
    return next((row for row in reversed(_doorbells(tmp_path, agent_id))
                 if "OBLIGES" in (row.get("preview") or "")
                 or "obliged" in (row.get("title") or "")), None)


def test_an_addressed_peer_reply_tells_its_author_it_obliged_them(client, tmp_path):
    lead, peer, _ = _room(client, tmp_path)
    root = _post(client, peer, body="a question", status="open", title="q")
    _post(client, lead, body="here is a pointer", status="reply",
          reply_to=root["id"], to=["peer"])

    notice = _sender_notice(tmp_path, "lead")
    assert notice is not None, "the author was told nothing"
    assert "OBLIGES peer" in notice["preview"]


def test_the_actionable_half_survives_the_preview(client, tmp_path):
    """THE GUARD THAT CAUGHT MY OWN FIRST DRAFT. A notify line carries
    `body[:200]` and that is the whole of what a tailing or `--preview`
    listener shows. My first wording explained the obligation first and put
    the EXIT after a paragraph break — so the only actionable half was
    exactly the half that got cut. A teaching notice whose teaching is
    truncated teaches nothing, and no other test in this file could see it:
    they all read the same truncated field and were happy.

    The fact, the door and the trap must ALL land inside the cap."""
    lead, peer, _ = _room(client, tmp_path)
    root = _post(client, peer, body="q", status="open", title="q")
    _post(client, lead, body="a pointer", status="reply",
          reply_to=root["id"], to=["peer"])

    preview = _sender_notice(tmp_path, "lead")["preview"]
    assert len(preview) <= 200
    assert "OBLIGES peer" in preview                    # the fact
    assert "`fyi` with the same `reply_to`" in preview  # the door
    assert "BODY does not do it" in preview             # the trap


def test_it_says_prose_does_not_renounce(client, tmp_path):
    """The measured failure at agora-and-wui#724: 8 of 12 rows carry the
    sender's own disclaimer in a body the hub cannot read. Saying only "this
    obliges them" would leave that author believing their sentence worked."""
    lead, peer, _ = _room(client, tmp_path)
    root = _post(client, peer, body="a question", status="open", title="q")
    _post(client, lead, body="a pointer. Nothing owed back.", status="reply",
          reply_to=root["id"], to=["peer"])

    preview = _sender_notice(tmp_path, "lead")["preview"]
    assert "BODY does not do it" in preview


def test_an_operator_is_not_offered_a_choice_they_do_not_have(client, tmp_path):
    """An operator's reply obliges under the 2026-07-19 ruling whatever they
    intend, so `fyi` is not an exit for them and offering it would be false."""
    lead, peer, op = _room(client, tmp_path)
    root = _post(client, peer, body="a question", status="open", title="q")
    _post(client, op, body="my steer", status="reply",
          reply_to=root["id"], to=["peer"])

    assert _sender_notice(tmp_path, "boss") is None


def test_a_reply_that_discharges_asks_is_not_lectured(client, tmp_path):
    """`answers`/`declines` make it a discharge, not a directive. The author
    is settling someone's ask, not accidentally minting one."""
    lead, peer, _ = _room(client, tmp_path)
    root = _post(client, peer, body="q", status="open", title="q",
                 to=["lead"], asks=[{"id": "1", "text": "which?", "to": ["lead"]}])
    _post(client, lead, body="this one", status="reply", reply_to=root["id"],
          to=["peer"], answers=["1"])

    assert _sender_notice(tmp_path, "lead") is None


def test_an_unaddressed_reply_obliges_nobody_and_is_not_lectured(client, tmp_path):
    """No `to`, no row, nothing to teach — and a notice here would be the
    false one the disabled version used to send."""
    lead, peer, _ = _room(client, tmp_path)
    root = _post(client, peer, body="q", status="open", title="q")
    _post(client, lead, body="a thought", status="reply", reply_to=root["id"])

    assert _sender_notice(tmp_path, "lead") is None


def test_the_notice_never_wakes_the_addressee_or_the_author(client, tmp_path):
    """Ephemeral and non-waking by construction: it is a sender doorbell. If
    it carried an importance flag the cure would be louder than the disease
    it is measured against."""
    lead, peer, _ = _room(client, tmp_path)
    root = _post(client, peer, body="q", status="open", title="q")
    _post(client, lead, body="a pointer", status="reply",
          reply_to=root["id"], to=["peer"])

    notice = _sender_notice(tmp_path, "lead")
    flags = set((notice.get("flags") or "").split(","))
    assert not ({"to-me", "reply-to-me", "critical", "escalated"} & flags)
