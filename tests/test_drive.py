"""The external resume-driver (agora drive): reception made STRUCTURAL.

These tests exercise the loop with an INJECTED spawn — no real cursor-agent —
so the guarantees the design rests on are pinned deterministically: a wake
drives exactly one bounded turn that yields by returning; the session id
persists across wakes and rotates; a per-hour budget parks a runaway; a
crashing wake is quarantined after N strikes (the poison-message bound);
and the sandbox default is never silently dropped.
"""

from __future__ import annotations

import pytest

from agora.drive import (BOOT_PROMPT, POISON_STRIKES, WAKE_PROMPT, Driver)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    return tmp_path


def _driver(home, spawn, **kw):
    return Driver("worker", "http://127.0.0.1:1", spawn=spawn, **kw)


def test_a_turn_boots_fresh_then_resumes_the_session(home):
    """First turn has no session -> BOOT_PROMPT; the spawn returns a session
    id that persists; the next turn RESUMES it with the static WAKE_PROMPT."""
    calls = []

    def spawn(prompt, sid):
        calls.append((prompt, sid))
        return "sess-1", True

    d = _driver(home, spawn)
    d.run_turn()
    d.run_turn()
    assert calls[0] == (BOOT_PROMPT, None)          # boot: no session yet
    assert calls[1] == (WAKE_PROMPT, "sess-1")      # resume with static prompt
    assert (home / "drive-worker.session").read_text() == "sess-1"


def test_turn_budget_parks_a_runaway(home, monkeypatch):
    """More than turn_budget wakes in an hour -> the loop parks instead of
    spawning (the runaway-loop bound; review E)."""
    monkeypatch.setattr("agora.drive.time.sleep", lambda s: None)
    n = {"spawns": 0}

    def spawn(prompt, sid):
        n["spawns"] += 1
        return "s", True

    d = _driver(home, spawn, turn_budget=3)
    ran = [d.run_turn() for _ in range(6)]
    assert n["spawns"] == 3                          # budget capped the spawns
    assert ran.count(False) == 3                     # the rest parked


def test_poison_wake_is_quarantined_after_strikes(home):
    """A wake whose turn keeps crashing (spawn ok=False) is quarantined after
    POISON_STRIKES so it stops eating turns; the attempt ledger drives it."""
    (home / "worker-inbox.log").write_text("x")      # stable wake key

    def spawn(prompt, sid):
        return sid, False                            # every turn crashes

    d = _driver(home, spawn)
    for _ in range(POISON_STRIKES):
        assert d.run_turn() is True                  # strikes accrue
    # Next wake on the same (unchanged) backlog is quarantined: no spawn.
    assert d.run_turn() is False


def test_session_rotates_to_flush_bloat_and_residue(home):
    """After session_rotate successful turns the driver drops --resume and
    boots fresh (context-bloat + injection-residue flush); the hub holds the
    durable memory so only scratch is lost."""
    seen = []

    def spawn(prompt, sid):
        seen.append(sid)
        return f"s{len(seen)}", True

    d = _driver(home, spawn, session_rotate=2)
    d.run_turn()                     # boot -> s1
    d.run_turn()                     # resume s1 -> s2, hits rotate -> session cleared
    d.run_turn()                     # boots fresh again (sid None)
    assert seen == [None, "s1", None]


def test_crashed_resume_drops_session_and_boots_next(home):
    """A failed resume (session gone stale) drops the session so the NEXT
    wake boots fresh rather than resuming a dead id forever."""
    (home / "worker-inbox.log").write_text("k")
    scripted = [("s1", True), (None, False), ("s2", True)]

    def spawn(prompt, sid):
        return scripted.pop(0)

    d = _driver(home, spawn)
    d.run_turn()                                     # -> s1
    assert d.session_id == "s1"
    d.run_turn()                                     # crashes -> session dropped
    assert d.session_id is None
    d.run_turn()                                     # boots fresh -> s2
    assert d.session_id == "s2"


def test_real_spawn_defaults_to_sandbox_enabled(home, monkeypatch):
    """The safety default (review E ship-blocker): the real spawn command
    carries --sandbox enabled and NOT --force unless sandbox is explicitly
    'none'. Verified by capturing the argv the driver would exec."""
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = '{"session_id":"z","result":"ok"}'

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr("agora.drive.subprocess.run", fake_run)

    d = Driver("worker", "http://h:1")               # default sandbox
    d._spawn_cursor_agent("p", None)
    assert "--sandbox" in captured["cmd"]
    i = captured["cmd"].index("--sandbox")
    assert captured["cmd"][i + 1] == "enabled"
    assert "--force" not in captured["cmd"]
    assert "--approve-mcps" in captured["cmd"]

    d2 = Driver("worker", "http://h:1", sandbox="")  # explicit opt-out
    d2._spawn_cursor_agent("p", None)
    assert "--force" in captured["cmd"] and "--sandbox" not in captured["cmd"]
