"""The executable MCP runtime probe must reject false-positive installs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agora import __version__
from agora.mcp import server
from agora.mcp.runtime import (
    MCP_SELF_CHECK_COMPONENT,
    probe_mcp_runtime,
    resolve_mcp_command,
)


def test_server_self_check_proves_the_supported_fastmcp_api(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agora-mcp", "--self-check"])
    server.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["component"] == MCP_SELF_CHECK_COMPONENT
    assert payload["status"] == "ok"
    assert payload["api"] == "mcp.server.fastmcp.FastMCP"
    assert payload["mcp_sdk"].startswith("1.")


def test_runtime_probe_rejects_import_only_false_positive(tmp_path):
    fake = tmp_path / "agora-mcp"
    fake.write_text(
        "#!/bin/sh\necho 'mcp exists but mcp.server.fastmcp does not' >&2\nexit 1\n"
    )
    fake.chmod(0o755)
    probe = probe_mcp_runtime(str(fake))
    assert probe.ok is False
    assert probe.reason == "runtime-incompatible"
    assert "fastmcp" in (probe.detail or "")


def test_runtime_probe_requires_the_agora_marker(tmp_path):
    fake = tmp_path / "agora-mcp"
    fake.write_text('#!/bin/sh\necho \'{"status":"ok"}\'\n')
    fake.chmod(0o755)
    probe = probe_mcp_runtime(str(fake))
    assert probe.ok is False
    assert probe.reason == "invalid-self-check"


def test_runtime_probe_rejects_another_agora_version(tmp_path):
    fake = tmp_path / "agora-mcp"
    payload = json.dumps(
        {
            "component": MCP_SELF_CHECK_COMPONENT,
            "status": "ok",
            "agora": "0.0.0",
            "mcp_sdk": "1.29.0",
        }
    )
    fake.write_text(f"#!/bin/sh\necho '{payload}'\n")
    fake.chmod(0o755)
    probe = probe_mcp_runtime(str(fake))
    assert probe.ok is False
    assert probe.reason == "runtime-version-mismatch"
    assert __version__ in (probe.detail or "")


def test_runtime_probe_scrubs_agora_environment(tmp_path, monkeypatch):
    fake = tmp_path / "agora-mcp"
    payload = json.dumps(
        {
            "component": MCP_SELF_CHECK_COMPONENT,
            "status": "ok",
            "agora": __version__,
            "mcp_sdk": "1.29.0",
        }
    )
    fake.write_text(
        f"#!/bin/sh\nif env | grep -q '^AGORA_'; then exit 91; fi\necho '{payload}'\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("AGORA_API_KEY", "agora_secret_probe_sentinel")
    monkeypatch.setenv("AGORA_ADMIN_KEY", "admin-secret-probe-sentinel")
    monkeypatch.setenv("AGORA_URL", "http://sensitive.invalid")
    assert probe_mcp_runtime(str(fake)).ok is True


def test_server_rejects_unknown_arguments(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["agora-mcp", "--unknown"])
    with pytest.raises(SystemExit, match="usage: agora-mcp"):
        server.main()


def test_server_rejects_an_incompatible_installed_major(monkeypatch):
    monkeypatch.setattr(server.importlib.metadata, "version", lambda _name: "2.0.0")
    with pytest.raises(SystemExit, match=r">=1\.28\.1,<2.*2\.0\.0"):
        server._load_fastmcp()


def test_mcp_resolution_prefers_the_running_agora_sibling_over_path(
    tmp_path, monkeypatch
):
    install_bin = tmp_path / "installed" / "bin"
    stale_bin = tmp_path / "stale" / "bin"
    install_bin.mkdir(parents=True)
    stale_bin.mkdir(parents=True)
    launcher = install_bin / "agora"
    sibling = install_bin / "agora-mcp"
    stale = stale_bin / "agora-mcp"
    for path in (launcher, sibling, stale):
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(launcher)])
    monkeypatch.setenv("PATH", f"{stale_bin}{os.pathsep}{os.environ['PATH']}")
    assert Path(resolve_mcp_command()) == sibling.resolve()


def test_mcp_resolution_prefers_path_over_unrelated_interpreter_sibling(
    tmp_path, monkeypatch
):
    runner_dir = tmp_path / "runner"
    path_bin = tmp_path / "path-bin"
    python_bin = tmp_path / "python-bin"
    for directory in (runner_dir, path_bin, python_bin):
        directory.mkdir()
    wanted = path_bin / "agora-mcp"
    unrelated = python_bin / "agora-mcp"
    fake_python = python_bin / "python"
    marker = json.dumps(
        {
            "component": MCP_SELF_CHECK_COMPONENT,
            "status": "ok",
            "agora": __version__,
            "mcp_sdk": "1.28.1",
        }
    )
    for path in (wanted, unrelated):
        path.write_text(f"#!/bin/sh\necho '{marker}'\n")
        path.chmod(0o755)
    fake_python.write_text("#!/bin/sh\nexit 0\n")
    fake_python.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(runner_dir / "plain-script")])
    monkeypatch.setattr(sys, "executable", str(fake_python))
    monkeypatch.setenv("PATH", f"{path_bin}{os.pathsep}{os.environ['PATH']}")
    assert Path(resolve_mcp_command()) == wanted.resolve()


def test_mcp_resolution_skips_stale_path_candidate(tmp_path, monkeypatch):
    runner_dir = tmp_path / "runner"
    stale_bin = tmp_path / "stale-bin"
    healthy_bin = tmp_path / "healthy-bin"
    for directory in (runner_dir, stale_bin, healthy_bin):
        directory.mkdir()
    stale = stale_bin / "agora-mcp"
    healthy = healthy_bin / "agora-mcp"
    stale.write_text("#!/bin/sh\necho incompatible >&2\nexit 1\n")
    marker = json.dumps(
        {
            "component": MCP_SELF_CHECK_COMPONENT,
            "status": "ok",
            "agora": __version__,
            "mcp_sdk": "1.29.0",
        }
    )
    healthy.write_text(f"#!/bin/sh\necho '{marker}'\n")
    for path in (stale, healthy):
        path.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(runner_dir / "plain-script")])
    monkeypatch.setenv(
        "PATH",
        f"{stale_bin}{os.pathsep}{healthy_bin}{os.pathsep}{os.environ['PATH']}",
    )
    assert Path(resolve_mcp_command()) == healthy.resolve()
