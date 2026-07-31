"""The external resume-driver (agora drive): reception made STRUCTURAL.

These tests exercise the loop with an INJECTED spawn — no real cursor-agent —
so the guarantees the design rests on are pinned deterministically: a wake
drives exactly one bounded turn that yields by returning; the session id
persists across wakes and rotates; a per-hour budget parks a runaway; a
crashing wake is quarantined after N strikes (the poison-message bound);
and the sandbox default is never silently dropped.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from agora.drive import (BOOT_PROMPT, DEFAULT_BROADCAST_TURN_BUDGET,
                         DEFAULT_TURN_BUDGET, DEFAULT_WORK_BUDGET,
                         FAILURE_LEDGER_MAX_BYTES, INFRA_BACKOFF_MAX,
                         MUTE_NOTICE_INTERVAL, QUARANTINE_TTL,
                         RECEPTION_TURN_TIMEOUT,
                         AbstractCodeDriveAdapter, CodexDriveAdapter,
                         POISON_STRIKES, TURN_TIMEOUT, WAKE_PROMPT, Driver,
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
        to_consume=frozenset(),
        structured=(("dm:operator--worker", 2, "message-1", frozenset({"1"})),),
    )
    monkeypatch.setattr(
        d, "_reception_debt",
        lambda: ReceptionDebt(frozenset(), frozenset()),
    )
    monkeypatch.setattr(d, "_message_pending_asks", lambda *_: frozenset({"1"}))
    monkeypatch.setattr(d, "_owned_live_claims", lambda: [])
    evidence = d._verify_reception_debt(TurnEvidence(ok=True), "wake")
    assert not evidence.ok
    assert evidence.reason == "debt-remains"
    assert "pending_without_linked_claim=message-1" in (evidence.detail or "")

    monkeypatch.setattr(
        d, "_owned_live_claims",
        lambda: [("dm:operator--worker", "claim:msg-1", 1,
                  {"source_message_id": "message-1"})],
    )
    assert d._verify_reception_debt(TurnEvidence(ok=True), "wake").ok


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
    assert d.reception_session_id == "s1"
    d.run_turn()                                     # crashes -> session dropped
    assert d.reception_session_id is None
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
        ("claude", setup_claude, "claude",
         "drive-worker.claude.reception-v2.session", ["--resume", "claude-s"],
         [{"session_id": "claude-s", "result": "ok"}], "claude-s"),
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
    assert d._pending_wake is True
    output = capsys.readouterr().out
    assert "stage=reception" in output and "reason=debt-remains" in output
    assert "question-1" in output


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
    # And it costs no poison strike.
    assert d._attempts() == {}
    assert d._quarantined == set()


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
    assert d._attempts() == {}                    # not a poison strike either


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
    assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "airelay/gpt-5.4-mini"
    cfg = json.loads(a.environment()["OPENCODE_CONFIG_CONTENT"])
    assert cfg["mcp"]["agora"]["command"] == [a.mcp.command]
    assert cfg["permission"]["agora*"] == "allow"
    assert cfg["permission"]["webfetch"] == "deny"      # write != all
    # session resume
    assert "--session" in a.build_command(WAKE_PROMPT, "ses_x")


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
# The driver scored that as a harness crash, so three of them quarantined the
# wake key — and because the key is the owed SIGNATURE, an obligation the seat
# could not settle produced the SAME key forever. The seat went permanently
# deaf to exactly the debt it most needed to answer.


def _hang(*_a, **_k):
    raise subprocess.TimeoutExpired(cmd="opencode", timeout=600, output="")


def test_timeout_with_no_tool_calls_is_infrastructure_not_a_strike(home,
                                                                  monkeypatch):
    """The outage shape: killed at the timeout having called nothing. That is
    a fact about the PROVIDER, so it must be named, never struck."""
    monkeypatch.setattr(subprocess, "run", _hang)
    d = Driver("worker", "http://127.0.0.1:1", harness="opencode", cwd=home)
    for _ in range(POISON_STRIKES + 2):
        d._infra_retry_at = 0.0            # stand in for the backoff elapsing
        assert d.run_turn() is True
    assert d._last_turn_stage == "infrastructure"
    assert d._attempts() == {}                 # no poison strike, ever
    assert d._quarantined == set()             # so the seat is never deafened
    assert d._pending_wake is True             # and the obligation is HELD
    assert d._infra_retry_after() > 0          # behind a real backoff


def test_provider_failure_backs_off_and_recovers(home, monkeypatch):
    """Exponential backoff with a ceiling, cleared by one healthy turn."""
    monkeypatch.setattr(subprocess, "run", _hang)
    d = Driver("worker", "http://127.0.0.1:1", harness="opencode", cwd=home)
    d.run_turn()
    first = d._infra_retry_after()
    d._infra_retry_at = 0.0                    # simulate the wait elapsing
    d.run_turn()
    assert d._infra_retry_after() > first      # ...and it grows
    for _ in range(10):
        d._infra_retry_at = 0.0
        d.run_turn()
    assert d._infra_retry_after() <= INFRA_BACKOFF_MAX + 1

    d._spawn = lambda prompt, sid: ("s1", True)
    d._infra_retry_at = 0.0
    assert d.run_turn() is True
    assert d._infra_failures == 0 and d._infra_retry_after() == 0.0


def test_a_failing_provider_never_blocks_a_wake_forever(home, monkeypatch):
    """The whole point: when the provider heals, the held obligation runs."""
    monkeypatch.setattr(subprocess, "run", _hang)
    d = Driver("worker", "http://127.0.0.1:1", harness="opencode", cwd=home)
    for _ in range(5):
        d._infra_retry_at = 0.0
        d.run_turn()
    ran = []
    d._spawn = lambda prompt, sid: (ran.append(sid) or ("s1", True))
    d._infra_retry_at = 0.0
    assert d.run_turn() is True and len(ran) == 1


def test_quarantine_expires_and_restores_the_seat(home):
    """A quarantine is a bounded pause, not a life sentence: the wake key is
    the owed signature, which for an unsettleable debt never changes."""
    (home / "worker-inbox.log").write_text("x")
    d = _driver(home, lambda prompt, sid: (sid, False))
    for _ in range(POISON_STRIKES):
        assert d.run_turn() is True
    key = d._wake_key()
    assert d.run_turn() is False                     # deaf, as designed...
    d._quarantine_until[key] = time.time() - 1       # ...until the ttl lapses
    ran = []
    d._spawn = lambda prompt, sid: (ran.append(sid) or ("s1", True))
    assert d.run_turn() is True and len(ran) == 1
    assert d._attempts() == {}                       # strikes go with it


def test_quarantine_is_announced_with_its_expiry(home, capsys):
    (home / "worker-inbox.log").write_text("x")
    d = _driver(home, lambda prompt, sid: (sid, False))
    for _ in range(POISON_STRIKES):
        d.run_turn()
    d.run_turn()
    out = capsys.readouterr().out
    assert "reason=quarantined" in out and "retry_in=" in out
    assert f"ttl={QUARANTINE_TTL:.0f}s" in out


def test_mute_seat_says_so_at_intervals(home, capsys):
    """Never silent: a driver whose wakes are all held still heartbeats, so it
    must announce that it is NOT processing obligations."""
    d = _driver(home, lambda prompt, sid: (sid, False))
    d._pending_wake = True
    d._mute_notice()
    assert "AGORA_DRIVE mute" in capsys.readouterr().out
    d._mute_notice()                                  # throttled...
    assert "AGORA_DRIVE mute" not in capsys.readouterr().out
    d._last_mute_notice -= MUTE_NOTICE_INTERVAL + 1   # ...until the interval
    d._mute_notice()
    assert "AGORA_DRIVE mute" in capsys.readouterr().out
    d._pending_wake = False
    d._mute_notice()
    assert "AGORA_DRIVE mute" not in capsys.readouterr().out


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
    """opencode's real 429 text, verified against the live log."""
    a = _make_adapter("opencode", model=None, provider=None,
                      permissions="write", harness_args=None, cwd=home,
                      mcp=_binding(home), reasoning_effort=None)
    ev = a.assess_turn(
        "", "AI_APICallError: Rate limit exceeded. Please try again later.\n"
        "providerID=opencode modelID=deepseek-v4-flash-free\n", 1, "wake")
    assert ev.ok is False and ev.stage == "infrastructure"
    assert ev.reason == "provider-failure"
    # A genuine harness crash still is one.
    assert a.assess_turn("", "Segmentation fault", 139, "wake").stage == "harness"


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
    d._infra_failures = 1                        # provider just failed
    d.run_work_turn()
    assert seen[-1] == RECEPTION_TURN_TIMEOUT    # unproven: fail fast
    assert "AGORA_DRIVE turn-start" in capsys.readouterr().out


def test_no_work_chunk_while_the_provider_is_failing(home):
    d = _driver(home, lambda prompt, sid: (sid, True))
    d._infra_retry_at = time.time() + 60
    assert d._chain_step() is False              # never bet an hour on a dead
    assert d._chain_live is False                # endpoint


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
