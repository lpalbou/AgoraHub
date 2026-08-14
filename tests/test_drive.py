"""The external resume-driver (agora drive): reception made STRUCTURAL.

These tests exercise the loop with an INJECTED spawn — no real cursor-agent —
so the guarantees the design rests on are pinned deterministically: a wake
drives exactly one bounded turn that yields by returning; the session id
persists across wakes and rotates; a per-hour budget parks a runaway; a
crashing turn is SPACED and its wake kept (the one failure mechanism —
nothing is ever dropped); and the sandbox default is never silently dropped.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from agora.drive import (BACKOFF_MAX, BOOT_PROMPT,
                         DEFAULT_BROADCAST_TURN_BUDGET,
                         DEFAULT_TURN_BUDGET, DEFAULT_WORK_BUDGET,
                         FAILURE_LEDGER_MAX_BYTES,
                         RECEPTION_TURN_TIMEOUT,
                         AbstractCodeDriveAdapter, CodexDriveAdapter,
                         TURN_TIMEOUT, WAKE_PROMPT, WORK_STRIKES,
                         WORK_STRIKE_TTL, Driver,
                         ReceptionDebt, TurnEvidence, _make_adapter,
                         run_drive)
from agora.mcp.runtime import MCPBinding
from agora.setup_harness import (setup_claude, setup_codex, setup_cursor,
                                 write_workspace_seat)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    return tmp_path


def _driver(home, spawn, **kw):
    return Driver("worker", "http://127.0.0.1:1", spawn=spawn, **kw)


def _binding(home: Path | None = None) -> MCPBinding:
    return MCPBinding(command="agora-mcp", agent_id="worker",
                      url="http://hub:8765", home=home or Path.home())


def test_abstractcode_adapter_uses_native_state_mcp_and_skill(home):
    adapter = AbstractCodeDriveAdapter(
        model="test-model", provider="openai", cwd=home,
        mcp=_binding(home), reasoning_effort="high",
    )
    cmd = adapter.build_command(BOOT_PROMPT, None)
    assert cmd[:3] == ["abstractcode", "exec", "--json"]
    assert ["--skill", "agora-channels"] == cmd[
        cmd.index("--skill"):cmd.index("--skill") + 2
    ]
    # PERMISSION_VOCAB is ("all",) — MCP is gated below the bypass mode, so a
    # driven seat only functions there; the declared default renders full-auto.
    assert ["--permission-mode", "full-auto"] == cmd[
        cmd.index("--permission-mode"):cmd.index("--permission-mode") + 2
    ]
    assert ["--provider", "openai"] == cmd[
        cmd.index("--provider"):cmd.index("--provider") + 2
    ]
    state = Path(cmd[cmd.index("--state-file") + 1])
    config = json.loads(state.with_suffix(".config.json").read_text())
    server = config["mcp_servers"]["agora"]
    assert server["transport"] == "stdio"
    assert server["command"] == ["agora-mcp"]
    assert server["env"]["AGORA_AGENT_ID"] == "worker"


def test_abstractcode_turn_requires_completed_mcp_reception(home):
    adapter = AbstractCodeDriveAdapter(
        model=None, permissions="write", cwd=home, mcp=_binding(home)
    )
    raw = "\n".join(json.dumps(row) for row in [
        {"event": "tool_result", "tool": "mcp::agora::whoami", "success": True},
        {"event": "tool_result", "tool": "mcp::agora::check_inbox", "success": True},
        {"event": "tool_result", "tool": "mcp::agora::ack_inbox", "success": True},
        {"event": "final", "status": "completed", "run_id": "r1", "exit_code": 0},
    ])
    assert adapter.assess_turn(raw, "", 0, "boot").ok
    minimal = "\n".join(json.dumps(row) for row in [
        {"event": "tool_result", "tool": "mcp::agora::check_inbox", "success": True},
        {"event": "final", "status": "completed", "run_id": "r2", "exit_code": 0},
    ])
    assert adapter.assess_turn(minimal, "", 0, "boot").ok
    missing = adapter.assess_turn(
        json.dumps({"event": "final", "status": "completed"}), "", 0, "wake"
    )
    assert not missing.ok and missing.reason == "no-agora-tool-call"


def test_structured_ask_rejects_prose_promise_without_real_claim(home, monkeypatch):
    d = _driver(home, lambda p, s: (s, True))
    d._reception_debt_verification_required = True
    d._reception_debt_before = ReceptionDebt(
        to_answer=frozenset({"message-1"}),
        structured=(("dm:operator--worker", 2, "message-1", frozenset({"1"})),),
    )
    monkeypatch.setattr(
        d, "_reception_debt",
        lambda: ReceptionDebt(frozenset()),
    )
    monkeypatch.setattr(d, "_message_pending_asks", lambda *_: frozenset({"1"}))
    monkeypatch.setattr(d, "_linked_claim_sources", lambda: set())
    evidence = d._verify_reception_debt(TurnEvidence(ok=True), "wake")
    assert not evidence.ok
    assert evidence.reason == "debt-remains"
    assert "pending_without_linked_claim=message-1" in (evidence.detail or "")

    monkeypatch.setattr(
        d, "_linked_claim_sources", lambda: {"message-1"})
    assert d._verify_reception_debt(TurnEvidence(ok=True), "wake").ok


def test_standing_claimed_commission_does_not_fail_the_turn(home, monkeypatch):
    """2026-08-04: an operator commission stays in to_answer until the
    completion report, BY DESIGN — so a delegate mid-delivery must not be
    scored failed (and eventually muted) for the ledger doing its job. The
    linked claim is the excusal; a surviving row with NO claim still
    fails the turn, so the anti-lurk bound holds."""
    d = _driver(home, lambda p, s: (s, True))
    d._reception_debt_verification_required = True
    d._reception_debt_before = ReceptionDebt(
        to_answer=frozenset({"commission"}))
    monkeypatch.setattr(d, "_reception_debt",
                        lambda: ReceptionDebt(frozenset({"commission"})))
    monkeypatch.setattr(d, "_linked_claim_sources", lambda: {"commission"})
    assert d._verify_reception_debt(TurnEvidence(ok=True), "wake").ok
    monkeypatch.setattr(d, "_linked_claim_sources", lambda: set())
    v = d._verify_reception_debt(TurnEvidence(ok=True), "wake")
    assert not v.ok and "to_answer=commission" in (v.detail or "")


def test_linked_claim_sources_count_blocked_but_never_done(home, monkeypatch):
    """A claim `blocked: waiting on the operator` is a materialized plan
    with a recorded reason; a `done` claim links nothing (its obligation is
    over) and another seat's claim is not this seat's receipt."""
    d = _driver(home, None)
    rows = [(1.0, "room", "claim:a"), (1.0, "room", "claim:b"),
            (1.0, "room", "claim:c"), (1.0, "room", "phase:x")]
    vals = {"claim:a": (1, {"owner": "worker",
                            "blocked_on": "external", "needs": "the vendor build to land", "status": "blocked: waiting on operator",
                            "source_message_id": "m-blocked"}),
            "claim:b": (1, {"owner": "worker", "status": "done",
                            "source_message_id": "m-done"}),
            "claim:c": (1, {"owner": "other", "status": "in_progress",
                            "source_message_id": "m-other"})}
    monkeypatch.setattr(d, "_work_rows", lambda: rows)
    monkeypatch.setattr(d, "_read_work_row", lambda ch, k: vals.get(k))
    assert d._linked_claim_sources() == {"m-blocked"}


def test_fanned_out_asks_only_count_the_seat_s_own_ask(home, monkeypatch):
    """A decomposition that addresses each ask to a different seat must not
    fail the seat that answered its own (live at-test#446: one message with
    four per-ask `to` lists muted 5 of 7 seats that had all answered)."""
    d = _driver(home, lambda p, s: (s, True))
    row = {
        "id": "message-1",
        # Global pending: worker answered "1"; "2"/"3" are other seats' asks.
        "pending_asks": ["2", "3"],
        "data": {"asks": [
            {"id": "1", "text": "yours", "to": ["worker", "other"]},
            {"id": "2", "text": "not yours", "to": ["someone"]},
            {"id": "3", "text": "not yours either", "to": ["editor"]},
        ]},
    }
    monkeypatch.setattr("agora.config.get_cached_key", lambda *_: "k")

    class _Resp:
        @staticmethod
        def json():
            return row

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
    # Worker's own ask is answered -> no debt of its own remains.
    assert d._message_pending_asks("at-test", 446, "message-1") == frozenset()

    # Its own ask still open -> the debt is real and still reported.
    row["pending_asks"] = ["1", "2", "3"]
    assert d._message_pending_asks("at-test", 446, "message-1") == frozenset({"1"})

    # An ask addressed to nobody is everyone's obligation.
    row["data"] = {"asks": [{"id": "1", "text": "broadcast"}]}
    row["pending_asks"] = ["1"]
    assert d._message_pending_asks("at-test", 446, "message-1") == frozenset({"1"})

    # No structured asks: whole-message obligation, unchanged behaviour.
    row["data"] = {}
    row["pending_asks"] = ["1"]
    assert d._message_pending_asks("at-test", 446, "message-1") == frozenset({"1"})


def _fake_harness(tmp_path, name: str, *,
                  payload_lines: list[dict]) -> tuple[os.PathLike, os.PathLike]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / f"{name}.log"
    script = bin_dir / name
    payload = "\n".join(
        f"print(json.dumps({line!r}))" for line in payload_lines
    )
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"log = pathlib.Path({str(log)!r})\n"
        "log.parent.mkdir(parents=True, exist_ok=True)\n"
        "with log.open('a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')\n"
        f"{payload}\n"
    )
    script.chmod(0o755)
    return bin_dir, log


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
    assert (home / "drive-worker.reception-v2.session").read_text() == "sess-1"


def test_driver_clears_stale_owedsig_before_first_arm(home, monkeypatch):
    """A restarted driver must not inherit a prior listener's debt watermark.

    If `listen-<id>.owedsig` survives from an earlier owner that already
    ANNOUNCED a wake but never drove the turn, the next driver must clear it
    before its first arm so unchanged owed debt still wakes once.
    """
    (home / "listen-worker.owedsig").write_text("stale-sig")
    seen = []
    calls = []

    def spawn(prompt, sid):
        calls.append((prompt, sid))
        return "sess-1", True

    def fake_run_listen(**kwargs):
        seen.append((home / "listen-worker.owedsig").exists())
        if len(seen) == 1:
            return 2 if not seen[-1] else 0
        raise AssertionError("driver did not wake on the first arm")

    monkeypatch.setattr("agora.drive.run_listen", fake_run_listen)

    d = _driver(home, spawn)
    assert d.run(max_turns=1) == 0
    assert seen == [False]
    assert calls == [(BOOT_PROMPT, None)]


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


def test_default_driver_limits_are_light_abuse_fuses(home):
    """Defaults leave ample room for healthy reception and initiative."""
    driver = _driver(home, lambda p, s: ("s", True))
    assert DEFAULT_TURN_BUDGET == 250
    assert DEFAULT_BROADCAST_TURN_BUDGET == 100
    assert DEFAULT_WORK_BUDGET == 100
    assert TURN_TIMEOUT == 3600.0
    assert driver.turn_budget == 250
    assert driver.broadcast_turn_budget == 100
    assert driver.work_budget == 100
    assert driver.work_timeout == 3600.0


def test_codex_adapter_parses_thread_started_events_as_session_ids():
    adapter = CodexDriveAdapter(model=None, permissions="write", cwd=Path.cwd(),
                                mcp=_binding())
    raw = (
        '{"type":"thread.started","thread_id":"codex-thread"}\n'
        '{"type":"turn.completed"}\n'
    )
    assert adapter.parse_session_id(raw, None) == "codex-thread"


def test_codex_resume_omits_exec_only_sandbox_flag():
    adapter = CodexDriveAdapter(model=None, permissions="write", cwd=Path.cwd(),
                                mcp=_binding())
    cmd = adapter.build_command("wake", "codex-thread")
    assert cmd[:3] == ["codex", "exec", "resume"]
    assert "-s" not in cmd and "--sandbox" not in cmd
    assert "sandbox_workspace_write.network_access=false" in cmd
    assert "mcp_servers.agora.required=true" in cmd
    assert "--dangerously-bypass-hook-trust" not in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert cmd[-2:] == ["codex-thread", "wake"]


def test_codex_reasoning_effort_is_an_explicit_native_override():
    adapter = CodexDriveAdapter(model="gpt-5.5", permissions="write",
                                cwd=Path.cwd(), mcp=_binding(),
                                reasoning_effort="xhigh")
    cmd = adapter.build_command("wake", None)
    assert "model_reasoning_effort=\"xhigh\"" in cmd


@pytest.mark.parametrize("level", ["read", "all"])
def test_codex_permission_vocabulary_is_write_only(home, level):
    """Codex declares PERMISSION_VOCAB=("write",) on purpose: `all` would drop
    the OS sandbox, letting shell network access bypass MCP entirely. The
    refusal comes from the same generic vocabulary check as reasoning — no
    vendor branch, and the legacy `--sandbox disabled/none` aliases map to
    `all` and are refused the same way."""
    with pytest.raises(SystemExit, match=r"accepts --permissions write"):
        Driver("worker", "http://hub:1", harness="codex", permissions=level,
               cwd=home)
    with pytest.raises(SystemExit, match=r"accepts --permissions write"):
        Driver("worker", "http://hub:1", harness="codex", sandbox="disabled",
               cwd=home)


def test_codex_turn_requires_successful_reception_mcp_calls():
    adapter = CodexDriveAdapter(model=None, permissions="write", cwd=Path.cwd(),
                                mcp=_binding())
    no_tools = adapter.assess_turn(
        '{"type":"turn.completed"}\n', "", 0, "wake"
    )
    assert no_tools.ok is False
    assert (no_tools.stage, no_tools.reason) == ("mcp-use", "no-agora-tool-call")

    # check_inbox ALONE is a complete reception pass. `ack_inbox` is correctly
    # absent when nothing new arrived, and demanding it scored a correct no-op
    # turn as a failure — which cost a poison strike and destroyed the
    # resumable session, so the seat cold-started on every later wake.
    # Whether debt was really settled is proven against /owed, not by counting
    # ceremonial tool calls.
    quiet_pass = adapter.assess_turn(
        '{"type":"item.completed","item":{"type":"mcp_tool_call",'
        '"server":"agora","tool":"check_inbox","status":"completed",'
        '"error":null}}\n'
        '{"type":"turn.completed"}\n', "", 0, "wake"
    )
    assert quiet_pass.ok is True, quiet_pass

    # But a turn that never even looked at the inbox is still a failure.
    never_looked = adapter.assess_turn(
        '{"type":"item.completed","item":{"type":"mcp_tool_call",'
        '"server":"agora","tool":"list_channels","status":"completed",'
        '"error":null}}\n'
        '{"type":"turn.completed"}\n', "", 0, "wake"
    )
    assert never_looked.ok is False
    assert never_looked.reason == "incomplete-reception-pass"

    complete = adapter.assess_turn(
        '{"type":"item.completed","item":{"type":"mcp_tool_call",'
        '"server":"agora","tool":"whoami","status":"completed",'
        '"error":null}}\n'
        '{"type":"item.completed","item":{"type":"mcp_tool_call",'
        '"server":"agora","tool":"check_inbox","status":"completed",'
        '"error":null}}\n'
        '{"type":"item.completed","item":{"type":"mcp_tool_call",'
        '"server":"agora","tool":"ack_inbox","status":"completed",'
        '"error":null}}\n'
        '{"type":"turn.completed"}\n', "", 0, "wake"
    )
    assert complete.ok is True
    assert complete.tools == ("whoami", "check_inbox", "ack_inbox")

    # A boot turn that skipped `whoami` is fine too: a resumed thread already
    # knows who it is, and the seat did the thing that matters (looked at its
    # inbox). Ceremony is not evidence.
    boot_without_identity = adapter.assess_turn(
        '{"type":"item.completed","item":{"type":"mcp_tool_call",'
        '"server":"agora","tool":"check_inbox","status":"completed",'
        '"error":null}}\n'
        '{"type":"item.completed","item":{"type":"mcp_tool_call",'
        '"server":"agora","tool":"ack_inbox","status":"completed",'
        '"error":null}}\n'
        '{"type":"turn.completed"}\n', "", 0, "boot"
    )
    assert boot_without_identity.ok is True


def test_codex_turn_rejects_mixed_success_and_failed_mcp_calls():
    adapter = CodexDriveAdapter(model=None, permissions="write", cwd=Path.cwd(),
                                mcp=_binding())
    raw = (
        '{"type":"item.completed","item":{"type":"mcp_tool_call",'
        '"server":"agora","tool":"check_inbox","status":"completed",'
        '"error":null}}\n'
        '{"type":"item.completed","item":{"type":"mcp_tool_call",'
        '"server":"agora","tool":"post_message","status":"failed",'
        '"error":"hub rejected the reply"}}\n'
        '{"type":"item.completed","item":{"type":"mcp_tool_call",'
        '"server":"agora","tool":"ack_inbox","status":"completed",'
        '"error":null}}\n'
    )
    evidence = adapter.assess_turn(raw, "", 0, "wake")
    assert evidence.ok is False
    assert (evidence.stage, evidence.reason) == (
        "mcp-call", "post_message-failed"
    )
    assert evidence.tools == ("check_inbox", "ack_inbox")


def test_codex_turn_rejects_completed_mcp_application_failure():
    adapter = CodexDriveAdapter(model=None, permissions="write", cwd=Path.cwd(),
                                mcp=_binding())
    result = {
        "content": [{
            "type": "text",
            "text": json.dumps({
                "ok": False, "error": 503, "detail": "hub unavailable"
            }),
        }],
        "structured_content": None,
    }
    raw = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call", "server": "agora",
            "tool": "check_inbox", "status": "completed",
            "error": None, "result": result,
        },
    })
    evidence = adapter.assess_turn(raw, "", 0, "wake")
    assert evidence.ok is False
    assert (evidence.stage, evidence.reason) == (
        "mcp-call", "check_inbox-failed"
    )
    assert evidence.detail == "hub unavailable"


def test_codex_turn_requires_a_successful_terminal_event():
    adapter = CodexDriveAdapter(model=None, permissions="write", cwd=Path.cwd(),
                                mcp=_binding())
    calls = (
        '{"type":"item.completed","item":{"type":"mcp_tool_call",'
        '"server":"agora","tool":"check_inbox","status":"completed",'
        '"error":null}}\n'
        '{"type":"item.completed","item":{"type":"mcp_tool_call",'
        '"server":"agora","tool":"ack_inbox","status":"completed",'
        '"error":null}}\n'
    )
    missing = adapter.assess_turn(calls, "", 0, "wake")
    assert (missing.stage, missing.reason) == (
        "harness", "missing-terminal-event"
    )
    failed = adapter.assess_turn(
        calls + '{"type":"turn.failed","error":{"message":"model failed"}}\n',
        "", 0, "wake"
    )
    assert failed.ok is False
    assert (failed.stage, failed.reason) == ("harness", "turn-failed")


def test_codex_required_mcp_startup_failure_is_named():
    adapter = CodexDriveAdapter(model=None, permissions="write", cwd=Path.cwd(),
                                mcp=_binding())
    evidence = adapter.assess_turn(
        "", "required MCP servers failed to initialize: agora: handshaking "
        "with MCP server failed: connection closed", 1, "boot"
    )
    assert evidence.ok is False
    assert (evidence.stage, evidence.reason) == (
        "mcp-init", "required-server-unavailable"
    )


def test_codex_structured_model_error_outranks_incidental_stderr():
    adapter = CodexDriveAdapter(model=None, permissions="write", cwd=Path.cwd(),
                                mcp=_binding())
    raw = json.dumps({
        "type": "turn.failed",
        "error": {"message": "Unsupported value: max for reasoning.effort"},
    })
    evidence = adapter.assess_turn(
        raw, "Reading additional input from stdin...", 1, "boot"
    )
    assert (evidence.stage, evidence.reason) == (
        "harness-config", "model-reasoning-incompatible"
    )
    assert "reasoning.effort" in (evidence.detail or "")


def test_spawn_turn_detaches_child_stdin(home, monkeypatch):
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = '{"session_id":"z","result":"ok"}'
        stderr = ""

    def fake_run(cmd, **kw):
        captured["kw"] = kw
        return FakeProc()

    monkeypatch.setattr("agora.drive.subprocess.run", fake_run)

    Driver("worker", "http://h:1", harness="codex", cwd=home)._spawn_turn("p", None)
    assert captured["kw"]["stdin"] is subprocess.DEVNULL


def test_spawn_turn_binds_mcp_without_exporting_credentials(home, monkeypatch):
    captured = {}
    workspace = home / "workspace"
    workspace.mkdir()
    setup_codex(workspace, "worker", "http://hub:1", "owns receipts",
                "agora-mcp", api_key="agora_worker")

    class FakeProc:
        returncode = 0
        stdout = (
            '{"type":"thread.started","thread_id":"z"}\n'
            '{"type":"item.completed","item":{"type":"mcp_tool_call",'
            '"server":"agora","tool":"whoami","status":"completed",'
            '"error":null}}\n'
            '{"type":"item.completed","item":{"type":"mcp_tool_call",'
            '"server":"agora","tool":"check_inbox","status":"completed",'
            '"error":null}}\n'
            '{"type":"item.completed","item":{"type":"mcp_tool_call",'
            '"server":"agora","tool":"ack_inbox","status":"completed",'
            '"error":null}}\n'
            '{"type":"turn.completed"}\n'
        )
        stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return FakeProc()

    monkeypatch.setattr("agora.drive.subprocess.run", fake_run)
    monkeypatch.setenv("AGORA_API_KEY", "agora_" + "s" * 48)
    monkeypatch.setenv("AGORA_ADMIN_KEY", "agora_" + "a" * 48)
    monkeypatch.setenv("AGORA_URL", "http://wrong-hub:9")
    monkeypatch.setenv("AGORA_AGENT_ID", "wrong-seat")

    Driver("worker", "http://hub:1", harness="codex", cwd=workspace)._spawn_turn("p", None)
    child_env = captured["kw"]["env"]
    assert not any(key.startswith("AGORA_") for key in child_env)
    assert child_env["PATH"] == os.environ["PATH"]
    joined = "\n".join(captured["cmd"])
    assert 'AGORA_AGENT_ID="worker"' in joined
    assert 'AGORA_URL="http://hub:1"' in joined
    assert f'AGORA_HOME="{home}"' in joined
    assert 'AGORA_ABOUT="owns receipts"' in joined
    assert 'AGORA_DOWNLOAD_DIR=' not in joined
    assert 'AGORA_API_KEY=""' in joined
    assert 'AGORA_ADMIN_KEY=""' in joined
    assert "agora_worker" not in joined


def test_a_crashing_wake_is_spaced_out_never_dropped(home):
    """A wake whose turn keeps crashing must stop EATING turns without ever
    ceasing to be answerable: the next attempt is spaced, not dropped.

    This replaces the poison quarantine. The old rule dropped the wake for an
    hour, and since the wake key was the owed SIGNATURE, an obligation the
    seat could not settle alone produced the same key forever — the seat went
    deaf to precisely the debt it most needed help with.
    """
    def spawn(prompt, sid):
        return sid, False                            # every turn crashes

    d = _driver(home, spawn)
    assert d.run_turn() is True                      # first crash: spawned
    assert d._pending_wake is True                   # the wake is KEPT
    assert d.run_turn() is False                     # ...and spaced out
    assert d._hold(has_debt=True)[0] == "backoff"
    d._retry_at = 0.0                                # the wait elapses
    assert d.run_turn() is True                      # the SAME wake is retried


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
    assert d.reception_session_id == "s1"
    d.run_turn()                                     # crashes -> session dropped
    assert d.reception_session_id is None
    d._retry_at = 0.0                                # the backoff elapses
    d.run_turn()                                     # boots fresh -> s2
    assert d.reception_session_id == "s2"


def test_reception_and_work_sessions_are_isolated_and_legacy_is_ignored(home):
    (home / "drive-worker.session").write_text("storm-trained-legacy")
    calls = []

    def spawn(prompt, sid):
        calls.append((prompt, sid))
        return ("reception" if len(calls) == 1 else "work"), True

    d = _driver(home, spawn)
    d.run_turn()
    d._activate_work_claim("design", "claim:x")
    d.run_work_turn()
    assert calls[0][1] is None and calls[1][1] is None
    assert d.reception_session_id == "reception"
    assert d.work_session_id == "work"
    assert (home / "drive-worker.session").read_text() == "storm-trained-legacy"


def test_changing_work_claim_rotates_only_the_work_session(home):
    d = _driver(home, lambda prompt, sid: ("s", True))
    d.reception_session_id = "reception"
    d._activate_work_claim("design", "claim:a")
    d.run_work_turn()
    assert d.work_session_id == "s"
    d._activate_work_claim("design", "claim:b")
    assert d.work_session_id is None
    assert d.reception_session_id == "reception"


def test_backlog_wake_from_listen_drives_a_turn(home, monkeypatch):
    """Missed-wake recovery lives in run_listen now (arm-time backlog check,
    exit 2): the driver treats a backlog wake exactly like a live one —
    one bounded turn per rc=2, idle rc=0 drives nothing."""
    spawns = []

    def spawn(prompt, sid):
        spawns.append(prompt)
        return "s", True

    d = _driver(home, spawn)
    codes = iter([2, 0, 2])                           # backlog, idle, live wake
    monkeypatch.setattr("agora.drive.run_listen",
                        lambda **kw: next(codes, 0))
    d.run(max_turns=2)
    assert len(spawns) == 2                           # one turn per wake, none for idle


def test_loop_listens_with_signal_passthrough(home, monkeypatch):
    """The embedded listen must NOT swallow SIGTERM into a clean return
    (live finding: pkill'd drivers survived — the listener's own handlers
    turned the kill into rc=0 and the loop re-armed). The driver passes
    signal_passthrough so the default handlers stay and the process dies."""
    seen = {}

    def listen(**kw):
        seen.update(kw)
        raise KeyboardInterrupt                       # end after one call

    monkeypatch.setattr("agora.drive.run_listen", listen)
    d = _driver(home, lambda p, s: ("s", True))
    with pytest.raises(KeyboardInterrupt):
        d.run()
    assert seen.get("signal_passthrough") is True
    assert seen.get("important_only") is True
    assert seen.get("once") is True


def test_idle_timeout_without_debt_never_spawns(home, monkeypatch):
    """A quiet hub costs zero LLM turns: idle timeouts (rc 0 — run_listen's
    arm-time backlog check found no debt) drive nothing."""
    spawns = []

    def spawn(prompt, sid):
        spawns.append(prompt)
        return "s", True

    d = _driver(home, spawn)
    calls = {"n": 0}

    def listen(**kw):
        calls["n"] += 1
        if calls["n"] >= 4:
            raise KeyboardInterrupt                   # end the test loop
        return 0

    monkeypatch.setattr("agora.drive.run_listen", listen)
    with pytest.raises(KeyboardInterrupt):
        d.run()
    assert spawns == []


def test_an_unowned_broadcast_wake_buys_no_turn(home, monkeypatch, capsys):
    """0140 field test 2: seats woke on room-wide hub traffic that obliged
    them nothing and spent a paid turn manufacturing a receipt. The mail is
    delivered and waits; the DRIVER declines the turn — loudly, never
    silently — and an addressed/owed wake is untouched."""
    from agora.listen import _DRIVER_BROADCAST_WAKE, _DRIVER_UNOWNED_WAKE
    spawns = []

    def spawn(prompt, sid):
        spawns.append(prompt)
        return "s", True

    d = _driver(home, spawn)
    codes = [_DRIVER_UNOWNED_WAKE, _DRIVER_UNOWNED_WAKE,
             _DRIVER_BROADCAST_WAKE, 2]
    calls = {"n": 0}

    def listen(**kw):
        calls["n"] += 1
        if calls["n"] > len(codes):
            raise KeyboardInterrupt
        return codes[calls["n"] - 1]

    monkeypatch.setattr("agora.drive.run_listen", listen)
    with pytest.raises(KeyboardInterrupt):
        d.run()
    assert len(spawns) == 2                    # only the broadcast and the owed
    out = capsys.readouterr().out
    assert out.count("wake-noop") == 2
    assert "reason=unowned-broadcast owed=0" in out
    assert "mail delivered and waiting" in out


def test_the_wake_prompt_authorizes_the_empty_turn():
    """A wake that carries no work must be allowed to end in silence. The
    prompt is the only place a driven seat learns that, and without it the
    seat manufactures a receipt (8.3% ceremony with live asks vs 50%
    without)."""
    assert "END THE TURN WITHOUT POSTING ANYTHING" in WAKE_PROMPT
    assert "nothing owed BY YOU and no ask names you" in WAKE_PROMPT
    assert "anti-pattern" in WAKE_PROMPT
    assert "END WITHOUT POSTING" in BOOT_PROMPT


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

    d2 = Driver("worker", "http://h:1", permissions="all")  # explicit opt-out
    d2._spawn_cursor_agent("p", None)
    assert "--force" in captured["cmd"] and "--sandbox" not in captured["cmd"]


def test_codex_resume_omits_boot_only_sandbox_flag():
    adapter = CodexDriveAdapter(model=None, permissions="write", cwd=Path.cwd(),
                                mcp=_binding())

    boot = adapter.build_command("boot", None)
    resume = adapter.build_command("resume", "thread-1")

    assert boot[:2] == ["codex", "exec"]
    assert "-s" in boot and "workspace-write" in boot
    assert "sandbox_workspace_write.network_access=false" in boot
    assert resume[:3] == ["codex", "exec", "resume"]
    assert "-s" not in resume
    assert "workspace-write" not in resume
    assert "sandbox_workspace_write.network_access=false" in resume
    assert "mcp_servers.agora.required=true" in boot
    assert "mcp_servers.agora.required=true" in resume
    assert resume[-2:] == ["thread-1", "resume"]


@pytest.mark.parametrize(
    "harness,setup,bin_name,session_path,resume_tokens,payload_lines,session_id",
    [
        ("cursor", setup_cursor, "cursor-agent",
         "drive-worker.reception-v2.session", ["--resume", "cursor-s"],
         [{"session_id": "cursor-s", "result": "ok"}], "cursor-s"),
        ("codex", setup_codex, "codex",
         "drive-worker.codex.reception-v2.session", ["resume", "codex-s"],
         [{"type": "thread.started", "thread_id": "codex-s"},
          {"type": "item.completed",
           "item": {"type": "mcp_tool_call", "server": "agora",
                    "tool": "whoami", "status": "completed", "error": None}},
          {"type": "item.completed",
           "item": {"type": "mcp_tool_call", "server": "agora",
                    "tool": "check_inbox", "status": "completed", "error": None}},
          {"type": "item.completed",
           "item": {"type": "mcp_tool_call", "server": "agora",
                    "tool": "ack_inbox", "status": "completed", "error": None}},
          {"type": "turn.completed"}], "codex-s"),
        # A REAL stream-json transcript, not a bare envelope: the old fixture
        # ({"session_id": ..., "result": "ok"}) asserted that claude emits no
        # tool evidence, which is exactly the defect it should have caught.
        ("claude", setup_claude, "claude",
         "drive-worker.claude.reception-v2.session", ["--resume", "claude-s"],
         [{"type": "system", "subtype": "init", "session_id": "claude-s",
           "mcp_servers": [{"name": "agora", "status": "connected"}]},
          {"type": "assistant", "session_id": "claude-s",
           "message": {"content": [
               {"type": "tool_use", "id": "t1",
                "name": "mcp__agora__check_inbox", "input": {}}]}},
          {"type": "user", "session_id": "claude-s",
           "message": {"content": [
               {"type": "tool_result", "tool_use_id": "t1", "is_error": None,
                "content": [{"type": "text", "text": "{\"ok\": true}"}]}]}},
          {"type": "result", "subtype": "success", "is_error": False,
           "session_id": "claude-s", "result": "ok"}], "claude-s"),
    ],
)
def test_run_drive_launches_the_selected_harness_end_to_end(home, tmp_path,
                                                            monkeypatch,
                                                            harness, setup,
                                                            bin_name,
                                                            session_path,
                                                            resume_tokens,
                                                            payload_lines,
                                                            session_id):
    workspace = tmp_path / harness
    workspace.mkdir()
    if harness == "cursor":
        setup(workspace, "worker", "http://hub:8765", "", "agora-mcp", True)
    elif harness == "codex":
        setup(workspace, "worker", "http://hub:8765", "", "agora-mcp",
              with_hook=True)
    else:
        setup(workspace, "worker", "http://hub:8765", "", "agora-mcp", True)
    write_workspace_seat(workspace, agent_id="worker", url="http://hub:8765",
                         about="", harnesses=(harness,),
                         default_drive_harness=harness)
    bin_dir, log = _fake_harness(tmp_path, bin_name,
                                 payload_lines=payload_lines)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(
        "agora.drive._owed_snapshot",
        lambda *_args, **_kwargs: (
            (0, 0), "",
            {"to_answer": [], "to_consume": [],
             "counts": {"to_answer": 0, "to_consume": 0}},
        ),
    )

    assert run_drive(cwd=workspace, once=True) == 0
    assert run_drive(cwd=workspace, once=True) == 0

    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["cwd"] == str(workspace)
    joined = rows[1]["argv"]
    for token in resume_tokens:
        assert token in joined
    if harness == "codex":
        assert "-s" not in joined and "--sandbox" not in joined
        assert "--dangerously-bypass-approvals-and-sandbox" not in joined
    assert (home / session_path).read_text() == session_id


def test_run_drive_requires_explicit_choice_for_multi_harness_seat(home, tmp_path):
    workspace = tmp_path / "multi"
    workspace.mkdir()
    write_workspace_seat(workspace, agent_id="worker", url="http://hub:8765",
                         about="", harnesses=("cursor", "codex"),
                         default_drive_harness=None)
    with pytest.raises(SystemExit, match="multiple Agora harnesses configured"):
        run_drive(cwd=workspace, once=True)


def test_run_drive_reports_operator_interrupt_without_traceback(home, tmp_path,
                                                                monkeypatch,
                                                                capsys):
    workspace = tmp_path / "codex-interrupt"
    workspace.mkdir()
    write_workspace_seat(workspace, agent_id="worker", url="http://hub:8765",
                         about="", harnesses=("codex",),
                         default_drive_harness="codex")

    def interrupted(self, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(Driver, "run", interrupted)
    assert run_drive(cwd=workspace) == 130
    assert "event=stopped status=ok" in capsys.readouterr().out


def test_run_once_exits_nonzero_when_the_turn_fails(home):
    d = _driver(home, lambda _prompt, sid: (sid, False))
    assert d.run(once=True) == 1


def test_run_drive_prefers_canonical_seat_over_ambient_identity(home, tmp_path,
                                                                  monkeypatch,
                                                                  capsys):
    workspace = tmp_path / "seat"
    workspace.mkdir()
    write_workspace_seat(
        workspace, agent_id="canonical", url="http://canonical:8765",
        about="", harnesses=("codex",), default_drive_harness="codex",
    )
    monkeypatch.setenv("AGORA_AGENT_ID", "wrong-seat")
    monkeypatch.setenv("AGORA_URL", "http://wrong-hub:9999")
    seen = {}

    def fake_run(self, **_kwargs):
        seen.update(agent=self.agent_id, hub=self.hub)
        return 0

    monkeypatch.setattr(Driver, "run", fake_run)
    assert run_drive(cwd=workspace, once=True) == 0
    assert seen == {"agent": "canonical", "hub": "http://canonical:8765"}
    output = capsys.readouterr().out
    assert output.count("reason=workspace-seat-wins") == 2


def test_codex_reception_rejects_ack_only_when_original_debt_remains(
        home, monkeypatch, capsys):
    owed = {
        "to_answer": [{"id": "question-1"}],
        "to_consume": [],
        "counts": {"to_answer": 1, "to_consume": 0},
    }
    monkeypatch.setattr(
        "agora.drive._owed_snapshot",
        lambda *_args, **_kwargs: ((1, 0), "question-1", owed),
    )

    class FakeProc:
        returncode = 0
        stderr = ""
        stdout = (
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"item.completed","item":{"type":"mcp_tool_call",'
            '"server":"agora","tool":"whoami","status":"completed",'
            '"error":null}}\n'
            '{"type":"item.completed","item":{"type":"mcp_tool_call",'
            '"server":"agora","tool":"check_inbox","status":"completed",'
            '"error":null}}\n'
            '{"type":"item.completed","item":{"type":"mcp_tool_call",'
            '"server":"agora","tool":"ack_inbox","status":"completed",'
            '"error":null}}\n'
            '{"type":"turn.completed"}\n'
        )

    monkeypatch.setattr("agora.drive.subprocess.run", lambda *a, **k: FakeProc())
    d = Driver("worker", "http://hub:1", harness="codex", cwd=home)
    assert d.run_turn() is True
    assert d._last_turn_ok is False
    output = capsys.readouterr().out
    assert "stage=reception" in output and "reason=debt-remains" in output
    assert "question-1" in output
    # DIAGNOSED, NOT PENALISED. The turn reached the hub and looked; a second
    # identical turn has identical inputs. Live 2026-08-03: every one of these
    # verdicts respawned a turn within the same second, and the held wake
    # blocked the work chunk the debt was actually asking for.
    assert d._pending_wake is False
    assert d._fail_streak == 0                   # and no backoff either


def test_reception_verification_fails_OPEN_when_the_hub_is_unreadable(
        home, monkeypatch, capsys):
    """An unreadable /owed is a fact about the NETWORK, never a verdict on the
    agent's turn.

    It used to be scored as a failed turn, which bumped the on-disk poison
    ledger; three transient blips (a hub restart, a slow response, the 5s
    timeout) quarantined the wake key permanently and the seat went silently
    deaf to that obligation. Measured live: 18 quarantined keys on one seat,
    6 on another, all at exactly 3 strikes.
    """
    monkeypatch.setattr(
        "agora.drive._owed_snapshot",
        lambda *_args, **_kwargs: (None, None, None),
    )

    class FakeProc:
        returncode = 0
        stderr = ""
        stdout = (
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"item.completed","item":{"type":"mcp_tool_call",'
            '"server":"agora","tool":"whoami","status":"completed",'
            '"error":null}}\n'
            '{"type":"item.completed","item":{"type":"mcp_tool_call",'
            '"server":"agora","tool":"check_inbox","status":"completed",'
            '"error":null}}\n'
            '{"type":"item.completed","item":{"type":"mcp_tool_call",'
            '"server":"agora","tool":"ack_inbox","status":"completed",'
            '"error":null}}\n'
            '{"type":"turn.completed"}\n'
        )

    monkeypatch.setattr("agora.drive.subprocess.run", lambda *a, **k: FakeProc())
    d = Driver("worker", "http://hub:1", harness="codex", cwd=home)
    assert d.run_turn() is True
    assert d._last_turn_ok is True          # fails OPEN
    output = capsys.readouterr().out
    # ...but never silently: the skipped verification is on the record.
    assert "status=skipped" in output
    assert "no-owed-snapshot-before-turn" in output
    # And it costs the seat nothing: no backoff, no held wake.
    assert d._fail_streak == 0
    assert d._pending_wake is False


def test_unconsumed_answers_never_fail_a_turn(home, monkeypatch):
    """The delegate's own penalty, deleted.

    `to_consume` is the hub's advisory 'someone answered you; use it or close
    it' ledger — it never escalates and never wakes anyone. The driver used to
    score it as unsettled debt, so the seat that had just orchestrated a
    fan-out was marked FAILED the moment its peers ANSWERED. Live 2026-08-03,
    that is exactly what happened to the delegate at 00:27:49, 00:32:53 and
    02:38:58 (`to_consume=<answer id>`), and each verdict respawned an
    identical turn in the same second.
    """
    owed = {"to_answer": [{"id": "q-1"}],
            "to_consume": [{"answer_id": "a-1"}, {"answer_id": "a-2"}],
            "counts": {"to_answer": 1, "to_consume": 2}}
    after = {"to_answer": [], "to_consume": [{"answer_id": "a-1"},
                                             {"answer_id": "a-2"}],
             "counts": {"to_answer": 0, "to_consume": 2}}
    snapshots = iter([((1, 2), "q-1", owed), ((0, 2), "a-1,a-2", after)])
    monkeypatch.setattr("agora.drive._owed_snapshot",
                        lambda *a, **k: next(snapshots))
    d = _driver(home, lambda p, s: ("s", True))
    d.verify_reception_debt = True
    d._reception_debt_verification_required = True
    d._reception_debt_before = d._reception_debt()
    assert d._reception_debt_before.to_answer == frozenset({"q-1"})
    # The answered question is settled; the unread answers are NOT debt.
    assert d._verify_reception_debt(TurnEvidence(ok=True), "wake").ok


def test_codex_reception_accepts_original_debt_settled_during_turn(
        home, monkeypatch):
    before = {
        "to_answer": [{"id": "question-1"}],
        "to_consume": [{"answer_id": "answer-1"}],
        "counts": {"to_answer": 1, "to_consume": 1},
    }
    after = {
        "to_answer": [{"id": "new-question"}],
        "to_consume": [],
        "counts": {"to_answer": 1, "to_consume": 0},
    }
    snapshots = iter([
        ((1, 1), "question-1,answer-1", before),
        ((1, 0), "new-question", after),
    ])
    monkeypatch.setattr(
        "agora.drive._owed_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )

    class FakeProc:
        returncode = 0
        stderr = ""
        stdout = (
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            + "\n".join(json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call", "server": "agora",
                    "tool": tool, "status": "completed", "error": None,
                },
            }) for tool in (
                "whoami", "check_inbox", "read_message",
                "post_message", "ack_inbox",
            ))
            + '\n{"type":"turn.completed"}\n'
        )

    monkeypatch.setattr("agora.drive.subprocess.run", lambda *a, **k: FakeProc())
    d = Driver("worker", "http://hub:1", harness="codex", cwd=home)
    assert d.run_turn() is True
    assert d._last_turn_ok is True
    assert d.reception_session_id == "thread-1"


def test_codex_mcp_successful_retry_settles_earlier_tool_error(home):
    adapter = CodexDriveAdapter(
        model=None, permissions="write", cwd=home, mcp=_binding(home)
    )
    rows = [
        {"type": "item.completed", "item": {
            "type": "mcp_tool_call", "server": "agora", "tool": "send_dm",
            "status": "failed", "error": "bad first shape"}},
        {"type": "item.completed", "item": {
            "type": "mcp_tool_call", "server": "agora", "tool": "send_dm",
            "status": "completed", "error": None}},
        {"type": "turn.completed"},
    ]
    successful, failed = adapter._mcp_calls("\n".join(json.dumps(r) for r in rows))
    assert "send_dm" in successful
    assert failed == []

    successful, failed = adapter._mcp_calls(
        "\n".join(json.dumps(r) for r in reversed(rows))
    )
    assert "send_dm" in successful
    assert failed == []


def test_codex_mcp_binding_carries_download_dir_only_in_server_config(
        home, monkeypatch):
    download_dir = home / "confined-downloads"
    monkeypatch.setenv("AGORA_DOWNLOAD_DIR", str(download_dir))
    captured = {}

    class FakeProc:
        returncode = 0
        stderr = ""
        stdout = (
            '{"type":"item.completed","item":{"type":"mcp_tool_call",'
            '"server":"agora","tool":"check_inbox","status":"completed",'
            '"error":null}}\n'
            '{"type":"item.completed","item":{"type":"mcp_tool_call",'
            '"server":"agora","tool":"ack_inbox","status":"completed",'
            '"error":null}}\n'
            '{"type":"turn.completed"}\n'
        )

    def fake_run(cmd, **kwargs):
        captured.update(cmd=cmd, env=kwargs["env"])
        return FakeProc()

    monkeypatch.setattr("agora.drive.subprocess.run", fake_run)
    Driver("worker", "http://hub:1", harness="codex", cwd=home)._spawn_turn(
        WAKE_PROMPT, "thread-1"
    )
    assert "AGORA_DOWNLOAD_DIR" not in captured["env"]
    assert f'AGORA_DOWNLOAD_DIR="{download_dir}"' in "\n".join(captured["cmd"])


# -- the uniform harness contract (model / reasoning / provider) ---------------

def test_unsupported_knob_is_refused_at_arm_time_naming_the_harness(home):
    """A knob a harness cannot express is refused BEFORE the loop starts.

    Accepted-and-dropped is the failure mode this replaces: the seat armed
    `status=ok` and then died rc=1 on every single wake — permanently mute,
    and healthy in the one line an operator reads.
    """
    with pytest.raises(SystemExit) as e:
        run_drive(agent_id="worker", url="http://hub:1", harness="claude",
                  provider="airelay", cwd=home, once=True)
    assert "--provider is not supported by the 'claude' harness" in str(e.value)
    assert "abstractcode" in str(e.value)      # names who CAN

    with pytest.raises(SystemExit) as e:
        run_drive(agent_id="worker", url="http://hub:1", harness="cursor",
                  reasoning_effort="high", cwd=home, once=True)
    assert "--reasoning-effort is not supported" in str(e.value)


def test_reasoning_value_is_validated_against_the_harness_vocabulary(home):
    """agora's flag spans several vendors' vocabularies. `max` is real for some
    harnesses and unknown to AbstractCode, whose CLI stops at `xhigh`."""
    with pytest.raises(SystemExit) as e:
        run_drive(agent_id="worker", url="http://hub:1", harness="abstractcode",
                  model="gpt-5.4", reasoning_effort="max", cwd=home, once=True)
    detail = str(e.value)
    assert "accepts --reasoning-effort" in detail
    assert "xhigh" in detail and "got 'max'" in detail
    # ...and a value the harness DOES know is not refused here.
    assert "medium" in AbstractCodeDriveAdapter.REASONING_VOCAB
    assert "max" not in AbstractCodeDriveAdapter.REASONING_VOCAB


def test_abstractcode_rotation_unlinks_state_and_keeps_the_sidecar(home):
    """AbstractCode's memory is the --state-file, not a vendor resume id, so
    clearing agora's session POINTER rotated nothing and context grew forever
    (there is no headless self-compaction). The sidecar must survive: it holds
    provider/model and the MCP server block."""
    adapter = AbstractCodeDriveAdapter(
        model="gpt-5.4", provider="airelay", cwd=home,
        mcp=_binding(home))
    state = Path(adapter.build_command(BOOT_PROMPT, None)[
        adapter.build_command(BOOT_PROMPT, None).index("--state-file") + 1])
    state.write_text('{"session_id": "sess_old"}')
    sidecar = state.with_suffix(".config.json")
    assert sidecar.exists()

    adapter.rotate_session("reception")
    assert not state.exists()                       # context flushed
    assert sidecar.exists()                         # configuration kept
    cfg = json.loads(sidecar.read_text())
    assert cfg["model"] == "gpt-5.4" and cfg["provider"] == "airelay"
    assert "agora" in cfg["mcp_servers"]
    adapter.rotate_session("reception")             # idempotent


def test_effective_model_reports_the_sidecar_not_harness_default(home):
    """`model=harness-default` is a lie when a sidecar pins one — and that line
    is where an operator looks first when a seat is quiet."""
    adapter = AbstractCodeDriveAdapter(
        model="gpt-5.4", provider="airelay", cwd=home,
        mcp=_binding(home))
    adapter.build_command(BOOT_PROMPT, None)        # writes the sidecar
    assert adapter.effective_model() == "gpt-5.4"

    blind = AbstractCodeDriveAdapter(model=None, cwd=home,
                                     mcp=_binding(home))
    assert "gpt-5.4" in blind.effective_model()     # read from the sidecar


def test_adapter_env_may_never_carry_an_agora_credential(home):
    """An ambient AGORA_* key in a harness process is how a tool ends up
    posting under a foreign identity; the bearer belongs to the 0600 cache."""
    d = Driver("worker", "http://hub:1", harness="codex", cwd=home)
    d._adapter.environment = lambda: {"AGORA_API_KEY": "agora_leak"}
    with pytest.raises(SystemExit) as e:
        d._harness_env()
    assert "AGORA_API_KEY" in str(e.value)
    # Non-secret identity IS allowed (the pi bridge rides it), and so is the
    # explicit EMPTY key that forces agora-mcp onto the 0600 cache.
    d._adapter.environment = lambda: {"AGORA_AGENT_ID": "worker",
                                      "AGORA_URL": "http://hub:1",
                                      "AGORA_API_KEY": ""}
    assert d._harness_env()["AGORA_AGENT_ID"] == "worker"


def test_abstractcode_tui_is_drivable_with_a_named_identity_limitation(home):
    """Superseded 2026-07-30: a real hub turn on this harness was verified
    (check_inbox -> post_message -> ack_inbox, hub receipt), so refusing it as
    "no single-turn, no tool-reach" was agora guessing wrong about another
    framework — the exact habit the contract exists to end.

    What it genuinely cannot do is tell a turn WHICH seat it is. That is a
    degraded capability with a named limitation, not a refusal.
    """
    from agora.drive import AbstractCodeTuiDriveAdapter as Tui

    assert "single-turn" not in Tui.UNMET and "tool-reach" not in Tui.UNMET
    assert Tui.IDENTITY_SCOPE == "process"
    cmd = Tui(model="m", permissions="write", cwd=home, mcp=_binding(home),
              harness_args={"workflow": "b:f"}).build_command("p", None)
    assert cmd[:2] == ["abstractcode-tui", "exec"]
    assert "--workspace" in cmd and "--workflow" in cmd


def test_impossible_configuration_aborts_instead_of_retrying_forever(home):
    """A CONFIG error can never succeed, so it must not be retried.

    Semantic failures ARE retried (debt often needs another pass), so without a
    fatal class a bad `--reasoning-effort` respawned a turn on every wake
    forever. Live example: codex maps `ultra` to the API's `max`, which gpt-5.4
    rejects — the API's own message names the values it does accept, so quote it
    and stop.
    """
    (home / "worker-inbox.log").write_text("x")
    detail = ('{"error": {"message": "Unsupported value: \'max\' is not '
              'supported with the \'gpt-5.4\' model. Supported values are: '
              '\'none\', \'low\', \'medium\', \'high\', and \'xhigh\'."}}')

    def spawn(prompt, sid):
        d._last_turn_stage = "harness-config"
        d._last_turn_detail = detail
        return sid, False

    d = _driver(home, spawn)
    with pytest.raises(SystemExit) as excinfo:
        d.run_turn()
    message = str(excinfo.value)
    assert "no retry can fix it" in message
    assert "Supported values are" in message      # the harness's own words
    assert "--reasoning-effort" in message        # and where to look
    assert d._fail_streak == 0                    # fatal, so never backed off


def test_reasoning_vocabularies_match_each_harness(home):
    """Ground truth from LIVE probes, not a binary's enum.

    Codex's CLI validates nothing (it has a `Custom` passthrough and forwards
    any string), so the API is the authority: `none` returns rc=0, while
    `minimal` and `ultra` both 400 — `ultra` because codex translates it to the
    API's `max`, which every reachable model rejects. AbstractCode's canonical
    set is `reasoning.py::CANONICAL_VALUES`.
    """
    assert CodexDriveAdapter.REASONING_VOCAB == (
        "none", "low", "medium", "high", "xhigh")
    assert AbstractCodeDriveAdapter.REASONING_VOCAB == (
        "auto", "none", "minimal", "low", "medium", "high", "xhigh")
    for vocab in (CodexDriveAdapter.REASONING_VOCAB,
                  AbstractCodeDriveAdapter.REASONING_VOCAB):
        assert "max" not in vocab and "ultra" not in vocab
    # `minimal` is valid on AbstractCode and used to be unreachable from agora's
    # flag (argparse refused it before per-harness validation ever ran)...
    assert "minimal" in AbstractCodeDriveAdapter.REASONING_VOCAB
    # ...but NOT on codex: the API refuses it, so agora refuses it at arm time.
    assert "minimal" not in CodexDriveAdapter.REASONING_VOCAB
    # `none` DOES work on codex (verified rc=0) — removing it was a regression.
    assert "none" in CodexDriveAdapter.REASONING_VOCAB


def test_codex_defaults_to_gpt_5_4_medium_not_the_ambient_config(home):
    """A driven seat's model is agora's decision, never a leftover.

    Live failure this pins: `model = "gpt-5.6-sol"` in $CODEX_HOME/config.toml
    made EVERY codex wake fail with a 400 ("requires a newer version of Codex"),
    because with no --model the harness read its own ambient config.
    """
    adapter = _make_adapter("codex", model=None, provider=None,
                            permissions="write", cwd=home, mcp=_binding(home))
    assert adapter.model == "gpt-5.4"
    assert adapter.reasoning_effort == "medium"
    cmd = adapter.build_command(WAKE_PROMPT, None)
    assert "gpt-5.4" in cmd
    assert 'model_reasoning_effort="medium"' in " ".join(cmd)

    # An explicit flag still wins over the declared default.
    explicit = _make_adapter("codex", model="gpt-5.5", provider=None,
                             permissions="write", cwd=home, mcp=_binding(home),
                             reasoning_effort="high")
    assert explicit.model == "gpt-5.5" and explicit.reasoning_effort == "high"

    # Harnesses without a pinned default still defer to themselves.
    for name in ("claude", "cursor"):
        other = _make_adapter(name, model=None, provider=None,
                              permissions="write", cwd=home, mcp=_binding(home))
        assert other.model is None


# -- opencode + pi (added 0.12.60; ground-truthed with 28 live runs) ----------

def test_opencode_command_pins_dir_and_config_layer(home):
    """`opencode run` resolves the parent shell's $PWD, not the process cwd —
    verified live: a turn without `--dir` ran in the wrong directory with no
    project config and no AGENTS.md. The per-run config layer rides
    OPENCODE_CONFIG_CONTENT (its highest-precedence, deep-merged layer)."""
    from agora.drive import OpencodeDriveAdapter

    a = OpencodeDriveAdapter(model="gpt-5.4-mini", provider="airelay",
                             permissions="write", cwd=home, mcp=_binding(home))
    cmd = a.build_command(WAKE_PROMPT, None)
    assert cmd[:2] == ["opencode", "run"]
    assert cmd[cmd.index("--dir") + 1] == str(home.resolve())
    assert cmd[cmd.index("--title") + 1] == "agora:worker:turn"
    assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "airelay/gpt-5.4-mini"
    cfg = json.loads(a.environment()["OPENCODE_CONFIG_CONTENT"])
    assert cfg["mcp"]["agora"]["command"] == [a.mcp.command]
    assert cfg["permission"]["agora*"] == "allow"
    assert cfg["permission"]["webfetch"] == "deny"      # write != all
    env = a.environment()
    assert env["XDG_DATA_HOME"] == str(home / ".agora" / "opencode" / "data")
    assert env["XDG_CACHE_HOME"] == str(home / ".agora" / "opencode" / "cache")
    assert env["XDG_STATE_HOME"] == str(home / ".agora" / "opencode" / "state")
    assert Path(env["XDG_DATA_HOME"]).is_dir()
    # session resume
    resume = a.build_command(WAKE_PROMPT, "ses_x")
    assert "--session" in resume
    assert resume[resume.index("--title") + 1] == "agora:worker:resume"


def test_opencode_rejected_agora_tool_fails_the_turn_despite_rc0(home):
    """Headless permission `ask` is AUTO-REJECTED and the process still exits
    0 — the exact looks-alive-settles-nothing shape. Verified live."""
    from agora.drive import OpencodeDriveAdapter

    a = OpencodeDriveAdapter(model="m", permissions="write", cwd=home,
                             mcp=_binding(home))
    stdout = (
        '{"type":"tool_use","part":{"type":"tool","tool":"agora_whoami",'
        '"state":{"status":"error","error":"permission rejected"}}}\n'
        '{"type":"step_finish"}\n')
    verdict = a.assess_turn(stdout, "", 0, "wake")
    assert verdict.ok is False
    assert verdict.reason == "agora-tool-rejected"
    ok = a.assess_turn(
        '{"type":"tool_use","part":{"type":"tool","tool":"agora_whoami",'
        '"state":{"status":"completed"}}}\n', "", 0, "wake")
    assert ok.ok is True and ok.tools == ("agora_whoami",)


def test_opencode_pins_external_directory_at_every_level(home):
    """Out-of-workspace access is a SEPARATE opencode permission. Left
    unstated it falls to opencode's own default, so the meaning of an
    operator's `--permissions` word would change under agora whenever
    opencode changed that default."""
    from agora.drive import OpencodeDriveAdapter

    levels = {}
    for level in ("read", "write", "all"):
        a = OpencodeDriveAdapter(model="m", permissions=level, cwd=home,
                                 mcp=_binding(home))
        block = json.loads(a.environment()["OPENCODE_CONFIG_CONTENT"])["permission"]
        levels[level] = block["external_directory"]
    assert levels == {"read": "deny", "write": "deny", "all": "allow"}


def test_opencode_refused_tool_is_named_on_both_refusal_paths(home):
    """A refused `bash` never fails an opencode turn, so without this the
    driver log is green while the seat is stuck — and what the model is told
    ("The user rejected permission...") is a sentence no user typed. A live
    seat believed it, burned ~40 minutes and filed a blocked claim asking the
    operator for permission the operator had already granted (2026-08-01)."""
    from agora.drive import OpencodeDriveAdapter

    a = OpencodeDriveAdapter(model="m", permissions="write", cwd=home,
                             mcp=_binding(home))
    # path 1 — a permission left on opencode's `ask` default (stderr).
    stderr = ("\x1b[93m\x1b[1m! \x1b[0mpermission requested: "
              "external_directory (/Users/x/Desktop/*, /Users/x/gen/*); "
              "auto-rejecting\n"
              "\x1b[93m\x1b[1m! \x1b[0mpermission requested: "
              "external_directory (/Users/x/Desktop/*); auto-rejecting\n")
    asked = a.turn_notices("", stderr)
    assert len(asked) == 1                        # one line per permission
    assert "permission=external_directory" in asked[0]
    assert "/Users/x/Desktop/*" in asked[0] and "/Users/x/gen/*" in asked[0]
    assert "which no user did" in asked[0]

    # path 2 — agora's own pinned `deny` (stdout tool error, no stderr line).
    stdout = json.dumps({"type": "tool_use", "part": {
        "type": "tool", "tool": "bash", "state": {
            "status": "error",
            "input": {"command": 'touch "/Users/x/Desktop/a.txt"'},
            "error": "The user has specified a rule which prevents you from "
                     "using this specific tool call. Here are some of the "
                     "relevant rules [...]"}}}) + "\n"
    ruled = a.turn_notices(stdout, "")
    assert len(ruled) == 1
    assert "tool=bash" in ruled[0] and "--permissions write" in ruled[0]
    assert '/Users/x/Desktop/a.txt' in ruled[0]
    for line in asked + ruled:
        assert "--permissions all" in line and str(home.resolve()) in line
    # A clean turn says nothing: this must not become per-turn noise.
    assert a.turn_notices("", "") == []


def test_turn_notices_default_to_silence(home):
    """Every other harness inherits the hook and stays quiet."""
    from agora.drive import ClaudeDriveAdapter, CodexDriveAdapter

    for cls in (ClaudeDriveAdapter, CodexDriveAdapter):
        a = cls(model="m", permissions="write", cwd=home, mcp=_binding(home))
        assert a.turn_notices("some stdout", "some stderr") == []


def test_pi_command_carries_the_bridge_and_owns_the_session_namespace(home):
    """pi ships no MCP client, so agora's bridge extension rides `-e`; the
    session id is CALLER-chosen (`--session-id` creates if missing), so agora
    owns the namespace and resume can never fork silently."""
    from agora.drive import PiDriveAdapter

    a = PiDriveAdapter(model="gpt-5.4-mini", provider="airelay",
                       permissions="read", cwd=home, mcp=_binding(home))
    cmd = a.build_command(WAKE_PROMPT, "agora-worker")
    assert cmd[:4] == ["pi", "-p", "--mode", "json"]
    assert "--approve" in cmd                       # trust is explicit
    ext = cmd[cmd.index("-e") + 1]
    assert ext.endswith("pi_ext/agora.js") and Path(ext).is_file()
    assert "--no-builtin-tools" in cmd              # read = agora tools only
    assert cmd[cmd.index("--session-id") + 1] == "agora-worker"
    env = a.environment()
    assert env["AGORA_MCP_COMMAND"] == a.mcp.command
    assert env["AGORA_API_KEY"] == "" and env["AGORA_ADMIN_KEY"] == ""
    assert f"pi-{a.mcp.agent_id}" in env["PI_CODING_AGENT_DIR"]


def test_pi_truncated_stream_is_a_failed_turn(home):
    """agent_settled is pi's healthy-turn terminator; rc=0 without it means
    the stream died (verified live: a bridge holding the event loop open)."""
    from agora.drive import PiDriveAdapter

    a = PiDriveAdapter(model="m", permissions="write", cwd=home,
                       mcp=_binding(home))
    good = a.assess_turn(
        '{"type":"session","version":3,"id":"s1"}\n'
        '{"type":"tool_execution_end","toolName":"agora_whoami","isError":false}\n'
        '{"type":"agent_settled"}\n', "", 0, "wake")
    assert good.ok is True and good.tools == ("agora_whoami",)
    truncated = a.assess_turn(
        '{"type":"tool_execution_end","toolName":"agora_whoami","isError":false}\n',
        "", 0, "wake")
    assert truncated.ok is False and truncated.reason == "stream-truncated"
    assert a.parse_session_id(
        '{"type":"session","version":3,"id":"piseat1"}\n', None) == "piseat1"


# -- the 2026-07-31 fleet-silence class ------------------------------------
#
# Eight opencode seats went mute for hours while every driver stayed alive and
# heartbeating. The provider (a free tier) began rate-limiting at 04:59; each
# turn booted a session, called NOTHING, and was killed at the turn timeout.
# The driver scored that as a harness crash, and three such turns QUARANTINED
# the wake key — and because the key was the owed SIGNATURE, an obligation the
# seat could not settle produced the SAME key forever. The seat went deaf to
# exactly the debt it most needed to answer.
#
# The quarantine (and its on-disk strike ledger) is gone as of 2026-08-03: in
# the entire live record it never fired once, and its only possible act was to
# DROP an obligation. One mechanism remains — exponential backoff, which holds
# the wake instead of dropping it and always expires. These tests pin that.


def _hang(*_a, **_k):
    raise subprocess.TimeoutExpired(cmd="opencode", timeout=600, output="")


def test_timeout_with_no_tool_calls_is_infrastructure_not_a_strike(home,
                                                                  monkeypatch):
    """The outage shape: killed at the timeout having called nothing. That is
    a fact about the PROVIDER, so it must be named, backed off — and the
    obligation KEPT, however many times it repeats."""
    monkeypatch.setattr(subprocess, "run", _hang)
    d = Driver("worker", "http://127.0.0.1:1", harness="opencode", cwd=home)
    for _ in range(5):
        d._retry_at = 0.0                  # stand in for the backoff elapsing
        assert d.run_turn() is True        # every wake still SPAWNS a turn
    assert d._last_turn_stage == "infrastructure"
    assert d._pending_wake is True             # the obligation is HELD...
    assert d._backoff_retry_after() > 0        # ...behind a real backoff
    assert d._hold(has_debt=True)[0] == "backoff"


def test_repeated_harness_failure_backs_off_and_never_drops_the_wake(home):
    """A crashing turn is the ONLY thing that used to earn a quarantine.

    Now it earns spacing: the wake is held, the retry grows to a ceiling, and
    a healthy turn clears the whole streak. Nothing is ever dropped, so the
    seat cannot go deaf to a specific obligation.
    """
    d = _driver(home, lambda prompt, sid: (sid, False))
    waits = []
    for _ in range(6):
        d._retry_at = 0.0                        # the previous wait elapsed
        assert d.run_turn() is True              # ALWAYS retried
        assert d._pending_wake is True           # ALWAYS kept
        waits.append(d._backoff_retry_after())
    assert waits[0] < waits[1] < waits[2]        # exponential...
    assert waits[-1] <= BACKOFF_MAX + 1          # ...with a ceiling
    d._spawn = lambda prompt, sid: ("s1", True)
    d._retry_at = 0.0
    assert d.run_turn() is True
    assert d._fail_streak == 0 and d._backoff_retry_after() == 0.0


def test_a_failing_provider_never_blocks_a_wake_forever(home, monkeypatch):
    """The whole point: when the provider heals, the held obligation runs."""
    monkeypatch.setattr(subprocess, "run", _hang)
    d = Driver("worker", "http://127.0.0.1:1", harness="opencode", cwd=home)
    for _ in range(5):
        d._retry_at = 0.0
        d.run_turn()
    ran = []
    d._spawn = lambda prompt, sid: (ran.append(sid) or ("s1", True))
    d._retry_at = 0.0
    assert d.run_turn() is True and len(ran) == 1


def test_a_held_turn_announces_its_state_and_release(home, capsys):
    """Every hold is on stdout with the second it releases — that IS the
    'never silently mute' guarantee, now once per loop pass instead of once
    per five minutes."""
    d = _driver(home, lambda prompt, sid: (sid, False))
    d.run_turn()                                     # fails -> backoff
    capsys.readouterr()
    assert d.run_turn() is False                     # held, not spawned
    out = capsys.readouterr().out
    assert "AGORA_DRIVE state=backoff" in out
    assert "wake=held" in out and "next=" in out

    parked = _driver(home, lambda prompt, sid: ("s", True), turn_budget=1)
    assert parked.run_turn() is True
    capsys.readouterr()
    assert parked.run_turn() is False
    out = capsys.readouterr().out
    assert "AGORA_DRIVE state=parked" in out and "reason=turn-budget" in out
    assert "next=" in out


def test_a_provider_failure_is_infrastructure_on_every_harness(home):
    """One classifier, centrally: only opencode used to re-stage a 429, so the
    same rate limit was `infrastructure` there and `harness` everywhere else.
    Live 2026-08-03 02:37 recorded `stage=harness reason=timeout` with
    `detail="429: timed out after 3600s"` — the diagnosis was in the detail
    while the stage blamed the seat."""
    d = _driver(home, lambda p, s: (s, True))
    crashed = TurnEvidence(ok=False, stage="harness", reason="nonzero-exit",
                           detail="upstream returned 429 Too Many Requests")
    restaged = d._classify_provider_failure(crashed, "")
    assert restaged.stage == "infrastructure"
    assert restaged.reason == "provider-failure"
    # Only the HARNESS's own words are read (stderr + the detail the adapter
    # extracted from the harness), never the model's transcript — a turn that
    # merely crashed stays a harness failure.
    plain = TurnEvidence(ok=False, stage="harness", reason="nonzero-exit",
                         detail="segmentation fault")
    assert d._classify_provider_failure(plain, "").stage == "harness"
    # A healthy turn is never re-staged.
    assert d._classify_provider_failure(TurnEvidence(ok=True), "503").ok
    # A config refusal is never laundered into "retry forever".
    config = TurnEvidence(ok=False, stage="harness-config", reason="x",
                          detail="503 service unavailable")
    assert d._classify_provider_failure(config, "").stage == "harness-config"


def test_every_failed_turn_lands_in_the_failure_ledger(home, monkeypatch):
    """Unconditional, unlike --turn-log: the outage left no durable trace."""
    monkeypatch.setattr(subprocess, "run", _hang)
    d = Driver("worker", "http://127.0.0.1:1", harness="opencode", cwd=home)
    d.run_turn()
    ledger = home / "drive-worker.failures.jsonl"
    row = json.loads(ledger.read_text().splitlines()[-1])
    assert row["stage"] == "infrastructure" and row["reason"] == "no-tool-calls"
    assert row["agent"] == "worker" and row["harness"] == "opencode"
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600   # operator eyes only


def test_failure_ledger_is_size_capped(home, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _hang)
    d = Driver("worker", "http://127.0.0.1:1", harness="opencode", cwd=home)
    ledger = home / "drive-worker.failures.jsonl"
    ledger.write_text("x" * (FAILURE_LEDGER_MAX_BYTES + 10))
    d.run_turn()
    assert ledger.stat().st_size < FAILURE_LEDGER_MAX_BYTES


def test_rate_limit_stderr_is_a_provider_failure(home):
    """opencode's real 429 text, verified against the live log — now judged by
    the ONE central classifier rather than that adapter's private branch."""
    stderr = ("AI_APICallError: Rate limit exceeded. Please try again later.\n"
              "providerID=opencode modelID=deepseek-v4-flash-free\n")
    a = _make_adapter("opencode", model=None, provider=None,
                      permissions="write", harness_args=None, cwd=home,
                      mcp=_binding(home), reasoning_effort=None)
    d = _driver(home, lambda p, s: (s, True))
    ev = d._classify_provider_failure(a.assess_turn("", stderr, 1, "wake"),
                                      stderr)
    assert ev.ok is False and ev.stage == "infrastructure"
    assert ev.reason == "provider-failure"
    # A genuine harness crash still is one.
    crash = a.assess_turn("", "Segmentation fault", 139, "wake")
    assert d._classify_provider_failure(crash, "").stage == "harness"


def test_work_chunk_is_capped_and_announced_while_it_blocks(home, monkeypatch,
                                                            capsys):
    """A chunk blinds reception for its whole timeout, so an UNPROVEN provider
    never gets the full one, and the blindness is visible on stdout."""
    monkeypatch.setattr(subprocess, "run", _hang)
    d = Driver("worker", "http://127.0.0.1:1", harness="opencode", cwd=home,
               work_timeout=TURN_TIMEOUT)
    seen = []
    d._spawn = lambda prompt, sid: (seen.append(d._turn_timeout) or (sid, False))
    d.run_work_turn()
    assert seen[-1] == TURN_TIMEOUT              # healthy: the full budget
    d._fail_streak = 1                           # the harness just failed
    d.run_work_turn()
    assert seen[-1] == RECEPTION_TURN_TIMEOUT    # unproven: fail fast
    assert "AGORA_DRIVE turn-start" in capsys.readouterr().out


def test_no_work_chunk_while_the_harness_is_failing(home):
    """A chunk runs the SAME binary against the SAME endpoint as the turn that
    just failed, and it would blind the seat for up to --work-timeout doing
    it. So the backoff gates both, and the listen window shrinks to the retry
    instant instead of the idle ceiling."""
    d = _driver(home, lambda prompt, sid: (sid, True))
    d._retry_at = time.time() + 60
    snap = ("commons", "claim:x", 1)
    assert d._chain_step(snap) is False
    assert d._chain_block(snap)[0] == "harness-failing"
    assert 0 < d._listen_window(snap) <= 60      # not the 1200s ceiling


def test_a_work_chunk_that_never_reached_the_hub_feeds_the_one_backoff(home):
    """The chunk path used to bypass the ONLY failure mechanism.

    Measured on this tree before the fix: three consecutive chunks against a
    429 endpoint each still got the FULL --work-timeout (3600s), because the
    "unproven harness" cap reads a streak that only reception ever fed. Three
    hours deaf per row, with `_hold` and `_chain_block` both reporting healthy.
    A transport failure is a transport failure whichever prompt hit it.
    """
    d = _driver(home, None, work_timeout=TURN_TIMEOUT)
    seen = []

    def spawn(prompt, sid):
        seen.append(d._turn_timeout)
        d._last_turn_stage = "infrastructure"     # a 429 on the WORK path
        d._last_turn_detail = "429: rate limited"
        return None, False

    d._spawn = spawn
    snap = ("commons", "claim:x", 1)
    assert d._chain_step(snap) is True            # the first chunk runs
    assert seen == [TURN_TIMEOUT]                 # ...at the full budget
    assert d._fail_streak == 1                    # ...and IS recorded
    # ...so the next chunk does not even start, and reception is spaced too.
    assert d._chain_step(snap) is False
    assert seen == [TURN_TIMEOUT]
    assert d._chain_block(snap)[0] == "infrastructure"
    assert d._hold(has_debt=True)[0] == "backoff"


def test_off_row_receipt_takes_no_strike(home):
    """THE NOVEL-FLEET STALL (2026-08-04). The strike rule read only the
    SELECTED row, so a chunk that did exactly what WORK_PROMPT commands —
    advance a claim and write done/blocked on it — still struck the phase
    row that selected it (the terminal claim drops out of the snapshot,
    which falls back to the unchanged phase). Three by-the-book chunks
    retired the steward's only live row and six armed drivers idled 17.5h.
    A receipt on ANY row this seat owns is work, not spinning."""
    d = _driver(home, None)
    d.run_work_turn = lambda: True
    snap = ("scifi", "phase:novel", 5)
    d._continuation_snapshot = lambda: snap        # selected row unchanged
    now = time.time()
    d._work_rows = lambda: [(now + 60.0, "scifi", "claim:export"),
                            (now - 9999.0, "scifi", "phase:novel")]
    d._read_work_row = lambda ch, key: (
        2, {"owner": "worker", "blocked_on": "external", "needs": "the vendor build to land", "status": "blocked: waiting on operator"})
    assert d._chain_step(snap) is True
    assert d._strike_count("scifi/phase:novel@5") == 0   # receipt off-row

    # The same chunk with NO receipt anywhere is still a strike: the
    # anti-spin bound survives the fix.
    d._work_rows = lambda: [(now - 9999.0, "scifi", "phase:novel")]
    assert d._chain_step(snap) is True
    assert d._strike_count("scifi/phase:novel@5") == 1

    # Another seat's fresh row is NOT this seat's receipt.
    d._work_rows = lambda: [(now + 60.0, "scifi", "claim:other"),
                            (now - 9999.0, "scifi", "phase:novel")]
    d._read_work_row = lambda ch, key: (1, {"owner": "someone-else"})
    assert d._chain_step(snap) is True
    assert d._strike_count("scifi/phase:novel@5") == 2


def test_strikes_expire_after_cooldown(home):
    """Strike-out used to last the process lifetime, and the only seat that
    would ever bump a struck stewarded phase is the steward the strikes
    had retired — a deadlock, not a bound. Strikes now age out."""
    d = _driver(home, None)
    ck = "scifi/phase:novel@5"
    d._work_strikes[ck] = WORK_STRIKES
    d._work_strike_at[ck] = time.time() - (WORK_STRIKE_TTL + 1)
    assert d._strike_count(ck) == 0
    assert ck not in d._work_strikes           # slate wiped, not just masked
    # Fresh strikes still bind.
    d._work_strikes[ck] = WORK_STRIKES
    d._work_strike_at[ck] = time.time()
    assert d._strike_count(ck) == WORK_STRIKES


def test_a_workspace_only_chunk_is_never_a_transport_failure(home):
    """The other half: a chunk that DID work but touched no Agora tool is a
    SEMANTIC verdict (`mcp-use`). Holding reception for that would penalise a
    seat for working, so only the per-version strike ledger bounds it."""
    d = _driver(home, None)

    def spawn(prompt, sid):
        d._last_turn_stage = "mcp-use"            # ran fine, no agora call
        d._last_turn_detail = "no successful Agora MCP tool call"
        return None, False

    d._spawn = spawn
    assert d.run_work_turn() is True
    assert d._fail_streak == 0                    # NOT spaced
    assert d._hold(has_debt=True) is None         # reception untouched
    assert d._chain_block(("commons", "claim:x", 1)) is None


def test_a_held_pass_says_its_hold_exactly_once(home, monkeypatch):
    """One honest line per loop pass. A pass that armed already stating its
    hold must not repeat the identical line when the wake it was holding
    rings again — a parked seat's log used to carry every hold twice."""
    d = _driver(home, lambda prompt, sid: ("s", True), turn_budget=1,
                max_wait=5.0)
    log: list[str] = []
    monkeypatch.setattr("agora.drive._emit", log.append)
    # The pidfile touch IS the top of the loop, so it delimits one pass.
    monkeypatch.setattr(d, "_touch_drive_pid", lambda: log.append("--- pass"))
    calls = {"n": 0}

    def listen(**kw):
        calls["n"] += 1
        if calls["n"] >= 5:
            raise KeyboardInterrupt
        return 2                                  # a real obligation, always

    monkeypatch.setattr("agora.drive.run_listen", listen)
    with pytest.raises(KeyboardInterrupt):
        d.run()
    passes, current = [], []
    for line in log:
        if line.startswith("--- pass"):
            passes.append(current)
            current = []
        else:
            current.append(line)
    passes.append(current)
    for lines in passes:
        said = [ln.split(" agent=")[0] for ln in lines
                if ln.startswith("AGORA_DRIVE state=")]
        assert len(said) == len(set(said)), f"a pass said one state twice: {lines}"
    # ...and a pass that holds the wake is never SILENT about holding it.
    assert any("wake=held" in ln for ln in log)


def test_an_unreadable_work_scan_never_reads_as_an_idle_seat(home, monkeypatch):
    """`no-continuable-work` is a CLAIM about the hub, and a walk that raised
    never earned it. A delegate mid hub-blip holding a live claim must not log
    the same line as a seat that genuinely has nothing to do — that is the
    difference between 'it finished' and 'it cannot see its own work'."""
    d = _driver(home, lambda prompt, sid: ("s", True))
    log: list[str] = []
    monkeypatch.setattr("agora.drive._emit", log.append)
    monkeypatch.setattr("agora.drive.run_listen",
                        lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt))
    # No cached key -> the store walk cannot even start (_scan_ok stays False).
    monkeypatch.setattr("agora.drive._config.get_cached_key",
                        lambda hub, agent: None)
    with pytest.raises(KeyboardInterrupt):
        d.run()
    armed = [ln for ln in log if ln.startswith("AGORA_DRIVE state=armed")]
    assert armed and "reason=work-scan-unreadable" in armed[-1], armed
    assert "no-continuable-work" not in armed[-1]


def test_opencode_names_the_workspace_model(home):
    """The ambient-default trap that made a free tier drive eight seats: say
    which brain is answering, and warn when only the global layer knows."""
    a = _make_adapter("opencode", model=None, provider=None,
                      permissions="write", harness_args=None, cwd=home,
                      mcp=_binding(home), reasoning_effort=None)
    assert a.effective_model() == "harness-default"     # -> warn_effective_model
    (home / "opencode.json").write_text(json.dumps({"model": "vendor/some-model"}))
    assert a.effective_model().startswith("vendor/some-model")
    assert "opencode.json" in a.effective_model()


# -- the mute seat: claude turn evidence (2026-08-04) -------------------------


def _claude_adapter(home=None):
    return _make_adapter("claude", model=None, provider=None,
                         permissions="all", harness_args=None,
                         cwd=home or Path("."), mcp=_binding(home or Path(".")),
                         reasoning_effort=None)


def _claude_stream(*events) -> str:
    return "\n".join(json.dumps(e) for e in events)


_CLAUDE_INIT_OK = {"type": "system", "subtype": "init",
                   "mcp_servers": [{"name": "agora", "status": "connected"}]}
_CLAUDE_DONE = {"type": "result", "subtype": "success", "is_error": False,
                "result": "done"}


def _claude_call(tool: str, text: str = '{"ok": true}', *, err=None):
    return ({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1",
                 "name": f"mcp__agora__{tool}", "input": {}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": err,
                 "content": [{"type": "text", "text": text}]}]}})


def test_claude_turn_with_no_tool_calls_is_scored_mcp_use():
    """THE 6.8-SECOND TURN. A claude work chunk that called nothing exited 0
    and was scored `ok=true`, because the adapter had no `assess_turn` and
    `EVIDENCE` was None. Fleet-wide that hid 12 zero-tool turns, including a
    reception wake whose own text said the agora server was unavailable."""
    a = _claude_adapter()
    prose_only = _claude_stream(_CLAUDE_INIT_OK, _CLAUDE_DONE)
    ev = a.assess_turn(prose_only, "", 0, "work")
    assert (ev.ok, ev.stage, ev.reason) == (False, "mcp-use", "no-agora-tool-call")
    # A turn that really worked still passes, and now NAMES its tools.
    ev = a.assess_turn(
        _claude_stream(_CLAUDE_INIT_OK, *_claude_call("post_message"),
                       _CLAUDE_DONE), "", 0, "work")
    assert ev.ok and ev.tools == ("post_message",)


def test_claude_dead_agora_server_fails_the_turn_despite_rc_zero():
    """claude exits 0 with a dead MCP server: live on 2026-08-01 a seat spent
    a whole wake replying 'AGORA_MCP_UNAVAILABLE ... reception pass cannot
    complete' and was scored healthy, wake consumed, no backoff."""
    a = _claude_adapter()
    down = {"type": "system", "subtype": "init",
            "mcp_servers": [{"name": "agora", "status": "failed"}]}
    ev = a.assess_turn(_claude_stream(down, _CLAUDE_DONE), "", 0, "wake")
    assert (ev.ok, ev.stage, ev.reason) == (
        False, "mcp-init", "required-server-unavailable")


def test_claude_agora_refusal_is_not_a_healthy_pass():
    """A transport-successful call carrying agora's {"ok": false} refusal —
    a 403'd post — must not score as a pass (codex/opencode guard this)."""
    a = _claude_adapter()
    ev = a.assess_turn(_claude_stream(
        _CLAUDE_INIT_OK,
        *_claude_call("post_message", '{"ok": false, "detail": "blocked"}'),
        _CLAUDE_DONE), "", 0, "work")
    assert not ev.ok and ev.stage == "mcp-call"


def test_claude_truncated_stream_is_not_a_completed_turn():
    """No terminal `result` event = the turn was cut off, not completed."""
    a = _claude_adapter()
    ev = a.assess_turn(
        _claude_stream(_CLAUDE_INIT_OK, *_claude_call("check_inbox")),
        "", 0, "wake")
    assert (ev.ok, ev.reason) == (False, "missing-terminal-event")


def test_claude_reception_pass_requires_check_inbox_only():
    """Same relaxation codex carries: only check_inbox is required, and a
    correct no-op reception pass must not be scored a failed turn."""
    a = _claude_adapter()
    ev = a.assess_turn(_claude_stream(
        _CLAUDE_INIT_OK, *_claude_call("post_message"), _CLAUDE_DONE),
        "", 0, "wake")
    assert (ev.ok, ev.reason) == (False, "incomplete-reception-pass")
    ev = a.assess_turn(_claude_stream(
        _CLAUDE_INIT_OK, *_claude_call("check_inbox"), _CLAUDE_DONE),
        "", 0, "wake")
    assert ev.ok


def test_claude_build_command_asks_for_a_parseable_stream():
    """stream-json is what carries tool_use; --verbose is mandatory with it
    (the CLI exits 1 otherwise), so the two must never drift apart."""
    a = _claude_adapter()
    argv = a.build_command("p", None)
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv


def test_claude_turn_carries_the_agora_skill():
    """agora is MCP + SKILL, and this adapter shipped only the MCP half:
    0 skill loads in 4,585 tool calls across two 5-seat runs. `claude` has
    no --skill flag, so the skill rides in --append-system-prompt — the one
    surface `--resume` re-sends and compaction cannot evict."""
    argv = _claude_adapter().build_command("p", None)
    assert "--append-system-prompt" in argv
    body = argv[argv.index("--append-system-prompt") + 1]
    assert "# Working in agora channels" in body
    assert not body.lstrip().startswith("---")   # frontmatter is metadata
    assert argv[-1] == "p"                       # prompt stays positional
