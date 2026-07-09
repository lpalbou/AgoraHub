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


def test_resolve_key_without_any_admin_key_explains_both_paths(isolated_home):
    with pytest.raises(SystemExit) as exc:
        _config.resolve_key("http://hub-machine:8765", "castor")
    message = str(exc.value)
    assert "AGORA_ADMIN_KEY" in message   # the remote-machine remedy
    assert "agora up" in message          # the hub-machine remedy


def test_cached_key_wins_over_registration(isolated_home):
    _config.cache_key("http://hub-machine:8765", "castor", "agora_cached")
    assert _config.resolve_key("http://hub-machine:8765", "castor") == "agora_cached"
    # Secrets written by the cache are not world-readable.
    keys_file = isolated_home / "keys.json"
    assert keys_file.stat().st_mode & 0o077 == 0
    assert json.loads(keys_file.read_text())
