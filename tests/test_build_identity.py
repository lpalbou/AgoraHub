"""Which build is running — `claim:hub-states-its-build`.

THE INCIDENT (2026-08-22, `decision:a-commit-is-not-a-deployment`). Nine
commits to `src/agora` landed between 14:02 and 19:05; the hub the fleet was
talking to had been started from the 14:02 install and served none of them.
`reply_to_seq`, the obligation `reason` enum and the pickup ladder were all
committed, green, announced as shipped — and not running. Two client seats
changed their own code on the strength of those receipts, and the gap was
found by accident an hour after it started mattering.

Nothing was broken. The hub simply could not be ASKED. `/whoami` served
`version: 0.17.8`, and that string was byte-identical across all nine
commits, so "is my fix live?" had no answer short of grepping someone's
site-packages.

These tests hold three things, and the second is the one that matters:

1. The hub states a build identity at all, authenticated and not.
2. **It never guesses.** An undeterminable field is `None` — never a value
   derived from `version`. A plausible-but-stale sha ends an investigation
   with a wrong conclusion; a null one starts a correct one. This is the
   same ruling as `reply_to_seq`'s absent slot and the `reason` enum's null.
3. `built_at` tracks the file that was actually installed, so a stale build
   cannot look fresh by being imported.

Every test here is written so that removing the thing it checks turns it
RED — see the `_unknowable` cases, which assert the null rather than the
happy path, because the happy path passes on a hub that fabricates.
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agora import __version__
from agora.hub.app import create_app
from agora.build_info import _SOURCE_ENV, build_identity, reset_cache

ADMIN_KEY = "test-admin"


@pytest.fixture(autouse=True)
def _clean_cache():
    """The identity memoises at first use; a test that mutates the
    environment must not inherit or leak a resolved value."""
    reset_cache()
    yield
    reset_cache()


def make_client() -> TestClient:
    return TestClient(create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                                 rate_per_minute=600.0))


# -- the shape ----------------------------------------------------------------

def test_build_identity_always_states_all_three_keys():
    """A partial answer is the ambiguity this replaces: a reader must be able
    to tell "unknown" from "not served by this hub", and only a key that is
    always present can carry that distinction in its value."""
    build = build_identity()
    assert set(build) == {"version", "source", "built_at"}
    assert build["version"] == __version__


def test_version_is_not_a_substitute_for_source():
    """The whole defect in one assertion. `version` is a release string that
    nine commits shared; if `source` ever falls back to it, this field is
    decoration and the next stale hub is undetectable again."""
    build = build_identity()
    assert build["source"] != build["version"]


def test_built_at_is_iso_utc_to_the_second_or_none():
    build = build_identity()
    stamp = build["built_at"]
    if stamp is None:
        return
    assert stamp.endswith("Z") and len(stamp) == 20, stamp
    from datetime import datetime
    datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")


def test_built_at_follows_init_py_not_the_directory(tmp_path, monkeypatch):
    """A stale build must not look fresh because it was IMPORTED.

    Writing `__pycache__` bumps the package DIRECTORY's mtime. Had this read
    the directory, the 14:02 build would have reported itself as installed
    moments ago, every time it ran — the exact lie this module exists to
    stop. Delete the `/ "__init__.py"` from `_built_at` and this goes red.
    """
    from agora import build_info
    pkg = tmp_path / "agora"
    pkg.mkdir()
    init = pkg / "__init__.py"
    init.write_text("")
    os.utime(init, (1_000_000_000, 1_000_000_000))   # 2001-09-09T01:46:40Z
    # Touch the directory the way an import would, well after the file.
    (pkg / "__pycache__").mkdir()
    assert build_info._built_at(pkg) == "2001-09-09T01:46:40Z"


# -- it never guesses ---------------------------------------------------------

def test_an_unknowable_source_is_none_not_a_fabrication(tmp_path, monkeypatch):
    """No `.git`, no env stamp — the plain `uv tool install` that was actually
    running on 2026-08-22. The honest answer is `None`, and a reader who sees
    it knows to go and look."""
    from agora import build_info
    monkeypatch.delenv(_SOURCE_ENV, raising=False)
    installed = tmp_path / "site-packages" / "agora"
    installed.mkdir(parents=True)
    assert build_info._git_sha(installed) is None


def test_an_unreadable_package_yields_none_rather_than_raising(tmp_path):
    """`whoami` is on every session-start path. A build-identity probe that
    can raise would take the hub's most load-bearing call down with it, so
    the failure mode is a null field and nothing else."""
    from agora import build_info
    assert build_info._built_at(tmp_path / "does-not-exist") is None


def test_an_explicit_build_stamp_wins_and_is_used_verbatim(monkeypatch):
    """The installed case: a wheel carries no `.git`, so a release step may
    stamp the sha it was cut from. Verbatim — the hub never reformats or
    validates it, because inventing a shape here would mean rejecting a true
    answer it did not recognise."""
    monkeypatch.setenv(_SOURCE_ENV, "abc1234")
    assert build_identity()["source"] == "abc1234"


def test_a_blank_stamp_is_not_an_answer(monkeypatch):
    """An env var set to empty by a broken CI step must degrade to the same
    honest `None` as an unset one — never to an empty string, which renders
    as a present-but-blank field and reads as "no source" when the truth is
    "the stamp is broken"."""
    monkeypatch.setenv(_SOURCE_ENV, "   ")
    from agora import build_info
    resolved = build_identity()["source"]
    assert resolved is None or resolved == build_info._git_sha(
        Path(build_info.__file__).resolve().parent)


# -- it is served -------------------------------------------------------------

def test_whoami_serves_the_build_to_an_authenticated_seat():
    client = make_client()
    admin = {"Authorization": f"Bearer {ADMIN_KEY}"}
    key = client.post("/agents", json={"id": "probe", "mission": "m"},
                      headers=admin).json()["api_key"]
    body = client.get("/whoami",
                      headers={"Authorization": f"Bearer {key}"}).json()
    assert set(body["build"]) == {"version", "source", "built_at"}
    assert body["build"]["version"] == body["version"]


def test_healthz_serves_the_build_without_any_credential():
    """The operator who found the gap held no seat key on that hub — they were
    looking at a process. "Which build is serving?" must be answerable by
    whoever can reach the port, exactly as `protocol` already is."""
    body = make_client().get("/healthz").json()
    assert "build" in body
    assert set(body["build"]) == {"version", "source", "built_at"}


def test_the_two_surfaces_cannot_disagree():
    """One derivation. Two surfaces answering "what build" from two sources is
    the defect this room has now named three times (`closure-is-a-hub-verdict`,
    the ask checkbox vs its label, the Resolve button vs the chip)."""
    client = make_client()
    admin = {"Authorization": f"Bearer {ADMIN_KEY}"}
    key = client.post("/agents", json={"id": "probe2", "mission": "m"},
                      headers=admin).json()["api_key"]
    whoami = client.get("/whoami",
                        headers={"Authorization": f"Bearer {key}"}).json()
    assert whoami["build"] == client.get("/healthz").json()["build"]
