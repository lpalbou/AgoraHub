#!/usr/bin/env python3
"""Screen a candidate PyPI project name for collisions BEFORE renaming.

PyPI rejects a new name if it collides with an existing project under either:
  1. PEP 503 normalization  — lowercase; runs of - _ . collapse to a single '-'
     (so `Agora_Hub`, `agora.hub`, `agora--hub` all == `agora-hub`).
  2. PyPI's stricter "too similar" check — effectively lowercase and strip ALL
     non-alphanumerics, then compare (so `agora-hub` ~ `agorahub` ~ `agora.hub`).

This tool checks a candidate's PEP 503 form and its fully-stripped form against
the live PyPI index. It catches the common rejection causes. It is NOT a
perfect replica of PyPI's unicode-confusable logic, so treat a PASS as "very
likely accepted" — the only 100% authoritative check is the upload itself
(PyPI validates the name server-side first; see the note this script prints).

Usage:
    python scripts/check_pypi_name.py agorahub agoria moot
"""

from __future__ import annotations

import re
import sys
import urllib.request


def pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.lower())


def stripped(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def exists(project: str) -> bool:
    """True if a project with this exact name is registered on PyPI."""
    url = f"https://pypi.org/pypi/{project}/json"
    req = urllib.request.Request(url, headers={"User-Agent": "name-check"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise
    except Exception:
        return False


def check(candidate: str) -> None:
    canon = pep503(candidate)
    strip = stripped(candidate)
    # Probe the candidate, its PEP503 form, and its stripped form (the stripped
    # form is the most likely "too similar" collision — e.g. agorahub for agora-hub).
    probes = {candidate, canon, strip, canon.replace("-", "")}
    hits = sorted(p for p in probes if exists(p))
    if hits:
        print(f"[X] {candidate:16} LIKELY REJECTED — collides with: {', '.join(hits)}")
    else:
        print(f"[OK] {candidate:16} likely free (pep503='{canon}', stripped='{strip}')")


def main() -> None:
    names = sys.argv[1:] or ["agorahub", "agora-hub", "agoria", "moot"]
    print("Screening PyPI names (best-effort; authoritative check = upload):\n")
    for n in names:
        check(n)
    print(
        "\nAuthoritative check (needs your PyPI token): build then attempt upload.\n"
        "  uv build\n"
        "  uv publish            # or: twine upload dist/*\n"
        "PyPI validates the NAME first; a 'too similar' rejection happens before\n"
        "anything is stored. A success registers the project (which is the goal).\n"
        "To rehearse without touching real PyPI, use TestPyPI (separate namespace,\n"
        "same normalization): uv publish --publish-url https://test.pypi.org/legacy/"
    )


if __name__ == "__main__":
    main()
