"""The flight recorder (`agora drive --turn-log`): the FULL event stream of
every spawned turn, appended as JSONL.

What we want: turn_start before the spawn (a wedged turn still shows it
began), the raw cursor-agent JSON event lines verbatim (they ARE the
transcript), stderr on capture, turn_end with duration/outcome/session —
including the partial stream of a TIMED-OUT turn. Recording is opt-in,
0600, best-effort: a broken log path warns once and never breaks a turn.
"""

from __future__ import annotations

import json
import stat
import subprocess

import pytest

from agora.drive import WAKE_PROMPT, WORK_PROMPT, Driver


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    return tmp_path


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


_STREAM = (
    '{"type":"assistant","text":"checking inbox"}\n'
    '{"type":"tool_call","name":"check_inbox"}\n'
    '{"session_id":"sess-42"}\n'
)


def _events(path):
    """Parse the JSONL back: (structured driver events, raw passthrough)."""
    structured, raw = [], []
    for line in path.read_text().splitlines():
        obj = json.loads(line)
        (structured if "event" in obj else raw).append(obj)
    return structured, raw


def test_off_by_default_writes_nothing(home, monkeypatch):
    d = Driver("worker", "http://127.0.0.1:1")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(_STREAM))
    d._spawn_cursor_agent(WAKE_PROMPT, None)
    assert not list(home.glob("*.jsonl"))


def test_full_turn_recorded_default_path(home, monkeypatch):
    d = Driver("worker", "http://127.0.0.1:1", turn_log="default")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Proc(_STREAM, stderr="warn: x"))
    sid, ok = d._spawn_cursor_agent(WAKE_PROMPT, "sess-41")
    assert (sid, ok) == ("sess-42", True)
    log = home / "drive-worker.turns.jsonl"
    structured, raw = _events(log)
    kinds = [e["event"] for e in structured]
    assert kinds == ["turn_start", "turn_stderr", "turn_end"]
    start, stderr_ev, end = structured
    assert start["kind"] == "wake" and start["session"] == "sess-41"
    assert stderr_ev["text"] == "warn: x"
    assert end["ok"] is True and end["session"] == "sess-42"
    assert end["dur_s"] >= 0
    # The raw cursor-agent lines rode through VERBATIM, order preserved.
    assert raw == [{"type": "assistant", "text": "checking inbox"},
                   {"type": "tool_call", "name": "check_inbox"},
                   {"session_id": "sess-42"}]
    # Operator eyes only.
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_custom_path_and_work_kind(home, monkeypatch, tmp_path):
    target = tmp_path / "flight" / "core.jsonl"
    target.parent.mkdir()
    d = Driver("worker", "http://127.0.0.1:1", turn_log=str(target))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(_STREAM))
    d._spawn_cursor_agent(WORK_PROMPT, "sess-41")
    structured, _raw = _events(target)
    assert structured[0]["kind"] == "work"


def test_timeout_partial_stream_is_recorded(home, monkeypatch):
    d = Driver("worker", "http://127.0.0.1:1", turn_log="default")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="cursor-agent", timeout=600,
                                        output=_STREAM)

    monkeypatch.setattr(subprocess, "run", boom)
    sid, ok = d._spawn_cursor_agent(WAKE_PROMPT, "sess-41")
    assert ok is False and sid == "sess-42"      # salvage still works
    structured, raw = _events(home / "drive-worker.turns.jsonl")
    assert [e["event"] for e in structured] == ["turn_start", "turn_end"]
    assert structured[1]["reason"] == "timeout"
    assert raw[-1] == {"session_id": "sess-42"}  # the partial stream is there


def test_failed_turn_recorded_with_rc(home, monkeypatch):
    d = Driver("worker", "http://127.0.0.1:1", turn_log="default")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Proc("half\n", returncode=7))
    _sid, ok = d._spawn_cursor_agent(WAKE_PROMPT, None)
    assert ok is False
    lines = (home / "drive-worker.turns.jsonl").read_text().splitlines()
    assert "half" in lines[1]                    # non-JSON passthrough kept
    end = json.loads(lines[-1])
    assert end["ok"] is False and end["rc"] == 7


def test_unwritable_log_warns_once_never_breaks_turn(home, monkeypatch,
                                                     capsys):
    d = Driver("worker", "http://127.0.0.1:1",
               turn_log=str(home / "no-such-dir" / "x.jsonl"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(_STREAM))
    sid, ok = d._spawn_cursor_agent(WAKE_PROMPT, None)
    assert (sid, ok) == ("sess-42", True)        # the turn succeeded anyway
    d._spawn_cursor_agent(WAKE_PROMPT, sid)
    out = capsys.readouterr().out
    assert out.count("warn=turn-log-unwritable") == 1   # once, not per write


def test_injected_spawn_paths_unaffected(home):
    """Tests and custom harnesses inject spawn; the recorder hooks only the
    real cursor-agent spawn, so injected drivers stay byte-identical."""
    calls = []
    d = Driver("worker", "http://127.0.0.1:1", turn_log="default",
               spawn=lambda p, s: (calls.append(p) or "s", True))
    d.run_turn()
    assert calls and not (home / "drive-worker.turns.jsonl").exists()


def test_recorder_off_is_true_noop_even_without_stderr_attr(home,
                                                            monkeypatch):
    """Review F1 (the shipped-blocker class): with the recorder OFF, the
    spawn must not touch stderr/stdout attributes beyond what pre-feature
    code did — a proc object LACKING .stderr must work."""
    class _Bare:
        returncode = 0
        stdout = '{"session_id":"sess-9"}\n'
        # deliberately NO stderr attribute

    d = Driver("worker", "http://127.0.0.1:1")   # no turn_log
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Bare())
    sid, ok = d._spawn_cursor_agent(WAKE_PROMPT, None)
    assert (sid, ok) == ("sess-9", True)
    assert not list(home.glob("*.jsonl"))


def test_preexisting_loose_file_is_repaired_to_0600(home, monkeypatch):
    """Review F2: a log file pre-created 0644 must be clamped on first
    write — transcripts are operator-eyes-only, whatever mode the path
    carried before."""
    log = home / "drive-worker.turns.jsonl"
    log.write_text("old\n")
    log.chmod(0o644)
    d = Driver("worker", "http://127.0.0.1:1", turn_log="default")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(_STREAM))
    d._spawn_cursor_agent(WAKE_PROMPT, None)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert log.read_text().startswith("old\n")   # append, never truncate


def test_session_scan_survives_blank_and_garbage_lines(home, monkeypatch):
    """Live finding (2026-07-28): a blank or non-JSON stdout line silently
    killed resume lineage (whole-loop except aborted the scan). The scan
    must be per-line tolerant and still find a LATER session_id."""
    noisy = ('\n<<<GARBAGE not-json\n'
             '{"type":"assistant","text":"hi"}\n'
             '{"session_id":"sess-late"}\n')
    d = Driver("worker", "http://127.0.0.1:1")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(noisy))
    sid, ok = d._spawn_cursor_agent(WAKE_PROMPT, None)
    assert (sid, ok) == ("sess-late", True)


def test_relative_turn_log_warns_at_init(home, capsys):
    Driver("worker", "http://127.0.0.1:1", turn_log="rel/turns.jsonl")
    assert "warn=turn-log-in-workspace" in capsys.readouterr().out


def test_turn_log_redacts_agora_bearer_values(home, monkeypatch):
    leaked = "agora_0123456789abcdef0123456789abcdef0123456789abcdef"
    stream = json.dumps({"type": "assistant", "text": leaked}) + "\n"
    d = Driver("worker", "http://127.0.0.1:1", turn_log="default")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Proc(stream, stderr=f"key={leaked}"))
    d._spawn_cursor_agent(WAKE_PROMPT, None)
    text = (home / "drive-worker.turns.jsonl").read_text()
    assert leaked not in text
    assert text.count("agora_[REDACTED]") == 2


def test_turn_log_preserves_normal_agora_identifiers(home, monkeypatch):
    stream = json.dumps({"path": "agora_protocol.py"}) + "\n"
    d = Driver("worker", "http://127.0.0.1:1", turn_log="default")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(stream))
    d._spawn_cursor_agent(WAKE_PROMPT, None)
    text = (home / "drive-worker.turns.jsonl").read_text()
    assert "agora_protocol.py" in text


# -- title fallback on triage surfaces (2026-08-01) ---------------------------


def test_display_title_falls_back_to_the_body_first_line():
    """`title` is optional on post_message and models differ on whether they
    fill optional args: on 2026-08-01 the one claude-harness seat left 11 of
    31 posts title-less while every opencode seat filled all of theirs.
    Bodies were intact, so the triage surfaces were blanking information the
    record already had. Authored titles always win; derived ones are marked."""
    from agora.render import display_title

    assert display_title("Real subject", "body") == "Real subject"
    # Markdown heading noise is stripped; the derived line is marked with '~'.
    assert display_title("", "## 5-Slot Rerun: Complete Delivery\n\nrest") == \
        "~ 5-Slot Rerun: Complete Delivery"
    # Whitespace-only titles count as absent; leading blank lines are skipped.
    assert display_title("   ", "\n\n  first real line\nsecond") == "~ first real line"
    # Nothing to derive from stays empty rather than inventing a title.
    assert display_title("", "") == ""
    assert display_title("", "   \n  ") == ""
    # Long first lines are capped so a headline column stays a headline.
    out = display_title("", "x" * 300)
    assert out.startswith("~ ") and len(out) <= 95 and out.endswith("…")


def test_render_surfaces_use_the_fallback_title():
    """The fix has to land where receivers actually triage."""
    from agora.render import render_envelopes, render_messages

    msg = {"id": "m1", "channel": "c", "seq": 1, "sender": "editor",
           "status": "reply", "title": "", "body": "Final editorial sign-off"}
    assert "~ Final editorial sign-off" in render_messages([msg])
    env = {"id": "m1", "channel": "c", "seq": 1, "sender": "editor",
           "status": "reply", "title": "", "body": "Final editorial sign-off",
           "kind": "message", "urgency": "inbox", "effective_urgency": "inbox"}
    assert "~ Final editorial sign-off" in render_envelopes([env])
    # An authored title is never rewritten.
    msg["title"] = "Sign-off"
    assert "title: Sign-off" in render_messages([msg])
