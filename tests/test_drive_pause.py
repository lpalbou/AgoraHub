"""`agora pause` must pause the HARNESSES, not just the hub's write path.

The hub enforces a pause with 423 on writes while leaving reads open. That
alone stopped nothing expensive: drivers kept waking and spawning turns, and a
turn already in flight ran to completion against a hub that would refuse every
post it made — the work lost, the provider call paid for. Observed live on
2026-09-07: the operator paused a 19-seat fleet and all 19 harness turns kept
running.

These pin the three halves of the fix: the seat can tell, it will not START a
turn while paused, and a turn already running is ENDED.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

import pytest

from agora.drive import DRIVE_PAUSE_POLL, Driver


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    return tmp_path


def _drv(home, hub="http://127.0.0.1:1"):
    return Driver("worker", hub, harness="codex", cwd=home)


# --- the seat can tell -------------------------------------------------------

def test_hub_paused_reads_the_healthz_flag(home, monkeypatch):
    for served, expected in ((True, True), (False, False)):
        monkeypatch.setattr("httpx.get",
                            lambda *a, **k: _Resp({"paused": served}))
        assert _drv(home)._hub_paused() is expected


def test_unknown_is_never_paused(home, monkeypatch):
    """A blip, a restarting hub, or an older hub without the field must not
    park a working seat: guessing 'paused' stops a fleet silently."""
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr("httpx.get", boom)
    assert _drv(home)._hub_paused() is None

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp({"ok": True}))
    assert _drv(home)._hub_paused() is None


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# --- it will not START a turn while paused -----------------------------------

def test_await_resume_parks_until_the_operator_resumes(home, monkeypatch):
    answers = [True, True, False]
    monkeypatch.setattr(Driver, "_hub_paused",
                        lambda self: answers.pop(0) if answers else False)
    monkeypatch.setattr("agora.drive.time.sleep", lambda _s: None)
    d = _drv(home)
    states = []
    monkeypatch.setattr(Driver, "_state",
                        lambda self, s, **kw: states.append(s))
    assert d._await_resume() is True          # parked, then released
    assert states == ["paused", "armed"]
    assert answers == []                       # polled until it was let go


def test_await_resume_is_a_no_op_on_a_live_hub(home, monkeypatch):
    monkeypatch.setattr(Driver, "_hub_paused", lambda self: False)
    called = []
    monkeypatch.setattr("agora.drive.time.sleep",
                        lambda _s: called.append(_s))
    assert _drv(home)._await_resume() is False
    assert called == []                        # never sleeps on a live hub


# --- a turn already running is ENDED ----------------------------------------

def test_paused_mid_turn_kills_the_process_tree(home, monkeypatch):
    """The real thing: a genuinely running child, killed by the watcher."""
    monkeypatch.setattr(Driver, "_hub_paused", lambda self: True)
    monkeypatch.setattr("agora.drive.DRIVE_PAUSE_POLL", 0.2)
    d = _drv(home)
    monkeypatch.setattr(Driver, "_state", lambda self, s, **kw: None)

    aborted = threading.Event()
    t0 = time.time()
    proc = d._run_turn_process(
        ["/bin/sh", "-c", "sleep 60 & wait"], aborted)
    elapsed = time.time() - t0

    assert aborted.is_set(), "watcher did not report the pause"
    assert proc.returncode != 0, "a killed turn cannot report success"
    assert elapsed < 30, f"turn was not ended promptly ({elapsed:.1f}s)"


def test_a_live_hub_lets_the_turn_finish(home, monkeypatch):
    monkeypatch.setattr(Driver, "_hub_paused", lambda self: False)
    monkeypatch.setattr("agora.drive.DRIVE_PAUSE_POLL", 0.2)
    d = _drv(home)
    aborted = threading.Event()
    proc = d._run_turn_process(["/bin/sh", "-c", "echo hi"], aborted)
    assert not aborted.is_set()
    assert proc.returncode == 0
    assert "hi" in proc.stdout


def test_spawn_turn_reports_a_pause_as_operator_not_failure(home, monkeypatch):
    """An aborted turn must not be charged to the seat: no _record_failure,
    so a pause cannot back a seat off or feed the poison ledger."""
    class Killed:
        returncode = -signal.SIGTERM
        stdout = ""
        stderr = ""

    def fake_run(self, cmd, aborted):
        aborted.set()
        return Killed()

    monkeypatch.setattr(Driver, "_run_turn_process", fake_run)
    charged = []
    monkeypatch.setattr(Driver, "_record_failure",
                        lambda self, **kw: charged.append(kw))
    d = _drv(home)
    sid, ok = d._spawn_turn("p", "sess-1")
    assert ok is False
    assert charged == [], "a pause was charged to the seat as a failure"


def test_turn_child_is_its_own_process_group(home, monkeypatch):
    """start_new_session is what makes the GROUP kill possible; without it a
    kill hits the direct child only and descendants reparent to PID 1."""
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        captured.update(kw)
        return FakeProc()

    monkeypatch.setattr("agora.drive.subprocess.run", fake_run)
    monkeypatch.setattr(Driver, "_hub_paused", lambda self: False)
    _drv(home)._run_turn_process(["true"], threading.Event())
    assert captured.get("start_new_session") is True
