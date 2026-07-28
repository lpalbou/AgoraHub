"""The committed openapi.json is the release artifact TS clients generate
their types from (agora-0118 move 1). This test is the SHAPE-drift tripwire:
any route/model change that alters the served schema fails here until the
artifact is regenerated (scripts/export_openapi.py) — which is exactly the
moment to ask whether the change needs a version bump and a
PROTOCOL_SEMANTICS entry.

Scope honesty (adversary P0-1): shape only. A behavior change with an
unchanged schema — the 0102 class — is invisible to this file and is pinned
by tests/vectors/ instead. The two guards are complements.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _canonical_text() -> str:
    """EXACTLY what `export_openapi.py` writes and `--check` compares — one
    canonical form for both staleness gates (adversary P2-4: two checks with
    different strictness means CI passes what --check fails)."""
    from export_openapi import generate

    return json.dumps(generate(), indent=1, sort_keys=True) + "\n"


def test_openapi_artifact_is_current():
    import fastapi
    import pydantic
    import pytest

    artifact = ROOT / "openapi.json"
    assert artifact.exists(), \
        "openapi.json missing: run scripts/export_openapi.py and commit it"
    committed = json.loads(artifact.read_text())
    gen = committed.get("info", {}).get("x-agora-generator", {})
    here = {"fastapi": fastapi.__version__, "pydantic": pydantic.VERSION}
    if gen and gen != here:
        # Byte-exact comparison is only meaningful between equal schema
        # emitters (impl adversary P2-4: pydantic/FastAPI releases change
        # JSON-schema rendering, so an unpinned CI matrix would flap on
        # diffs no code change caused). The artifact-owning machine keeps
        # the exact gate via `scripts/export_openapi.py --check`.
        pytest.skip(f"artifact generated with {gen}, this env is {here} — "
                    "exact comparison not meaningful across toolchains")
    assert artifact.read_text() == _canonical_text(), (
        "openapi.json is STALE relative to the code. Run "
        "scripts/export_openapi.py, review the contract diff (does it need "
        "a version bump / PROTOCOL_SEMANTICS entry?), and commit the artifact.")


def test_artifact_versions_on_the_wire_contract_not_the_release():
    """info.version is the WIRE protocol (adversary P1-1): a routine release
    bump must not churn the artifact, or reviewers learn to rubber-stamp the
    one diff that matters."""
    from agora import PROTOCOL_VERSION

    info = json.loads((ROOT / "openapi.json").read_text())["info"]
    assert info["version"] == PROTOCOL_VERSION
    assert info["x-agora-protocol"] == PROTOCOL_VERSION


def test_typed_surfaces_are_in_the_schema():
    """The parity spine's types must actually be served — a refactor that
    silently reverts a route to dict[str, Any] reintroduces the drift class
    (additionalProperties: true is where hand-kept client shapes come from)."""
    from export_openapi import generate

    schemas = generate()["components"]["schemas"]
    for name in ("OwedReport", "ObligationRow", "ConsumeRow", "CloseRow",
                 "WaitingRow",
                 "MessageRow", "Envelope", "WhoamiReport"):
        assert name in schemas, f"{name} fell out of the served OpenAPI"
    row = schemas["ObligationRow"]["properties"]
    # `sender` is canonical; `from` is served until the agora/0.4 bump and
    # must be MARKED deprecated so generated TS types warn consumers off
    # (adversary P1-2). The 0.4 coupled-edit inventory lives in
    # docs/backlog/proposed/0117_protocol_0_4_semantic_bump.md — this
    # assertion is on that list to FLIP (assert "from" not in row).
    assert "sender" in row and "from" in row
    assert row["from"].get("deprecated") is True
    assert row["age_minutes"].get("deprecated") is True
    # The capability ledger is IN the typed contract (adversary P1-4):
    # feature detection from generated types is its whole point.
    assert "semantics" in schemas["WhoamiReport"]["properties"]
