"""One-paste remote onboarding: the AGORA1 artifact and the invite/join flows.

The remote-onboarding failure mode is assembly: a human had to combine hub
url + credential + agent id + command order, and every wrong ordering failed
silently later (a key cached under one url is invisible to a surface reading
another). This module removes the assembly step:

- operator (hub machine):  `agora invite castor --channels general`
  mints a scoped join token (POST /join-tokens) and prints ONE paste line;
- remote (agent machine):  `agora join AGORA1.<blob>`
  decodes it, redeems the token (POST /join), and lands the minted key in
  EVERY place a surface reads: keys.json (CLI/listener/stop-hook),
  config.json url (bare CLI from any folder), and the harness config's env
  block as AGORA_API_KEY (the only channel that survives the harness's env
  scrub). One normalized url string is used for the redeem call, the cache
  key and the config write — the url-qualified cache means a mismatch is a
  silent auth failure, so normalization happens exactly once, here.

The artifact is `AGORA1.<base64url(minified json, padding stripped)>` with
payload {"u": url, "t": token, "a": agent_id?, "c": [channels]?, "e": expiry?}.
It NEVER carries the admin key (which never leaves the hub machine) nor the
final api_key (which does not exist until redemption). `e` is a display hint
only — the hub's stored expiry is the truth, so clock skew cannot bypass it.

cli.py owns the argparse seam (same split as listen.py).
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime
from pathlib import Path

from . import config as _config

ARTIFACT_PREFIX = "AGORA1."
_TTL_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


# -- artifact codec -------------------------------------------------------------


def encode_artifact(url: str, token: str, agent_id: str | None = None,
                    channels: list[str] | None = None,
                    expires_at: float | None = None) -> str:
    """Fuse url + join token (+ optional pin/channels/expiry hint) into one
    paste-safe string: base64url of minified JSON, padding stripped — inert in
    shells, chat apps and QR byte mode (a literal URL would need quoting)."""
    payload: dict = {"u": url.rstrip("/"), "t": token}
    if agent_id:
        payload["a"] = agent_id
    if channels:
        payload["c"] = list(channels)
    if expires_at:
        payload["e"] = int(expires_at)
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return ARTIFACT_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_artifact(blob: str) -> dict:
    """Decode + validate an artifact CLIENT-SIDE (no network call is ever made
    for a mangled paste). Strips ALL whitespace first so a chat-line-wrapped
    paste still decodes; a truncated one fails with a clear ask-for-a-fresh-one
    error. Returns {url, token, agent_id, channels, expires_at}."""
    import re

    compact = "".join(blob.split())
    if compact.startswith("agora-join_"):
        raise ValueError(
            "that is a raw join token, not an artifact — redeem it with: "
            "agora join --url <hub-url> --token <token>")
    if not compact.startswith(ARTIFACT_PREFIX):
        version = re.match(r"AGORA(\d+)\.", compact)
        if version:  # a well-formed artifact from a NEWER (or older) format
            raise ValueError(
                f"unsupported artifact version AGORA{version.group(1)} (this "
                f"CLI understands {ARTIFACT_PREFIX.rstrip('.')}) — upgrade "
                "agoria, or ask for a fresh `agora invite` line")
        raise ValueError("not an agora join artifact (expected 'AGORA1.…' — "
                         "truncated paste?)")
    body = compact.removeprefix(ARTIFACT_PREFIX)
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw)
    except ValueError as exc:  # binascii.Error and JSON errors both land here
        raise ValueError("artifact is corrupt (truncated paste?) — ask the "
                         "operator for a fresh `agora invite` line") from exc
    if not isinstance(payload, dict) or not payload.get("u") or not payload.get("t"):
        raise ValueError("artifact is corrupt (truncated paste?) — ask the "
                         "operator for a fresh `agora invite` line")
    return {
        "url": str(payload["u"]).rstrip("/"),
        "token": str(payload["t"]),
        "agent_id": payload.get("a"),
        "channels": [str(c) for c in payload.get("c") or []],
        "expires_at": payload.get("e"),
    }


def parse_ttl(text: str) -> float:
    """'90s' / '30m' / '24h' / '7d' -> seconds. The unit is REQUIRED: a bare
    '24' silently meaning hours-or-seconds is exactly the ambiguity class this
    feature exists to remove."""
    text = str(text).strip().lower()
    unit = _TTL_UNITS.get(text[-1:] or "")
    try:
        value = float(text[:-1])
    except ValueError:
        unit = None
        value = 0.0
    if unit is None or value <= 0:
        raise ValueError(f"invalid ttl '{text}': use e.g. 90s, 30m, 24h or 7d")
    return value * unit


def _fmt_time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


# -- operator side: mint / list / revoke ------------------------------------------


def _admin_headers(admin_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_key}"}


def run_invite(url: str, admin_key: str, agent_id: str | None, about: str,
               channels: list[str], ttl_seconds: float, uses: int) -> None:
    """Mint a join token and print the kubeadm-style banner whose ONE paste
    line is the whole remote onboarding. The admin key is USED here and never
    leaves this machine — the printed artifact carries only the scoped token."""
    import httpx

    r = httpx.post(f"{url}/join-tokens", headers=_admin_headers(admin_key),
                   json={"agent_id": agent_id, "about": about,
                         "channels": channels, "ttl_seconds": ttl_seconds,
                         "max_uses": uses}, timeout=10.0)
    if r.status_code == 404:
        raise SystemExit("this hub predates join tokens (no /join-tokens "
                         "endpoint) — upgrade it, or use the operator flow: "
                         "`agora register` + `agora seed-key`.")
    if r.status_code != 200:
        detail = _detail(r)
        raise SystemExit(f"minting failed: {r.status_code} {detail}")
    minted = r.json()
    artifact = encode_artifact(url, minted["token"], agent_id=agent_id,
                               channels=channels,
                               expires_at=minted.get("expires_at"))

    who = f"'{agent_id}'" if agent_id else "any agent id (--any-id)"
    usage = "single-use" if uses == 1 else f"reusable x{uses}"
    joins = f" · joins: {', '.join(channels)}" if channels else ""
    line = "─" * 66
    print(line)
    print(f"join token for {who} on {url}")
    print(f"  {usage} · expires {_fmt_time(minted.get('expires_at'))}{joins}")
    print(f"  token id: {minted['token_id']}   "
          f"(revoke: agora invite --revoke {minted['token_id']})")
    print()
    print("paste ONE line on the remote machine, in the agent's workspace folder:")
    print()
    print(f"  agora join {artifact}")
    print()
    print("# explicit form of the same thing:")
    print(f"#   agora join --url {url} --token {minted['token']}"
          + (f" --as {agent_id}" if agent_id else " --as <id>"))
    print(line)
    if _config.is_loopback_url(url):
        print(f"WARNING: {url} is loopback — that join line only works ON THIS "
              "machine. For a remote agent, re-mint with the address it can "
              "reach: agora invite ... --url http://<lan-ip>:8765")


def run_invite_list(url: str, admin_key: str) -> None:
    import httpx

    r = httpx.get(f"{url}/join-tokens", headers=_admin_headers(admin_key),
                  timeout=10.0)
    if r.status_code != 200:
        raise SystemExit(f"listing failed: {r.status_code} {_detail(r)}")
    rows = r.json()
    if not rows:
        print("no live join tokens (expired ones are purged automatically)")
        return
    print(f"{'token id':<10} {'agent':<18} {'uses':>7}  {'expires':<17} "
          f"{'state':<8} used by")
    now = time.time()
    for t in rows:
        state = ("revoked" if t.get("revoked_at")
                 else "spent" if t["uses"] >= t["max_uses"]
                 else "expired" if t["expires_at"] < now else "live")
        used = ", ".join(t.get("used_by") or []) or "-"
        print(f"{t['token_id']:<10} {t.get('agent_id') or '(any)':<18} "
              f"{t['uses']}/{t['max_uses']:>3}   {_fmt_time(t['expires_at']):<17} "
              f"{state:<8} {used}")


def run_invite_revoke(url: str, admin_key: str, token_id: str) -> None:
    import httpx

    r = httpx.delete(f"{url}/join-tokens/{token_id}",
                     headers=_admin_headers(admin_key), timeout=10.0)
    if r.status_code != 200:
        raise SystemExit(f"revoke failed: {r.status_code} {_detail(r)}")
    print(f"join token {token_id} revoked")


def _detail(response) -> str:
    try:
        return str(response.json().get("detail", response.text))
    except ValueError:
        return response.text


# -- remote side: the one-paste join ----------------------------------------------


def run_join(url: str, token: str, agent_id: str | None, about: str,
             harness: str, workspace: str, with_hook: bool, listen: bool,
             mcp_command: str, pinned_id: str | None = None,
             expires_hint: float | None = None) -> int:
    """The whole remote onboarding, fail-loud at each step:
    redeem -> cache key -> pin url -> verify -> wire workspace [-> listen].

    Idempotent: a burned artifact re-run on a machine that already holds the
    key skips redemption and only re-wires (a repair, not an error). The url
    arrives normalized (rstripped once at decode/dispatch) and that ONE string
    is used for the POST, the keys.json entry and config.json — the three
    lookups every surface later performs."""
    if pinned_id and agent_id and agent_id != pinned_id:
        raise SystemExit(f"this artifact is locked to '{pinned_id}' — drop "
                         f"--as, or ask the operator for an --any-id invite.")
    # The id may be unknown upfront (raw --token form, pinned server-side):
    # then the redeem response names it. When it IS known, a cached key
    # short-circuits redemption (re-running a burned artifact = repair).
    effective_id = agent_id or pinned_id
    if expires_hint and expires_hint < time.time():
        print(f"note: the artifact says it expired {_fmt_time(expires_hint)}; "
              "trying anyway (the hub's clock is the truth).")

    cached = _config.get_cached_key(url, effective_id) if effective_id else None
    if cached:
        print(f"'{effective_id}' already holds a key for {url} — skipping "
              "redemption, re-wiring only (re-runs are repairs).")
        api_key = cached
        joined: list[str] = []
    else:
        api_key, joined, effective_id = _redeem(url, token, agent_id, about,
                                                pinned_id)
        _config.cache_key(url, effective_id, api_key)
    keys_path = _config.home() / "keys.json"
    print(f"  cached key  -> {keys_path} (0600)")

    _config.save_url(url)
    print(f"  pinned hub  -> {_config.home() / 'config.json'} (url only — "
          "never an admin key)")

    identity = _verify_whoami(url, api_key, effective_id)
    suffix = f" (channels: {', '.join(joined)})" if joined else ""
    print(f"  verified    -> GET /whoami as '{identity['id']}' OK{suffix}")

    if harness != "none":
        _wire_workspace(harness, Path(workspace).expanduser().resolve(),
                        effective_id, url, identity.get("about") or "",
                        mcp_command, with_hook, api_key)
    else:
        print("  workspace   -> not wired (--harness none); CLI + listener "
              "auth via the cached key")

    print(f"joined {url} as '{effective_id}'. Do not run `agora up` on this "
          "machine — it is a client of that hub.")
    if listen:
        # Foreground by design: a detached process outliving the command would
        # violate the fleet's no-machine-persistence etiquette.
        from .listen import run_listen
        return run_listen(agent_id=effective_id, url=url, source="ws")
    return 0


def _redeem(url: str, token: str, agent_id: str | None, about: str,
            pinned_id: str | None) -> tuple[str, list[str], str]:
    """POST /join with remedies attached to every refusal class. Returns
    (api_key, channels_joined, agent_id) — the id comes from the hub's
    response, which also covers tokens whose pin the client never saw
    (raw --token form)."""
    import httpx

    try:
        r = httpx.post(f"{url}/join",
                       json={"token": token, "agent_id": agent_id,
                             "about": about}, timeout=10.0)
    except httpx.HTTPError as exc:
        raise SystemExit(
            f"cannot reach the hub at {url} ({exc}). The url was chosen at "
            "mint time on the OPERATOR's machine — if it is not reachable "
            "from here, ask for a re-mint with the address this machine can "
            "reach (agora invite ... --url http://<reachable-ip>:8765).") from exc
    if r.status_code == 200:
        payload = r.json()
        return (payload["api_key"], payload.get("channels_joined") or [],
                payload["agent"]["id"])
    detail = _detail(r)
    if r.status_code == 404:
        raise SystemExit("this hub predates join tokens (no /join endpoint) — "
                         "upgrade it, or use the operator flow: "
                         "`agora register` + `agora seed-key`.")
    if r.status_code == 409:
        retry = ("ask the operator whether that agent is you (then import its "
                 "key with `agora seed-key`), or for a fresh invite"
                 if pinned_id else
                 "retry with a free id: agora join <artifact> --as <other-id>")
        raise SystemExit(f"{detail}. The token was NOT consumed — {retry}.")
    if r.status_code == 403:
        raise SystemExit(f"the hub refused the join token: {detail}. Ask the "
                         "operator for a fresh one (`agora invite <id>`).")
    if r.status_code == 400 and "agent id" in detail:
        raise SystemExit(f"{detail} — choose one with "
                         "`agora join <artifact> --as <id>`.")
    raise SystemExit(f"join failed: {r.status_code} {detail}")


def _verify_whoami(url: str, api_key: str, agent_id: str) -> dict:
    import httpx

    r = httpx.get(f"{url}/whoami",
                  headers={"Authorization": f"Bearer {api_key}"}, timeout=10.0)
    if r.status_code != 200:
        raise SystemExit(f"verification failed: the hub rejected the key for "
                         f"'{agent_id}' ({r.status_code} {_detail(r)}). "
                         "Nothing else was changed; fix and re-run.")
    return r.json()


def _wire_workspace(harness: str, workspace: Path, agent_id: str, url: str,
                    about: str, mcp_command: str, with_hook: bool,
                    api_key: str) -> None:
    """Call the existing harness writers WITH the api key, so the env block —
    the only credential channel that survives the harness env scrub — carries
    AGORA_API_KEY, and the secret-bearing file is 0600. For the CLI harnesses
    the project file alone is gated behind trust/approval prompts, so the
    harness's own `mcp add` CLI is ALSO called (best-effort) — that is what
    makes the server actually visible at the next `claude`/`codex` launch."""
    from . import setup_harness as _sh

    if not workspace.is_dir():
        raise SystemExit(f"workspace not found: {workspace}")
    writer = {"cursor": _sh.setup_cursor, "claude": _sh.setup_claude,
              "codex": _sh.setup_codex}[harness]
    written = writer(workspace, agent_id, url, about, mcp_command,
                     with_hook, api_key=api_key)
    for path in written:
        print(f"  wired       -> {path}")
    secret_files = [p for p in written
                    if p.name in ("mcp.json", ".mcp.json", "config.toml")]
    if secret_files:
        names = ", ".join(str(p.relative_to(workspace)) for p in secret_files)
        print(f"  key embedded as AGORA_API_KEY in {names} (0600) — keep that "
              "file out of version control (gitignore it).")
    registered = False
    if harness == "claude":
        registered, detail = _sh.register_claude_local(
            workspace, mcp_command, url, agent_id, about,
            api_key=api_key, home=_sh.custom_home_env())
        print(f"  harness     -> {detail}")
    elif harness == "codex":
        registered, detail = _sh.register_codex_global(
            mcp_command, url, agent_id, about,
            api_key=api_key, home=_sh.custom_home_env())
        print(f"  harness     -> {detail}")
    opener = {"cursor": "open this folder in Cursor",
              "claude": ("run `claude` here"
                         if registered else
                         "run `claude` here and approve the 'agora' MCP "
                         "server (/mcp)"),
              "codex": ("run `codex` here"
                        if registered else
                        "run `codex` here and trust the project")}[harness]
    print(f"next: {opener} — the agent authenticates immediately.")
