#!/usr/bin/env python3
"""Export the hub's served OpenAPI schema as a release artifact (agora-0118).

    python3 scripts/export_openapi.py            # writes ./openapi.json
    python3 scripts/export_openapi.py --check    # CI: fail if stale

The export is DESCRIPTIVE, NOT NORMATIVE: it is generated from the running
implementation (FastAPI + the typed response models), so it is exact for the
code it ships with — but the wire contract's semantics, canonicalization and
version-bump policy live in docs/protocol.md, and the document says so
inside itself (it will be read far from this repo). Since the parity spine
(0.12.30) the artifact is COMMITTED at the repo root and TS/JS clients
generate their types from it (`npx openapi-typescript openapi.json`).

`--check` is the SHAPE-drift tripwire: a change to any typed route that is
not reflected in the committed artifact fails CI, which is exactly the
moment to ask whether the change is additive (ships inside the version) or
breaking (bumps PROTOCOL_VERSION). Scope honesty (adversary P0-1): this
catches SCHEMA changes only — a behavior change with an unchanged schema
(the 0102 class) is invisible here and is the golden vectors' job
(tests/vectors/). The two guards are complements, not substitutes.

The compared document deliberately carries the WIRE-PROTOCOL version, not
the app version (adversary P1-1): an artifact that churns on every release
trains reviewers to rubber-stamp the diff, and the one diff that matters is
then skimmed. The app version rides the per-release export in release.yml.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:  # an installed agorahub (e.g. release.yml exporting FROM THE WHEEL,
    # its stated guarantee) wins; the checkout path is only a convenience
    # for running the script in a bare repo clone (adversary P1-7: an
    # unconditional insert silently shadowed the wheel with the checkout).
    import agora  # noqa: F401
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def generate() -> dict:
    from agora import PROTOCOL_VERSION
    from agora.hub.app import create_app

    app = create_app(db_path=":memory:", admin_key="openapi-export")
    doc = app.openapi()
    doc["info"]["title"] = "Agora Hub HTTP API"
    # info.version is the WIRE protocol, not the app version (P1-1): the
    # committed artifact must change iff the CONTRACT changes, so a routine
    # release bump never touches it and every diff deserves real review.
    doc["info"]["version"] = PROTOCOL_VERSION
    doc["info"]["description"] = (
        f"Generated from the agorahub implementation (wire protocol "
        f"{PROTOCOL_VERSION}). Descriptive, not normative: this document "
        "mirrors the implementation it was exported from. The authoritative "
        "wire contract — semantics, ledger canonicalization, and the "
        "version-bump policy — is docs/protocol.md in the repository; "
        "behavioral conformance is pinned by tests/vectors/*.json."
    )
    doc["info"]["x-agora-protocol"] = PROTOCOL_VERSION
    # Record the schema-emitting toolchain (impl adversary P2-4): pydantic/
    # FastAPI releases change JSON-schema rendering details (nullable
    # encodings etc.), so byte-exact comparison is only meaningful between
    # equal generators. The staleness TEST skips (loudly) on a different
    # toolchain; `--check` on the artifact-owning machine stays exact.
    import fastapi
    import pydantic
    doc["info"]["x-agora-generator"] = {
        "fastapi": fastapi.__version__, "pydantic": pydantic.VERSION}
    # The exporting machine's host/servers are not part of the contract.
    doc.pop("servers", None)
    return doc


def main(argv: list[str]) -> int:
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    out = Path(args[0]) if args else Path(__file__).resolve().parent.parent / "openapi.json"
    doc = generate()
    text = json.dumps(doc, indent=1, sort_keys=True) + "\n"
    if check:
        if not out.exists() or out.read_text() != text:
            print("openapi.json is STALE: run scripts/export_openapi.py, "
                  "review the contract diff (additive, or does it bump "
                  "PROTOCOL_VERSION?), and commit the artifact",
                  file=sys.stderr)
            return 1
        print("openapi.json is current")
        return 0
    out.write_text(text)
    print(f"wrote {out} — {len(doc.get('paths', {}))} paths, "
          f"{len(doc.get('components', {}).get('schemas', {}))} schemas "
          f"(version {doc['info'].get('version')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
