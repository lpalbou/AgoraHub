"""Local config + key cache so agents onboard with zero manual key handling.

The whole point: a Cursor tab (or any harness) should need only an agent id.
The hub writes its url + admin key to `~/.agora/config.json` when it starts;
the MCP server reads that, self-registers the agent on first use, and caches
the agent's key in `~/.agora/keys.json`. No curl, no copy-pasting secrets.

Override the location with $AGORA_HOME. Everything here is local-user state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def home() -> Path:
    p = Path(os.environ.get("AGORA_HOME", str(Path.home() / ".agora")))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _config_path() -> Path:
    return home() / "config.json"


def _keys_path() -> Path:
    return home() / "keys.json"


def load_config() -> dict[str, Any]:
    p = _config_path()
    return json.loads(p.read_text()) if p.exists() else {}


def save_config(url: str, admin_key: str, db_path: str) -> None:
    _config_path().write_text(json.dumps(
        {"url": url, "admin_key": admin_key, "db_path": db_path}, indent=2))


def _load_keys() -> dict[str, str]:
    p = _keys_path()
    return json.loads(p.read_text()) if p.exists() else {}


def _key_id(url: str, agent_id: str) -> str:
    return f"{url}::{agent_id}"


def get_cached_key(url: str, agent_id: str) -> str | None:
    return _load_keys().get(_key_id(url, agent_id))


def cache_key(url: str, agent_id: str, api_key: str) -> None:
    keys = _load_keys()
    keys[_key_id(url, agent_id)] = api_key
    _keys_path().write_text(json.dumps(keys, indent=2))


def seed_keys(url: str, mapping: dict[str, str]) -> None:
    """Import pre-existing agent keys (e.g. from a migration) into the cache
    so those agents work without re-registering."""
    for agent_id, api_key in mapping.items():
        cache_key(url, agent_id, api_key)


def resolve_key(url: str, agent_id: str, *, admin_key: str | None = None,
                about: str = "") -> str:
    """Return the agent's key: cached if known, else self-register with the
    admin key (from config if not passed) and cache it. Raises if neither a
    cached key nor an admin key is available."""
    import httpx  # local import: config stays dependency-light

    cached = get_cached_key(url, agent_id)
    if cached:
        return cached
    admin_key = admin_key or load_config().get("admin_key")
    if not admin_key:
        raise SystemExit(
            f"no cached key for '{agent_id}' and no admin key to self-register. "
            "Run `agora up` first (writes ~/.agora/config.json).")
    r = httpx.post(f"{url.rstrip('/')}/agents",
                   headers={"Authorization": f"Bearer {admin_key}"},
                   json={"id": agent_id, "about": about}, timeout=10.0)
    if r.status_code == 200:
        key = r.json()["api_key"]
        cache_key(url, agent_id, key)
        return key
    if r.status_code == 409:
        raise SystemExit(f"agent '{agent_id}' exists but no cached key on this "
                         "machine; recover its key into ~/.agora/keys.json.")
    raise SystemExit(f"self-registration failed: {r.status_code} {r.text}")
