"""A driven seat is BOUND to its hub — by flags and files, never by env.

Operator rule (2026-09-09): configuration travels as flags (`--url`, `--home`,
`--as`) and workspace files (`.agora/seat.json`, the turn marker
`.agora/driven.json`); the environment carries credentials only. History: a
lab seat once ran `agora up` from a turn and posted on the operator's
production hub because its CLI fell through to ~/.agora.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from agora import config as _config
from agora.drive import Driver
from agora.mcp.runtime import MCPBinding


def test_harness_env_carries_no_agora_variable_and_no_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    monkeypatch.setenv("AGORA_URL", "http://wrong-hub:9")          # must NOT leak
    d = Driver("worker", "http://hub:1", harness="codex", cwd=tmp_path)
    env = d._harness_env()
    assert not any(k.startswith("AGORA_") or k.startswith("HUB_DRIVEN") for k in env)


def test_mcp_binding_configures_the_server_by_flags_and_env_carries_only_credentials(tmp_path):
    b = MCPBinding(command="agora-mcp", agent_id="worker", url="http://hub:1/",
                   home=tmp_path, about="owns x", download_dir=None)
    args = b.args(tools="driven")
    assert args[:6] == ["--url", "http://hub:1", "--home", str(tmp_path.resolve()), "--as", "worker"]
    assert args[-2:] == ["--tools", "driven"] and "--about" in args
    env = b.environment()
    assert set(env) == {"AGORA_API_KEY", "AGORA_ADMIN_KEY"} and not any(env.values())


def test_every_argv_harness_binds_the_server_by_args(tmp_path):
    from agora.drive import _make_adapter
    b = MCPBinding(command="agora-mcp", agent_id="worker", url="http://hub:1", home=tmp_path)
    claude = _make_adapter("claude", model=None, provider=None, permissions="write",
                           harness_args=None, cwd=tmp_path, mcp=b, reasoning_effort=None)
    cmd = claude.build_command("hello", None)
    cfg = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]["agora"]
    assert cfg["args"] == b.args(tools="driven") and set(cfg["env"]) == {"AGORA_API_KEY", "AGORA_ADMIN_KEY"}
    codex = _make_adapter("codex", model=None, provider=None, permissions="write",
                          harness_args=None, cwd=tmp_path, mcp=b, reasoning_effort=None)
    joined = " ".join(codex.build_command("hello", None))
    assert '"--as", "worker"' in joined and '"--tools", "driven"' in joined
    assert "AGORA_URL" not in joined and "AGORA_AGENT_ID" not in joined


def test_the_turn_marker_binds_the_cli_to_the_seats_hub(tmp_path, monkeypatch):
    from agora import cli
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agora").mkdir()
    import time
    (tmp_path / ".agora" / "driven-worker.json").write_text(json.dumps({
        "agent_id": "worker", "url": "http://seat-hub:8891", "home": str(tmp_path / "h"),
        "pid": os.getpid(), "started_at": time.time()}))
    with pytest.raises(SystemExit, match="agora up: refused"):
        cli.cmd_up(argparse.Namespace())
    assert cli._hub_url(argparse.Namespace(url=None)) == "http://seat-hub:8891"
    assert cli._hub_url(argparse.Namespace(url="http://seat-hub:8891/")) == "http://seat-hub:8891"
    with pytest.raises(SystemExit, match="cannot address http://foreign:8765"):
        cli._hub_url(argparse.Namespace(url="http://foreign:8765"))
    with pytest.raises(SystemExit, match="cannot use --home"):
        cli._apply_home(argparse.Namespace(home=str(tmp_path / "other")))
    cli._apply_home(argparse.Namespace(home=None))
    assert os.environ["AGORA_HOME"] == str((tmp_path / "h").resolve())
    # A stale marker (its driver gone) binds nothing.
    (tmp_path / ".agora" / "driven-worker.json").write_text(json.dumps({
        "agent_id": "worker", "url": "http://seat-hub:8891", "home": "/x", "pid": 2 ** 22 + 7,
        "started_at": time.time()}))
    assert cli._driven_marker() is None, "a dead pid binds nothing"
    (tmp_path / ".agora" / "driven-worker.json").write_text(json.dumps({
        "agent_id": "worker", "url": "http://seat-hub:8891", "home": "/x", "pid": os.getpid(),
        "started_at": time.time() - 3 * 3600}))
    assert cli._driven_marker() is None, "a marker older than the drive pidfile bound binds nothing (pid reuse)"
    monkeypatch.delenv("AGORA_URL", raising=False)
    monkeypatch.setattr(_config, "load_config", lambda: {})
    assert cli._hub_url(argparse.Namespace(url="http://foreign:8765")) == "http://foreign:8765"


def test_the_workspace_seat_record_is_the_default_hub(tmp_path, monkeypatch):
    from agora import cli
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agora").mkdir()
    (tmp_path / ".agora" / "seat.json").write_text(json.dumps({
        "schema": 1, "agent_id": "worker", "url": "http://seat-hub:8891", "about": "",
        "home": str(tmp_path / "h"), "harnesses": ["claude"], "default_drive_harness": "claude"}))
    monkeypatch.setenv("AGORA_URL", "http://legacy-env:1")
    assert cli._hub_url(argparse.Namespace(url=None)) == "http://seat-hub:8891", "file beats env"
    assert cli._hub_url(argparse.Namespace(url="http://elsewhere:2")) == "http://elsewhere:2", "flag beats file"


def test_mcp_server_resolves_flags_then_seat_file_then_env(tmp_path, monkeypatch):
    from agora.mcp import server
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGORA_API_KEY", "k")
    monkeypatch.setenv("AGORA_URL", "http://legacy-env:1")
    monkeypatch.setattr(server._config, "load_config", lambda: {"url": "http://cached-hub:1"})
    monkeypatch.setattr(server, "_ARGV", {})
    assert server._resolve_credentials()[0] == "http://legacy-env:1"
    (tmp_path / ".agora").mkdir()
    (tmp_path / ".agora" / "seat.json").write_text(json.dumps({
        "schema": 1, "agent_id": "worker", "url": "http://seat-hub:8891", "about": "",
        "harnesses": ["claude"], "default_drive_harness": "claude"}))
    assert server._resolve_credentials()[0] == "http://seat-hub:8891"
    monkeypatch.setattr(server, "_ARGV", {"url": "http://flag-hub:7"})
    assert server._resolve_credentials() == ("http://flag-hub:7", "k")


def test_config_home_is_the_flag_for_this_process(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path / "env"))
    monkeypatch.setattr(_config, "_HOME_OVERRIDE", None)
    assert _config.home() == tmp_path / "env"
    _config.set_home(tmp_path / "flag")
    assert _config.home() == (tmp_path / "flag").resolve()
    monkeypatch.setattr(_config, "_HOME_OVERRIDE", None)
