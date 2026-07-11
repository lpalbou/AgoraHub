"""Remote-onboarding config resolution.

A remote machine has no ~/.agora/config.json (that file is written by
`agora up` on the hub machine), so the CLI must honor the same environment
variables the MCP server does: AGORA_URL for the hub address and
AGORA_ADMIN_KEY for first-use self-registration. Without this parity,
`agora <cmd> --as <id>` on a remote machine dead-ends with "run agora up".
"""

import argparse
import json

import pytest

from agora import config as _config
from agora.cli import _hub_url


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Point AGORA_HOME at an empty dir so the real ~/.agora never leaks in."""
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    monkeypatch.delenv("AGORA_URL", raising=False)
    monkeypatch.delenv("AGORA_ADMIN_KEY", raising=False)
    return tmp_path


def _args(url=None):
    return argparse.Namespace(url=url)


def test_hub_url_prefers_flag_then_env_then_config(isolated_home, monkeypatch):
    # No flag, no env, no config -> local default.
    assert _hub_url(_args()) == "http://127.0.0.1:8765"

    # Env var (the remote-machine path) overrides the default...
    monkeypatch.setenv("AGORA_URL", "http://hub-machine:8765/")
    assert _hub_url(_args()) == "http://hub-machine:8765"

    # ...and the config file, but an explicit flag beats everything.
    _config.save_config(url="http://from-config:8765",
                        admin_key="k", db_path="db")
    assert _hub_url(_args()) == "http://hub-machine:8765"
    assert _hub_url(_args(url="http://flag:1")) == "http://flag:1"


def test_hub_url_falls_back_to_config_without_env(isolated_home):
    _config.save_config(url="http://from-config:8765",
                        admin_key="k", db_path="db")
    assert _hub_url(_args()) == "http://from-config:8765"


def test_resolve_key_uses_admin_key_from_env(isolated_home, monkeypatch):
    """Self-registration must work with AGORA_ADMIN_KEY exported and no
    config file — the exact state of a freshly provisioned remote machine."""
    calls = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"api_key": "agora_remote_key"}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["url"] = url
        calls["auth"] = headers["Authorization"]
        return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("AGORA_ADMIN_KEY", "env-admin-secret")

    key = _config.resolve_key("http://hub-machine:8765", "castor")
    assert key == "agora_remote_key"
    assert calls["url"] == "http://hub-machine:8765/agents"
    assert calls["auth"] == "Bearer env-admin-secret"
    # The key is cached for subsequent calls (no second registration).
    assert _config.get_cached_key("http://hub-machine:8765", "castor") == key


def test_resolve_key_no_credential_error_is_surface_aware(isolated_home):
    """The remedy must match where the hub runs: `agora up` is only correct
    on the hub machine; a remote must be pointed at the join flow instead
    (running `agora up` there would start a wrong local hub)."""
    with pytest.raises(SystemExit) as exc:
        _config.resolve_key("http://hub-machine:8765", "castor")
    remote_msg = str(exc.value)
    assert "agora join" in remote_msg
    assert "agora seed-key" in remote_msg
    assert "agora register" in remote_msg
    assert "AGORA_ADMIN_KEY" in remote_msg      # still possible, discouraged
    assert "run `agora up`" not in remote_msg.lower()

    with pytest.raises(SystemExit) as exc:
        _config.resolve_key("http://127.0.0.1:8765", "castor")
    local_msg = str(exc.value)
    assert "agora up" in local_msg              # the hub-machine remedy


def test_cached_key_wins_over_registration(isolated_home):
    _config.cache_key("http://hub-machine:8765", "castor", "agora_cached")
    assert _config.resolve_key("http://hub-machine:8765", "castor") == "agora_cached"
    # Secrets written by the cache are not world-readable.
    keys_file = isolated_home / "keys.json"
    assert keys_file.stat().st_mode & 0o077 == 0
    assert json.loads(keys_file.read_text())


def test_save_url_never_writes_credentials(isolated_home):
    """The remote-machine config writer: url only on a fresh machine, other
    fields preserved on the hub machine — but it never ADDS an admin key."""
    _config.save_url("http://192.168.1.9:8765")
    cfg_path = isolated_home / "config.json"
    assert json.loads(cfg_path.read_text()) == {"url": "http://192.168.1.9:8765"}
    assert cfg_path.stat().st_mode & 0o077 == 0

    # On the hub machine (config written by `agora up`) the admin key SURVIVES
    # a url update — save_url merges, it does not truncate.
    _config.save_config(url="http://old:1", admin_key="adm", db_path="db")
    _config.save_url("http://new:2")
    assert json.loads(cfg_path.read_text()) == {
        "url": "http://new:2", "admin_key": "adm", "db_path": "db"}


def test_is_loopback_url():
    for local in ("http://127.0.0.1:8765", "http://127.1.2.3:1",
                  "http://localhost:8895", "http://[::1]:8765"):
        assert _config.is_loopback_url(local), local
    for remote in ("http://192.168.1.146:8765", "http://hub-machine:8765",
                   "https://agora.example.com"):
        assert not _config.is_loopback_url(remote), remote


def test_setup_cursor_cmd_honors_agora_url_and_keeps_keyless_path(
        isolated_home, tmp_path, monkeypatch, capsys):
    """The URL trap, killed: with $AGORA_URL exported and NO flag, setup must
    write that url (not 127.0.0.1) into mcp.json. With no credential anywhere
    the output stays the keyless config — byte-identical to the old writer —
    and nothing is chmod-clamped."""
    from agora.cli import cmd_setup_cursor

    monkeypatch.setenv("AGORA_URL", "http://192.168.1.146:8765")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    args = argparse.Namespace(agent="remote-mbp", workspace=str(workspace),
                              about="", url=None, key=None, with_hook=False)
    cmd_setup_cursor(args)
    out = capsys.readouterr().out
    assert "self-registers on first tool use" in out  # keyless path kept

    mcp_path = workspace / ".cursor" / "mcp.json"
    expected_server = {
        "command": json.loads(mcp_path.read_text())["mcpServers"]["agora"]["command"],
        "env": {"AGORA_URL": "http://192.168.1.146:8765",
                "AGORA_AGENT_ID": "remote-mbp", "AGORA_ABOUT": ""},
    }
    assert json.loads(mcp_path.read_text())["mcpServers"]["agora"] == expected_server
    assert mcp_path.stat().st_mode & 0o077 != 0       # no secret, no clamp
    # keyless = no key cached, nothing registered, no config.json written
    assert not (isolated_home / "keys.json").exists()
    assert not (isolated_home / "config.json").exists()
