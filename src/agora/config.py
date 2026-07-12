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
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


def _write_secret(path: Path, text: str) -> None:
    """Secrets (bearer/admin keys) must not be world-readable (audit M2).
    Written with the default umask first, then clamped — also repairs files
    created by earlier versions."""
    path.write_text(text)
    path.chmod(0o600)


def _config_path() -> Path:
    return home() / "config.json"


def _keys_path() -> Path:
    return home() / "keys.json"


def load_config() -> dict[str, Any]:
    p = _config_path()
    return json.loads(p.read_text()) if p.exists() else {}


def save_config(url: str, admin_key: str, db_path: str) -> None:
    _write_secret(_config_path(), json.dumps(
        {"url": url, "admin_key": admin_key, "db_path": db_path}, indent=2))


def save_url(url: str) -> None:
    """Pin the default hub url WITHOUT touching credentials: merge {"url": url}
    into config.json, preserving whatever else is there (on the hub machine the
    admin key written by `agora up` survives) but never ADDING an admin key or
    db path. This is the writer remote onboarding uses — a remote machine's
    config must hold a url and nothing secret beyond it (0600 regardless)."""
    cfg = load_config()
    cfg["url"] = url
    _write_secret(_config_path(), json.dumps(cfg, indent=2))


def _same_hub(a: str | None, b: str | None) -> bool:
    """Do two hub urls name the same hub? Compared trailing-slash-insensitively.
    Used to bind a stored admin key to the hub it belongs to (a false negative
    is safe — it just declines the key and asks for an explicit one)."""
    return bool(a) and bool(b) and a.rstrip("/") == b.rstrip("/")


def is_loopback_url(url: str) -> bool:
    """True when the hub url points at this machine (same check `agora listen`
    uses to pick file mode): loopback hostnames and the 127/8 block. Error
    messages branch on this — advice that is right locally ("run agora up")
    is actively wrong for a hub on another machine."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    return host in ("localhost", "::1") or host.startswith("127.")


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
    _write_secret(_keys_path(), json.dumps(keys, indent=2))


def seed_keys(url: str, mapping: dict[str, str]) -> None:
    """Import pre-existing agent keys (e.g. from a migration) into the cache
    so those agents work without re-registering."""
    for agent_id, api_key in mapping.items():
        cache_key(url, agent_id, api_key)


def resolve_key(url: str, agent_id: str, *, admin_key: str | None = None,
                about: str = "") -> str:
    """Return the agent's key: cached if known, else self-register with the
    admin key (explicit, then $AGORA_ADMIN_KEY, then config) and cache it.
    The env fallback matches the MCP server, so a remote machine — which has
    no ~/.agora/config.json — onboards with two exported variables
    (AGORA_URL + AGORA_ADMIN_KEY) instead of failing with 'run agora up'.
    Raises if neither a cached key nor an admin key is available."""
    import httpx  # local import: config stays dependency-light

    cached = get_cached_key(url, agent_id)
    if cached:
        return cached
    # The config admin key is the credential OF THE HUB config.json names — it
    # is NOT a universal key. Using it against a different url is exactly how a
    # hub-2 agent that resolved the default url silently self-registered on the
    # production hub (the wrong-hub incident). Only accept it when the config's
    # url matches the target; otherwise fall through to the loud remedy.
    cfg = load_config()
    config_admin = cfg.get("admin_key") if _same_hub(cfg.get("url"), url) else None
    admin_key = (admin_key or os.environ.get("AGORA_ADMIN_KEY") or config_admin)
    if not admin_key:
        # Surface-aware remedy: `agora up` is only correct where the hub runs.
        # Telling a REMOTE user to run it would start a wrong local hub.
        if is_loopback_url(url):
            raise SystemExit(
                f"no cached key for '{agent_id}' and no admin key to "
                "self-register. Run `agora up` first (writes "
                "~/.agora/config.json); agents then self-register.")
        raise SystemExit(
            f"no cached key for '{agent_id}' at {url} (a hub on another "
            "machine). Ask the hub operator for a join artifact and run "
            f"`agora join AGORA1....`, or have them run `agora register "
            f"{agent_id}` and import the key here with `agora seed-key "
            f"{agent_id} --url {url} --key <agora_...>`. Exporting "
            "AGORA_ADMIN_KEY also works but grants more than needed.")
    r = httpx.post(f"{url.rstrip('/')}/agents",
                   headers={"Authorization": f"Bearer {admin_key}"},
                   json={"id": agent_id, "about": about}, timeout=10.0)
    if r.status_code == 200:
        key = r.json()["api_key"]
        cache_key(url, agent_id, key)
        return key
    if r.status_code == 409:
        raise SystemExit(
            f"agent '{agent_id}' exists but no cached key on this machine; "
            "keys are unrecoverable (hashed at rest). Import the key saved at "
            f"registration with `agora seed-key {agent_id} --url {url} "
            "--key <agora_...>`, or pick a new id.")
    raise SystemExit(f"self-registration failed: {r.status_code} {r.text}")
