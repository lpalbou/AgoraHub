"""The standalone ledger verifier must verify a real hub transcript from the
ledger response alone (docs/protocol.md canonicalization, rules 1-4), and
catch tampering. This is the spec's proof-of-sufficiency: the script imports
nothing from agora, so if these tests pass, the documented rules — not the
implementation — are what verified the chain."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agora.hub.app import create_app

ADMIN_KEY = "test-admin"

# Import scripts/verify_ledger.py as a module without packaging it.
_spec = importlib.util.spec_from_file_location(
    "verify_ledger", Path(__file__).parent.parent / "scripts" / "verify_ledger.py")
verify_ledger = importlib.util.module_from_spec(_spec)
sys.modules["verify_ledger"] = verify_ledger
_spec.loader.exec_module(verify_ledger)


@pytest.fixture()
def client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY, rate_per_minute=600.0)
    return TestClient(app)


def _register(client: TestClient, agent_id: str) -> str:
    r = client.post("/agents", headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                    json={"id": agent_id, "about": ""})
    assert r.status_code == 200
    return r.json()["api_key"]


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _make_transcript(client: TestClient) -> tuple[str, dict]:
    """A channel exercising every canonicalization hazard: unicode (escaping),
    nested unordered data (recursive key sorting), floats in data (shortest
    round-trip), addressing + criticality (the fields the old ledger response
    omitted), and a reply chain."""
    key = _register(client, "alice")
    key_b = _register(client, "bob")
    client.post("/channels", headers=_auth(key), json={"name": "verbatim"})
    invite = client.post("/channels/verbatim/invites", headers=_auth(key),
                         json={"agent_id": "bob"})
    assert invite.status_code == 200
    joined = client.post("/channels/verbatim/join", headers=_auth(key_b),
                         json={"invite_token": invite.json()["invite_token"]})
    assert joined.status_code == 200

    first = client.post("/channels/verbatim/messages", headers=_auth(key), json={
        "title": "unicode — émojis 🦉 and «quotes»",
        "body": "café \u00e9\u0301 mixed\nnewline", "status": "open",
        "to": ["bob"],
        # Floats chosen to pin the repr behaviors protocol.md rule 2 calls
        # out as diverging from ECMA-262: integral float (5.0 keeps .0),
        # zero-padded exponents (1e-07, 1e+16), negative zero.
        "data": {"z_last": {"b": 2, "a": 1}, "pi": 3.141592653589793,
                 "neg": -0.5, "big": 1752430471.123456, "n": None,
                 "integral": 5.0, "tiny": 1e-07, "huge": 1e+16,
                 "negzero": -0.0,
                 "arr": [1, "two", {"y": 0, "x": 1}]},
    })
    assert first.status_code == 200
    first_id = first.json()["id"]
    r = client.post("/channels/verbatim/messages", headers=_auth(key_b), json={
        "title": "reply", "body": "plain ascii", "status": "reply",
        "reply_to": first_id,
    })
    assert r.status_code == 200
    ledger = client.get("/channels/verbatim/ledger", headers=_auth(key)).json()
    return key, ledger


def test_standalone_verifier_confirms_a_real_transcript(client):
    _, ledger = _make_transcript(client)
    assert ledger["verified"] is True                    # hub's own view
    result = verify_ledger.verify(ledger)                # independent recompute
    assert result["ok"] is True
    assert result["broken_at"] is None
    # system turns (channel created / member joined) are chained too
    assert result["hashed"] == len(ledger["turns"]) >= 2
    assert result["computed_head"] == ledger["head"]

    # The response itself must carry every hashed field (a verifier cannot
    # invent urgency/critical/downgraded/to) with hash-time types.
    # alice authors two rows now: the seeded charter's fs audit (every room is
    # born with a charter, 0146) and her actual post. Pick the post.
    mine = next(t for t in ledger["turns"]
                if t["sender"] == "alice" and t["kind"] == "message")
    for field in ("urgency", "critical", "downgraded", "to"):
        assert field in mine
    assert mine["critical"] in (0, 1) and mine["downgraded"] in (0, 1)
    assert mine["to"] == ["bob"]


def test_verifier_detects_tampering_and_names_the_seq(client):
    _, ledger = _make_transcript(client)
    edited = next(i for i, t in enumerate(ledger["turns"]) if t["sender"] == "alice")
    tampered = json.loads(json.dumps(ledger))
    tampered["turns"][edited]["body"] = "café \u00e9\u0301 mixed\nnewline (edited)"
    result = verify_ledger.verify(tampered)
    assert result["ok"] is False
    assert result["broken_at"] == tampered["turns"][edited]["seq"]

    # A recomputed-but-unanchored rewrite: fixing the edited turn's hash still
    # breaks at the next turn (its prev no longer matches) — the doc's
    # "wholesale rewrite needs every subsequent hash AND changes the head".
    rehashed = json.loads(json.dumps(tampered))
    prev = rehashed["turns"][edited - 1]["hash"] if edited else ""
    rehashed["turns"][edited]["hash"] = verify_ledger.turn_hash(
        prev, rehashed["turns"][edited], rehashed["channel"])
    result = verify_ledger.verify(rehashed)
    assert result["ok"] is False
    assert result["broken_at"] == rehashed["turns"][edited + 1]["seq"]


def test_verifier_head_mismatch_is_caught(client):
    _, ledger = _make_transcript(client)
    forged = json.loads(json.dumps(ledger))
    forged["head"] = "0" * 64
    result = verify_ledger.verify(forged)
    assert result["ok"] is False and result["broken_at"] is None
    assert result["head_mismatch"] is True


def test_unhashed_turn_after_a_hashed_one_is_tampering(client):
    """Rule 3: legacy (hash: null) turns are legitimate only BEFORE the chain
    starts. Un-hashing a tail turn must flag, not silently restart — and the
    served head must stay the last HASHED turn's hash, so the hub and a
    doc-faithful verifier agree on this scenario (spec review finding)."""
    key, ledger = _make_transcript(client)
    last = ledger["turns"][-1]

    # Verifier side: null the tail hash in the served document.
    doc = json.loads(json.dumps(ledger))
    doc["turns"][-1]["hash"] = None
    result = verify_ledger.verify(doc)
    assert result["ok"] is False
    assert result["broken_at"] == last["seq"]

    # Hub side: same surgery in the database — verified must flip false and
    # the served head must be the last hashed turn's hash, not "".
    from agora.hub import http_api  # the service is on the app; reach its db
    service = client.app.state.service
    service.db._conn.execute("UPDATE messages SET hash = NULL WHERE channel = ?"
                             " AND seq = ?", ("verbatim", last["seq"]))
    service.db._conn.commit()
    after = client.get("/channels/verbatim/ledger", headers=_auth(key)).json()
    assert after["verified"] is False
    assert after["broken_at"] == last["seq"]
    assert after["head"] == ledger["turns"][-2]["hash"]  # last still-hashed turn


def test_non_finite_data_is_refused_at_post(client):
    """NaN/Infinity would hash and store but make the ledger unservable and
    unparseable outside Python — the hub must refuse them with a teaching 400
    (strict-JSON gate), keeping every stored transcript verifiable."""
    key = _register(client, "carol")
    client.post("/channels", headers=_auth(key), json={"name": "strict"})
    # httpx refuses to ENCODE inf, but Python's stdlib json.dumps emits
    # `Infinity` by default (allow_nan=True) — so a real client can and will
    # produce this body. Send it raw, exactly as such a client would.
    body = '{"title": "bad", "body": "x", "status": "fyi", "data": {"x": Infinity}}'
    r = client.post("/channels/strict/messages",
                    headers={**_auth(key), "Content-Type": "application/json"},
                    content=body)
    assert r.status_code == 400
    assert "NaN/Infinity" in r.json()["detail"]
    # The channel stays healthy and verifiable after the refusal.
    led = client.get("/channels/strict/ledger", headers=_auth(key)).json()
    assert led["verified"] is True
    assert verify_ledger.verify(led)["ok"] is True


def test_verifier_cli_exit_codes(client, tmp_path, capsys):
    _, ledger = _make_transcript(client)
    good = tmp_path / "ledger.json"
    good.write_text(json.dumps(ledger))
    assert verify_ledger.main([str(good)]) == 0
    assert "INTACT" in capsys.readouterr().out

    ledger["turns"][1]["title"] = "edited"
    bad = tmp_path / "tampered.json"
    bad.write_text(json.dumps(ledger))
    assert verify_ledger.main([str(bad)]) == 1
    assert "TAMPERED" in capsys.readouterr().out

    assert verify_ledger.main([str(tmp_path / "missing.json")]) == 2
