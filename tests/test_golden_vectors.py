"""Golden conformance vectors (agora-0118, parity move 7).

This file is the REFERENCE runner for `tests/vectors/*.json`: language-
independent fixtures that pin the wire contract's behavioral facts (who owes
what after which posts, what history rows say about their threads, what the
groups composite leaves behind). A TS/JS/Rust client proves parity by
replaying the same files over HTTP against a scratch hub and applying the
same matching rules:

- objects: every EXPECTED key present and matching; extra served keys allowed
  (additive evolution never breaks a vector; removal/rename does — on purpose:
  that is the wire-contract tripwire that forces a version bump and a
  PROTOCOL_SEMANTICS entry, the guard the unnamed 0102 change lacked).
- lists under "match": exact length, positional subset-match.
- lists under "match_subset": every expected element must subset-match SOME
  served element (order/extra rows free) — for surfaces with system chatter.
- "$ref.field": resolves to a captured value from the setup step that
  declared "ref" (posts capture the served message; groups capture the
  composite result).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agora.hub.app import create_app

ADMIN_KEY = "vector-admin"
VECTOR_DIR = Path(__file__).parent / "vectors"
# Numbered files are hub-replay vectors; canonicalization.json is the
# fixed-value ledger set with its own runner below.
VECTORS = sorted(p for p in VECTOR_DIR.glob("*.json")
                 if p.name[0].isdigit())


class VectorWorld:
    """One scratch hub + the ref table a vector accumulates while running."""

    def __init__(self) -> None:
        self.client = TestClient(create_app(
            db_path=":memory:", admin_key=ADMIN_KEY, rate_per_minute=6000.0))
        self.keys: dict[str, dict[str, str]] = {}
        self.refs: dict[str, dict[str, Any]] = {}

    # -- $ref resolution --------------------------------------------------
    def resolve(self, value: Any) -> Any:
        if isinstance(value, str) and value.startswith("$") and "." in value:
            name, field = value[1:].split(".", 1)
            # Only DECLARED refs resolve; any other $-string is a literal
            # (impl adversary P2-5: a body like "$1.50" must not KeyError).
            if name in self.refs:
                return self.refs[name][field]
            return value
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        return value

    # -- setup ops ---------------------------------------------------------
    def run_op(self, step: dict[str, Any]) -> None:
        op = step["op"]
        if op == "register":
            r = self.client.post("/agents", json={"id": step["id"]},
                                 headers={"Authorization": f"Bearer {ADMIN_KEY}"})
            assert r.status_code == 200, r.text
            self.keys[step["id"]] = {
                "Authorization": f"Bearer {r.json()['api_key']}"}
        elif op == "channel":
            owner = self.keys[step["owner"]]
            r = self.client.post("/channels", json={"name": step["name"]},
                                 headers=owner)
            assert r.status_code == 200, r.text
            for member in step.get("members", []):
                t = self.client.post(f"/channels/{step['name']}/invites",
                                     json={"agent_id": member}, headers=owner)
                assert t.status_code == 200, t.text
                j = self.client.post(f"/channels/{step['name']}/join",
                                     json={"invite_token": t.json()["invite_token"]},
                                     headers=self.keys[member])
                assert j.status_code == 200, j.text
        elif op == "post":
            payload = {k: self.resolve(v) for k, v in step.items()
                       if k in ("body", "title", "status", "to", "data",
                                "reply_to", "urgency")}
            r = self.client.post(f"/channels/{step['channel']}/messages",
                                 json=payload, headers=self.keys[step["as"]])
            assert r.status_code == 200, f"{step}: {r.text}"
            if "ref" in step:
                self.refs[step["ref"]] = r.json()
        elif op == "read":
            r = self.client.get(
                f"/channels/{step['channel']}/messages/{self.resolve(step['id'])}",
                headers=self.keys[step["as"]])
            assert r.status_code == 200, r.text
        elif op == "group":
            r = self.client.post("/groups", headers=self.keys[step["as"]], json={
                "name": step["name"], "members": step.get("members", []),
                "purpose": step.get("purpose", ""),
                "opening_post": step.get("opening_post", "")})
            assert r.status_code == 200, r.text
            if "ref" in step:
                self.refs[step["ref"]] = r.json()
        elif op == "rate":
            r = self.client.put(
                f"/channels/{step['channel']}/messages/"
                f"{self.resolve(step['message'])}/rating",
                json={"value": step["value"]},
                headers=self.keys[step["as"]])
            assert r.status_code == 200, f"{step}: {r.text}"
        elif op == "unrate":
            r = self.client.delete(
                f"/channels/{step['channel']}/messages/"
                f"{self.resolve(step['message'])}/rating",
                headers=self.keys[step["as"]])
            assert r.status_code == 200, f"{step}: {r.text}"
        elif op == "join_via_invite_dm":
            dm = self.client.get(f"/channels/{step['dm']}/messages",
                                 headers=self.keys[step["as"]]).json()
            tokens = [m["data"]["invite_token"] for m in dm
                      if (m.get("data") or {}).get("invite_token")]
            assert tokens, f"no invite token found in {step['dm']}"
            r = self.client.post(f"/channels/{step['channel']}/join",
                                 json={"invite_token": tokens[-1]},
                                 headers=self.keys[step["as"]])
            assert r.status_code == 200, r.text
        else:
            raise AssertionError(f"unknown op {op!r}")

    # -- expectations --------------------------------------------------------
    def run_expect(self, step: dict[str, Any]) -> None:
        call = step["call"]
        if call == "owed":
            served: Any = self.client.get(
                "/owed", headers=self.keys[step["as"]]).json()
        elif call == "messages":
            served = self.client.get(
                f"/channels/{step['channel']}/messages",
                params={"since": step.get("since", 0)},
                headers=self.keys[step["as"]]).json()
            # System rows (channel-created etc.) are hub chatter, not contract.
            served = [m for m in served if m.get("kind") == "message"]
        elif call == "leaderboard":
            path = (f"/channels/{step['channel']}/reputation"
                    if step.get("channel") else "/reputation")
            served = self.client.get(path, headers=self.keys[step["as"]]).json()
        elif call == "group_result":
            served = self.refs[step["ref"]]
        else:
            raise AssertionError(f"unknown call {call!r}")
        # A typo'd assertion key must FAIL, not silently assert nothing
        # (impl adversary P2-5: an expect step with 'mtach' went green).
        known = {"call", "as", "channel", "since", "ref", "match", "match_subset"}
        unknown = set(step) - known
        assert not unknown, f"unknown expect keys {sorted(unknown)} in {step}"
        assert "match" in step or "match_subset" in step, \
            f"expect step asserts nothing: {step}"
        if "match" in step:
            subset_match(self.resolve(step["match"]), served, path=call)
        if "match_subset" in step:
            expected = self.resolve(step["match_subset"])
            assert isinstance(served, list)
            for want in expected:
                if not any(_matches(want, got) for got in served):
                    raise AssertionError(
                        f"{call}: no served row matches {want!r};\n"
                        f"served: {json.dumps(served, indent=1)[:2000]}")


def _matches(expected: Any, served: Any) -> bool:
    try:
        subset_match(expected, served, path="")
        return True
    except AssertionError:
        return False


def subset_match(expected: Any, served: Any, path: str) -> None:
    """The reference matching rule (see module docstring)."""
    if isinstance(expected, dict):
        assert isinstance(served, dict), f"{path}: expected object, got {served!r}"
        for k, v in expected.items():
            assert k in served, f"{path}.{k}: MISSING from served payload " \
                                f"(wire-contract regression?) — served keys: " \
                                f"{sorted(served)}"
            subset_match(v, served[k], f"{path}.{k}")
    elif isinstance(expected, list):
        assert isinstance(served, list), f"{path}: expected list, got {served!r}"
        assert len(expected) == len(served), \
            f"{path}: expected {len(expected)} rows, served {len(served)}: " \
            f"{json.dumps(served, default=str)[:1500]}"
        for i, (e, s) in enumerate(zip(expected, served)):
            subset_match(e, s, f"{path}[{i}]")
    else:
        assert expected == served, f"{path}: expected {expected!r}, served {served!r}"


@pytest.mark.parametrize("vector_path", VECTORS, ids=[p.stem for p in VECTORS])
def test_golden_vector(vector_path: Path) -> None:
    vector = json.loads(vector_path.read_text())
    world = VectorWorld()
    for step in vector["setup"]:
        world.run_op(step)
    for step in vector["expect"]:
        if "op" in step:
            world.run_op(step)
        else:
            world.run_expect(step)


def test_vectors_exist_and_are_wellformed():
    """The suite must never silently run zero vectors (glob typo, move)."""
    assert len(VECTORS) >= 4
    for p in VECTORS:
        v = json.loads(p.read_text())
        assert v.get("name") and v.get("setup") and v.get("expect"), p.name


def test_canonicalization_fixtures():
    """The fixed-value ledger canonicalization set (design adversary P0-1):
    the highest-risk cross-client drift is number formatting (Python repr vs
    ECMA-262 — integral floats, exponent forms, -0.0), and it CANNOT be
    pinned by live-hub vectors because ledger hashes include timestamps.
    These cases have no clock: payload -> exact canonical string -> exact
    sha256. The hub side must reproduce them from its own _ledger_payload
    (proving the fixtures state the real rule, not a parallel one); any
    independent verifier replays the same file."""
    import hashlib

    from agora.db import Database

    doc = json.loads((VECTOR_DIR / "canonicalization.json").read_text())
    assert len(doc["cases"]) >= 5
    for case in doc["cases"]:
        canon = Database._ledger_payload(**case["payload"])
        assert canon == case["canonical"], case["why"]
        digest = hashlib.sha256(("\n" + canon).encode()).hexdigest()
        assert digest == case["sha256_of_empty_prev"], case["why"]


def test_subset_matcher_self_fixtures():
    """The matching rule is itself a parity surface (design adversary P1-6):
    a TS runner re-implements it from prose, and a matcher drift silently
    weakens every vector. These triples pin the verdicts any runner must
    reproduce."""
    triples: list[tuple[Any, Any, bool]] = [
        ({"a": 1}, {"a": 1, "extra": 2}, True),     # extra served keys OK
        ({"a": 1}, {"a": 2}, False),                 # value mismatch
        ({"a": None}, {}, False),                    # expected null != absent
        ({"a": []}, {"a": []}, True),                # empty list matches
        ([{"a": 1}], [{"a": 1}, {"a": 2}], False),   # match: length is exact
        ({"a": {"b": [1]}}, {"a": {"b": [1], "c": 2}}, True),  # nested subset
        ({"a": "1"}, {"a": 1}, False),               # no type coercion
    ]
    for expected, served, verdict in triples:
        assert _matches(expected, served) is verdict, (expected, served)
