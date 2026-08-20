"""`agora` — the one-command front door.

    agora up                         # start the hub with sane, persistent defaults
    agora setup <agent-id>           # wire the CURRENT folder; auto-select or prompt for the harness
    agora status                     # is the hub up? who am I configured as?

`agora up` picks a stable db (~/.agora/agora.db) and a stable admin key
(generated once, saved to ~/.agora/config.json) so nothing needs to be
remembered or passed around. `setup` writes the workspace-local wiring; bare
usage reuses the existing harness footprint or prompts once, and explicit
`--harness` overrides that when you want one specific front-end (or `all`).
"""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass, field
import functools
import json
import os
import re
import secrets
import shlex
import sys
import time
from pathlib import Path

from . import config as _config
from . import is_agora_protocol
from .hook import HOOK_EVENTS as _HOOK_EVENTS
from .setup_harness import SUPPORTED_HARNESSES
from . import db_locate as _db_locate
from .agent_id import validate_agent_id
from .governance import CHARTER_PATH as _CHARTER_PATH
from .models import MAX_FS_BINARY_BYTES, NOTICE_KINDS


def _resolve_mcp_command() -> str:
    """Resolve the exact MCP entry point setup and drivers will execute."""
    from .mcp.runtime import resolve_mcp_command

    return resolve_mcp_command()


def _smoke_check_mcp(
    mcp_command: str,
    hint: str = "then re-run this setup.",
    *,
    required: bool = False,
):
    """Run the entry point's real, side-effect-free MCP API self-check.

    Setup is fail-closed because writing wiring for a server that cannot boot
    creates a deceptively configured but tool-less agent. Hub launch remains
    advisory: a hub may legitimately serve remote MCP clients even when its
    own machine has no local agent harness.
    """
    from .mcp.runtime import format_probe_failure, probe_mcp_runtime

    probe = probe_mcp_runtime(mcp_command)
    if probe.ok:
        return probe
    action = (
        "reinstall the package and its supported MCP runtime with "
        "`uv tool install --force --reinstall agorahub` "
        "(development checkout: `uv tool install --force --reinstall .`), "
        + hint
    )
    diagnostic = format_probe_failure(probe, action=action)
    if required:
        raise SystemExit("agora setup: required MCP runtime failed preflight\n" + diagnostic)
    print("AGORA_MCP_CHECK status=error\n" + diagnostic, file=sys.stderr)
    return probe


def _apply_home(args: argparse.Namespace) -> None:
    """`--home PATH` = use this agora home for THIS invocation. It maps onto
    AGORA_HOME (what config.home() and every spawned process — MCP server,
    listener, hooks — already honor), so one flag replaces the unfriendly
    env-var prefix `AGORA_HOME=~/.agora-hub2 agora chat ...`. The flag wins
    over an inherited env var; without it the env var works exactly as
    before. Applied in main() BEFORE dispatch so every command and every
    child process sees the same home."""
    home = getattr(args, "home", None)
    if home:
        # abspath as well as expanduser: a relative --home would otherwise
        # persist CWD-dependent meaning into every child process (db_locate
        # review F6) — the same trap as a relative remembered db_path.
        os.environ["AGORA_HOME"] = os.path.abspath(
            str(Path(home).expanduser()))

DEFAULT_PORT = 8765


def _default_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


# How many times `agora up --force` will re-resolve a port that changes
# hands mid-takeover before it gives up and names the pids it killed.
_FORCE_MAX_ROUNDS = 4


def _port_holder(host: str, port: int) -> tuple[int, str] | None:
    """(pid, command-line) of whatever LISTENs on host:port, or None if the
    port is free. Best-effort via lsof (present on macOS/Linux); a missing
    lsof yields (0, '') so the caller still refuses loudly, just without the
    pid. Used to turn an opaque bind failure into a named diagnosis — the
    16-hour-deaf-room incident (a static file server squatted the hub port,
    answered 404s politely, and nothing crashed)."""
    import socket
    # Is the port actually taken? A connect that succeeds = someone listens.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        taken = probe.connect_ex((host if host != "0.0.0.0" else "127.0.0.1",
                                  port)) == 0
    finally:
        probe.close()
    if not taken:
        return None
    import subprocess
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines()[1:]:  # skip header
            cols = line.split(None, 2)
            if len(cols) >= 2 and cols[1].isdigit():
                pid = int(cols[1])
                try:
                    cmd = subprocess.run(
                        ["ps", "-p", str(pid), "-o", "command="],
                        capture_output=True, text=True, timeout=5).stdout.strip()
                except Exception:
                    cmd = cols[0]
                return pid, cmd
    except Exception:
        pass
    return 0, ""  # taken, but holder unidentifiable — still refuse loudly


def _identify_hub(url: str) -> tuple[bool, str]:
    """(is-an-agora-hub, version) for whatever answers /healthz at `url`.
    IDENTITY, not compatibility (agora/0.4: one rule, one helper): whether
    we may take a port over depends on the holder being an agora hub AT
    ALL, never on which version it speaks."""
    try:
        import httpx
        r = httpx.get(f"{url}/healthz", timeout=3.0)
        body = r.json()
        if r.status_code == 200 and is_agora_protocol(body.get("protocol")):
            return True, body.get("version", "?")
    except Exception:
        pass  # not an agora hub (or not answering) — the squatter path
    return False, "?"


def _take_port_from(host: str, port: int, pid: int) -> str:
    """SIGTERM (then SIGKILL) `pid` and watch the PORT, not just the pid.
    Returns 'freed' when the port came free, 'changed' when a DIFFERENT
    process now holds it (the caller must re-verify THAT one before
    signaling it), or 'survived' when `pid` still holds it after SIGKILL.
    Exits 3 on EPERM."""
    import signal
    for sig, grace in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass  # already gone — someone else may still hold the port
        except PermissionError:
            print(f"REFUSING to start: pid {pid} is not ours to kill "
                  "(EPERM) — another user owns that hub.", file=sys.stderr)
            raise SystemExit(3)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            holder = _port_holder(host, port)
            if holder is None:
                return "freed"
            if holder[0] != pid:
                return "changed"
            time.sleep(0.25)
    return "survived"


def _preflight_port(host: str, port: int, url: str,
                    force: bool = False) -> None:
    """Before binding, diagnose a busy port instead of dying on a raw
    EADDRINUSE (agora-0096). If a healthy agora hub already holds it, say so
    and exit 0 (a double-launch is not an error) — unless `force`, which
    TAKES THE PORT OVER: SIGTERM the VERIFIED hub (escalating to SIGKILL),
    wait for the port to free, and proceed, so `agora up --force` in a
    terminal always ends with the newest installed hub serving and its logs
    in THAT terminal. A NON-hub squatter is never killed, force or not
    (killing an unverified process on protocol suspicion is how innocent
    daemons die): name the pid+command and exit 3.

    The port can be held by a process OTHER than the one we just killed —
    an inherited listen fd, a lingering predecessor, a supervisor that
    respawned the hub, or simply a second lsof row picked first. Killing
    the first pid we saw and then only asking "is the port free?" blamed an
    innocent pid for surviving and left the REAL hub serving (0.14.0 field
    test). So the takeover re-resolves the holder every round and keeps
    going, re-verifying each new holder as a hub before signaling it."""
    seen: list[int] = []
    for _round in range(_FORCE_MAX_ROUNDS):
        holder = _port_holder(host, port)
        if holder is None:
            if seen:
                print(f"--force: port {port} is free; starting fresh.",
                      file=sys.stderr)
            return  # free: proceed to bind
        pid, cmd = holder
        # Something listens. Is it a real agora hub? Re-asked every round:
        # the new holder is not necessarily the same software.
        is_hub, version = _identify_hub(url)
        if is_hub and not force:
            print(f"an agora hub is ALREADY running at {url} "
                  f"(version {version}) — nothing to do. "
                  "Stop it first if you meant to restart, or take the port "
                  "over with `agora up --force`.", file=sys.stderr)
            raise SystemExit(0)
        if not is_hub:
            who = f"pid {pid} ({cmd})" if pid else "an unidentified process"
            print(
                f"REFUSING to start: port {port} is held by {who} — NOT an "
                f"agora hub. This is exactly the silent-squatter class that "
                f"left the room deaf for 16h (a stray static file server on "
                f"the hub port). Free the port (kill {pid or 'that pid'}) "
                f"and retry, or start on a different port with --port. "
                f"(--force never kills an UNVERIFIED process — only a hub "
                f"that answers /healthz as agora.)", file=sys.stderr)
            raise SystemExit(3)
        if not pid:
            print(f"REFUSING to start: a hub answers at {url} but its pid "
                  "is unidentifiable (lsof missing or opaque) — nothing "
                  "safe to kill. Stop it by hand and retry.",
                  file=sys.stderr)
            raise SystemExit(3)
        if pid in seen:                       # already SIGKILLed, still here
            print(f"REFUSING to start: pid {pid} survived SIGTERM and "
                  f"SIGKILL and port {port} is still held — inspect it by "
                  "hand.", file=sys.stderr)
            raise SystemExit(3)
        seen.append(pid)
        print(f"--force: taking over port {port} from the running agora "
              f"hub (version {version}, pid {pid}) — SIGTERM, then "
              "SIGKILL if it lingers.", file=sys.stderr)
        outcome = _take_port_from(host, port, pid)
        if outcome == "freed":
            print(f"--force: port {port} is free; starting fresh.",
                  file=sys.stderr)
            return
        if outcome == "survived":
            print(f"REFUSING to start: pid {pid} survived SIGTERM and "
                  f"SIGKILL and port {port} is still held — inspect it by "
                  "hand.", file=sys.stderr)
            raise SystemExit(3)
        # 'changed': a different process holds the port now. Round again —
        # re-verify it as a hub, then take it over too.
    held = ", ".join(str(p) for p in seen)
    print(f"REFUSING to start: port {port} kept changing hands after "
          f"{_FORCE_MAX_ROUNDS} takeover rounds (killed {held}, and it is "
          "STILL held) — something is respawning hubs. Stop that supervisor "
          "and retry.", file=sys.stderr)
    raise SystemExit(3)


def _preflight_foreign_hub(db_path: str, cfg: dict, url: str) -> None:
    """Refuse to open a db another LIVE hub is serving. WAL admits two
    writer processes on one file, so `agora up --port 8766` against the
    running hub's db would boot fine and double every notify fan-out. The
    same-port double launch is already handled (exit 0 in _preflight_port);
    this catches the different-port case: config remembers the db AND the
    config url answers as an agora hub somewhere other than where we are
    about to bind (adversarial review F8, 2026-07-27)."""
    cfg_db, cfg_url = cfg.get("db_path"), cfg.get("url")
    if not (cfg_db and cfg_url) or cfg_url.rstrip("/") == url.rstrip("/"):
        return
    if os.path.realpath(cfg_db) != os.path.realpath(db_path):
        return
    try:
        import httpx
        r = httpx.get(f"{cfg_url.rstrip('/')}/healthz", timeout=3.0)
        if r.status_code == 200 and is_agora_protocol(
                r.json().get("protocol")):
            print(f"REFUSING to start: a hub at {cfg_url} is already "
                  f"serving this db ({db_path}). Two hubs on one SQLite "
                  "file double-deliver every message. Stop that hub first, "
                  "or start this one on its OWN db with --db.",
                  file=sys.stderr)
            raise SystemExit(3)
    except SystemExit:
        raise
    except Exception:
        pass  # config url answers nothing hub-like: proceed


def cmd_up(args: argparse.Namespace) -> None:
    import uvicorn

    from .hub.app import create_app

    # Observability under supervision (framework dm#21, 2026-07-25): when
    # stdout is a PIPE (supervisor, `| tee log`), Python block-buffers it —
    # the banner below sat invisible in the buffer while the hub served,
    # the log read EMPTY, and two healthy hubs were SIGKILLed as "wedged"
    # the same evening. Line-buffer both streams so every print lands the
    # moment it happens; the post-bind "ready" line comes from lifespan.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass  # non-reconfigurable stream (tests, exotic wrappers)

    home = _config.home()
    cfg = _config.load_config()
    # Source-aware db resolution (db_locate): an EXPLICIT --db may create a
    # new database; a REMEMBERED path (config.json, $AGORA_DB) may only open
    # an existing one — the a2a->agora rename incident (2026-07-27) showed a
    # stale remembered path silently minting an empty hub is the worst
    # failure this command has. Refusals exit 3 with named remedies.
    resolved = _db_locate.resolve(args.db, os.environ.get("AGORA_DB"),
                                  cfg.get("db_path"), str(home / "agora.db"))
    db_notices = _db_locate.preflight_up(
        resolved, home=home, default=str(home / "agora.db"),
        config_exists=(home / "config.json").exists())
    db_path = resolved.path
    _preflight_foreign_hub(db_path, cfg, _default_url(args.port))
    admin_key = os.environ.get("AGORA_ADMIN_KEY") or cfg.get("admin_key") or secrets.token_hex(16)
    url = _default_url(args.port)

    # Hub-written notify files: the hub maintains <id>-inbox.log for every
    # local agent itself, so no watcher processes, supervisors or OS services
    # are ever needed on the hub's machine. --notify-dir '' disables.
    notify_dir = args.notify_dir if args.notify_dir is not None else str(home)

    print(f"agora hub → {url}")
    print(f"  db:     {db_path}")
    for notice in db_notices:
        print(f"  {notice}")
    print(f"  config: {_config.home() / 'config.json'} (admin key saved; agents self-register)")
    if notify_dir:
        print(f"  notify: {notify_dir}/<agent>-inbox.log (hub-written; nothing to run)")
    if args.cors_origins:
        print(f"  cors:   {', '.join(origin.strip() for origin in args.cors_origins if origin.strip())}")
    # Paste-safe hints (no <angle brackets>: the shell reads `<x>` as a
    # redirect). Cover BOTH the local setup and the remote join flow, since
    # this line is the last thing printed before the hub blocks the terminal.
    print("  local agent:   agora setup AGENT_ID --harness FRAMEWORK   "
          "(cursor|claude|codex|abstractcode|abstractcode-tui; run in its\n           workspace)")
    print(f"  remote agent:  agora invite AGENT_ID --url {url}   "
          "(mints a one-paste `agora join ...` line for the other machine)")
    # Guard the seats, not just the hub: a venv swap under already-wired
    # agora-mcp binaries (reinstall without [mcp]) freezes every NEW session
    # on this machine while old processes keep working — invisible until
    # forensics. Probe at launch, warn loudly, never block the hub.
    _smoke_check_mcp(_resolve_mcp_command(),
                     hint="then restart affected agent sessions (running "
                          "ones keep the old code in memory).")
    # Refuse a squatted port with a NAMED diagnosis instead of a raw bind
    # error or (worse) letting a look-alike squatter answer politely while
    # the room goes deaf (agora-0096, the 16h-deaf-room incident).
    # --force takes over from a VERIFIED hub only (fresh restart on the
    # newest installed code, logs in THIS terminal).
    _preflight_port(args.host, args.port, url, force=args.force)
    app = create_app(db_path=db_path, admin_key=admin_key,
                     rate_per_minute=args.rate_per_minute,
                     notify_dir=notify_dir or None,
                     notify_rotate_mb=args.notify_rotate_mb,
                     max_attachment_bytes=int(args.max_attachment_mb * 1024 * 1024)
                     if args.max_attachment_mb else None,
                     max_channel_attachment_bytes=int(args.max_channel_attachment_mb * 1024 * 1024)
                     if args.max_channel_attachment_mb else None,
                     cors_origins=[origin.strip() for origin in (args.cors_origins or [])
                                   if origin.strip()],
                     # Boot SEED only (meta wins; agora-0137): first enable
                     # adopts it, a live hub's durable choice never yields
                     # to a hand-edited file.
                     embedding=cfg.get("embedding"))
    # Persist config only AFTER the db opened and migrated successfully.
    # Saving earlier planted remembered lies twice over: a crashed boot
    # re-blessed the very path it failed on, and a no-op double launch
    # (`up --db /new` while a hub is serving) rewrote db_path to a file no
    # hub was using (adversarial review F4, 2026-07-27).
    _config.save_config(url=url, admin_key=admin_key, db_path=db_path)
    # Operator-set hub rules are NEVER auto-upgraded (their prose is theirs).
    # But this build enforces mechanisms only the rules teach, so a stored
    # text from before a protocol bump leaves the hub enforcing what no agent
    # was ever told. Silent until now: the 0.14.0 field test upgraded a hub
    # that kept serving a v8 snapshot of an OLDER packaged default, and the
    # whole fleet ran a session without phase rows or consumes batching.
    _warn_stale_hub_rules(app)
    _warn_stale_hub_charter(app)
    # Pin WS keepalive explicitly: connection-derived presence relies on dead
    # sockets being detected within a bounded window (audit M4). Defaults can
    # differ per uvicorn/ws backend; make the bound deliberate.
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                ws_ping_interval=20.0, ws_ping_timeout=20.0)


def _warn_stale_hub_rules(app: Any) -> None:
    """Say so, once at boot, when the SERVED hub rules never mention a
    mechanism this build enforces. Version 0 (the packaged default) is
    always current by construction; only a stored operator text can fall
    behind. Marker-based, so rules rewritten in the operator's own words
    stay silent — this fires on a MISSING mechanism, not on a diff.

    Everything here is best-effort: a diagnostic must never be the reason
    a hub fails to boot."""
    from .governance import rules_missing_markers
    try:
        rules = app.state.service.hub_rules()
        if not rules.get("version"):
            return                      # packaged default: current by build
        missing = rules_missing_markers(rules.get("text") or "")
    except Exception:
        return                          # never let a warning break a boot
    if not missing:
        return
    print(f"  WARNING: hub rules v{rules['version']} (operator-set) never "
          f"mention {len(missing)} mechanism(s) this build enforces:")
    for why in missing:
        print(f"    - {why}")
    print("    Agents are served THESE rules at every whoami, so they will "
          "never be taught the above.\n"
          "    Merge the packaged default into your text and publish it: "
          "`agora rules --set FILE`.")


def _stale_charter_lines(version: int, text: str) -> list[str]:
    """The hub-charter twin of the rules-drift warning (0146). Same doctrine:
    operator prose is NEVER auto-upgraded, so a charter published before a
    kind of seat existed keeps being served with that kind missing — and the
    role model is exactly the document a new seat consults to learn what it
    may do. Marker-based (a rewrite in the operator's own words stays
    silent), and version 0 is the packaged default: current by construction.

    Returns printable lines so the SAME text can be shown at boot and by
    `agora status` — the 0.14.0 incident was not that no warning existed,
    it was that the only place it printed was a boot an operator had run
    hours earlier."""
    from .governance import charter_missing_roles, charter_scoping_advice
    if not version:
        return []
    out: list[str] = []
    missing = charter_missing_roles(text or "")
    if missing:
        out.append(f"  WARNING: hub charter v{version} (operator-set) never "
                   f"describes {len(missing)} kind(s) of seat this hub "
                   f"implements:")
        out += [f"    - {why}" for why in missing]
        out.append("    Seats read THIS text to learn what they may do, so those "
                   "powers are undocumented on this hub.\n"
                   "    Merge the packaged default and publish it: "
                   "`agora charter set FILE` (see `agora charter show --version 0`).")
    # 0147: a text that describes every seat kind may still be unsliceable —
    # every seat then pays for every role's rules on every read. Advice, not a
    # warning: nothing is wrong, and nothing is hidden.
    advice = charter_scoping_advice(text or "")
    if advice:
        out.append(f"  hub charter v{version}: served WHOLE to every seat.")
        out += advice
    return out


def _warn_stale_hub_charter(app: Any) -> None:
    """Boot-time half of the charter-drift warning. Best-effort like its
    rules twin: a diagnostic must never be the reason a hub fails to boot."""
    try:
        doc = app.state.service.hub_charter()
        lines = _stale_charter_lines(doc.get("version") or 0, doc.get("text") or "")
    except Exception:
        return
    for line in lines:
        print(line)


def _setup_key(url: str, agent_id: str, about: str,
               key_flag: str | None) -> str | None:
    """The agent key a setup command should cache: seed an
    operator-minted --key if one was passed (verifying it against the hub so a
    paste truncation fails HERE, not at first tool use), then resolve — cache
    hit, else admin-key self-registration. Returns None only when NO
    credential exists at all: that is today's keyless config, where the MCP
    server lazily self-registers on first use (local first-run unchanged)."""
    if key_flag:
        _config.seed_keys(url, {agent_id: key_flag})
        _whoami_check(url, key_flag)
    if not (key_flag or _config.get_cached_key(url, agent_id)
            or os.environ.get("AGORA_ADMIN_KEY")
            or _config.load_config().get("admin_key")):
        return None
    return _config.resolve_key(url, agent_id, about=about)


def _whoami_check(url: str, api_key: str) -> dict:
    """Verify a key against the hub; loud, actionable failure."""
    import httpx

    r = httpx.get(f"{url}/whoami",
                  headers={"Authorization": f"Bearer {api_key}"}, timeout=10.0)
    if r.status_code != 200:
        raise SystemExit(f"the hub at {url} rejected this key "
                         f"({r.status_code}): check for paste truncation, or "
                         "ask the operator to re-mint (`agora register`).")
    return r.json()


def _print_kickoff(harness: str = "cursor") -> None:
    """A rule only reaches a harness session's context INSIDE a turn, so a
    just-launched idle session never arms itself — it needs one kick-off
    turn. That turn is three words: setup installs the agora skill for the
    harness, and the skill owns the whole boot (identity, orientation,
    reception, readiness). The old paste-a-paragraph kickoff is gone —
    operator finding, 2026-07-15: a long prompt that restates what the rule
    and skill already teach is noise with drift risk."""
    print(_kickoff_text(harness))


def _kickoff_text(harness: str = "cursor") -> str:
    # `.get`, never `[]`: this is the last line of a SUCCESSFUL setup, so a
    # missing entry turned a completed wiring into a traceback (same class as
    # the join.py opener KeyError). A new harness degrades to its own name.
    launch = {
        "cursor": "cursor-agent (or open the folder in Cursor)",
        "claude": "claude",
        "codex": "codex",
        "abstractcode": "abstractcode",
        "abstractcode-tui": "abstractcode-tui",
        "opencode": "opencode",
        "pi": "pi",
        "all": "your preferred supported framework",
    }.get(harness, harness)
    return ("\nStart the agent: launch "
            f"{launch} in this folder and give it one message:\n\n"
            "  start agora protocol\n")


@dataclass
class _HarnessSetupResult:
    harness: str
    title: str
    written: list[Path]
    lines: list[str]
    issues: list[str] = field(default_factory=list)


def _effective_hook_choice(cmd: str, args: argparse.Namespace) -> bool:
    with_hook = bool(getattr(args, "with_hook", False))
    no_hook = bool(getattr(args, "no_hook", False))
    if with_hook and no_hook:
        sys.exit(f"agora {cmd}: choose one of --with-hook or --no-hook")
    return not no_hook


def _validate_agent_id_or_exit(agent_id: str) -> None:
    try:
        validate_agent_id(agent_id)
    except ValueError as exc:
        sys.exit(f"invalid agent id '{agent_id}': {exc}")


def _prompt_harness_choice(cmd: str, *, allow_none: bool = False) -> str:
    options = [
        ("1", "cursor", "Cursor / cursor-agent"),
        ("2", "claude", "Claude Code"),
        ("3", "codex", "Codex CLI"),
        ("4", "abstractcode", "AbstractCode"),
        ("5", "all", "all supported harnesses"),
    ]
    if allow_none:
        options.append(("6", "none", "skip workspace wiring"))
    print(f"agora {cmd}: no existing harness footprint found in this workspace.")
    print("Choose the wiring to install:")
    for key, value, label in options:
        print(f"  {key}) {value:<6} {label}")
    mapping = {key: value for key, value, _ in options}
    mapping.update({value: value for _, value, _ in options})
    while True:
        choice = input("selection: ").strip().lower()
        picked = mapping.get(choice)
        if picked:
            return picked
        print("enter one of: " + ", ".join(k for k in mapping if len(k) == 1))


def _resolve_harnesses(cmd: str, workspace: Path, selection: str | None, *,
                       allow_none: bool = False) -> tuple[str, ...]:
    from .setup_harness import (detect_workspace_harnesses,
                                expand_harness_selection)

    if selection not in (None, "", "auto"):
        return expand_harness_selection(selection, allow_none=allow_none)
    detected = detect_workspace_harnesses(workspace)
    if len(detected) == 1:
        print(f"note: auto-selected `{detected[0]}` from the existing workspace "
              "footprint.")
        return detected
    if len(detected) > 1:
        label = ", ".join(detected)
        print(f"note: auto-selected the already-present harness set: {label}.")
        return detected
    if sys.stdin.isatty() and sys.stdout.isatty():
        picked = _prompt_harness_choice(cmd, allow_none=allow_none)
        return expand_harness_selection(picked, allow_none=allow_none)
    choices = ["cursor", "claude", "codex", "abstractcode",
               "abstractcode-tui", "all"]
    if allow_none:
        choices.append("none")
    sys.exit(f"agora {cmd}: no existing harness footprint found in {workspace}. "
             "Re-run with --harness " + "|".join(choices) +
             ", or run the command interactively once and choose.")


def _resolve_setup_request(args: argparse.Namespace) -> tuple[str, str, bool]:
    from .setup_harness import SUPPORTED_HARNESSES

    legacy_harness = getattr(args, "legacy_harness", None)
    if legacy_harness:
        return args.agent, legacy_harness, True
    target = args.target
    legacy_agent = getattr(args, "legacy_agent", None)
    if legacy_agent is not None:
        if target not in SUPPORTED_HARNESSES:
            sys.exit(f"agora setup: unknown harness '{target}'")
        chosen = getattr(args, "harness", "auto")
        if chosen not in ("auto", "all", target):
            sys.exit("agora setup: positional harness and --harness/--framework "
                     "disagree")
        return legacy_agent, target, True
    return target, getattr(args, "harness", "auto"), False


def _setup_result(harness: str, workspace: Path, agent_id: str, url: str,
                  about: str, mcp_command: str, api_key: str | None,
                  *, with_hook: bool, headless: bool,
                  bootstrap_cli: bool) -> _HarnessSetupResult:
    from . import setup_harness as _sh

    lines: list[str] = []
    issues: list[str] = []
    if harness == "cursor":
        written = _sh.setup_cursor(workspace, agent_id, url, about,
                                   mcp_command, with_hook,
                                   api_key=api_key, headless=headless)
        lines.append("  hook: installed the driver-aware stop-hook backstop."
                     if with_hook else
                     "  hook: skipped (--no-hook); the background listener is "
                     "the only reception surface in interactive sessions.")
        if headless:
            lines.append("  --headless is a deprecated no-op (wiring is "
                         "identical; the running driver IS the mode). "
                         f"Drive quickstart: cd {workspace} && agora drive")
        else:
            lines.append("  launch: open this folder in Cursor or run "
                         "`cursor-agent` here."
                         + (" The agent authenticates immediately."
                            if api_key else
                            " The agent self-registers on first tool use."))
        return _HarnessSetupResult("cursor", "Cursor", written, lines,
                                   issues=issues)

    if harness == "claude":
        written = _sh.setup_claude(workspace, agent_id, url, about,
                                   mcp_command, with_hook, api_key=api_key)
        lines.append("  reception: SessionStart/Stop wake hooks plus the "
                     "stop-hook backstop installed."
                     if with_hook else
                     "  reception: manual only (--no-hook skipped Claude's "
                     "idle-wake hooks and stop-hook backstop).")
        if bootstrap_cli:
            registered, detail = _sh.register_claude_local(
                workspace, mcp_command, url, agent_id, about,
                api_key=api_key, home=_sh.custom_home_env())
            lines.append(f"  {detail}")
            if not registered:
                issues.append(f"Claude Code vendor bootstrap needs action: {detail}")
            lines.append("  launch: run `claude` in this folder — the "
                         "'agora' MCP server is already registered."
                         if registered else
                         "  launch: run `claude` in this folder and approve "
                         "the project 'agora' server via /mcp if Claude asks.")
        else:
            lines.append("  launch: run `claude` in this folder. The project "
                         "files are ready; trust the workspace and approve the "
                         "project 'agora' server via /mcp if Claude asks.")
        return _HarnessSetupResult("claude", "Claude Code", written, lines,
                                   issues=issues)

    if harness == "abstractcode":
        written = _sh.setup_abstractcode(
            workspace, agent_id, url, about, mcp_command, with_hook,
            api_key=api_key,
        )
        lines.append(
            "  reception: `agora drive` owns unattended wakes; AbstractCode "
            "exposes no native hook registration surface, so the generic "
            "--with-hook default requires no extra hook file."
        )
        lines.append(
            "  launch: run `abstractcode --state-file "
            ".abstractcode/agora.state.json --skill agora-channels` for an "
            "interactive session, or `agora drive` unattended "
            "(use `agora drive --harness abstractcode` only in a multi-"
            "harness workspace)."
        )
        return _HarnessSetupResult(
            "abstractcode", "AbstractCode", written, lines,
            issues=issues,
        )

    if harness == "abstractcode-tui":
        written = _sh.setup_abstractcode_tui(
            workspace, agent_id, url, about, mcp_command, with_hook,
            api_key=api_key,
        )
        lines.append(
            "  reception: IN-SESSION. This harness has no hook or idle-wake "
            "surface, so agora messages arrive when the agent looks; a seat "
            "that must stay reachable while idle needs a driven seat."
        )
        lines.append(
            "  driving: not yet — `agora harness-check abstractcode-tui` "
            "reports which parts of the harness contract are unmet "
            "(docs/harness_contract.md). agora does not configure your "
            "framework: how this agent reaches agora's tools is yours to set."
        )
        return _HarnessSetupResult(
            "abstractcode-tui", "AbstractCode-TUI", written, lines,
            issues=issues,
        )

    if harness == "opencode":
        written = _sh.setup_opencode(workspace, agent_id, url, about,
                                     mcp_command, with_hook, api_key=api_key)
        lines.append(
            "  reception: the .opencode/plugin/agora.js plugin relays asks + "
            "fyi into each prompt and asks after tool calls (mid-task). "
            "opencode has no idle-delivery surface, so between turns messages "
            "wait — use `agora drive opencode` for an always-reachable seat."
        )
        lines.append(
            "  launch: run `opencode` in this folder. Providers/models are "
            "yours to configure in opencode.json; agora added only its own "
            "`mcp.agora` server and `agora*` permission."
        )
        return _HarnessSetupResult("opencode", "opencode", written, lines,
                                   issues=issues)

    if harness == "pi":
        written = _sh.setup_pi(workspace, agent_id, url, about,
                               mcp_command, with_hook, api_key=api_key)
        lines.append(
            "  reception: agora's tools ride the .pi/extensions/agora.js "
            "bridge (pi ships no MCP client). pi has no idle wake: the seat "
            "checks its inbox at turn boundaries; use `agora drive pi` for an "
            "always-reachable seat."
        )
        lines.append(
            "  launch: run `pi` in this folder and approve the project "
            "extension once (pi's trust prompt). Providers/models are yours "
            "(pi's models.json)."
        )
        return _HarnessSetupResult("pi", "pi", written, lines, issues=issues)

    written = _sh.setup_codex(workspace, agent_id, url, about,
                              mcp_command, with_hook=with_hook,
                              api_key=api_key, dedicated=True)
    lines.append("  hook: installed the Stop hook backstop; approve it once "
                 "via /hooks (and re-approve if the file changes)."
                 if with_hook else
                 "  hook: skipped (--no-hook); Codex only sees new work on the "
                 "next human turn unless you re-enable the Stop hook.")
    if bootstrap_cli:
        registered, detail = _sh.register_codex_global(
            mcp_command, url, agent_id, about,
            api_key=api_key, home=_sh.custom_home_env())
        lines.append(f"  {detail}")
        if not registered:
            issues.append(f"Codex vendor bootstrap needs action: {detail}")
        lines.append("  launch: run `codex` in this folder"
                     + (" and trust the project when prompted."
                        if not registered else
                        " (trusting the project pins this workspace's "
                        "identity over the global bootstrap entry)."))
    else:
        lines.append("  launch: run `codex` in this folder and trust the "
                     "project when prompted so this workspace's "
                     "`.codex/config.toml` takes effect.")
    lines.append("  mode: dedicated LIVE Codex session. Nobody shares this "
                 "terminal; after `start agora protocol` the Stop hook keeps "
                 "the live turn alive around the standing "
                 "`wait_for_messages(45)` loop.")
    lines.append("  alternative: if you want an unattended external watcher "
                 "instead, run "
                 f"`cd {workspace} && agora drive` and do NOT use the "
                 "standing loop in a shared session.")
    return _HarnessSetupResult("codex", "Codex CLI", written, lines,
                               issues=issues)


def _harness_label(reports: list[_HarnessSetupResult]) -> str:
    if len(reports) == 1:
        return reports[0].title
    return ", ".join(report.harness for report in reports)


def _print_final_status(issues: list[str]) -> int:
    if not issues:
        print("\nstatus: READY")
        return 0
    print("\nstatus: NEEDS ACTION")
    for issue in issues:
        print(f"  - {issue}")
    return 2


def cmd_setup(args: argparse.Namespace) -> None:
    """Agent-first workspace wiring.

    Preferred shape: `agora setup <id>` to write the supported harness
    footprints into this workspace. Back-compat shapes remain:
    `agora setup <harness> <id>` and `agora setup-<harness> <id>`."""
    from .setup_harness import (install_skill, preflight_workspace_harnesses,
                                write_workspace_seat)

    agent_id, selection, legacy = _resolve_setup_request(args)
    if getattr(args, "deprecated_alias", None):
        print(f"note: `agora {args.deprecated_alias}` still works but is "
              f"deprecated — prefer `agora setup {agent_id} --harness "
              f"{selection}` (same flags).")
    elif legacy:
        print(f"note: `agora setup {selection} {agent_id}` still works but "
              f"is deprecated — prefer `agora setup {agent_id} --harness "
              f"{selection}`.")
    if getattr(args, "with_hook", False) and not getattr(args, "no_hook", False):
        print("note: `--with-hook` is now the default; use `--no-hook` to skip "
              "hook installation.")

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        sys.exit(f"workspace not found: {workspace}")
    _validate_agent_id_or_exit(agent_id)
    harnesses = _resolve_harnesses("setup", workspace, selection)
    with_hook = _effective_hook_choice("setup", args)
    headless = bool(getattr(args, "headless", False))
    vendor_bootstrap = bool(getattr(args, "vendor_bootstrap", False))
    if vendor_bootstrap and (len(harnesses) != 1 or harnesses[0] not in ("claude", "codex")):
        sys.exit("agora setup: --vendor-bootstrap requires exactly one harness "
                 "(`--harness claude|codex`)")

    try:
        preflight_workspace_harnesses(workspace, harnesses)
    except ValueError as exc:
        sys.exit(f"agora setup: {exc}")

    url = _hub_url(args)
    about = args.about or ""
    mcp_command = _resolve_mcp_command()
    _smoke_check_mcp(mcp_command, required=True)
    api_key = _setup_key(url, agent_id, about, args.key)

    reports = [
        _setup_result(harness, workspace, agent_id, url, about, mcp_command,
                      api_key, with_hook=with_hook,
                      headless=headless and harness in ("cursor", "codex", "abstractcode"),
                      bootstrap_cli=vendor_bootstrap and harness in ("claude", "codex"))
        for harness in harnesses
    ]
    default_drive = harnesses[0] if len(harnesses) == 1 else None
    seat_record = write_workspace_seat(
        workspace, agent_id=agent_id, url=url, about=about,
        harnesses=harnesses, default_drive_harness=default_drive)

    label = _harness_label(reports)
    print(f"configured '{workspace.name}' as agora agent '{agent_id}' ({label}):")
    print(f"  wrote {seat_record}")
    if api_key:
        print(f"  key: cached only in {_config.home() / 'keys.json'} (0600); "
              "harness config contains no bearer")
    issues: list[str] = []
    for report in reports:
        print(f"  {report.title}:")
        for path in report.written:
            print(f"    wrote {path}")
        skill_detail = install_skill(report.harness)
        print(f"    {skill_detail}")
        if skill_detail.startswith("skill: could not install"):
            issues.append(f"{report.title} skill installation needs action")
        for line in report.lines:
            indent = "    " if line.startswith("  ") else "  "
            print(indent + line.lstrip())
        issues.extend(report.issues)

    channel_issues = _setup_join_channels(args, agent_id=agent_id)
    issues.extend(channel_issues)
    code = _print_final_status(issues)
    print()
    if code == 0:
        if len(harnesses) > 1:
            print("Configured harnesses: " + ", ".join(harnesses) + ".")
            print("No default driver was selected; start one with "
                  "`agora drive --harness <name>`.\n")
        _print_kickoff(reports[0].harness if len(reports) == 1 else "all")
        return
    print("Resolve the items above, then launch the agent and send:\n\n"
          "  start agora protocol\n")
    raise SystemExit(code)


def _setup_join_channels(args: argparse.Namespace,
                         agent_id: str | None = None) -> list[str]:
    """PLACEMENT is part of wiring: `--channels a,b` joins the seat to its
    rooms at setup time, so it never boots member-of-nothing. Field finding
    (2026-07-14, operator's own test): a seat wired without placement
    improvised at boot and squatted the busiest public channel, polluting
    real work — placement decisions belong to the operator, mechanically,
    not to the agent's judgment."""
    import asyncio

    channels = [c.strip() for c in (getattr(args, "channels", "") or "").split(",")
                if c.strip()]
    if not channels:
        return []
    url = _hub_url(args)
    resolved_agent = agent_id or getattr(args, "agent", None)
    if not resolved_agent:
        sys.exit("agora setup: no agent id resolved for --channels placement")
    key = _config.resolve_key(url, resolved_agent)

    async def go() -> None:
        from .client import AgoraClient
        client = AgoraClient(url, key)
        failures: list[str] = []
        try:
            for chan in channels:
                try:
                    await client.join_channel(chan)
                    print(f"  joined '{chan}' as {resolved_agent}")
                except Exception as exc:
                    print(f"  could NOT join '{chan}': {exc} — create it "
                          f"first (`agora create-channel {chan} --as "
                          f"<operator-id> --public`) or join later with "
                          f"`agora join --channel {chan} --as {resolved_agent}`")
                    failures.append(f"channel placement failed for '{chan}'")
        finally:
            await client.close()
        return failures
    return asyncio.run(go())


def cmd_setup_cursor(args: argparse.Namespace) -> None:
    args.legacy_harness = "cursor"
    cmd_setup(args)


def cmd_setup_claude(args: argparse.Namespace) -> None:
    args.legacy_harness = "claude"
    cmd_setup(args)


def cmd_setup_codex(args: argparse.Namespace) -> None:
    args.legacy_harness = "codex"
    cmd_setup(args)


# -- operator verbs for remote onboarding (register / seed-key) ---------------


def _admin_key_or_exit(args: argparse.Namespace, url: str) -> str:
    """Admin credential, resolved exactly like resolve_key: explicit flag,
    then $AGORA_ADMIN_KEY, then the hub machine's config.json."""
    admin = (getattr(args, "admin_key", None)
             or os.environ.get("AGORA_ADMIN_KEY")
             or _config.load_config().get("admin_key"))
    if not admin:
        sys.exit(f"no admin key for {url}: pass --admin-key, export "
                 "AGORA_ADMIN_KEY, or run this on the hub machine "
                 "(where `agora up` saved ~/.agora/config.json).")
    return admin


def _admin_request(method: str, path: str, payload: dict | None = None,
                   params: dict | None = None) -> tuple[int, dict]:
    """One admin-key HTTP call to the local hub; (0, {}) when the hub is
    down (callers decide what down means for their verb)."""
    import httpx
    cfg = _config.load_config()
    url = cfg.get("url", "http://127.0.0.1:8765")
    key = os.environ.get("AGORA_ADMIN_KEY") or cfg.get("admin_key", "")
    try:
        resp = httpx.request(method, f"{url.rstrip('/')}{path}",
                             json=payload, params=params,
                             headers={"Authorization": f"Bearer {key}"},
                             timeout=15.0)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"detail": resp.text[:200]}
    except httpx.HTTPError:
        return 0, {}


def cmd_store(args: argparse.Namespace) -> None:
    """`agora store get|set|list` — the CLI store verb (field gap
    2026-07-27, framework dm#32: with the MCP bridge flaky, at least two
    seats had ZERO store write path — claims/decisions/work rows are
    coordination-critical state and the terminal must be able to carry
    them). Same hub rules apply: CAS via --expect-version, claim/work
    key validation happens hub-side."""
    async def go(c, a):
        if a.store_action == "get":
            row = await c.store_get(a.channel, a.key)
            print(json.dumps(row, indent=1))
            return
        if a.store_action == "list":
            rows = await c.store_keys(a.channel)
            for r in rows:
                if a.prefix and not str(r.get("key", "")).startswith(a.prefix):
                    continue
                print(f"{r.get('key')}  v{r.get('version')}"
                      f"  by {r.get('updated_by')}")
            return
        # set
        try:
            value = json.loads(a.value)
        except ValueError:
            sys.exit("store set: VALUE must be JSON (quote it; strings"
                     " need embedded quotes: '\"text\"')")
        kwargs = {}
        if a.expect_version is not None:
            kwargs["expect_version"] = a.expect_version
        row = await c.store_set(a.channel, a.key, value, **kwargs)
        print(f"ok: {a.key} v{row.get('version')} in {a.channel}")
    _run_agent_cmd(args, go)


def cmd_embedding(args: argparse.Namespace) -> None:
    """Semantic-search lifecycle (agora-0137): set | status | backfill |
    disable. One verb family on purpose — the near-twin `agora embed`
    was a named UX trap. All prints are the adversary-cycle texts."""
    action = args.action
    if action == "set":
        if not args.url or not args.model:
            sys.exit("usage: agora embedding set --url URL --model MODEL"
                     " [--api-key K] [--accept-recompute]")
        _config.save_embedding(args.url, args.model, args.api_key or "")
        code, body = _admin_request("PUT", "/admin/embedding", {
            "url": args.url, "model": args.model,
            "api_key": args.api_key or "",
            "accept_recompute": bool(args.accept_recompute)})
        if code == 0:
            print("hub not running — saved as boot seed"
                  f" ({_config.home() / 'config.json'}, 0600);"
                  " applies at next `agora up`.")
            return
        if code == 409:
            sys.stderr.write(body.get("detail", "refused") + "\n")
            raise SystemExit(3)
        if code != 200:
            sys.stderr.write(f"refused ({code}): {body.get('detail')}\n")
            raise SystemExit(3)
        probe = body.get("probe") or {}
        if not body.get("changed"):
            print(f"unchanged (already {body.get('model')}) — probe ok:"
                  f" {probe.get('dim')} dims")
            return
        print(f"probe ok: {probe.get('dim')} dims")
        print("saved to hub (live) + boot seed"
              f" ({_config.home() / 'config.json'}, 0600)")
        n = body.get("docs_to_embed")
        if body.get("pending"):
            print(f"filling the NEW model alongside the old ({n} docs);"
                  " the old model keeps serving until the fill flips")
        else:
            print(f"found {n} docs to embed — filling (~25 min on a local"
                  " 0.6b model)")
        print("follow: agora embedding status")
        return
    if action == "status":
        code, body = _admin_request("GET", "/admin/embedding")
        if code == 0:
            seed = _config.load_config().get("embedding") or {}
            print("hub not running. boot seed:",
                  json.dumps(seed) if seed else "(none)")
            return
        for k in ("state", "model", "pending_model", "url", "coverage",
                  "pending_coverage", "rows", "thread_alive",
                  "last_beat_age_s", "last_error", "breaker",
                  "seed_mismatch", "vectors_db"):
            if body.get(k) is not None:
                print(f"  {k}: {body[k]}")
        return
    if action == "backfill":
        # Repair-only (UX c2 P1-2): the derived work set fills by
        # construction; this verb just nudges the sweep and reports.
        code, body = _admin_request("GET", "/admin/embedding")
        if code == 0:
            sys.exit("hub not running")
        print(f"state: {body.get('state')} — the standing reconcile derives"
              " all missing/stale vectors itself; nothing to enqueue."
              " Watch: agora embedding status")
        return
    if action == "disable":
        code, body = _admin_request("DELETE", "/admin/embedding",
                                    params={"erase": str(bool(args.erase)).lower()})
        if code == 0:
            sys.exit("hub not running")
        if code != 200:
            sys.exit(f"refused ({code}): {body.get('detail')}")
        print("semantic search disabled"
              + (f" — {body.get('erased_rows')} vector rows erased"
                 if args.erase else " (vectors kept; re-enable resumes)"))
        return
    sys.exit(f"unknown action '{action}' (set|status|backfill|disable)")


def cmd_backup(args: argparse.Namespace) -> None:
    """Operator verb: `agora backup [OUT]` — verified point-in-time snapshot
    of the ENTIRE hub (messages, channel files, store, agents: it is one
    SQLite file). Safe against a LIVE hub (SQLite online backup API); the
    copy is integrity-checked after writing, so what you hold is a verified
    artifact, not a hopeful cp."""
    from . import backup as _backup

    cfg = _config.load_config()
    home = _config.home()
    resolved = _db_locate.resolve(args.db, os.environ.get("AGORA_DB"),
                                  cfg.get("db_path"), str(home / "agora.db"))
    _db_locate.preflight_backup(resolved, home=home,
                                default=str(home / "agora.db"))
    db_path = resolved.path
    out = Path(args.out) if args.out else _backup.default_snapshot_path(
        _config.home() / "backups")
    try:
        info = _backup.snapshot(db_path, out)
    except ValueError as e:
        sys.exit(f"backup failed: {e}")
    c = info["counts"]
    print(f"backup written and VERIFIED: {info['path']}\n"
          f"  {info['bytes'] / 1e6:.1f} MB — {c['messages']} messages, "
          f"{c['agents']} agents, {c['channels']} channels, "
          f"{c['fs_files']} fs files\n"
          f"  restore with: agora restore {info['path']}")


def cmd_restore(args: argparse.Namespace) -> None:
    """Operator verb: `agora restore SNAPSHOT` — replace the hub db with a
    verified snapshot. Refuses while a hub is RUNNING (stop it first); the
    current db is preserved aside as <db>.pre-restore-<ts>, so a restore can
    never destroy the only copy of anything."""
    from . import backup as _backup

    cfg = _config.load_config()
    home = _config.home()
    resolved = _db_locate.resolve(args.db, os.environ.get("AGORA_DB"),
                                  cfg.get("db_path"), str(home / "agora.db"))
    _db_locate.preflight_restore(resolved, home=home,
                                 default=str(home / "agora.db"))
    db_path = resolved.path
    url = _hub_url(args)
    # A restore under a live hub would race its WAL; refuse with the fix.
    try:
        import httpx
        r = httpx.get(f"{url}/healthz", timeout=2.0)
        if r.status_code == 200:
            sys.exit(f"a hub is RUNNING at {url} — stop it first "
                     "(kill the `agora up` process), restore, then start it "
                     "again. Restoring under a live hub would corrupt both.")
    except SystemExit:
        raise
    except Exception:
        pass  # nothing answering: safe to proceed
    try:
        info = _backup.restore(args.snapshot, db_path)
    except ValueError as e:
        sys.exit(f"restore refused: {e}")
    c = info["counts"]
    print(f"restored {args.snapshot} -> {info['path']}\n"
          f"  now contains: {c['messages']} messages, {c['agents']} agents, "
          f"{c['channels']} channels, {c['fs_files']} fs files")
    if info["preserved"]:
        print(f"  previous db preserved at: {info['preserved']}")
    print("  start the hub again with: agora up")


def cmd_rules(args: argparse.Namespace) -> None:
    """Operator verb: show or replace the hub rules — the general
    instructions every agent receives in /whoami. `agora rules` prints the
    current text (with its version); `agora rules --set FILE` replaces it
    live: every agent sees the new version at its next whoami, no workspace
    re-setup anywhere. The packaged default (version 0) serves until the
    first --set."""
    import httpx

    url = _hub_url(args)
    admin = _admin_key_or_exit(args, url)
    headers = {"Authorization": f"Bearer {admin}"}
    if args.set_file:
        text = Path(args.set_file).read_text()
        r = httpx.put(f"{url}/admin/rules", headers=headers,
                      json={"text": text}, timeout=10.0)
        if r.status_code != 200:
            sys.exit(f"setting hub rules failed: {r.status_code} {r.text}")
        print(f"hub rules updated to v{r.json()['version']} "
              f"({len(text.splitlines())} lines) — agents see it at their next whoami")
        return
    r = httpx.get(f"{url}/admin/rules", headers=headers, timeout=10.0)
    if r.status_code != 200:
        sys.exit(f"reading hub rules failed: {r.status_code} {r.text}")
    payload = r.json()
    print(f"# hub rules v{payload['version']}"
          + (" (packaged default; `agora rules --set FILE` to replace)"
             if payload["version"] == 0 else ""))
    print(payload["text"])


def _charter_diff(old: str, new: str, *, old_label: str,
                  new_label: str) -> str:
    """Unified diff between two charter texts, or "" when identical.

    A charter is prose an operator EDITS, so "what am I about to change?" is
    the question every publish has to answer before it lands — a full reprint
    of two screenfuls does not answer it."""
    import difflib

    lines = list(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=old_label, tofile=new_label, n=2))
    return "".join(lines)


#: Diff lines shown before the tail is folded. A wholesale replacement of a
#: 75-line charter would otherwise scroll the confirmation prompt off screen —
#: which is the one thing the preview exists to prevent.
_CHARTER_DIFF_LINES = 60


def _print_charter_diff(old: str, new: str, *, old_label: str,
                        new_label: str, header: str = "proposed change") -> bool:
    """Show the pending change; False when there is nothing to publish."""
    diff = _charter_diff(old, new, old_label=old_label, new_label=new_label)
    if not diff:
        return False
    lines = diff.rstrip("\n").splitlines()
    added = sum(1 for line in lines
                if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines
                  if line.startswith("-") and not line.startswith("---"))
    print(f"# {header}: +{added} -{removed} lines")
    print("\n".join(lines[:_CHARTER_DIFF_LINES]))
    if len(lines) > _CHARTER_DIFF_LINES:
        print(f"  … +{len(lines) - _CHARTER_DIFF_LINES} more diff lines "
              f"(the full texts: `agora charter show`)")
    return True


def _charter_confirm(args: argparse.Namespace, what: str) -> None:
    """Ask before publishing, but ONLY at a keyboard.

    `--yes` skips it; a pipe, a heredoc or CI skips it too (an interactive
    prompt no one can answer is a hang, not a safeguard). The diff has
    already been printed either way, so a scripted publish still leaves the
    change on the record."""
    if getattr(args, "yes", False) or not sys.stdin.isatty():
        return
    answer = input(f"publish this as {what}? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        sys.exit("aborted — nothing was published")


def _charter_editor_text(current: str, *, what: str) -> str:
    """$EDITOR on the CURRENT text; the saved buffer is the new charter.

    Aborts (never publishes) when the buffer comes back empty or unchanged —
    quitting the editor is how an operator says "never mind", and an editor
    that exits nonzero (`:cq`) means the same thing. `$AGORA_EDITOR` wins
    over `$VISUAL`/`$EDITOR` so a harness can drive this deterministically."""
    import subprocess
    import tempfile

    editor = (os.environ.get("AGORA_EDITOR") or os.environ.get("VISUAL")
              or os.environ.get("EDITOR"))
    if not editor:
        sys.exit("no editor: set $EDITOR (or $VISUAL/$AGORA_EDITOR), or pass "
                 "a FILE / `-` to read the text from stdin")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "charter.md"
        path.write_text(current)
        try:
            rc = subprocess.call([*shlex.split(editor), str(path)])
        except OSError as exc:
            sys.exit(f"could not run $EDITOR ({editor!r}): {exc}")
        if rc != 0:
            sys.exit(f"editor exited {rc} — nothing was published")
        text = path.read_text()
    if not text.strip():
        sys.exit("the buffer came back empty — nothing was published "
                 f"({what} is unchanged)")
    if text == current:
        sys.exit(f"no changes — {what} is unchanged, nothing was published")
    return text


def _charter_new_text(args: argparse.Namespace, current: str, *,
                      what: str, default_text: str) -> str:
    """The text a `set` will publish, from exactly ONE declared source:
    a FILE, `-` (stdin/heredoc), `--edit` ($EDITOR on the current text), or
    `--from-default` (the packaged text). Naming two is a mistake worth
    refusing — silently preferring one would publish the wrong document."""
    sources = [name for name, on in (
        ("FILE", bool(args.file) and args.file != "-"),
        ("-", args.file == "-"),
        ("--edit", bool(getattr(args, "edit", False))),
        ("--from-default", bool(getattr(args, "from_default", False))),
    ) if on]
    if len(sources) > 1:
        sys.exit(f"pick ONE source for the new text, not {', '.join(sources)}")
    if not sources:
        sys.exit("usage: agora charter set FILE | - | --edit | --from-default "
                 "[--channel X --as SEAT] [--yes]")
    if sources[0] == "--from-default":
        return default_text
    if sources[0] == "--edit":
        return _charter_editor_text(current, what=what)
    if sources[0] == "-":
        text = sys.stdin.read()
        if not text.strip():
            sys.exit("stdin was empty — nothing was published")
        return text
    path = Path(args.file)
    try:
        return path.read_text()
    except OSError as exc:
        sys.exit(f"cannot read {path}: {exc}")


def cmd_charter(args: argparse.Namespace) -> None:
    """Charter management, one verb for both scopes (0146).

        agora charter show                      the hub charter (who is who)
        agora charter show --diff               what the last publish changed
        agora charter show --channel design     that room's charter
        agora charter set FILE                  publish a new hub charter
        agora charter set -                     ... from stdin / a heredoc
        agora charter set --edit                ... in $EDITOR, save to publish
        agora charter set --from-default        ... back to the packaged text
        agora charter set FILE --channel design --as owner
        agora charter history [--channel X]     published versions
        agora charter history --diff N          what version N changed
        agora charter receipts [--channel X]    who has read the current one

    Hub scope is the operator's (admin key); channel scope is the owner's
    (`--as` an agent who owns the room, or an operator). Every subcommand
    takes `--channel` and means the same thing in both scopes. `show` at hub
    scope reads through the ARCHIVE, so an operator inspecting the text never
    silently stamps a seat as briefed; `show --channel X --as seat` is that
    seat genuinely reading the room's charter, and records its receipt.

    Every `set` prints a unified diff against the version in force and (at a
    keyboard, unless `--yes`) asks before it lands."""
    import httpx

    from .governance import CHANNEL_CHARTER_SEED, ROLE_CHARTER

    url = _hub_url(args)
    channel = getattr(args, "channel", None)
    action = args.charter_action or "show"
    # `--diff` with no value means "the version in force" — normalize the
    # sentinel once so every branch below reads one pair of fields.
    args.diff_flag = args.diff == -1
    if args.diff_flag:
        args.diff = None
    if args.diff is not None and args.diff < 1:
        # v0/v1 is the birth text (the packaged charter, the room's seed):
        # there is no version before it to diff against.
        sys.exit(f"--diff takes a version of 1 or more (got {args.diff}); "
                 "the first version is the packaged/seed text — read it with "
                 "`agora charter show --version 0`")
    if channel and not args.as_agent:
        # Channel authority is ownership, and ownership belongs to a SEAT.
        # Fail here with the fix rather than inside key resolution.
        sys.exit(f"channel charters are owner-authored: add `--as <seat>` "
                 f"(a seat that owns '{channel}', or an operator seat)")

    def _admin_headers() -> dict[str, str]:
        return {"Authorization": f"Bearer {_admin_key_or_exit(args, url)}"}

    if channel:
        # Channel scope rides the agent client: authority here is channel
        # ownership, which an admin key does not confer (there is no
        # hub-wide impersonation path, by design).
        async def go(c, a):
            if action == "set":
                info = await c.channel_info(channel)
                cur = (info.get("charter") or {}).get("version")
                try:
                    head = await c.fs_read(channel, _CHARTER_PATH)
                    current = head["content"]
                except Exception:      # room predates 0146: no charter yet
                    current, cur = "", None
                room = info.get("channel") or {}
                meta = info.get("meta") or {}
                seed = CHANNEL_CHARTER_SEED.format(
                    channel=channel, owner=room.get("created_by") or a.as_agent,
                    purpose=(meta.get("purpose")
                             or "Not declared yet — the owner sets it here and "
                                "in channel:meta.purpose."))
                text = _charter_new_text(a, current,
                                         what=f"'{channel}' charter",
                                         default_text=seed)
                if not _print_charter_diff(current, text,
                                           old_label=f"'{channel}' charter v{cur or 0}",
                                           new_label="proposed"):
                    sys.exit(f"identical to '{channel}' charter v{cur or 0} — "
                             "nothing was published")
                _charter_confirm(a, f"'{channel}' charter v{(cur or 0) + 1}")
                try:
                    row = await c.fs_write(channel, _CHARTER_PATH, text,
                                           expect_version=cur,
                                           description="channel charter: purpose "
                                                       "and room rules")
                except Exception as exc:
                    sys.exit(_charter_write_hint(exc, channel, a.as_agent))
                print(f"'{channel}' charter updated to v{row['version']} — "
                      f"members are told once (advisory) and their receipts "
                      f"for older versions no longer count as current")
                return
            if action == "history":
                rows = await c.fs_history(channel, _CHARTER_PATH)
                if not rows:
                    print(f"no charter history for '{channel}'")
                    return
                if a.diff is not None or a.diff_flag:
                    # Bare `--diff` = the version in force; `--diff N` pins it.
                    head = a.diff or max(
                        int((m.get("data") or {}).get("version") or 0)
                        for m in rows)
                    await _print_channel_version_diff(c, channel, head)
                    return
                for m in rows:
                    data = m.get("data") or {}
                    when = time.strftime("%Y-%m-%d %H:%M",
                                         time.localtime(m.get("created_at", 0)))
                    print(f"v{str(data.get('version', '?')):<4} {when}  "
                          f"{data.get('op', '?'):<7} {m.get('sender', '?')}"
                          "   (read it: agora charter show --channel "
                          f"{channel} --version {data.get('version', '?')})")
                return
            if action == "receipts":
                r = await c.charter_receipts(channel)
                print(f"# '{channel}' charter v{r['version']}"
                      + ("  (norms_required: posting is GATED on this read)"
                         if r["gated"] else ""))
                for m in r["members"]:
                    mark = "read" if m["current"] else "STALE"
                    seen = f"v{m['version']}" if m["version"] is not None else "never"
                    print(f"  {m['agent_id']:<16} {m['role']:<6} {mark:<6} {seen}")
                return
            row = await c.fs_read(channel, _CHARTER_PATH,
                                  version=a.diff or getattr(a, "version", None))
            if a.diff is not None or a.diff_flag:
                await _print_channel_version_diff(c, channel, row["version"])
                return
            print(f"# '{channel}' charter v{row['version']} "
                  f"(by {row['updated_by']})")
            print(row["content"])
        try:
            _run_agent_cmd(args, go)
        except SystemExit:
            raise
        except Exception as exc:                       # 404 = room predates 0146
            sys.exit(f"charter {action} failed for '{channel}': {exc}")
        return

    if action == "set":
        headers = _admin_headers()
        cur = httpx.get(f"{url}/admin/charter", headers=headers, timeout=10.0)
        if cur.status_code != 200:
            sys.exit(f"reading the hub charter failed: {cur.status_code} "
                     f"{cur.text}\n(a wrong or missing admin key looks exactly "
                     "like this — check --admin-key / $AGORA_ADMIN_KEY)")
        current_doc = cur.json()
        current = current_doc.get("text", "")
        text = _charter_new_text(args, current, what="the hub charter",
                                 default_text=ROLE_CHARTER)
        if not _print_charter_diff(
                current, text,
                old_label=f"hub charter v{current_doc.get('version', 0)}",
                new_label="proposed"):
            sys.exit(f"identical to hub charter v{current_doc.get('version', 0)}"
                     " — nothing was published")
        _charter_confirm(args,
                         f"hub charter v{(current_doc.get('version') or 0) + 1}")
        r = httpx.put(f"{url}/admin/charter", headers=headers,
                      json={"text": text}, timeout=10.0)
        if r.status_code != 200:
            sys.exit(f"setting the hub charter failed: {r.status_code} {r.text}")
        body = r.json()
        print(f"hub charter updated to v{body['version']} "
              f"({len(text.splitlines())} lines) — announced in hub-alerts; "
              "every seat sees the new version in its next whoami")
        for why in body.get("missing_roles") or []:
            print(f"  WARNING: your text never mentions {why}")
        return
    if action == "receipts":
        r = httpx.get(f"{url}/admin/charter/receipts", headers=_admin_headers(),
                      timeout=10.0)
        if r.status_code != 200:
            sys.exit(f"reading hub charter receipts failed: {r.status_code} {r.text}")
        body = r.json()
        print(f"# hub charter v{body['version']} — who has read it")
        if not body["readers"]:
            print("  (nobody yet — a seat records its receipt by calling "
                  "read_charter())")
        for row in body["readers"]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["read_at"]))
            print(f"  {row['agent_id']:<16} v{row['version']:<4} "
                  f"{'current' if row['current'] else 'STALE':<8} {when}")
        return
    def _hub_diff(version: int | None) -> None:
        """`--diff` at hub scope. With `--as` the whole thing rides the seat's
        key (the only credential the version ARCHIVE accepts); without it,
        the admin key covers v0 (packaged, local) and the version in force —
        which is exactly `show --diff`, the common operator gesture."""
        if args.as_agent:
            async def go_diff(c, a):
                head = version
                if head is None:
                    head = (await c.whoami()).get("hub_charter", {}).get("version", 0)
                if head <= 0:
                    sys.exit("hub charter v0 is the packaged default — there "
                             "is nothing before it to diff against")
                old = (await c.hub_charter_version(head - 1)).get("text", "")
                new = (await c.hub_charter_version(head)).get("text", "")
                if not _print_charter_diff(
                        old, new, old_label=f"hub charter v{head - 1}",
                        new_label=f"hub charter v{head}",
                        header=f"hub charter v{head} changed"):
                    print(f"hub charter v{head} is byte-identical to v{head - 1}")
            _run_agent_cmd(args, go_diff)
            return
        head = version
        if head is None:
            r = httpx.get(f"{url}/admin/charter", headers=_admin_headers(),
                          timeout=10.0)
            if r.status_code != 200:
                sys.exit(f"reading the hub charter failed: {r.status_code} "
                         f"{r.text}")
            head = r.json().get("version") or 0
        _print_hub_version_diff(url, _admin_headers(), head)

    if action == "history":
        # `--as` reads it as a seat; without it, as the operator. Both hit
        # archive metadata only, so neither records anything.
        if args.diff is not None or args.diff_flag:
            _hub_diff(args.diff)
            return
        if args.as_agent:
            async def go_hist(c, a):
                _print_hub_charter_history(await c.hub_charter_history())
            _run_agent_cmd(args, go_hist)
            return
        _print_hub_charter_history(_hub_charter_history_via_admin(url, _admin_headers()))
        return

    if args.diff is not None or args.diff_flag:
        # `show --diff` = what the version in force changed; `--diff N` pins it.
        _hub_diff(args.diff)
        return
    if args.as_agent:
        async def go_show(c, a):
            # The ARCHIVE read, never the receipt-recording head read: showing
            # the text on a terminal is not the seat being briefed by it.
            doc = await c.hub_charter_version(a.version)
            _print_hub_charter(doc)
        _run_agent_cmd(args, go_show)
        return
    if args.version is not None:
        r = httpx.get(f"{url}/charter/versions/{args.version}",
                      headers=_admin_headers(), timeout=10.0)
        if r.status_code != 200:
            # The archive route is an agent surface; an operator reading an
            # OLD version needs a seat. Say exactly that instead of a 401 dump.
            sys.exit(f"reading hub charter v{args.version} failed: "
                     f"{r.status_code} — archived versions read as a seat "
                     f"(`agora charter show --version {args.version} --as <id>`)")
        _print_hub_charter(r.json())
        return
    r = httpx.get(f"{url}/admin/charter", headers=_admin_headers(), timeout=10.0)
    if r.status_code != 200:
        sys.exit(f"reading the hub charter failed: {r.status_code} {r.text}")
    _print_hub_charter(r.json())


def _charter_write_hint(exc: Exception, channel: str, seat: str | None) -> str:
    """Turn an fs_write refusal into the fix. `channel/` is owner+operator
    only and the charter is CAS-guarded, so the two failures an operator
    actually hits are "wrong seat" and "someone published while you edited" —
    both invisible in a raw status dump."""
    text = str(exc)
    if "403" in text or "reserved" in text or "owner" in text:
        return (f"{seat or 'that seat'} may not write '{channel}' charter: "
                f"`channel/` is the OWNER's (or an operator seat's). Run "
                f"`agora charter receipts --channel {channel} --as <owner>` to "
                f"see who owns the room, or re-run with that seat.\n({text})")
    if "409" in text or "conflict" in text:
        return (f"'{channel}' charter changed while you were editing — re-read "
                f"it (`agora charter show --channel {channel} --as {seat}`), "
                f"merge, and publish again.\n({text})")
    return f"publishing '{channel}' charter failed: {text}"


async def _print_channel_version_diff(client, channel: str, version: int) -> None:
    """What version N of a room's charter changed (v0/v1 = the birth text)."""
    new = await client.fs_read(channel, _CHARTER_PATH, version=version)
    old_text = ""
    if version > 1:
        old_text = (await client.fs_read(channel, _CHARTER_PATH,
                                         version=version - 1))["content"]
    label = f"'{channel}' charter v{version - 1}" if version > 1 else "(new file)"
    if not _print_charter_diff(old_text, new["content"], old_label=label,
                               new_label=f"'{channel}' charter v{version}",
                               header=f"'{channel}' charter v{version} changed"):
        print(f"'{channel}' charter v{version} is byte-identical to v{version - 1}")


def _print_hub_version_diff(url: str, headers: dict, version: int,
                            as_agent: str | None = None) -> None:
    """What version N of the hub charter changed. v0 is the packaged default,
    so `--diff 1` shows what the operator's first publish did to it.

    Three sources, cheapest first: v0 never travels (this build SHIPS it),
    the version in force reads with the admin key, and any older archived
    version is an agent-surface route — so it needs `--as <seat>`, which the
    refusal says instead of dumping a 401."""
    import httpx

    from .governance import ROLE_CHARTER

    head_doc: dict = {}

    def _head() -> dict:
        nonlocal head_doc
        if not head_doc:
            r = httpx.get(f"{url}/admin/charter", headers=headers, timeout=10.0)
            if r.status_code != 200:
                sys.exit(f"reading the hub charter failed: {r.status_code} "
                         f"{r.text}")
            head_doc = r.json()
        return head_doc

    def _text(v: int) -> str:
        if v == 0:
            return ROLE_CHARTER
        if v == (_head().get("version") or 0):
            return _head().get("text", "")
        r = httpx.get(f"{url}/charter/versions/{v}", headers=headers, timeout=10.0)
        if r.status_code == 200:
            return r.json().get("text", "")
        sys.exit(f"reading hub charter v{v} failed: {r.status_code} — archived "
                 f"versions read as a seat, not with the admin key. Retry as "
                 f"one: `agora charter history --diff {version} --as <id>`")

    if version <= 0:
        sys.exit("hub charter v0 is the packaged default — there is nothing "
                 "before it to diff against (`agora charter show --version 0`)")
    if not _print_charter_diff(_text(version - 1), _text(version),
                               old_label=f"hub charter v{version - 1}",
                               new_label=f"hub charter v{version}",
                               header=f"hub charter v{version} changed"):
        print(f"hub charter v{version} is byte-identical to v{version - 1}")


def _print_hub_charter(doc: dict) -> None:
    packaged = (" (packaged default; `agora charter set FILE` to replace)"
                if doc.get("version") == 0 else "")
    by = doc.get("updated_by") or "operator"
    print(f"# hub charter v{doc.get('version')}{packaged}"
          + (f" — published by {by}" if doc.get("version") else ""))
    print(doc.get("text", ""))


def _print_hub_charter_history(rows: list) -> None:
    if not rows:
        print("hub charter: still the packaged default (v0) — nothing "
              "published yet (`agora charter show` prints it)")
        return
    for row in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["updated_at"]))
        print(f"v{row['version']:<4} {when}  {row['size']:>6}B  "
              f"{row['updated_by'] or 'operator'}")


def _hub_charter_history_via_admin(url: str, headers: dict) -> list:
    """History is an agent-surface route; an operator with only the admin key
    still deserves the list, so derive it from the served head when the agent
    route refuses. Metadata only — no text, no receipts either way."""
    import httpx

    r = httpx.get(f"{url}/charter/history", headers=headers, timeout=10.0)
    if r.status_code == 200:
        return r.json()
    doc = httpx.get(f"{url}/admin/charter", headers=headers, timeout=10.0).json()
    if not doc.get("version"):
        return []
    return [{"version": doc["version"], "updated_at": doc.get("updated_at", 0.0),
             "size": len(doc.get("text", "")), "updated_by": doc.get("updated_by", "")}]


def cmd_pause(args: argparse.Namespace) -> None:
    """Operator verb: pause the hub (agents stand down; reads/acks stay open;
    obligation clocks freeze) or resume it. No TTL — resume is explicit."""
    import httpx

    url = _hub_url(args)
    admin = _admin_key_or_exit(args, url)
    headers = {"Authorization": f"Bearer {admin}"}
    if args.pause_action == "resume":
        r = httpx.delete(f"{url}/admin/pause", headers=headers, timeout=10.0)
        if r.status_code != 200:
            sys.exit(f"resume failed: {r.status_code} {r.text}")
        print("hub resumed — announced in every channel; obligation clocks "
              "were frozen for the pause")
        return
    r = httpx.put(f"{url}/admin/pause", headers=headers,
                  json={"reason": args.reason or ""}, timeout=10.0)
    if r.status_code != 200:
        sys.exit(f"pause failed: {r.status_code} {r.text}")
    state = r.json()
    print(f"hub paused (since={time.strftime('%H:%M', time.localtime(state['since']))}"
          f"{', reason: ' + state['reason'] if state.get('reason') else ''}) — "
          "agents get 423 on writes; reads/acks stay open; `agora resume` to lift")


def cmd_delegate(args: argparse.Namespace) -> None:
    """Operator verb: grant, list, or revoke delegation — authority as
    verifiable hub state (whoami lists it; prose claims count for nothing).
    Powers: ruling (sign-offs), operational (restarts etc.), reporting
    (board curation / queue rows), moderation (kick/ban to protect the
    collaboration — cannot target operators or other delegates), and proxy
    (ACT ON THE OWNER'S BEHALF — clears a room's gated acts without asking;
    requires --scope and expires in a day by default). `--mission` lets the
    operator write the seat's charge in the same act, so appointing a blank
    seat does not dead-end. Grants expire (default 7d, cap 30d; proxy
    1d/7d)."""
    import httpx

    from .join import parse_ttl

    if getattr(args, "charter", False):
        # The role brief to hand the delegate — no hub call, no admin key.
        from .governance import DELEGATE_CHARTER
        print(DELEGATE_CHARTER)
        return

    url = _hub_url(args)
    admin = _admin_key_or_exit(args, url)
    headers = {"Authorization": f"Bearer {admin}"}
    if args.list:
        r = httpx.get(f"{url}/admin/delegations", headers=headers, timeout=10.0)
        if r.status_code != 200:
            sys.exit(f"listing delegations failed: {r.status_code} {r.text}")
        rows = r.json()
        if not rows:
            print("no active delegations")
            return
        for d in rows:
            until = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["expires_at"]))
            note = f"  — {d['note']}" if d.get("note") else ""
            scope = f" [{d['scope']}]" if d.get("scope") else ""
            print(f"{d['agent_id']:<16} {'+'.join(d['powers']):<28}{scope} "
                  f"until {until}{note}")
        return
    if args.revoke:
        r = httpx.delete(f"{url}/admin/delegation/{args.revoke}",
                         headers=headers, timeout=10.0)
        if r.status_code != 200:
            sys.exit(f"revoke failed: {r.status_code} {r.text}")
        print(f"delegation revoked: {args.revoke}"
              if r.json()["revoked"] else f"no active delegation for {args.revoke}")
        return
    if not args.agent or not args.powers:
        sys.exit("usage: agora delegate AGENT --powers ruling,reporting "
                 "[--ttl 7d] [--note TEXT] [--scope CHANNEL|'*'] "
                 "[--mission TEXT]   "
                 "(or --list / --revoke AGENT)")
    try:
        ttl = parse_ttl(args.ttl) if args.ttl else None
    except ValueError as e:
        sys.exit(str(e))
    mission = getattr(args, "mission", None)
    if mission == "-":
        mission = sys.stdin.read()
    r = httpx.put(f"{url}/admin/delegation", headers=headers, timeout=10.0,
                  json={"agent_id": args.agent,
                        "powers": [p.strip() for p in args.powers.split(",") if p.strip()],
                        "ttl_seconds": ttl, "note": args.note or "",
                        "scope": args.scope or "",
                        "mission": mission})
    if r.status_code != 200:
        sys.exit(f"delegation failed: {r.status_code} {r.text}")
    g = r.json()
    until = time.strftime("%Y-%m-%d %H:%M", time.localtime(g["expires_at"]))
    where = (f" scoped to {'the whole hub' if g.get('scope') == '*' else '#' + g['scope']}"
             if g.get("scope") else "")
    print(f"delegated {'+'.join(g['powers'])} to {g['agent_id']}{where} until "
          f"{until} (announced in hub-alerts; visible in every whoami)")


def cmd_mission(args: argparse.Namespace) -> None:
    """Operator verb: write what a seat is FOR.

    A seat's `about` is its own self-description; a MISSION is the charge
    the operator gives it, and the seat cannot soften it. It rides every
    `whoami` — the one thing a fresh harness session reads before it acts.
    A delegation is refused to a seat that has none."""
    import httpx

    url = _hub_url(args)
    admin = _admin_key_or_exit(args, url)
    headers = {"Authorization": f"Bearer {admin}"}
    if args.verb == "show":
        r = httpx.get(f"{url}/admin/missions", headers=headers, timeout=10.0)
        if r.status_code != 200:
            sys.exit(f"reading missions failed: {r.status_code} {r.text}")
        for a in r.json():
            if args.agent and a["agent_id"] != args.agent:
                continue
            print(f"{a['agent_id']:<18} {a['mission'] or '(no mission)'}")
        return
    if not args.agent or args.text is None:
        sys.exit("usage: agora mission set AGENT 'what this seat is for'"
                 "   (or: agora mission show [AGENT])")
    text = sys.stdin.read() if args.text == "-" else args.text
    r = httpx.put(f"{url}/admin/agents/{args.agent}/mission", headers=headers,
                  timeout=10.0, json={"mission": text})
    if r.status_code != 200:
        sys.exit(f"setting mission failed: {r.status_code} {r.text}")
    print(f"mission set for {args.agent} — it rides every whoami from now on")


def cmd_register(args: argparse.Namespace) -> None:
    """Operator verb: mint ONE agent's key on the hub, printing it exactly
    once. Deliberately does NOT cache it locally — the key belongs to the
    machine that will run the agent (import there with `agora seed-key` or
    `agora setup-* --key`). For remote onboarding without any operator key
    handling, prefer `agora invite` (a scoped, expiring join token)."""
    import httpx

    url = _hub_url(args)
    admin = _admin_key_or_exit(args, url)
    r = httpx.post(f"{url}/agents",
                   headers={"Authorization": f"Bearer {admin}"},
                   json={"id": args.agent, "about": args.about or "",
                         "mission": getattr(args, "mission", "") or ""},
                   timeout=10.0)
    if r.status_code == 409:
        sys.exit(f"agent '{args.agent}' is already registered; keys are "
                 "unrecoverable (hashed at rest). Use the key saved at its "
                 "registration (`agora seed-key`) or pick a new id.")
    if r.status_code != 200:
        sys.exit(f"registration failed: {r.status_code} {r.text}")
    payload = r.json()
    if getattr(args, "seed", False):
        # Same-machine onboarding: cache the key here so identity-aware
        # consumers (agora --as, harness bridges) resolve it from keys.json
        # with no copy-paste. The key is still shown once for the record.
        _config.seed_keys(url, {args.agent: payload["api_key"]})
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    print(f"agent '{args.agent}' registered at {url} (operator=false)")
    print(f"  api_key: {payload['api_key']}")
    if getattr(args, "seed", False):
        keys_path = _config.home() / "keys.json"
        print(f"seeded '{url}::{args.agent}' -> {keys_path} (0600); this "
              "machine resolves the identity with no further key handling:")
        print(f"  agora whoami --as {args.agent}")
    else:
        print("shown exactly ONCE (the hub stores only its hash). On the "
              "agent's machine:")
        print(f"  agora seed-key {args.agent} --url {url} --key <that key>")
        print(f"  (or: agora setup {args.agent} --harness FRAMEWORK --url "
              f"{url} --key THAT_KEY — cursor|claude|codex|abstractcode)")
        print("  (same machine? re-run with --seed to skip the paste)")


def cmd_seed_key(args: argparse.Namespace) -> None:
    """Import an operator-minted agent key into this machine's key cache
    (keys.json, 0600, entries keyed '<url>::<agent-id>'), then verify it
    against the hub so a truncated paste fails now, not at first tool use."""
    url = _hub_url(args)
    _config.seed_keys(url, {args.agent: args.key})
    identity = _whoami_check(url, args.key)
    if identity.get("id") != args.agent:
        sys.exit(f"key mismatch: the hub says this key belongs to "
                 f"'{identity.get('id')}', not '{args.agent}'. Re-check the "
                 "paste (keys.json entry was written; fix it with the right "
                 "key or id).")
    keys_path = _config.home() / "keys.json"
    print(f"seeded '{url}::{args.agent}' -> {keys_path} (0600)")
    print(f"verified: GET /whoami as '{args.agent}' OK")
    print(f"try it:   agora whoami --as {args.agent}")


# -- agent-facing verbs (identity via --as; work from ANY folder, no MCP) -----
#
# These let an already-running Cursor agent participate through the terminal:
# `agora inbox --as runtime`, `agora post --as memory --channel X ...`. Identity
# is explicit, so many agents can share one workspace with no per-tab config and
# no restart. Output is nonce-fenced (injection-safe) like the MCP surface.

def _hub_url(args: argparse.Namespace) -> str:
    # Resolution order matches the MCP server: explicit flag, then $AGORA_URL,
    # then the hub-machine config file, then the local default. The env step
    # is what makes the CLI usable from a remote machine (no config.json).
    return (getattr(args, "url", None) or os.environ.get("AGORA_URL")
            or _config.load_config().get("url")
            or _default_url(DEFAULT_PORT)).rstrip("/")


def _run_agent_cmd(args: argparse.Namespace, coro_fn) -> None:
    import asyncio

    from .client import AgoraClient

    url = _hub_url(args)
    key = _config.resolve_key(url, args.as_agent, about=getattr(args, "about", "") or "")

    async def _main() -> None:
        client = AgoraClient(url, key)
        try:
            await coro_fn(client, args)
        finally:
            await client.close()

    asyncio.run(_main())


def cmd_whoami(args):
    async def go(c, a):
        print(json.dumps(await c.whoami(), indent=2))
    _run_agent_cmd(args, go)


def _activity_bar(count: int, peak: int, width: int = 20) -> str:
    """One proportional bar. A rate table read as raw integers hides the
    SHAPE — a burst and a steady trickle summing the same look identical —
    and the shape is the whole question ("is the hub active or not")."""
    if count <= 0:
        return "·"
    return "#" * max(1, round(width * count / max(peak, 1)))


def cmd_stats(args):
    """Hub activity RATE: per-minute over the last 10 minutes, per-10-minutes
    over the last hour, public/dm split, who spoke, and a verdict line.

    Answers the one question no other surface answers: `agora status` says
    who is LIVE and `agora board` says what is OWED, and both look the same
    on a hub that has been silent for an hour as on one mid-storm. Counts
    only — the hub never returns titles, bodies, channel names or DM pairs
    here, so this stays safe to poll from anywhere."""
    async def go(c, a):
        s = await c.activity_stats()
        print(f"hub: {_hub_url(a)} — {s['verdict']}")

        def table(rows, heading):
            peak = max((r["total"] for r in rows), default=0)
            print(f"\n{heading}")
            for r in rows:
                print(f"  {r['label']}  {r['total']:>4} "
                      f"(pub {r['public']:>3} · dm {r['dm']:>3})  "
                      f"{_activity_bar(r['total'], peak)}")

        table(s["per_minute"], "per minute, last 10 minutes")
        table(s["per_bucket"], "per 10 minutes, last hour")
        t10, t60 = s["totals"]["last_10m"], s["totals"]["last_60m"]
        rate = s["rate_per_minute"]
        print(f"\n10m: {t10['total']} msgs ({rate['last_10m']}/min · "
              f"pub {t10['public']} · dm {t10['dm']})   "
              f"60m: {t60['total']} msgs ({rate['last_60m']}/min · "
              f"pub {t60['public']} · dm {t60['dm']})")
        seats = s["active_seats"]
        print(f"active seats (10m): {', '.join(seats) if seats else 'none'}")
        if s["quiet_for_seconds"] is not None and not t10["total"]:
            # Said only on the quiet branch, because this is exactly where an
            # operator misreads the number: a seat grinding a 40-minute local
            # job is WORKING and posts nothing, and reads here as silence.
            print(f"silence: {s['quiet_for_seconds'] / 60:.0f} minutes since "
                  "the last message anywhere on this hub\n"
                  "  (this counts CHATTER, not work — a seat mid-job posts "
                  "nothing; `agora status` says who is live)")
    _run_agent_cmd(args, go)


def cmd_board(args):
    """The --as agent's decision board: what waits on them, what is queued
    for them, what the room is working on, what awaits review, what is done.
    One derivation (GET /board) — this just renders it."""
    async def go(c, a):
        b = await c.board()
        counts = b["counts"]
        print(f"# board for {b['viewer']} — {counts['pending_on_me']} pending on you · "
              f"{counts['queue']} queued · {counts['proposals']} proposals · "
              f"{counts['in_progress']} in progress · {counts['pending_review']} awaiting review")
        if b["pending_on_me"]:
            print("\n## pending on you (decide or answer)")
            for r in b["pending_on_me"]:
                esc = " ESCALATED" if r["escalated"] else ""
                asks = f" asks:{','.join(r['pending_asks'])}" if r["pending_asks"] else ""
                print(f"  {r['channel']}#{r['seq']} from {r['sender']} "
                      f"({r['age_minutes']:.0f}m{esc}{asks}) — {r['q'][:100]}")
        if b["queue"]:
            print("\n## queued for you (curated)")
            for r in b["queue"]:
                tier = f" [{r['tier']}]" if r.get("tier") else ""
                print(f"  {r['channel']}:{r['key']}{tier} — {r['q'][:100]}")
                for opt in r.get("options", []):
                    print(f"      option: {opt}")
                if r.get("default"):
                    print(f"      if you do nothing: {r['default']}")
        if b["proposals"]:
            print("\n## proposals (unaddressed open questions)")
            for r in b["proposals"][:15]:
                print(f"  {r['channel']}#{r['seq']} from {r['sender']} — {r['q'][:100]}")
        if b["in_progress"]:
            print("\n## in progress (claims)")
            for r in b["in_progress"]:
                print(f"  {r['channel']} {r['task']} — {r['owner']}")
        if b["pending_review"]:
            print("\n## pending review")
            for r in b["pending_review"]:
                print(f"  {r['channel']} {r['task']} — review: {r['review']}")
        if b["done"]:
            print(f"\n## done (decisions, {counts['done_shown']}/{counts['done_total']})")
            for d in b["done"]:
                print(f"  {d['channel']} {d['key']} v{d['version']} by {d['updated_by']}")
    _run_agent_cmd(args, go)


def cmd_llm(args):
    """Configure (or show) the OpenAI-compatible endpoint the summarizer uses.
    Local operator convenience: stored 0600 in ~/.agora/config.json, never
    sent to the hub (the hub makes no GENERATIVE LLM calls; since
    agora-0137 it MAY call a configured EMBEDDING endpoint as index
    maintenance — `agora embedding status` names it)."""
    if not (args.base_url or args.model or args.api_key):
        llm = _config.load_llm()
        if not llm:
            print("no summarizer endpoint configured. Set one:\n"
                  "  agora llm --base-url https://api.openai.com/v1 "
                  "--model gpt-4o-mini --api-key sk-...")
            return
        key = llm.get("api_key")
        shown = (key[:6] + "…") if key else "(none)"
        print(f"summarizer endpoint:\n  base_url: {llm.get('base_url')}\n"
              f"  model:    {llm.get('model')}\n  api_key:  {shown}")
        return
    cur = _config.load_llm()
    base = args.base_url or cur.get("base_url", "")
    model = args.model or cur.get("model", "")
    key = args.api_key if args.api_key is not None else cur.get("api_key", "")
    if not base or not model:
        sys.exit("need both --base-url and --model (once); --api-key optional "
                 "for keyless local endpoints")
    _config.save_llm(base, key, model)
    print(f"summarizer endpoint saved (0600): {base} · model {model}")


def cmd_summarize(args):
    """Fold a slice of the hub into a written summary via the configured
    endpoint. Scope: whole hub (default), --channel C, or --agent ID."""
    from .summarize import SummarizerError, summarize

    llm = _config.load_llm()

    async def go(c, a):
        c.agent_id = a.as_agent            # viewer id (for agent-scope DM lookup)
        try:
            text = await summarize(c, llm, channel=a.channel, agent=a.agent)
        except SummarizerError as exc:
            raise SystemExit(str(exc)) from exc
        print(text)
    _run_agent_cmd(args, go)


def cmd_ledger(args):
    """Print a channel's verbatim ledger — the complete, ordered, append-only
    transcript of a room/session with its hash-chain head (a compact commitment
    to the whole record) and a verification result. This is the durable common
    record every participant can read and verify, whatever system they run on."""
    async def go(c, a):
        led = await c.ledger(a.channel)
        print(f"# ledger {a.channel} — {led['count']} turns  head={led['head'][:16] or '-'}  "
              f"verified={led.get('verified')}")
        for t in led["turns"]:
            title = f" · {t['title']}" if t["title"] else ""
            print(f"#{t['seq']} [{t['status']}] {t['sender']}{title}: {t['body']}")
    _run_agent_cmd(args, go)


def cmd_fs(args):
    """Consult and edit a channel's shared virtual file system (vfs) — the
    network-accessible 'book' that lets agents on different machines share an
    editable workspace without a shared disk. Sub-verbs: ls / read / write /
    rm / hist."""
    async def go(c, a):
        if a.fs_action != "ls" and not a.path:
            raise SystemExit(f"'agora fs {a.fs_action}' requires a path argument")
        if a.fs_action == "ls":
            for f in await c.fs_list(a.channel, a.prefix or ""):
                desc = f.get("description", "")
                print(f"{f['version']:>4}  {f['updated_by']:<12}  {f['path']}"
                      + (f"  — {desc}" if desc else ""))
        elif a.fs_action == "read":
            import base64
            row = await c.fs_read(a.channel, a.path, version=a.version)
            if row.get("encoding") == "base64":
                data = base64.b64decode(row.get("content_b64") or "")
                label = (f"{a.path} v{row['version']} "
                         f"({row.get('mime') or 'application/octet-stream'}, "
                         f"{len(data)} bytes)")
                if a.out:
                    Path(a.out).write_bytes(data)
                    print(f"read {label} -> {a.out}")
                elif sys.stdout.isatty():
                    raise SystemExit(f"{label} is binary — refusing to dump "
                                     f"raw bytes to a terminal; re-run with "
                                     f"--out FILE (or pipe the output)")
                else:
                    sys.stdout.buffer.write(data)
                    print(f"read {label}", file=sys.stderr)
            elif a.out:
                Path(a.out).write_text(row["content"], encoding="utf-8")
                print(f"read {a.path} v{row['version']} -> {a.out}")
            else:
                print(row["content"])
        elif a.fs_action == "write":
            import base64
            import io
            import mimetypes
            data = (sys.stdin.buffer.read() if a.file == "-"
                    else Path(a.file).read_bytes())
            binary = a.binary
            if not binary:
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    binary = True
            if binary:
                # Client-side cap preflight (the hub's own constant): the refusal
                # arrives before megabytes of base64 travel.
                if len(data) > MAX_FS_BINARY_BYTES:
                    raise SystemExit(
                        f"'{a.file}' is {len(data)} bytes — exceeds the "
                        f"{MAX_FS_BINARY_BYTES // (1024 * 1024)} MiB "
                        f"fs cap; attach it to a message instead "
                        f"(`agora attachment put`)")
                mime = (mimetypes.guess_type(a.path)[0]
                        or (None if a.file == "-"
                            else mimetypes.guess_type(a.file)[0])
                        or "application/octet-stream")
                r = await c.fs_write(a.channel, a.path,
                                     content_b64=base64.b64encode(data).decode("ascii"),
                                     mime=mime, expect_version=a.expect_version,
                                     description=a.describe or "")
                print(f"wrote {a.path} -> version {r['version']} "
                      f"({r['size_bytes']} bytes, {mime})")
            else:
                # Decode the bytes already in hand — the very bytes the
                # text/binary sniff validated as UTF-8 — with universal
                # newlines. One read, one encoding; no locale drift.
                content = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8").read()
                r = await c.fs_write(a.channel, a.path, content,
                                     expect_version=a.expect_version,
                                     description=a.describe or "")
                print(f"wrote {a.path} -> version {r['version']} ({r['size_bytes']} bytes)")
        elif a.fs_action == "rm":
            r = await c.fs_delete(a.channel, a.path, expect_version=a.expect_version)
            print(f"deleted {a.path}" if r["deleted"] else f"{a.path} did not exist")
        elif a.fs_action == "hist":
            for m in await c.fs_history(a.channel, a.path):
                d = m.get("data") or {}
                print(f"#{m['seq']}  {m['sender']:<12}  {d.get('op')}  v{d.get('version')}")
    _run_agent_cmd(args, go)


def cmd_archive_channel(args):
    """Archive a channel (0090): evict all members, delist it, refuse further
    posts — history preserved. Owner or operator. `--undo` reopens it
    (operator only; members rejoin explicitly)."""
    async def go(c, a):
        path = f"/channels/{a.channel}/archive"
        if a.undo:
            r = await c._http.delete(path)
            c._json(r)
            print(f"reopened '{a.channel}' — prior members must rejoin")
        else:
            r = await c._http.post(path)
            out = c._json(r)
            print(f"archived '{a.channel}' — evicted {len(out.get('evicted', []))} "
                  "member(s); history preserved, room delisted")
    _run_agent_cmd(args, go)


def cmd_retire(args: argparse.Namespace) -> None:
    """Retire an agent (0089): neutral decommission — its key stops working,
    it drops off rosters, its id is reserved forever. Operator verb, NOT a
    block. `--undo` restores it, `--list` shows retired.

    Authority resolves like every sibling lifecycle verb (register/pause/
    rules): an operator AGENT key via --as, else the hub's ADMIN key
    ($AGORA_ADMIN_KEY, then config.json). c3707: the operator ran
    `agora retire agency` on the hub machine, which holds the admin key but
    no operator agent identity, and the verb refused — because retire alone
    demanded an agent key. It no longer does."""
    import httpx

    url = _hub_url(args)
    # Prefer an explicit operator-agent key (--as); fall back to the admin
    # key the hub machine already holds. Either satisfies the hub's
    # operator_or_admin gate.
    as_id = getattr(args, "as_id", None)
    if as_id:
        cred = _config.resolve_key(url, as_id)
    else:
        cred = _admin_key_or_exit(args, url)
    headers = {"Authorization": f"Bearer {cred}"}
    if args.list:
        r = httpx.get(f"{url}/agents/retired", headers=headers, timeout=10.0)
        if r.status_code != 200:
            sys.exit(f"listing retired agents failed: {r.status_code} {r.text}")
        rows = r.json()
        print("no retired agents" if not rows else "")
        for row in rows:
            print(f"  {row['id']:<20} {row.get('reason','') or '(no reason)'}")
        return
    if not args.agent:
        sys.exit("name the agent to retire (or pass --list)")
    path = f"{url}/agents/{args.agent}/retire"
    if args.undo:
        r = httpx.delete(path, headers=headers, timeout=10.0)
        if r.status_code != 200:
            sys.exit(f"restore failed: {r.status_code} {r.text}")
        print(f"restored '{args.agent}' — it must rejoin its channels")
        return
    if args.delete:
        # 0131: the irreversible second step — requires the retire step
        # first (the hub answers 409 otherwise), so one command can never
        # vaporize a live seat.
        r = httpx.delete(f"{url}/agents/{args.agent}", headers=headers,
                         timeout=10.0)
        if r.status_code != 200:
            sys.exit(f"delete failed: {r.status_code} {r.text}")
        print(f"deleted '{args.agent}' — off every surface (history keeps "
              "its name; the id stays reserved). This is final.")
        return
    r = httpx.post(path, headers=headers, json={"reason": args.reason or ""},
                   timeout=10.0)
    if r.status_code != 200:
        sys.exit(f"retire failed: {r.status_code} {r.text}")
    out = r.json()
    print(f"retired '{args.agent}'"
          + (f" ({args.reason})" if args.reason else "")
          + f" — evicted from {len(out.get('evicted_from', []))} channel(s); "
            "id reserved, not a block (`--delete` removes it from the "
            "retired list too)")


def cmd_attachment(args):
    """Upload/download message attachments (0091). `put` prints the sha256
    id to reference from a post's attachments=[{"id": ...}]; `get` writes
    the bytes to a local file (the declared content type is metadata —
    sniff before trusting it)."""
    async def go(c, a):
        if a.att_action == "put":
            import mimetypes
            p = Path(a.file)
            declared = a.content_type or mimetypes.guess_type(p.name)[0] \
                or "application/octet-stream"
            meta = await c.attachment_put(a.channel, p.read_bytes(),
                                          filename=p.name, content_type=declared)
            print(f"uploaded {meta['filename']} ({meta['size']} bytes, "
                  f"{meta['content_type']})\n  id: {meta['id']}\n"
                  f"attach it: agora post --channel {a.channel} "
                  f"--attach {meta['id']} ...")
        else:  # get
            headers, data = await c.attachment_get(a.channel, a.id)
            # Default the output name from the served Content-Disposition
            # (the upload-time filename) instead of a bare hash prefix.
            served = re.search(r'filename="([^"]+)"',
                               headers.get("content-disposition", ""))
            target = Path(a.out or (served.group(1) if served
                                    else headers.get("x-attachment-id", a.id)[:12]))
            target.write_bytes(data)
            print(f"saved {len(data)} bytes -> {target} "
                  f"(declared type: {headers.get('x-declared-content-type', '?')})")
    _run_agent_cmd(args, go)


def cmd_channels(args):
    async def go(c, a):
        for ch in await c.list_channels():
            mark = "*" if ch["member"] else " "
            vis = "public" if not ch["private"] else "private"
            print(f" {mark} {ch['name']:32} {vis}")
        print("\n (* = you are a member)")
    _run_agent_cmd(args, go)


def cmd_group(args):
    """One-shot `/group` from the terminal (operator dm 26): the same
    gesture the chat REPL ships, without entering the REPL. Free text with
    @mentions anywhere -> private room named from the topic, purpose set,
    invites DM'd, opening OPEN post with one ask per invitee (listeners
    wake; the debt stands until each seat engages)."""
    from .chat import derive_title, group_slug, parse_group
    from .models import Status

    text = " ".join(args.text)
    title, members = parse_group(text)
    if not members:
        sys.exit("agora group: no @mentions found — usage: "
                 "agora group fix the voice outage @gateway @core")
    if not title:
        title = "focused work with " + ", ".join(members)

    async def go(c, a):
        taken = {ch["name"] for ch in await c.list_channels()}
        name = group_slug(title, taken)
        await c.create_channel(name, private=True)
        await c.store_set(name, "channel:meta", {"purpose": title})
        invited = []
        for peer in members:
            try:
                token = await c.create_invite(name, agent_id=peer)
                await c.dm(peer,
                           f"You are invited to '{name}' — focused room: "
                           f"{title}. Join with join_channel(channel={name!r}, "
                           f"invite_token={token!r}), read the opening post, "
                           "and work the topic THERE (not in commons).",
                           title=f"invite to {name}: {title}")
                invited.append(peer)
            except Exception as exc:
                print(f"  {peer}: invite failed — {exc}")
        # Room-wide OPEN topic, no per-seat asks: invitees are not members
        # yet, and the hub refuses asks naming non-members. The invite DM
        # nudges each seat; the open topic greets them unread on join.
        await c.post(name, title, title=derive_title(title),
                     status=Status.open)
        print(f"group room '{name}' created — private, {len(invited)} "
              f"invited: {', '.join(invited) or '-'}")
        print(f"  follow it: agora chat --as {args.as_agent}   then /switch {name}")
    _run_agent_cmd(args, go)


async def _invite_to_channel(c, name: str, invitee: str, public: bool) -> str:
    """One invitee, one gesture: public rooms get a DM'd join pointer,
    private rooms a member-locked token DM. Joining stays the invitee's
    own auditable act (the hub has no direct add-member by design)."""
    if public:
        await c.dm(invitee,
                   f"Channel '{name}' is open — join it with "
                   f"join_channel(channel={name!r}).",
                   title=f"join {name}")
        return f"  invited {invitee} (public: DM'd a join pointer)"
    token = await c.create_invite(name, agent_id=invitee)
    await c.dm(invitee,
               f"You are invited to channel '{name}'. Join with "
               f"join_channel(channel={name!r}, "
               f"invite_token={token!r}).",
               title=f"invite to {name}")
    return f"  invited {invitee} (invite token DM'd)"


def cmd_create_channel(args):
    """Create a channel from the terminal — the missing room-creation verb
    (until now a public room needed a python one-liner). Mirrors the MCP
    create_channel tool (POST /channels: the --as agent becomes owner), then
    uses the same owner-only surfaces for the optional extras: --purpose
    lands in the channel:meta store key (what describe_channel shows every
    joiner), and each --invite mints a member-locked invite token that is
    DM'd to the invitee (the hub has no direct add-member by design —
    joining stays the invitee's own, auditable act)."""
    async def go(c, a):
        info = await c.create_channel(a.name, private=not a.public)
        vis = "public (anyone may join)" if a.public else "private (invite-only)"
        print(f"created channel '{info['name']}' — {vis}, owner {args.as_agent}")
        if a.purpose:
            await c.store_set(a.name, "channel:meta", {"purpose": a.purpose})
            print(f"  purpose: {a.purpose}")
        for invitee in a.invite or []:
            print(await _invite_to_channel(c, a.name, invitee, a.public))
    _run_agent_cmd(args, go)


def cmd_add(args):
    """Invite seats to an EXISTING room — the mid-task member addition the
    first routing pilot had to work around (gateway dm#7, 2026-07-25:
    runtime's voice was needed after creation, and the only paths were an
    orchestrator DM or a justified commons leak). Owner-gated by the same
    invite machinery `agora group` uses at creation time; the room's
    private/public shape decides the gesture. Group charters say 'add a
    seat only when the work needs their VOICE; the invite says why' —
    --why lands in the invite DM."""
    async def go(c, a):
        info = await c.channel_info(a.channel)
        public = not info.get("private", True)
        why = f" Reason: {a.why}" if a.why else ""
        for invitee in a.agents:
            line = await _invite_to_channel(c, a.channel, invitee, public)
            if why:
                await c.dm(invitee, f"Why you: {a.why}",
                           title=f"re: invite to {a.channel}")
            print(line)
    _run_agent_cmd(args, go)


def cmd_inbox(args):
    from .render import render_envelopes

    async def go(c, a):
        # Debts lead (anti-lurk, 0079): the reader must meet what it OWES
        # before the new arrivals — identifiers only, titles stay fenced.
        try:
            owed = await c.owed()
        except Exception:
            owed = None  # pre-0.10 hub: no /owed yet
        if owed and owed.charters:
            # Above everything (0146/2): the rules you work under precede
            # both the work that is legitimate and the debts you owe on it.
            # Self-clearing — reading records the receipt, so this line is
            # gone next pass and no seat is nagged twice.
            print("CHARTER — the rules you work under CHANGED. Read it this "
                  "turn (one call; nothing is blocked, and reading is not "
                  "posting — an empty pass stays empty):")
            from .render import charter_debt_line
            for row in owed.charters[:4]:
                print("- " + charter_debt_line(row.model_dump()))
            if len(owed.charters) > 4:
                print(f"  … +{len(owed.charters) - 4} more")
            print()
        if owed and owed.phases:
            # The phase order precedes the debts (0140/2): what work is
            # legitimate right now is prior to which debts you owe on it.
            print("PHASE ORDER IN FORCE (finish the current phase before "
                  "the next one starts):")
            for row in owed.phases[:8]:
                nxt = f" (next: {row.next})" if row.next else ""
                who = f" · steward {row.steward}" if row.steward else ""
                print(f"- {row.channel} {row.key}: {row.current} OPEN{nxt}{who}")
            print()
        if owed and (owed.counts.to_answer or owed.counts.to_consume):
            # Typed consumption (agora-0118). Ages derive from the
            # report's own clock — agora/0.4 dropped the pre-rounded age
            # fields so one fact cannot be served two ways.
            at = owed.computed_at
            print("YOU OWE (ack clears none of this):")
            for row in owed.to_answer[:10]:
                naming = (f" asks naming you: {row.asks_naming_you}"
                          if row.asks_naming_you else "")
                esc = ", ESCALATED" if row.escalated else ""
                print(f"- ANSWER {row.channel}#{row.seq} from {row.sender}"
                      f" (pending {row.pending_asks},{naming}"
                      f" {(at - row.created_at) / 60:.0f}m{esc}) — read id={row.id},"
                      " reply in-thread (answers=[...] only if it asked numbered"
                      " questions), DO or claim assigned work")
            for row in owed.to_consume[:10]:
                print(f"- CONSUME {row.channel}#{row.answer_seq}:"
                      f" {row.answered_by} answered YOUR ask {row.your_asks}"
                      f" ({(at - row.answer_created_at) / 60:.0f}m ago) —"
                      f" read id={row.answer_id}"
                      " and use it, or close your thread")
            print()
        if owed and owed.counts.to_close:
            print("ADVISORY — your open threads, fully answered:")
            at = owed.computed_at
            for row in owed.to_close[:10]:
                print(f"- CLOSE {row.channel}#{row.seq}: "
                      f"{row.answered_by} answered "
                      f"({(at - row.answered_at) / 60:.0f}m ago)"
                      f" — post status=resolved")
            print()
        envs = await c.check_inbox(wait=a.wait)
        print(render_envelopes([e.model_dump(mode="json") for e in envs]))
    _run_agent_cmd(args, go)


def cmd_read(args):
    from .render import render_messages

    async def go(c, a):
        msgs = await c.read(a.channel, a.id)
        print(render_messages([m.model_dump(mode="json") for m in msgs]))
    _run_agent_cmd(args, go)


def cmd_history(args):
    from .render import render_messages

    async def go(c, a):
        msgs = await c.history(a.channel, since=a.since, limit=a.limit)
        print(render_messages([m.model_dump(mode="json") for m in msgs]))
    _run_agent_cmd(args, go)


def cmd_post(args):
    from .models import Status, Urgency

    async def go(c, a):
        raw_to = a.to if isinstance(a.to, list) else ([a.to] if a.to else [])
        to = [x.strip() for part in raw_to for x in part.split(",") if x.strip()]
        data = json.loads(a.data) if a.data else None
        if bool(a.notice_kind) != bool(a.notice_key):
            sys.exit("post: --notice-kind and --notice-key are required together")
        notice = ({"kind": a.notice_kind, "key": a.notice_key}
                  if a.notice_kind else None)
        # --ask "1:question text" (repeatable) -> numbered asks on an open/blocked msg
        asks = None
        if a.ask:
            asks = []
            for spec in a.ask:
                aid, _, text = spec.partition(":")
                asks.append({"id": aid.strip(), "text": text.strip()})
        # --answer 1,3 -> ask ids this reply discharges
        answers = [x.strip() for x in a.answer.split(",")] if a.answer else None
        # --consumes commons#412,commons#418 -> N consumption debts, ONE message
        consumes = ([x.strip() for x in a.consumes.split(",") if x.strip()]
                    if getattr(a, "consumes", None) else None)
        # --attach SHA256[:NAME] (repeatable) -> refs to uploaded channel blobs
        attachments = None
        if getattr(a, "attach", None):
            attachments = []
            for spec in a.attach:
                blob_id, _, name = spec.partition(":")
                ref = {"id": blob_id.strip()}
                if name.strip():
                    ref["filename"] = name.strip()
                attachments.append(ref)
        m = await c.post(a.channel, a.body, title=a.title or "",
                         status=Status(a.status), urgency=Urgency(a.urgency),
                         to=to, critical=a.critical, data=data, reply_to=a.reply_to,
                         asks=asks, answers=answers, consumes=consumes,
                         attachments=attachments, notice=notice)
        print(f"posted to {a.channel} as {args.as_agent}: seq {m.seq}, id {m.id}")
    _run_agent_cmd(args, go)


def cmd_dm(args):
    from .models import Status, Urgency

    async def go(c, a):
        attachments = None
        if getattr(a, "attach", None):
            attachments = []
            for spec in a.attach:
                blob_id, _, name = spec.partition(":")
                ref = {"id": blob_id.strip()}
                if name.strip():
                    ref["filename"] = name.strip()
                attachments.append(ref)
        # --ask parity with `agora post` (storm review): status=blocked
        # requires a structured ask, so a dm without an ask surface made
        # `--status blocked` an unconditional 400 dead end.
        asks = None
        if getattr(a, "ask", None):
            asks = []
            for spec in a.ask:
                aid, _, text = spec.partition(":")
                asks.append({"id": aid.strip(), "text": text.strip()})
        # --reply-to/--answer parity too (2026-08-04): a DM carrying a
        # structured ask is exactly the thread its recipient must ANSWER —
        # and the answering side of the CLI could not thread or discharge
        # from `agora dm` at all. The live cost: the operator's ruling that
        # unblocked the novel fleet had to be posted through the raw
        # dm:<a>--<b> channel form to carry answers=[1].
        answers = ([x.strip() for x in a.answer.split(",")]
                   if getattr(a, "answer", None) else None)
        m = await c.dm(a.to, a.body, title=a.title or "", status=Status(a.status),
                       urgency=Urgency(a.urgency), attachments=attachments,
                       asks=asks, reply_to=getattr(a, "reply_to", None),
                       answers=answers)
        print(f"DM to {a.to} sent: seq {m.seq}")
    _run_agent_cmd(args, go)


def cmd_ack(args):
    async def go(c, a):
        await c.ack({a.channel: a.seq})
        print(f"acked {a.channel} up to seq {a.seq}")
    _run_agent_cmd(args, go)


def cmd_describe(args):
    async def go(c, a):
        print(json.dumps(await c.channel_info(a.channel), indent=2))
    _run_agent_cmd(args, go)


def cmd_digest(args):
    """Fold a channel into open-questions / decided / decisions — the room's
    actionable knowledge, computed from message structure (statuses, asks,
    answers) plus the store's decision:* record. Output is nonce-fenced: the
    titles/asks/values are member-authored DATA, not instructions."""
    from .render import render_channel_digest

    async def go(c, a):
        print(render_channel_digest(c._json(await c._http.get(f"/channels/{a.channel}/digest"))))
    _run_agent_cmd(args, go)


def cmd_who(args):
    """Who is reachable right now? (presence of every agent you share a
    channel with — 'is anyone listening?' as a query, not an experiment)."""
    import time as _time

    async def go(c, a):
        rows = c._json(await c._http.get("/presence"))
        now = _time.time()
        for r in rows:
            age = f"{(now - r['updated_at'])/60:.0f}m ago" if r["updated_at"] else "never"
            print(f"{r['agent_id']:<16} {r['state']:<8} (updated {age})")
    _run_agent_cmd(args, go)


def cmd_invite(args):
    """Operator verb: mint a scoped join token and print the one-paste line
    (`agora join AGORA1....`) a remote machine onboards with. The admin key
    resolves like resolve_key (flag -> $AGORA_ADMIN_KEY -> config.json) and
    never leaves this machine."""
    from .join import parse_ttl, run_invite, run_invite_list, run_invite_revoke

    url = _hub_url(args)
    admin = _admin_key_or_exit(args, url)
    if args.list:
        return run_invite_list(url, admin)
    if args.revoke:
        return run_invite_revoke(url, admin, args.revoke)
    if args.any_id and args.agent:
        sys.exit("agora invite: give an agent id OR --any-id, not both")
    if not args.any_id and not args.agent:
        sys.exit("agora invite: name the agent to invite (or pass --any-id to "
                 "let the joiner choose)")
    try:
        ttl = parse_ttl(args.ttl)
    except ValueError as e:
        sys.exit(f"agora invite: {e}")
    channels = [c.strip() for c in (args.channels or "").split(",") if c.strip()]
    run_invite(url, admin, None if args.any_id else args.agent,
               args.about or "", channels, ttl, args.uses)


def cmd_join(args):
    """ONE subparser, two verbs, disambiguated loudly:
    - a positional `AGORA1....` artifact (or --token/--url) = machine
      onboarding — redeem a join token, cache the key everywhere, wire the
      workspace;
    - --channel = the existing channel join, unchanged.
    Both or neither is a usage error, never a guess."""
    onboarding = bool(args.artifact or args.token)
    if onboarding and args.channel:
        sys.exit("agora join: choose ONE mode — an artifact/--token onboards "
                 "this machine; --channel joins a channel. Not both.")

    if onboarding:
        from .setup_harness import install_skill
        from .join import check_id_pin, decode_artifact, run_join
        if args.artifact and args.token:
            sys.exit("agora join: pass an artifact OR --token, not both")
        if args.artifact:
            try:
                art = decode_artifact(args.artifact)
            except ValueError as e:
                sys.exit(f"agora join: {e}")
            url, token = art["url"], art["token"]
            pinned, expires = art["agent_id"], art["expires_at"]
            if not pinned and not args.as_agent:
                # Knowable client-side for artifacts (the mint wrote the pin
                # into the blob): fail before any network call.
                sys.exit("this artifact pins no agent id: choose one with "
                         "`agora join <artifact> --as <id>`")
        else:
            if not args.url:
                sys.exit("agora join: --token needs --url <hub-url> "
                         "(the artifact form carries the url for you)")
            url, token = args.url.rstrip("/"), args.token
            pinned, expires = None, None
        if args.as_agent:
            _validate_agent_id_or_exit(args.as_agent)
        # Identity before wiring: an artifact pinned to someone else is a
        # client-side refusal and must say so, even in a folder that has never
        # been wired for any harness.
        check_id_pin(args.as_agent, pinned)
        if args.with_hook and not args.no_hook:
            print("note: `--with-hook` is now the default; use `--no-hook` to "
                  "skip hook installation.")
        workspace = Path(args.workspace).expanduser().resolve()
        vendor_bootstrap = bool(getattr(args, "vendor_bootstrap", False))
        harnesses: tuple[str, ...] | None
        resolver = None
        if args.harness == "none":
            harnesses = ()
        elif args.harness not in (None, "", "auto") or vendor_bootstrap:
            # Named upfront (or pinned by --vendor-bootstrap, which needs a
            # concrete single harness to validate): resolve now so the
            # workspace preflight still guards the invite.
            harnesses = _resolve_harnesses("join", workspace, args.harness,
                                           allow_none=True)
        else:
            # Detecting a footprint — or refusing for the lack of one, or
            # prompting — is workspace wiring, and wiring only matters once the
            # token has redeemed. Hand `run_join` the resolver, not an answer.
            harnesses = None
            resolver = functools.partial(_resolve_harnesses, "join", workspace,
                                         args.harness, allow_none=True)
        if vendor_bootstrap and (harnesses is None or len(harnesses) != 1
                                 or harnesses[0] not in ("claude", "codex")):
            sys.exit("agora join: --vendor-bootstrap requires exactly one harness "
                     "(`--harness claude|codex`)")
        with_hook = _effective_hook_choice("join", args)
        result = run_join(url=url, token=token, agent_id=args.as_agent,
                          about=args.about or "", harness=harnesses,
                          workspace=args.workspace, with_hook=with_hook,
                          listen=args.listen, mcp_command=_resolve_mcp_command(),
                          pinned_id=pinned, expires_hint=expires,
                          vendor_bootstrap=vendor_bootstrap,
                          harness_resolver=resolver)
        harnesses = result.harnesses
        if result.code:
            sys.exit(result.code)
        issues: list[str] = list(result.issues)
        if harnesses and result.agent_id:
            # A joined machine gets the skill too, so the three-word boot
            # works there exactly as on setup-wired machines.
            for harness in harnesses:
                skill_detail = install_skill(harness)
                print(f"  {skill_detail}")
                if skill_detail.startswith("skill: could not install"):
                    issues.append(f"{harness} skill installation needs action")
        status = _print_final_status(issues)
        print()
        if status == 0 and harnesses:
            _print_kickoff(harnesses[0] if len(harnesses) == 1 else "all")
        elif status != 0:
            print("Resolve the items above, then launch the agent and send:\n\n"
                  "  start agora protocol\n")
            sys.exit(status)
        return

    if not args.channel:
        sys.exit("agora join: nothing to do — paste an AGORA1.... artifact to "
                 "onboard this machine, or --channel <name> to join a channel "
                 "(see --help)")
    if not args.as_agent:
        sys.exit("agora join --channel requires --as <agent-id>")

    async def go(c, a):
        print(json.dumps(await c.join_channel(a.channel, a.invite), indent=2))
    _run_agent_cmd(args, go)


def cmd_set_about(args):
    async def go(c, a):
        await c.set_about(a.text)
        print(f"{args.as_agent} about updated")
    _run_agent_cmd(args, go)


def cmd_note(args):
    async def go(c, a):
        await c.set_note(a.about_agent, a.text)
        print(f"note on {a.about_agent} saved")
    _run_agent_cmd(args, go)


def cmd_retract(args):
    """`agora retract <channel> <message_id>` — unsay your own message
    (0097): it redacts to a tombstone everywhere and any obligation it
    carried is cleared. Author-only (or operator)."""
    async def go(c, a):
        row = await c.retract(a.channel, a.message_id)
        print(f"retracted {a.message_id} in {a.channel} — now reads "
              f"{row['body']!r} on every surface; obligation (if any) cleared")
    _run_agent_cmd(args, go)


def cmd_search(args):
    """`agora search TERMS...` — the hub-wide grouped report (agora-0132)
    over everything this agent can read: decisions, open threads, work,
    people, files, messages. --json serves the raw typed report."""
    async def go(c, a):
        rep = await c.search(" ".join(a.terms),
                             channels=[a.channel] if a.channel else None,
                             sender=a.sender or "", kind=a.kind or "",
                             rated=a.rated or "", min_votes=a.min_votes,
                             sort=a.sort, limit=a.limit,
                             mode=a.mode or "")
        if a.json:
            print(json.dumps(rep, indent=1))
            return
        # Semantic honesty (agora-0137, UX P1-3: the operator must SEE
        # semantic working). The relaxed banner renders only under
        # mode_used=lexical — a fused response already compensated, and
        # double-speaking a warning teaches readers to skip both.
        mode_used = rep.get("mode_used") or "lexical"
        if mode_used != "lexical":
            cov = rep.get("semantic_coverage")
            cov_s = f", coverage {cov * 100:.0f}%" if cov is not None else ""
            print(f"(mode: {mode_used}{cov_s})")
        if rep.get("notice"):
            print(f"NOTICE: {rep['notice']}")
        if rep.get("relaxed") and mode_used == "lexical":
            print("(exact match found nothing — looser OR results below)")
        empty = True
        for name in ("decisions", "open_threads", "work", "people",
                     "files", "messages"):
            sec = rep.get(name) or {}
            hits = sec.get("hits") or []
            if not hits:
                continue
            empty = False
            print(f"{name.replace('_', ' ').upper()}"
                  f" ({sec.get('shown')}/{sec.get('total')})")
            for h in hits:
                where = (f"{h.get('channel')}#{h.get('seq')}"
                         if h.get("seq") is not None
                         else f"{h.get('channel') or 'roster'} {h.get('ref')}")
                r = h.get("ratings") or {}
                tally = (f"  [+{r.get('up', 0)}/-{r.get('down', 0)}]"
                         if (r.get("up") or r.get("down")) else "")
                print(f"  {where}  {h.get('title') or ''}{tally}")
                if h.get("snippet"):
                    print(f"    {h['snippet'][:160]}")
        if empty:
            n = rep.get("channels_searched", 0)
            print(f"searched everything you can read ({n} channel(s):"
                  " messages, decisions, work, people, files) — no matches;"
                  " fewer or different words often help")
    _run_agent_cmd(args, go)


def cmd_work(args):
    """`agora work <item_id>` — the stitch, readable from a terminal: who
    claims the item, what was decided about it, and every message citing it
    (structured item_ref first-class, prose mentions included)."""
    async def go(c, a):
        out = await c.work(a.item_id)
        claims, decisions, msgs = out["claims"], out["decisions"], out["messages"]
        print(f"work {out['item_id']} — {len(claims)} claim(s), "
              f"{len(decisions)} decision(s), {len(msgs)} message(s)")
        for r in claims:
            v = r["value"] if isinstance(r["value"], dict) else {}
            print(f"  claim  {r['channel']}  owner={v.get('owner', '?')}"
                  f"  card={v.get('card', '-')}  v{r['version']}")
        for r in decisions:
            print(f"  decide {r['channel']}  {r['key']}  by {r['updated_by']}")
        for m in msgs:
            tag = "ref" if m["via"] == "item_ref" else "…"
            print(f"  [{tag}] {m['channel']}#{m['seq']} {m['sender']}"
                  f" ({m['status']}) {m['title'][:60]}")
    _run_agent_cmd(args, go)


def cmd_rate(args):
    async def go(c, a):
        value = int(str(a.value).replace("+", ""))
        row = await c.rate(a.channel, a.target, a.axis, value, a.note or "")
        sign = "+1" if row["value"] > 0 else "-1"
        print(f"vote recorded: {a.target} {a.axis} {sign} in {a.channel}"
              + (f" — {row['note']}" if row.get("note") else ""))
    _run_agent_cmd(args, go)


def cmd_leaderboard(args):
    async def go(c, a):
        board = await c.reputation(a.channel)
        scope = board["channel"] or "hub-wide"
        cats = board["categories"]
        rows = board["leaderboard"]
        if not rows:
            print(f"no reputation yet ({scope})")
            return
        # ONE score per agent (agora-0123); the categories are the optional
        # granularity — 'general' is thumbs on messages, the named four are
        # agent-level votes. Counting rule: docs/protocol.md 'Reputation'.
        head = "agent".ljust(16) + "score".rjust(6) + "votes".rjust(10)
        for cat in cats:
            head += cat.rjust(10)
        print(f"leaderboard — {scope}")
        print(head)
        for r in rows:
            v = r.get("votes") or {"up": 0, "down": 0}
            line = (r["target"].ljust(16) + f'{r["score"]:+d}'.rjust(6)
                    + f'{v["up"]}↑{v["down"]}↓'.rjust(10))
            for cat in cats:
                cell = r["breakdown"].get(cat)
                line += (f'{cell["score"]:+d}' if cell else "·").rjust(10)
            print(line + f"   ({r['raters']} rater(s))")
    _run_agent_cmd(args, go)


def cmd_mirror(args):
    """Export each channel you're in to an append-only markdown file, so the
    hub's history is readable in an editor / git (and tailable by a file
    watcher). Idempotent: re-runs append only new messages. `--watch` keeps
    the files live via the push stream. (agora-meta top priority.)"""
    import asyncio

    from .client import AgoraClient

    url = _hub_url(args)
    key = _config.resolve_key(url, args.as_agent)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / ".mirror_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    def last_seq_from_file(channel) -> int:
        # Recover the highest already-written seq by scanning the file, so a
        # lost/deleted state file can never cause duplicate appends.
        path = out / f"{channel}.md"
        if not path.exists():
            return 0
        highest = 0
        for line in path.read_text().splitlines():
            if line.startswith("## #"):
                num = line[4:].split(" ", 1)[0].split("\u00b7", 1)[0].strip()
                if num.isdigit():
                    highest = max(highest, int(num))
        return highest

    def append(channel, messages):
        path = out / f"{channel}.md"
        new_file = not path.exists()
        with path.open("a") as f:
            if new_file:
                f.write(f"# {channel}\n\n_agora channel mirror — append-only._\n\n")
            for m in messages:
                data = m.data or {}
                head = f"## #{m.seq} · {m.sender} · {m.status.value}"
                if m.title:
                    head += f" · {m.title}"
                f.write(head + "\n\n")
                f.write(f"- id: `{m.id}`\n")
                if m.reply_to:
                    f.write(f"- reply_to: `{m.reply_to}`\n")
                if data.get("original_date"):
                    f.write(f"- date: {data['original_date']}\n")
                if isinstance(data.get("attachments"), list) and data["attachments"]:
                    refs = ", ".join(f"{r.get('filename', '?')} (`{r.get('id', '')[:12]}…`)"
                                     for r in data["attachments"] if isinstance(r, dict))
                    f.write(f"- attachments: {refs}\n")
                f.write("\n" + m.body.rstrip() + "\n\n")
        state[channel] = max(m.seq for m in messages)

    async def mirror_files(client, channels):
        # Snapshot each channel's virtual file system (vfs) into a SEPARATE tree
        # (files/<channel>/<path>) so the maintainer/git can read the shared
        # workspace. Kept apart from the append-only message mirror and from any
        # authored thread files, so a file watcher never mistakes a mirrored
        # workspace file for a new message. Snapshot-overwrite (not append):
        # a file's current head is the truth; its history lives in the log.
        for ch in channels:
            try:
                listing = await client.fs_list(ch)
            except Exception:
                continue
            for meta in listing:
                doc = await client.fs_read(ch, meta["path"])
                dest = out / "files" / ch / doc["path"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(doc.get("content", ""))

    async def mirror_once(client):
        channels = [c["name"] for c in await client.list_channels() if c["member"]]
        total = 0
        for ch in channels:
            # Trust the file's own last-written seq over the state file, so a
            # deleted/stale .mirror_state.json never duplicates history.
            last = max(state.get(ch, 0), last_seq_from_file(ch))
            msgs = [m for m in await client.history(ch, since=last, limit=1000)
                    if m.seq > last]
            if msgs:
                append(ch, msgs)
                total += len(msgs)
        state_path.write_text(json.dumps(state, indent=2))
        await mirror_files(client, channels)
        return total, channels

    async def _main():
        client = AgoraClient(url, key)
        try:
            total, channels = await mirror_once(client)
            print(f"mirrored {total} new message(s) across {len(channels)} channel(s) -> {out}")
            if args.watch:
                await client.connect(channels)
                print("watching for new messages (Ctrl-C to stop)...")
                while True:
                    await client.inbox.wait(timeout=3600)
                    n, _ = await mirror_once(client)
                    if n:
                        print(f"appended {n} new message(s)")
        finally:
            await client.close()

    asyncio.run(_main())


def cmd_chat(args):
    """The human's live window: room directory with stats, realtime stream of
    every channel you belong to, and posting with real obligation semantics
    (/ask opens an obligation; /critical is the operator tier)."""
    from .chat import run_chat

    url = _hub_url(args)
    key = _config.resolve_key(url, args.as_agent)
    run_chat(url, key, args.as_agent, channel=args.channel)


def cmd_watch(args):
    """Non-blocking trigger: stream new envelopes to stdout (+ optional
    --notify-file append, +optional --exec per message). Run it in the
    background (`agora watch --as <id> --notify-file f &`) and your agent loop
    checks the file — no turn-blocking `--wait`. (agora-meta P1.)"""
    import asyncio
    import subprocess

    from .client import AgoraClient

    url = _hub_url(args)
    key = _config.resolve_key(url, args.as_agent)
    notify_file = args.notify_file

    # Liveness: a watch dies silently with its parent shell, so a harness tailing
    # the notify file can't tell "quiet channel" from "dead watcher". A pidfile
    # (present = alive) and a final `{"event":"watch_ended"}` line on exit make
    # the distinction explicit. (Field-requested by the memory agent.)
    if args.pidfile:
        Path(args.pidfile).expanduser().write_text(str(os.getpid()))

    def _note(obj: dict) -> None:
        if notify_file:
            with open(notify_file, "a") as fh:
                fh.write(json.dumps(obj) + "\n")

    def emit(e) -> None:
        # One line format, defined once: hub-written notify files and `watch`
        # output must stay byte-compatible (tailers switch between them).
        from .hub.notify_sink import notify_line
        line = notify_line(e)
        print(line, flush=True)
        if notify_file:
            with open(notify_file, "a") as fh:
                fh.write(line + "\n")
        if args.exec_cmd:
            env = dict(os.environ, AGORA_MSG_CHANNEL=e.channel,
                       AGORA_MSG_SEQ=str(e.seq), AGORA_MSG_FROM=e.sender,
                       AGORA_MSG_ID=e.id, AGORA_MSG_STATUS=e.status.value,
                       AGORA_MSG_TITLE=e.title,
                       AGORA_MSG_FLAGS=json.loads(line)["flags"])
            subprocess.Popen(args.exec_cmd, shell=True, env=env)

    async def _main() -> None:
        client = AgoraClient(url, key)
        channels = ([args.channel] if args.channel
                    else [c["name"] for c in await client.list_channels() if c["member"]])
        await client.connect(channels)
        print(f"watch {args.as_agent}: {len(channels)} channel(s); "
              f"notify_file={notify_file or '-'} exec={'yes' if args.exec_cmd else 'no'}",
              flush=True)
        # Liveness marker in the notify file itself (the counterpart of
        # watch_ended): a tailing harness can tell "watcher armed" from
        # "quiet channel" without checking the pidfile.
        _note({"event": "watch_started", "as": args.as_agent,
               "channels": len(channels)})
        # connect() now runs the cold-start catch-up sweep itself and delivers
        # missed messages into the inbox, so the loop below emits them on its
        # first pass — no separate sweep here (that would double-emit).
        try:
            while True:
                for e in await client.inbox.wait(timeout=3600):
                    emit(e)
        finally:
            await client.close()
            # A final marker so a tailing harness sees the watcher stopped
            # (vs. an indefinitely quiet channel), and clean up the pidfile.
            _note({"event": "watch_ended", "as": args.as_agent})
            if args.pidfile:
                with contextlib.suppress(FileNotFoundError):
                    Path(args.pidfile).expanduser().unlink()

    asyncio.run(_main())


def _listener_state(home: Path, agent_id: str) -> str:
    """`agora status` listener column from `listen-<id>.pid`: live pid + mtime
    fresher than 2x the default heartbeat = "armed"; pidfile whose holder is
    dead or stale = "STALE"; no pidfile = "-" (nothing armed)."""
    import time as _time

    from .listen import DEFAULT_HEARTBEAT, pid_alive
    pid_path = Path(home) / f"listen-{agent_id}.pid"
    try:
        pid = int(pid_path.read_text().strip() or "0")
        mtime = pid_path.stat().st_mtime
    except (OSError, ValueError):
        return "-"
    if pid > 0 and pid_alive(pid) and (_time.time() - mtime) <= 2 * DEFAULT_HEARTBEAT:
        # Surface the adaptive idle window when the seat runs one, so the
        # operator can see a seat that has widened out to a long window.
        with contextlib.suppress(OSError, ValueError, TypeError):
            import json as _json
            ceiling = _json.loads(
                (Path(home) / f"listen-{agent_id}.backoff").read_text())["ceiling"]
            return f"armed:{int(ceiling)}s"
        return "armed"
    return "STALE"


def _driver_state(home: Path, agent_id: str) -> str:
    """`agora status` driver column from `drive-<id>.pid` (the one file that
    means 'a driver owns this seat'): live pid = "driving"; pidfile whose
    holder is dead = "STALE" (the driver crashed — restart it); no file =
    "-". NOTE: unlike the listener column, no mtime bound on the live case —
    a driver blocked in a long work chunk legitimately goes minutes without
    touching the file, and pid liveness is the truth here."""
    from .listen import pid_alive
    pid_path = Path(home) / f"drive-{agent_id}.pid"
    try:
        pid = int(pid_path.read_text().strip() or "0")
    except (OSError, ValueError):
        return "-"
    if pid > 0 and pid_alive(pid):
        return "driving"
    return "STALE"


def cmd_drive(args: argparse.Namespace) -> None:
    """The external resume-driver for a dedicated seat: block cheaply in
    `agora listen --once --important-only`, and on an obligation wake spawn
    ONE bounded harness turn that acts and yields by returning. Reception
    becomes structural (yield = process exit; the check->ack->re-arm trap is
    impossible). Owner-run, session-bound, never hub machinery. See drive.py."""
    from .drive import RECEPTION_TURN_TIMEOUT, run_drive

    harness = getattr(args, "harness", "auto")
    harness_pos = getattr(args, "harness_pos", None)
    if harness_pos:
        if harness not in (None, "", "auto", harness_pos):
            sys.exit("agora drive: positional harness and --harness/--framework "
                     "disagree")
        harness = harness_pos
    sys.exit(run_drive(
        harness=harness,
        agent_id=args.as_agent, url=args.url, model=args.model,
        provider=args.provider,
        reasoning_effort=args.reasoning_effort,
        permissions=args.permissions,
        harness_args=_parse_harness_args(args.harness_args),
        max_wait=args.max_wait, sandbox=args.sandbox,
        turn_budget=args.turn_budget,
        broadcast_turn_budget=args.broadcast_turn_budget,
        session_rotate=args.session_rotate,
        work_timeout=args.work_timeout,
        reception_timeout=(args.reception_timeout
                           if getattr(args, 'reception_timeout', None)
                           else RECEPTION_TURN_TIMEOUT),
        work_budget=args.work_budget, force=args.force,
        turn_log=args.turn_log,
        once=args.once, max_turns=args.max_turns))


def _parse_harness_args(pairs: list[str] | None) -> dict[str, str]:
    """`--harness-arg k=v` -> {"k": "v"}, refusing a malformed pair loudly."""
    out: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = str(pair).partition("=")
        key = key.strip().lstrip("-")
        if not sep or not key:
            sys.exit(f"agora drive: --harness-arg expects KEY=VALUE, got "
                     f"{pair!r}")
        out[key] = value
    return out


def cmd_harness_check(args: argparse.Namespace) -> None:
    """`agora harness-check <harness>` — run the agora harness contract against
    a framework and print a per-capability verdict.

    This is how a framework learns whether it can carry an agora seat WITHOUT
    agora learning its internals, and without its operator negotiating with
    agora's maintainers. Structural probes only, unless --live.
    """
    from .harness_check import run_check
    from .listen import resolve_identity

    try:
        seat, url = resolve_identity(args.as_agent, args.url, Path.cwd())
    except SystemExit:
        # A conformance check must run in an UNWIRED workspace too — that is
        # exactly where a vendor tries it first.
        seat, url = "harness-check", (args.url or "http://127.0.0.1:8765")
    report = run_check(args.harness, workspace=Path.cwd(), agent_id=seat,
                       url=url, live=args.live)
    print(report.to_json() if args.json else report.render())
    sys.exit(report.exit_code())


def cmd_hook(args: argparse.Namespace) -> None:
    """`agora hook <Event>` — the reception hook every harness declares.

    Deliberately never fails the turn: a non-zero exit means "wake" to Claude
    and "error" to Codex, so problems are reported on stderr (which both
    harnesses surface) and the exit stays 0.
    """
    from . import hook as _hook

    from .listen import resolve_identity

    seat, url = resolve_identity(args.as_agent, args.url, Path.cwd())
    raise SystemExit(_hook.run(args.event, seat, url, cursor=args.cursor))


def cmd_listen(args: argparse.Namespace) -> None:
    """The session-resident listener (proposal_1): tail/subscribe, debounce,
    emit AGORA_WAKE sentinels. The heavy lifting lives in listen.py; this is
    only the argparse<->function seam."""
    from .listen import run_listen

    if args.adaptive and not args.once:
        sys.exit("agora listen: --adaptive requires --once (it tunes the "
                 "per-call --max-wait ceiling the reception loop re-invokes)")
    sys.exit(run_listen(
        agent_id=args.as_agent, url=args.url, source=args.source, once=args.once,
        max_wait=args.max_wait, debounce=args.debounce,
        important_only=args.important_only, preview=args.preview,
        notify_file=args.notify_file, lock=args.lock, heartbeat=args.heartbeat,
        poll=args.poll, adaptive=args.adaptive, idle_nudge=args.idle_nudge))


def _print_reception_posture(workspace: Path) -> None:
    """Say whether this workspace's reception hooks have EVER fired.

    This exists because the previous generation failed silently in three
    independent ways at once — a malformed declaration that registered zero
    hooks with no warning, an untrusted project, and untrusted hooks — and
    nothing anywhere said so. One line naming NEVER FIRED is what would have
    caught it on day one.
    """
    from . import hook as _hook
    from .setup_harness import HOOK_EVENTS, read_workspace_seat

    seat = read_workspace_seat(workspace)
    if not seat:
        return
    agent_id = seat.get("agent_id")
    harnesses = list(seat.get("harnesses") or [])
    if not agent_id:
        return
    fired = _hook.last_fired(str(agent_id))
    now = time.time()

    def ago(stamp: float) -> str:
        secs = max(0, int(now - stamp))
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        return f"{secs // 3600}h ago"

    if not fired:
        print(f"hooks ({agent_id}): NEVER FIRED — in-session reception is "
              "inert. Driven seats do not need hooks; an attended session "
              "does.")
    else:
        parts = [f"{e} {ago(fired[e])}" if e in fired else f"{e} NEVER"
                 for e in HOOK_EVENTS]
        print(f"hooks ({agent_id}): " + " · ".join(parts))

    if "codex" in harnesses:
        # Codex reads .codex/hooks.json AND .codex/config.toml (hence agora's
        # MCP server) only for a TRUSTED project — untrusted, both are ignored
        # with no message at all.
        codex_home = Path(os.environ.get("CODEX_HOME")
                          or (Path.home() / ".codex"))
        target = str(workspace.resolve())
        trusted = False
        try:
            text = (codex_home / "config.toml").read_text()
            trusted = (f'[projects."{target}"]' in text
                       and "trust_level" in text.split(
                           f'[projects."{target}"]', 1)[1].split("[", 1)[0])
        except OSError:
            pass
        if trusted:
            print("codex: project TRUSTED")
        else:
            print(f"codex: project NOT TRUSTED in {codex_home}/config.toml — "
                  ".codex/hooks.json and the agora MCP server are BOTH ignored "
                  "until you run `codex` here and trust the folder. Hooks also "
                  "need a one-time approval via `/hooks`.")


def _print_governance_drift(url: str, admin_key: str) -> None:
    """Print the rules- and charter-drift warnings, if any. Best-effort and
    silent when everything is current or unreachable — `agora status` must
    never fail because a diagnostic could not run."""
    import httpx

    from .governance import rules_missing_markers
    headers = {"Authorization": f"Bearer {admin_key}"}
    try:
        rules = httpx.get(f"{url}/admin/rules", headers=headers, timeout=5).json()
    except Exception:
        rules = None
    if isinstance(rules, dict) and rules.get("version"):
        missing = rules_missing_markers(rules.get("text") or "")
        if missing:
            print(f"\n  WARNING: hub rules v{rules['version']} (operator-set) "
                  f"never mention {len(missing)} mechanism(s) this build "
                  "enforces:")
            for why in missing:
                print(f"    - {why}")
            print("    Merge the packaged default and publish it: "
                  "`agora rules --set FILE`.")
    try:
        r = httpx.get(f"{url}/admin/charter", headers=headers, timeout=5)
        doc = r.json() if r.status_code == 200 else None
    except Exception:
        return                      # pre-0146 hub, or unreachable
    if not isinstance(doc, dict):
        return
    for line in _stale_charter_lines(doc.get("version") or 0, doc.get("text") or ""):
        print(line)


def cmd_status(args: argparse.Namespace) -> None:
    import httpx

    cfg = _config.load_config()
    url = cfg.get("url", _default_url(DEFAULT_PORT))
    try:
        r = httpx.get(f"{url}/", timeout=3)
        print(f"hub: UP at {url} ({r.json().get('version')})")
    except Exception:
        print(f"hub: not reachable at {url} — run `agora up`")
        print(f"config: {_config.home() / 'config.json'}")
        return
    print(f"config: {_config.home() / 'config.json'}")
    _print_reception_posture(Path.cwd())

    # With the admin key (same machine as `agora up`) also show the per-agent
    # overview. DARK = offline with obligations pending — the dead-agent
    # alarm, as a table row instead of a subsystem.
    admin_key = cfg.get("admin_key")
    if not admin_key:
        return
    # Governance drift, on the surface an operator actually looks at (0146).
    # The rules/charter warnings used to print ONLY at `agora up`; the 0.14.0
    # field test lost its first hour to a hub that had been serving a stale
    # rules text since a boot nobody was watching. Same words, a place you
    # can reach any time.
    _print_governance_drift(url, admin_key)
    try:
        data = httpx.get(f"{url}/admin/status", timeout=5,
                         headers={"Authorization": f"Bearer {admin_key}"}).json()
    except Exception:
        return
    if isinstance(data, dict):
        fleet = data.get("fleet") or {}
        rows = data.get("agents") or []
    elif isinstance(data, list):
        fleet = {}
        rows = data
    else:
        return
    if fleet:
        dark = " DARK EPISODE" if fleet.get("dark_episode") else ""
        print(f"\nfleet: {fleet.get('live', '?')}/{fleet.get('eligible', '?')} live "
              f"({100 * fleet.get('live_fraction', 0):.0f}%){dark}")
    print(f"\n{'agent':<16} {'state':<8} {'listener':<9} {'driver':<8} "
          f"{'unread':>6} {'pending':>7}  oldest-pending")
    # The hub can only see what CONTACTS it: an open-but-idle IDE tab makes no
    # calls, so it honestly reads offline even though it will respond at its
    # next prompt. Spell that out or every operator misreads the table.
    legend = ("  states: idle/working = live push connection | active = made an "
              "authenticated call <10m ago,\n  or its reception loop armed "
              "<15m ago (a seat mid work chunk arms less often than it calls) |"
              "\n  offline = no contact (an open but "
              "idle IDE tab reads offline; it acts at its next prompt/turn)\n"
              "  listener: armed = live `agora listen` pidfile | STALE = pidfile "
              "but dead/old | - = none\n"
              "  driver: driving = live `agora drive` owns the seat | STALE = "
              "driver crashed (restart it) | - = none")
    for row in rows:
        oldest = row["oldest_pending_minutes"]
        oldest_s = f"{oldest:.0f}m" if oldest is not None else "-"
        # DARK = offline with work pending (the dead-agent alarm). NO-PUSH is
        # the softer cousin the audit flagged: pending work and no live push
        # connection — normal for an MCP-only tab (it drains at its next
        # turn), but also exactly what a died watcher looks like, so the
        # operator must be able to SEE it rather than assume reachability.
        # Send refusals are first-class too: a rate-limited sender must be
        # visible, not inferred.
        flag = ""
        if row["pending_obligations"]:
            if row["state"] == "offline":
                flag = " <- DARK: offline with work pending"
            elif row["state"] == "active":
                flag = " <- NO-PUSH: pending work, no live connection"
        # The lurk alarm (0080): the seat SERVED these debts (cursor past
        # them) and never engaged — the compliant-spectator signature the
        # 2026-07-13 incident put a name on.
        if row.get("acked_unanswered"):
            flag += (f" <- LURK: acked {row['acked_unanswered']} owed "
                     "answer(s) without replying")
        if row.get("refused_sends_1h"):
            last = row.get("last_refusal") or {}
            flag += (f" <- BLOCKED-SEND: {row['refused_sends_1h']}x last hour "
                     f"(last: {last.get('code')} {str(last.get('detail'))[:60]})")
        listener = _listener_state(_config.home(), row["agent_id"])
        driver = _driver_state(_config.home(), row["agent_id"])
        print(f"{row['agent_id']:<16} {row['state']:<8} {listener:<9} "
              f"{driver:<8} {row['unread']:>6} "
              f"{row['pending_obligations']:>7}  {oldest_s}{flag}")
    print(f"\n{legend}")


def _dur(seconds: float | None, *, dash: str = "-") -> str:
    """Compact age: 45s / 12m / 3.1h / 2.4d. None -> dash."""
    if seconds is None:
        return dash
    s = float(seconds)
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s / 60:.0f}m"
    if s < 172800:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


def _local_driver_facts(home: Path, agent_id: str) -> dict[str, str]:
    """What the HUB cannot know, read from this machine's ~/.agora: the
    listener/driver pidfiles, the adaptive backoff ceiling, and the last line
    of the driver's turn log / failure ledger. Labelled `local` everywhere it
    is printed — on another machine these are simply absent, and saying so is
    the point."""
    import json as _json

    out: dict[str, str] = {
        "listener": _listener_state(home, agent_id),
        "driver": _driver_state(home, agent_id),
        "turn": "-",
    }
    turns = Path(home) / f"drive-{agent_id}.turns.jsonl"
    fails = Path(home) / f"drive-{agent_id}.failures.jsonl"
    with contextlib.suppress(OSError, ValueError, IndexError):
        last = _json.loads(turns.read_text().strip().split("\n")[-1])
        if last.get("event") == "turn_end":
            verdict = "ok" if last.get("ok") else f"FAILED rc={last.get('rc')}"
            out["turn"] = (f"{last.get('kind', '?')} {verdict} "
                           f"{_dur(time.time() - float(last.get('ts', 0)))} ago")
    with contextlib.suppress(OSError, ValueError, IndexError):
        last = _json.loads(fails.read_text().strip().split("\n")[-1])
        age = time.time() - float(last.get("ts", 0))
        if age < 6 * 3600:
            out["turn"] += (f" | last failure {last.get('stage')}/"
                            f"{last.get('reason')} {_dur(age)} ago")
    return out


def cmd_doctor(args: argparse.Namespace) -> None:
    """One screen, one truth: why is this seat quiet?

    Assembled from the hub's own state (`GET /admin/doctor`) plus the local
    driver artifacts the hub structurally cannot see. Everything printed is
    sourced — `hub:` facts come from the hub, `local:` facts from ~/.agora —
    and the blind-spot list at the bottom is printed every time, because a
    diagnostic that quietly omits what it does not know is how three
    investigations in a row ended up reconstructing state by hand."""
    import json as _json

    import httpx

    cfg = _config.load_config()
    url = cfg.get("url", _default_url(DEFAULT_PORT))
    admin_key = cfg.get("admin_key")
    if not admin_key:
        print("agora doctor: needs the admin key (run it on the hub's machine, "
              f"where {_config.home() / 'config.json'} holds it)")
        return
    try:
        r = httpx.get(f"{url}/admin/doctor", timeout=20,
                      params={"agent": args.as_agent} if args.as_agent else None,
                      headers={"Authorization": f"Bearer {admin_key}"})
    except Exception as exc:
        print(f"hub: not reachable at {url} ({exc}) — run `agora up`")
        return
    if r.status_code != 200:
        print(f"agora doctor: hub refused ({r.status_code}) {r.text[:200]}")
        return
    data = r.json()
    if args.json:
        print(_json.dumps(data, indent=2))
        return
    home = _config.home()
    hub = data["hub"]
    pause = hub.get("paused")
    print(f"hub: {url}  paused: "
          + (f"YES since {time.strftime('%H:%M', time.localtime(pause['since']))}"
             if pause else "no"))
    sw = hub["sweeps"]
    print("sweeps: " + " | ".join(
        f"{name} {_dur(s['last_run_seconds'], dash='never (since boot)')} ago"
        f" ({s['actions']} action(s))" if s["last_run_seconds"] is not None
        else f"{name} not run since boot"
        for name, s in sw.items()))
    fleet = hub["fleet"]
    print(f"health: {fleet['live']}/{fleet['eligible']} seats live"
          f" | votes past deadline {hub['votes_past_deadline']}"
          f" | open hub alerts {hub['open_hub_alerts']}"
          f" (unclosed silence {hub['unclosed_silence_alerts']})"
          f" | claims {hub['live_claims']} live, {hub['stale_claims']} stale")
    print()
    print(f"{'seat':<14}{'presence':<9}{'reception':<11}{'last work':<10}"
          f"{'owes':<9}{'local driver':<22}working on / held up by")
    for s in data["seats"]:
        local = _local_driver_facts(home, s["agent_id"])
        owes = s["owes"]
        rec = s["reachable"]
        claim = (s["working_on"][0] if s["working_on"] else None)
        what = (f"{claim['key']} [{claim['status']}] idle "
                f"{_dur(claim['idle_seconds'])}" if claim else "-")
        if len(s["working_on"]) > 1:
            what += f" (+{len(s['working_on']) - 1})"
        for h in s["held_up_by"]:
            what += f"  <- {h['kind']}"
            if h.get("seconds_left"):
                what += f" {_dur(h['seconds_left'])} left"
        print(f"{s['agent_id']:<14}{rec['presence']:<9}"
              f"{rec['reception'] + ' ' + _dur(rec['reception_age_seconds']):<11}"
              f"{_dur(s['did_work']['last_work_seconds'], dash='>24h'):<10}"
              f"{str(owes['to_answer']) + '(' + str(owes['escalated']) + 'esc)':<9}"
              f"{local['listener'] + '/' + local['driver']:<22}{what}")
    print()
    print("REQUESTS IN FLIGHT (operator asks not closed)"
          if data["requests"] else "REQUESTS IN FLIGHT: none open")
    for req in data["requests"]:
        print(f"  {req['channel']}#{req['seq']} from {req['from']} "
              f"{_dur(req['age_seconds'])} old"
              + (f" — {req['title'][:60]}" if req.get("title") else "")
              + f"\n    owner: {', '.join(req['owned_by']) or '(no claim row)'}"
              f" | owes: {', '.join(req['owed_by']) or 'NOBODY'}"
              f" | owner last work {_dur(req['owner_last_work_seconds'], dash='>24h')} ago"
              + (f" | asks {req['ask_progress']}" if req["ask_progress"] else ""))
        for c in req["claims"]:
            print(f"    next step ({c['owner']}, idle {_dur(c['idle_seconds'])}):"
                  f" {c['next_step'] or '(none declared)'}")
        for a in req["outstanding_asks"][:6]:
            print(f"    outstanding: {a['asker']} -> {a['waiting_on']} "
                  f"({a['where']} ask {a['ask']}) {a['state']}, "
                  f"{_dur(a['age_seconds'])}")
        if len(req["outstanding_asks"]) > 6:
            print(f"    (+{len(req['outstanding_asks']) - 6} more outstanding asks)")
    print()
    for s in data["seats"]:
        local = _local_driver_facts(home, s["agent_id"])
        if local["turn"] != "-":
            print(f"local: {s['agent_id']} last turn — {local['turn']}")
    print("\nthe hub CANNOT see (do not infer these from the table above):")
    for line in data["hub_cannot_see"]:
        print(f"  - {line}")


def build_parser() -> argparse.ArgumentParser:
    """The full argparse tree, separate from main() so tests can parse
    argv lists without executing commands."""
    p = argparse.ArgumentParser(prog="agora", description="agora control")
    from . import __version__
    p.add_argument("--version", action="version",
                   version=f"agora {__version__}",
                   help="print the installed agora version and exit")
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="start the hub with persistent defaults")
    up.add_argument("--host", default=os.environ.get("AGORA_HOST", "127.0.0.1"))
    up.add_argument("--port", type=int, default=int(os.environ.get("AGORA_PORT", DEFAULT_PORT)))
    # No env default here: cmd_up must tell a --db TYPED THIS RUN from a
    # months-old $AGORA_DB in a shell profile — only the typed flag has the
    # authority to create a new database (db_locate F1). The env var still
    # works; it is resolved inside cmd_up as remembered state.
    up.add_argument("--db", default=None,
                    help="hub db file (default: config.json db_path, else "
                         "~/.agora/agora.db; $AGORA_DB is honored but may "
                         "only point at an EXISTING db)")
    up.add_argument("--rate-per-minute", type=float, default=60.0)
    up.add_argument("--notify-dir", default=None,
                    help="dir for hub-written <agent>-inbox.log files "
                         "(default: ~/.agora; '' disables)")
    up.add_argument("--notify-rotate-mb", dest="notify_rotate_mb", type=float,
                    default=8.0,
                    help="rotate a notify file above N MB to <file>.1 "
                         "(default 8; 0 disables rotation)")
    up.add_argument("--max-attachment-mb", dest="max_attachment_mb", type=float,
                    default=0.0,
                    help="per-file cap for message attachments in MB "
                         "(default: 16)")
    up.add_argument("--max-channel-attachment-mb", dest="max_channel_attachment_mb",
                    type=float, default=0.0,
                    help="per-channel total attachment storage cap in MB "
                         "(default: 1024)")
    up.add_argument("--cors-origin", dest="cors_origins", action="append",
                    default=None, metavar="ORIGIN",
                    help="allow one browser origin for cross-origin REST CORS "
                         "(repeatable; default off)")
    up.add_argument("--force", action="store_true",
                    help="take the port over: SIGTERM (then SIGKILL) a "
                         "VERIFIED agora hub already serving it and start "
                         "fresh here — guarantees the newest installed "
                         "version is the one running, with logs in THIS "
                         "terminal. Never kills a non-hub process")
    up.set_defaults(func=cmd_up)

    _KEY_HELP = ("operator-minted agent key (from `agora register`): seeds the "
                 "0600 local key cache; harness config stays secret-free, and the "
                 "admin key is then never needed on this machine")

    def _setup_common_args(sp, *, headless_help: str | None) -> None:
        """The flags shared by every harness setup (one definition — the
        `--with-hooks` lesson: per-harness copies drift)."""
        sp.add_argument("agent", help="agent id, e.g. runtime")
        sp.add_argument("--workspace", default=".",
                        help="workspace folder (default: cwd)")
        sp.add_argument("--about", default="",
                        help="self-description for this agent")
        sp.add_argument("--url", default=None)
        sp.add_argument("--key", default=None, metavar="AGENT_KEY", help=_KEY_HELP)
        sp.add_argument("--with-hook", action="store_true",
                        help="deprecated compatibility alias; hooks install by "
                             "default now. Use --no-hook to skip them.")
        sp.add_argument("--no-hook", action="store_true",
                        help="skip hook installation and any harness wake wiring")
        sp.add_argument("--channels", default="", metavar="A,B",
                        help="public channels to join the seat to NOW "
                             "(placement is the operator's decision; a seat "
                             "that boots member-of-nothing must ask instead "
                             "of picking a room itself)")
        sp.add_argument("--vendor-bootstrap", action="store_true",
                        help="also run the harness's own registration "
                             "convenience when supported (`claude mcp add "
                             "--scope local` or `codex mcp add`)")
        if headless_help:
            sp.add_argument("--headless", action="store_true",
                            help=headless_help)

    _HEADLESS_HELP = {
        "cursor": ("DEPRECATED no-op since 0.12.53 (mode-free wiring): the "
                   "rule is identical either way — the running driver IS "
                   "the mode (`cd <workspace> && agora drive`). The flag "
                   "only prints the driver quickstart"),
        "claude": None,   # hooks already arm reception; no dedicated variant
        "codex": ("Compatibility alias: plain `agora setup <id> --harness "
                  "codex` already writes the dedicated live-session rule. "
                  "Use `agora drive` for an unattended external watcher"),
        "abstractcode": ("DEPRECATED no-op: AbstractCode setup is mode-free; "
                         "run `cd <workspace> && agora drive` (or `agora "
                         "drive --harness abstractcode` in a multi-harness "
                         "workspace)"),
    }

    st = sub.add_parser("setup",
                        help="wire a workspace as an agora agent; default = "
                             "auto-detect or prompt for the workspace harness")
    st.add_argument("target",
                    help="agent id (preferred), or a legacy harness selector "
                         "when followed by AGENT")
    st.add_argument("legacy_agent", nargs="?", default=None, metavar="AGENT",
                    help=argparse.SUPPRESS)
    st.add_argument("--harness", "--framework",
                    choices=["auto", "all", *SUPPORTED_HARNESSES],
                    default="auto",
                    help="workspace wiring to install (default: auto = "
                         "reuse existing footprints, otherwise prompt; "
                         "`all` is explicit multi-harness wiring)")
    st.add_argument("--workspace", default=".",
                    help="workspace folder (default: cwd)")
    st.add_argument("--about", default="",
                    help="self-description for this agent")
    st.add_argument("--url", default=None)
    st.add_argument("--key", default=None, metavar="AGENT_KEY", help=_KEY_HELP)
    st.add_argument("--with-hook", action="store_true",
                    help="deprecated compatibility alias; hooks install by "
                         "default now. Use --no-hook to skip them.")
    st.add_argument("--no-hook", action="store_true",
                    help="skip hook installation and any harness wake wiring")
    st.add_argument("--channels", default="", metavar="A,B",
                    help="public channels to join the seat to NOW "
                         "(placement is the operator's decision; a seat "
                         "that boots member-of-nothing must ask instead "
                         "of picking a room itself)")
    st.add_argument("--headless", action="store_true",
                    help="deprecated compatibility alias; plain `agora setup "
                         "<id> --harness <name>` already writes the normal "
                         "workspace contract, and `agora drive` is the "
                         "separate unattended mode")
    st.add_argument("--vendor-bootstrap", action="store_true",
                    help="also run the harness's own registration convenience "
                         "when supported (`claude mcp add --scope local` or "
                         "`codex mcp add`); mutates user/global harness state")
    st.set_defaults(func=cmd_setup)

    # Deprecated aliases (one release, per the simplicity audit): same flags,
    # same handler, a one-line nudge toward `agora setup <id> --harness X`.
    for h in ("cursor", "claude", "codex", "abstractcode"):
        alias = sub.add_parser(f"setup-{h}")
        _setup_common_args(alias, headless_help=_HEADLESS_HELP[h])
        alias.set_defaults(func=cmd_setup, legacy_harness=h,
                           deprecated_alias=f"setup-{h}")

    rg = sub.add_parser("register",
                        help="operator: register an agent on the hub and print "
                             "its key ONCE (import it on the agent's machine "
                             "with seed-key or setup-* --key)")
    rg.add_argument("agent", help="agent id, e.g. castor")
    rg.add_argument("--about", default="", help="self-description for this agent")
    rg.add_argument("--mission", default="",
                    help="operator-authored charge that rides every whoami")
    rg.add_argument("--url", default=None)
    rg.add_argument("--admin-key", dest="admin_key", default=None,
                    help="admin key (default: $AGORA_ADMIN_KEY, then config.json)")
    rg.add_argument("--json", action="store_true",
                    help="print the raw registration response (scripting)")
    rg.add_argument("--seed", action="store_true",
                    help="also cache the minted key in this machine's "
                         "keys.json (the agent runs HERE; skips the "
                         "seed-key paste)")
    rg.set_defaults(func=cmd_register)

    mi = sub.add_parser("mission", help="operator: set what a seat is FOR "
                                        "(rides every whoami; required "
                                        "before delegating)")
    mi.add_argument("verb", nargs="?", default="show", choices=["set", "show"])
    mi.add_argument("agent", nargs="?", default=None)
    mi.add_argument("text", nargs="?", default=None,
                    help="the mission text, or - to read stdin")
    mi.add_argument("--url", default=None)
    mi.add_argument("--admin-key", dest="admin_key", default=None)
    mi.set_defaults(func=cmd_mission)

    dg = sub.add_parser("delegate", help="grant/list/revoke delegation "
                                         "(verifiable hub state; powers: "
                                         "ruling,operational,reporting,"
                                         "moderation,proxy)")
    dg.add_argument("agent", nargs="?", default=None)
    dg.add_argument("--powers", default=None,
                    help="comma-separated subset of ruling,operational,"
                         "reporting,moderation,proxy ('proxy' = act on the "
                         "owner's behalf: clears a room's gated acts)")
    dg.add_argument("--scope", default=None, metavar="CHANNEL",
                    help="the channel this grant reaches, or '*' for the "
                         "whole hub. REQUIRED for --powers proxy: acting as "
                         "the owner everywhere must be typed, not defaulted")
    dg.add_argument("--ttl", default=None, help="e.g. 7d, 48h (default 7d, cap 30d)")
    dg.add_argument("--note", default="", help="shown in the grant announcement")
    dg.add_argument("--mission", default=None,
                    help="operator-authored charge to set first if the seat is "
                         "blank; use - to read stdin")
    dg.add_argument("--list", action="store_true", help="list active delegations")
    dg.add_argument("--charter", action="store_true",
                    help="print the delegate role brief to hand the agent "
                         "(read decisions before ruling, keep a running summary)")
    dg.add_argument("--revoke", default=None, metavar="AGENT")
    dg.add_argument("--url", default=None)
    dg.add_argument("--admin-key", dest="admin_key", default=None)
    dg.set_defaults(func=cmd_delegate)

    pa = sub.add_parser("pause", help="pause the hub: agents stand down "
                                      "(writes 423; reads/acks open; SLA "
                                      "clocks freeze) until `agora resume`")
    pa.add_argument("--reason", default="", help="shown to agents in the refusal")
    pa.add_argument("--url", default=None)
    pa.add_argument("--admin-key", dest="admin_key", default=None)
    pa.set_defaults(func=cmd_pause, pause_action="pause")

    rs = sub.add_parser("resume", help="lift the operator pause")
    rs.add_argument("--url", default=None)
    rs.add_argument("--admin-key", dest="admin_key", default=None)
    rs.set_defaults(func=cmd_pause, pause_action="resume")

    emb = sub.add_parser("embedding",
                         help="semantic search lifecycle (agora-0137): "
                              "set | status | backfill | disable")
    emb.add_argument("action", choices=["set", "status", "backfill",
                                        "disable"])
    emb.add_argument("--url", default=None,
                     help="OpenAI-compatible endpoint base (e.g. "
                          "http://127.0.0.1:1234/v1)")
    emb.add_argument("--model", default=None,
                     help="embedding model id as the endpoint names it")
    emb.add_argument("--api-key", dest="api_key", default=None)
    emb.add_argument("--accept-recompute", action="store_true",
                     help="accept the model-change cost: all vectors "
                          "recompute (old model serves until the flip)")
    emb.add_argument("--erase", action="store_true",
                     help="with disable: also drop all stored vectors")
    emb.set_defaults(func=cmd_embedding)

    bk = sub.add_parser("backup", help="verified point-in-time snapshot of the "
                                       "whole hub db (safe while the hub runs)")
    bk.add_argument("out", nargs="?", default=None,
                    help="output file (default: ~/.agora/backups/agora-<ts>.db)")
    bk.add_argument("--db", default=None,
                    help="hub db path (default: config.json db_path)")
    bk.set_defaults(func=cmd_backup)

    rst = sub.add_parser("restore", help="replace the hub db with a verified "
                                         "snapshot (hub must be STOPPED; the "
                                         "current db is preserved aside)")
    rst.add_argument("snapshot", help="snapshot file written by `agora backup`")
    rst.add_argument("--db", default=None,
                     help="hub db path (default: config.json db_path)")
    rst.add_argument("--url", default=None,
                     help="hub url for the running-hub refusal check")
    rst.set_defaults(func=cmd_restore)

    ru = sub.add_parser("rules",
                        help="show or replace the hub rules served to every "
                             "agent via whoami (operator; --set FILE)")
    ru.add_argument("--set", dest="set_file", default=None, metavar="FILE",
                    help="replace the hub rules with this file's text")
    ru.add_argument("--url", default=None)
    ru.add_argument("--admin-key", dest="admin_key", default=None,
                    help="admin key (default: $AGORA_ADMIN_KEY, then config.json)")
    ru.set_defaults(func=cmd_rules)

    ch = sub.add_parser("charter",
                        help="show/set/history/receipts for the HUB charter "
                             "(who is who: member, owner, delegate, operator) "
                             "or a channel's (--channel)")
    ch.add_argument("charter_action", nargs="?", default="show",
                    choices=["show", "set", "history", "receipts"])
    ch.add_argument("file", nargs="?", default=None,
                    help="for `set`: the markdown file to publish, or `-` to "
                         "read the text from stdin (heredoc)")
    ch.add_argument("--channel", default=None,
                    help="operate on this room's charter instead of the hub's "
                         "(needs --as an owner or operator seat); works with "
                         "every subcommand")
    ch.add_argument("--version", type=int, default=None,
                    help="for `show`: read one archived version verbatim")
    ch.add_argument("--edit", action="store_true",
                    help="for `set`: open $EDITOR on the charter in force; "
                         "saving publishes, an empty or unchanged buffer aborts")
    ch.add_argument("--from-default", dest="from_default", action="store_true",
                    help="for `set`: publish the PACKAGED text (hub: the role "
                         "charter; --channel: that room's seed charter)")
    ch.add_argument("-y", "--yes", action="store_true",
                    help="for `set`: skip the confirmation (the diff still "
                         "prints); implied when stdin is not a terminal")
    # `--diff` is both a flag (`show --diff` = what the version in force
    # changed) and a value (`history --diff 3`). One option, because an
    # operator asking "what changed?" should not have to know which.
    ch.add_argument("--diff", nargs="?", type=int, const=-1, default=None,
                    metavar="N",
                    help="show a unified diff instead of the text: bare = the "
                         "version in force, `--diff N` = what version N changed")
    ch.add_argument("--as", dest="as_agent", default=None,
                    help="agent identity (required with --channel)")
    ch.add_argument("--url", default=None)
    ch.add_argument("--admin-key", dest="admin_key", default=None,
                    help="admin key (default: $AGORA_ADMIN_KEY, then config.json)")
    ch.set_defaults(func=cmd_charter)

    lm = sub.add_parser("llm",
                        help="configure (or show) the OpenAI-compatible endpoint "
                             "`agora summarize` / chat `/summary` use (local, 0600)")
    lm.add_argument("--base-url", dest="base_url", default=None,
                    help="e.g. https://api.openai.com/v1 or a local gateway")
    lm.add_argument("--model", default=None, help="model name, e.g. gpt-4o-mini")
    lm.add_argument("--api-key", dest="api_key", default=None,
                    help="provider key (omit for keyless local endpoints)")
    lm.set_defaults(func=cmd_llm)

    sk = sub.add_parser("seed-key",
                        help="import an operator-minted agent key into this "
                             "machine's key cache (~/.agora/keys.json, 0600) "
                             "and verify it against the hub")
    sk.add_argument("agent", help="agent id the key belongs to")
    sk.add_argument("--key", required=True, metavar="AGENT_KEY",
                    help="the agora_... key printed by `agora register`")
    sk.add_argument("--url", default=None)
    sk.set_defaults(func=cmd_seed_key)

    st = sub.add_parser("status", help="check hub + config")
    st.set_defaults(func=cmd_status)

    dr = sub.add_parser("doctor",
                        help="one-screen diagnosis: per seat — reachable? "
                             "owes what? working on what? blocked by what? — "
                             "plus operator requests in flight and hub health")
    dr.add_argument("--as", dest="as_agent", default=None, metavar="AGENT_ID",
                    help="diagnose ONE seat (and the requests it owns/owes)")
    dr.add_argument("--json", action="store_true",
                    help="print the raw diagnostic instead of the table")
    dr.set_defaults(func=cmd_doctor)

    hc = sub.add_parser("harness-check",
                        help="check a framework against the agora harness "
                             "contract; prints a per-capability verdict")
    hc.add_argument("harness", choices=list(SUPPORTED_HARNESSES))
    hc.add_argument("--as", dest="as_agent", default=None, metavar="AGENT_ID")
    hc.add_argument("--url", default=None)
    hc.add_argument("--live", action="store_true",
                    help="additionally run ONE real turn (costs tokens)")
    hc.add_argument("--json", action="store_true")
    hc.set_defaults(func=cmd_harness_check)

    hk = sub.add_parser("hook", help="reception hook entry point invoked by a "
                                     "harness (SessionStart/UserPromptSubmit/"
                                     "PostToolUse/Stop); not for humans")
    hk.add_argument("event", choices=list(_HOOK_EVENTS))
    hk.add_argument("--as", dest="as_agent", default=None, metavar="AGENT_ID")
    hk.add_argument("--url", default=None)
    hk.add_argument("--cursor", action="store_true",
                    help="emit Cursor's followup_message shape instead of "
                         "hookSpecificOutput/decision")
    hk.set_defaults(func=cmd_hook)

    ln = sub.add_parser("listen", help="session-resident listener: emit AGORA_WAKE "
                                       "sentinels when new messages arrive")
    ln.add_argument("--as", dest="as_agent", default=None, metavar="AGENT_ID",
                    help="agent id (default: $AGORA_AGENT_ID, else the nearest "
                         "THIS folder's harness config; no parent search)")
    ln.add_argument("--source", choices=["auto", "file", "ws"], default="auto",
                    help="auto = tail the hub-written notify file when local, "
                         "else WebSocket push (default: auto)")
    ln.add_argument("--once", action="store_true",
                    help="single-shot: exit 2 on the first wake with a digest "
                         "on stderr (the Claude asyncRewake contract)")
    ln.add_argument("--max-wait", dest="max_wait", type=float, default=None,
                    help="--once: exit 0 silently after S seconds without a wake "
                         "(default: wait forever); with --adaptive, the CAP")
    ln.add_argument("--adaptive", action="store_true",
                    help="--once: the tool picks each window itself — 60s when "
                         "active, widening x2 to the --max-wait cap (default "
                         "1200s) when idle; state in listen-<id>.backoff. A "
                         "message returns instantly regardless, so wide idle "
                         "windows cost no latency, only fewer empty inferences")
    # Accepted NO-OP since 0.10.5: the synthetic "initiative wake" was
    # withdrawn (clock-driven uninformed turns are the lurker anti-pattern
    # in initiative costume; initiative now rides claims + the delegate's
    # addressed asks — backlog 0083, deprecated). The flag stays parseable
    # because 0.10.4-generated rules teach it: a hard removal would make
    # every re-arm fail with `unrecognized arguments` (the c2095 class).
    # Deliberately silent at runtime: in --once mode stderr IS the wake
    # payload some harnesses read.
    ln.add_argument("--idle-nudge", dest="idle_nudge", type=float, default=0.0,
                    help="deprecated no-op since 0.10.5 (the initiative "
                         "heartbeat was withdrawn; safe to keep in old "
                         "rules, remove at your next setup regen)")
    ln.add_argument("--debounce", type=float, default=15.0,
                    help="coalesce a burst into ONE wake sentinel (default 15s)")
    ln.add_argument("--important-only", dest="important_only", action="store_true",
                    help="wake only on to-me/reply-to-me/critical/escalated "
                         "or open/blocked")
    ln.add_argument("--preview", action="store_true",
                    help="append a neutralized title preview to wake sentinels "
                         "(default: identifiers only)")
    ln.add_argument("--notify-file", dest="notify_file", default=None,
                    help="ws mode: ALSO append raw notify lines here "
                         "(byte-compatible with hub-written files)")
    ln.add_argument("--lock", default=None,
                    help="lockfile path (default <AGORA_HOME>/listen-<id>.lock); "
                         "a second instance exits 0 immediately")
    ln.add_argument("--heartbeat", type=float, default=300.0,
                    help="touch the pidfile + emit a heartbeat sentinel every "
                         "S seconds (default 300)")
    ln.add_argument("--url", default=None)
    ln.add_argument("--poll", type=float, default=0.5, help=argparse.SUPPRESS)
    ln.set_defaults(func=cmd_listen)

    # Keep parser defaults coupled to the engine constants. These values are
    # operator policy, so duplicating literals here risks a CLI/runtime split.
    from .drive import (DEFAULT_BROADCAST_TURN_BUDGET,
                        DEFAULT_TURN_BUDGET, DEFAULT_WORK_BUDGET,
                        TURN_TIMEOUT)
    from .setup_harness import DRIVABLE_HARNESSES

    dr = sub.add_parser("drive",
                        help="external resume-driver for a dedicated seat: "
                             "wait on obligations, spawn one bounded harness "
                             "turn per wake (owner-run, session-bound)")
    dr.add_argument("harness_pos", nargs="?", choices=DRIVABLE_HARNESSES,
                    help="optional harness selector (same as --harness)")
    dr.add_argument("--as", dest="as_agent", default=None, metavar="AGENT_ID",
                    help="agent id (default: $AGORA_AGENT_ID, else the nearest "
                         "workspace wired by `agora setup`)")
    dr.add_argument("--harness", "--framework", choices=["auto", *DRIVABLE_HARNESSES],
                    default="auto",
                    help="harness to drive (default: auto = use the setup "
                         "record or a single wired harness; multi-harness "
                         "workspaces must choose explicitly)")
    dr.add_argument("--url", default=None)
    dr.add_argument("--model", default=None,
                    help="override the harness model for driven turns "
                         "(default: use the harness's configured/default model)")
    dr.add_argument("--provider", default=None,
                    help="AbstractCode provider override (for example openai "
                         "or ollama); rejected by other harnesses")
    # The UNION of every harness's own vocabulary; each adapter then validates
    # against its own (DriveAdapter.REASONING_VOCAB), so an unsupported value is
    # refused at arm time naming the legal set. `max` used to be offered here
    # and is accepted by NO harness — codex's enum is
    # minimal|low|medium|high|xhigh|ultra and abstractcode's is
    # auto|none|minimal|low|medium|high|xhigh — so it could only ever produce a
    # failing turn, and neither does `ultra` — codex translates it to the API's
    # `max`, which every reachable model rejects. `minimal` was missing despite
    # being valid on AbstractCode.
    dr.add_argument("--reasoning-effort", default=None,
                    choices=["auto", "none", "minimal", "low", "medium",
                             "high", "xhigh", "off", "max", "on"],
                    help="reasoning effort for driven turns (Codex and "
                         "AbstractCode). Values are validated against the "
                         "chosen harness's own vocabulary, which differs by "
                         "vendor")
    dr.add_argument("--harness-arg", dest="harness_args", action="append",
                    default=[], metavar="KEY=VALUE",
                    help="framework-specific argument passed through as "
                         "`--KEY VALUE` (repeatable). agora does not interpret "
                         "it: a framework may need a concept agora has no "
                         "opinion about, and inventing an agora flag per vendor "
                         "concept is how a protocol ends up carrying a "
                         "product's internals")
    dr.add_argument("--max-wait", dest="max_wait", type=float, default=1200.0,
                    help="idle ceiling for each listen window (a wake returns "
                         "instantly regardless; default 1200)")
    dr.add_argument("--permissions", choices=["read", "write", "all"],
                    default=None,
                    help="execution-permission level for driven turns, in "
                         "agora's vocabulary (default: write — write inside "
                         "the workspace, MCP allowed). Each harness declares "
                         "which levels it can express; an inexpressible level "
                         "is refused at arm time naming who supports it")
    dr.add_argument("--sandbox", choices=["enabled", "disabled", "none"],
                    default=None,
                    help="DEPRECATED alias for --permissions "
                         "(enabled=write, disabled/none=all); one release")
    dr.add_argument("--turn-budget", dest="turn_budget", type=int,
                    default=DEFAULT_TURN_BUDGET,
                    help="max spawned turns per rolling hour before parking "
                         f"(light abuse ceiling; default {DEFAULT_TURN_BUDGET})")
    dr.add_argument("--broadcast-turn-budget", dest="broadcast_turn_budget",
                    type=int, default=DEFAULT_BROADCAST_TURN_BUDGET,
                    help="unowned room-wide wake turns per rolling hour "
                         "(anti-storm fuse; addressed/owed work uses the "
                         "main --turn-budget; default "
                         f"{DEFAULT_BROADCAST_TURN_BUDGET})")
    dr.add_argument("--session-rotate", dest="session_rotate", type=int,
                    default=25,
                    help="turns on one harness session before rotating to a "
                         "fresh one (context-bloat + residue flush; default 25)")
    dr.add_argument("--reception-timeout", type=float, default=None,
                    help="seconds a RECEPTION turn may take (default 600). "
                         "Raise it for slow local inference: a 122B model "
                         "cannot finish whoami+charter+discovery in 600s, and "
                         "every boot turn dies at no-tool-calls having done "
                         "real work")
    dr.add_argument("--work-timeout", dest="work_timeout", type=float,
                    default=TURN_TIMEOUT,
                    help="hard cap for one spawned work chunk in seconds "
                         f"(default and maximum {TURN_TIMEOUT:.0f}; a full "
                         "job may span many chunks)")
    dr.add_argument("--work-budget", dest="work_budget", type=int,
                    default=DEFAULT_WORK_BUDGET,
                    help="max work chunks per rolling hour (default "
                         f"{DEFAULT_WORK_BUDGET}; "
                         "light runaway fuse only; "
                         "separate pool — reception's --turn-budget is "
                         "never consumed by work)")
    dr.add_argument("--force", action="store_true",
                    help="bypass the fresh interactive-listener guard. It "
                         "NEVER overrides a live driver.")
    dr.add_argument("--turn-log", dest="turn_log", nargs="?", const="default",
                    default=None, metavar="PATH",
                    help="FLIGHT RECORDER: append every spawned turn's full "
                         "event stream as JSONL (turn_start / raw harness "
                         "stdout / turn_stderr / "
                         "turn_end with duration+outcome). Bare flag logs "
                         "to ~/.agora/drive-<id>.turns.jsonl; pass PATH to "
                         "choose. File is 0600; writes are best-effort and "
                         "never break a turn. Off by default")
    dr.add_argument("--once", action="store_true",
                    help="drive a single turn now (boot) and exit")
    dr.add_argument("--max-turns", dest="max_turns", type=int, default=None,
                    help=argparse.SUPPRESS)   # harness/testing bound
    dr.set_defaults(func=cmd_drive)

    # --- agent-facing verbs (identity via --as) ---
    def _agent_parser(name, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--as", dest="as_agent", required=True, metavar="AGENT_ID",
                        help="act as this agent id (e.g. runtime)")
        sp.add_argument("--url", default=None)
        return sp

    _agent_parser("whoami", "print your identity").set_defaults(func=cmd_whoami)
    _agent_parser("board", "your decision board: pending on you, queued, "
                           "in progress, awaiting review, done").set_defaults(func=cmd_board)
    _agent_parser("stats", "hub activity rate: messages per minute (last 10m) "
                           "and per 10 minutes (last hour), public/dm split, "
                           "active seats, and whether the hub is moving"
                  ).set_defaults(func=cmd_stats)
    _agent_parser("channels", "list channels").set_defaults(func=cmd_channels)

    sm = _agent_parser("summarize", "LLM summary of the hub from your view "
                                    "(default), or --channel C / --agent ID")
    sm.add_argument("--channel", default=None, help="scope to one channel")
    sm.add_argument("--agent", default=None, metavar="AGENT_ID",
                    help="scope to everything about one peer (your DM + their "
                         "activity in your shared channels)")
    sm.set_defaults(func=cmd_summarize)

    gp = _agent_parser("group", "one line -> focused private room: "
                                "agora group TOPIC TEXT @seat1 @seat2")
    gp.add_argument("text", nargs="+",
                    help="topic text with @seat mentions anywhere in it")
    gp.set_defaults(func=cmd_group)

    cc = _agent_parser("create-channel",
                       "create a channel (the --as agent becomes owner)")
    cc.add_argument("name", help="channel name (simple slug: no spaces/slashes)")
    cc.add_argument("--public", action="store_true",
                    help="anyone may join (default: private, invite-only)")
    cc.add_argument("--purpose", "--about", dest="purpose", default=None,
                    metavar="TEXT",
                    help="one-line purpose stored in channel:meta "
                         "(what describe_channel shows joiners)")
    cc.add_argument("--invite", action="append", default=None,
                    metavar="AGENT_ID",
                    help="initial member to invite (repeatable): private = "
                         "mint + DM an invite token; public = DM a join "
                         "pointer")
    cc.set_defaults(func=cmd_create_channel)

    st = _agent_parser("store", "channel store from the terminal: "
                                "agora store get|set|list ... (the write "
                                "path that survives a flaky MCP bridge)")
    st.add_argument("store_action", choices=["get", "set", "list"])
    st.add_argument("channel", help="channel name")
    st.add_argument("key", nargs="?", default=None,
                    help="store key (get/set)")
    st.add_argument("value", nargs="?", default=None,
                    help="JSON value (set)")
    st.add_argument("--expect-version", dest="expect_version", type=int,
                    default=None, help="CAS guard (0 = must not exist)")
    st.add_argument("--prefix", default="", help="filter for list")
    st.set_defaults(func=cmd_store)

    ad = _agent_parser("add", "invite seats to an EXISTING room you own: "
                              "agora add CHANNEL seat1 seat2 [--why ...] "
                              "(mid-task member addition, pilot lesson)")
    ad.add_argument("channel", help="the room (you must own it for private rooms)")
    ad.add_argument("agents", nargs="+", help="agent ids to invite")
    ad.add_argument("--why", default="",
                    help="one line: why the work needs their voice (DM'd along)")
    ad.set_defaults(func=cmd_add)

    ib = _agent_parser("inbox", "show unread envelopes (optionally long-poll)")
    ib.add_argument("--wait", type=float, default=0.0, help="block up to N seconds for a message")
    ib.set_defaults(func=cmd_inbox)

    rd = _agent_parser("read", "read a message body (+ unread reply chain)")
    rd.add_argument("--channel", required=True); rd.add_argument("--id", required=True)
    rd.set_defaults(func=cmd_read)

    hi = _agent_parser("history", "read channel history")
    hi.add_argument("--channel", required=True)
    hi.add_argument("--since", type=int, default=0); hi.add_argument("--limit", type=int, default=200)
    hi.set_defaults(func=cmd_history)

    po = _agent_parser("post", "post a message to a channel")
    po.add_argument("--channel", required=True)
    po.add_argument("--status", default="fyi", choices=["open", "reply", "fyi", "blocked", "resolved"])
    po.add_argument("--urgency", default="inbox", choices=["inbox", "next_turn", "interrupt"])
    po.add_argument("--title", default="")
    # append, not store: `--to a --to b` must address BOTH. The plain store
    # action silently kept only the last flag — a four-seat commission went
    # out addressed to one seat and nobody could tell (fund1, commons#6).
    po.add_argument("--to", action="append", default=None,
                    help="recipient seat id (repeatable, or comma-separated)")
    po.add_argument("--reply-to", dest="reply_to", default=None)
    po.add_argument("--critical", action="store_true"); po.add_argument("--data", default=None)
    po.add_argument("--notice-kind", choices=NOTICE_KINDS)
    po.add_argument("--notice-key", help="stable event id; repeated keys are refused")
    po.add_argument("--ask", action="append", metavar="ID:TEXT",
                    help="a numbered ask (repeatable), e.g. --ask '1:confirm the payload cap?'")
    po.add_argument("--answer", default=None, metavar="IDS",
                    help="comma-separated ask ids this reply discharges, e.g. --answer 1,3")
    po.add_argument("--consumes", default=None, metavar="REFS",
                    help="comma-separated consumption debts THIS message "
                         "settles (channel#seq or message ids), e.g. "
                         "--consumes commons#412,commons#418 — one message "
                         "instead of one receipt per thread")
    po.add_argument("--attach", action="append", metavar="SHA256[:NAME]",
                    help="attach an uploaded blob by id (repeatable; "
                         "upload first with `agora attachment put`)")
    po.add_argument("body")
    po.set_defaults(func=cmd_post)

    dm = _agent_parser("dm", "send a private 1:1 message")
    dm.add_argument("--to", required=True)
    dm.add_argument("--status", default="fyi", choices=["open", "reply", "fyi", "blocked", "resolved"])
    dm.add_argument("--urgency", default="inbox", choices=["inbox", "next_turn", "interrupt"])
    dm.add_argument("--title", default="")
    dm.add_argument("--attach", action="append", metavar="SHA256[:NAME]",
                    help="attach an uploaded blob by id (upload to the dm:<a>--<b> "
                         "channel with `agora attachment put` first)")
    dm.add_argument("--ask", action="append", metavar="ID:TEXT",
                    help="a numbered ask (repeatable; required for "
                         "--status blocked), e.g. --ask '1:which schema?'")
    dm.add_argument("--reply-to", dest="reply_to", default=None,
                    help="message id this DM answers (thread it: an "
                         "unthreaded answer discharges nothing)")
    dm.add_argument("--answer", default="",
                    help="ask ids this reply discharges, e.g. --answer 1,3 "
                         "(requires --reply-to)")
    dm.add_argument("body")
    dm.set_defaults(func=cmd_dm)

    ak = _agent_parser("ack", "advance your triage cursor")
    ak.add_argument("--channel", required=True); ak.add_argument("--seq", type=int, required=True)
    ak.set_defaults(func=cmd_ack)

    fs = _agent_parser("fs", "channel virtual file system (vfs): ls/read/write/rm/hist")
    fs.add_argument("--channel", required=True)
    fs.add_argument("fs_action", choices=["ls", "read", "write", "rm", "hist"])
    fs.add_argument("path", nargs="?", default=None, help="file path (omit for ls)")
    fs.add_argument("--prefix", default=None, help="ls: only paths under this prefix")
    fs.add_argument("--file", default="-", help="write: read content from this file ('-' = stdin)")
    fs.add_argument("--binary", action="store_true",
                    help="write: force base64 upload even when the bytes "
                         "decode as utf-8 (non-utf-8 input goes base64 on its own)")
    fs.add_argument("--out", default=None,
                    help="read: write the (decoded) content to this file "
                         "instead of stdout (required for binary on a terminal)")
    fs.add_argument("--expect-version", dest="expect_version", type=int, default=None,
                    help="CAS guard: expected current version (0 = must not exist)")
    fs.add_argument("--version", type=int, default=None,
                    help="read: return this archived version instead of the head")
    fs.add_argument("--describe", default=None,
                    help="write: one line saying what this file IS (shown in listings)")
    fs.set_defaults(func=cmd_fs)

    ar = _agent_parser("archive-channel", "archive a channel (evict + delist, history kept); --undo reopens")
    ar.add_argument("--channel", required=True)
    ar.add_argument("--undo", action="store_true", help="reopen an archived channel (operator only)")
    ar.set_defaults(func=cmd_archive_channel)

    # Operator lifecycle verb (NOT _agent_parser): authority is an operator
    # agent key via --as OR the hub's admin key, exactly like register/pause/
    # rules. Requiring --as was the c3707 refusal — the hub machine holds the
    # admin key but no operator agent identity.
    rt = sub.add_parser("retire", help="retire an agent (neutral decommission, "
                                       "operator/admin); --undo restores, --list shows retired")
    rt.add_argument("agent", nargs="?", default=None, help="the agent id to retire")
    rt.add_argument("--as", dest="as_id", default=None, metavar="AGENT_ID",
                    help="act as this operator agent id (else the admin key is used)")
    rt.add_argument("--reason", default=None, help="neutral reason (stored, never 'banned')")
    rt.add_argument("--undo", action="store_true", help="restore a retired agent")
    rt.add_argument("--delete", action="store_true",
                    help="hard-delete a RETIRED agent: off every list, final "
                         "(history keeps attribution; id stays reserved)")
    rt.add_argument("--list", action="store_true", help="list retired agents (operator)")
    rt.add_argument("--url", default=None)
    rt.add_argument("--admin-key", dest="admin_key", default=None,
                    help="admin key (default: $AGORA_ADMIN_KEY, then config.json)")
    rt.set_defaults(func=cmd_retire)

    at = _agent_parser("attachment", "message attachments: put a file / get by id")
    at.add_argument("--channel", required=True)
    at.add_argument("att_action", choices=["put", "get"])
    at.add_argument("file", nargs="?", default=None, help="put: the local file to upload")
    at.add_argument("--id", default=None, help="get: the attachment's sha256 id")
    at.add_argument("--out", default=None, help="get: write bytes to this path")
    at.add_argument("--content-type", dest="content_type", default=None,
                    help="put: declared type (default: guessed from the filename)")
    at.set_defaults(func=cmd_attachment)

    de = _agent_parser("describe", "show channel metadata + members")
    de.add_argument("--channel", required=True); de.set_defaults(func=cmd_describe)

    wh = _agent_parser("who", "presence of agents you share channels with")
    wh.set_defaults(func=cmd_who)

    ct = _agent_parser("chat", "live chat/observation REPL (the human's window)")
    ct.add_argument("--channel", default=None, help="enter this room immediately")
    ct.set_defaults(func=cmd_chat)

    dg = _agent_parser("digest", "fold a channel into open/decided/decisions")
    dg.add_argument("--channel", required=True); dg.set_defaults(func=cmd_digest)

    lg = _agent_parser("ledger", "print a channel's verbatim ledger (transcript + verified head)")
    lg.add_argument("--channel", required=True); lg.set_defaults(func=cmd_ledger)

    # `join` carries TWO verbs (disambiguated in cmd_join, both/neither = loud
    # error): machine onboarding via a pasted artifact, and the original
    # channel join. Built by hand (not _agent_parser): --as is only mandatory
    # for the channel mode.
    jn = sub.add_parser("join",
                        help="onboard this machine with a pasted invite "
                             "(agora join AGORA1....) — or join a channel "
                             "(--channel NAME)")
    jn.add_argument("artifact", nargs="?", default=None,
                    help="AGORA1.... one-paste artifact from `agora invite` "
                         "(whitespace/line-wraps from chat are tolerated)")
    jn.add_argument("--as", dest="as_agent", default=None, metavar="AGENT_ID",
                    help="channel mode: act as this id (required); onboarding: "
                         "the id to claim when the artifact pins none")
    jn.add_argument("--channel", default=None,
                    help="channel mode: channel to join (public = no invite)")
    jn.add_argument("--invite", default=None,
                    help="channel mode: invite token for a private channel")
    jn.add_argument("--token", default=None, metavar="JOIN_TOKEN",
                    help="onboarding: raw agora-join_... token (explicit "
                         "alternative to the artifact; needs --url)")
    jn.add_argument("--url", default=None,
                    help="onboarding with --token: hub url (the artifact "
                         "form carries it)")
    jn.add_argument("--about", default="",
                    help="onboarding: self-description for the new agent")
    # Derived, not retyped: this list was hand-copied once and silently lost
    # `abstractcode-tui` — a harness you could set up but not join with.
    jn.add_argument("--harness", "--framework",
                    choices=["auto", "all", *SUPPORTED_HARNESSES, "none"],
                    default="auto",
                    help="onboarding: workspace wiring to install "
                         "(default auto = reuse existing footprints, otherwise "
                         "prompt; none = register + cache key only)")
    jn.add_argument("--workspace", default=".",
                    help="onboarding: workspace folder (default: cwd)")
    jn.add_argument("--with-hook", action="store_true",
                    help="deprecated compatibility alias; hooks install by "
                         "default now. Use --no-hook to skip them.")
    jn.add_argument("--no-hook", action="store_true",
                    help="onboarding: skip hook installation and wake wiring")
    jn.add_argument("--listen", action="store_true",
                    help="onboarding: arm a FOREGROUND `agora listen "
                         "--source ws` after wiring (headless nodes)")
    jn.add_argument("--vendor-bootstrap", action="store_true",
                    help="onboarding: also run the harness's own registration "
                         "convenience when supported (`claude mcp add --scope "
                         "local` or `codex mcp add`)")
    jn.set_defaults(func=cmd_join)

    iv = sub.add_parser("invite",
                        help="operator: mint a join token + one-paste line "
                             "for a remote machine (hub membership; for "
                             "CHANNEL invites use `agora join --channel` / "
                             "the invite_agent tool)")
    iv.add_argument("agent", nargs="?", default=None,
                    help="agent id the token is locked to (omit only with "
                         "--any-id)")
    iv.add_argument("--channels", default="",
                    help="comma-separated PUBLIC channels the joiner enters "
                         "automatically")
    iv.add_argument("--ttl", default="24h",
                    help="token lifetime, e.g. 90s/30m/24h/7d "
                         "(default 24h, cap 30d)")
    iv.add_argument("--uses", type=int, default=1,
                    help="redemptions allowed (default 1 = single-use, "
                         "max 100 for fleet provisioning)")
    iv.add_argument("--any-id", dest="any_id", action="store_true",
                    help="do not lock the token to an id (joiner picks via "
                         "`agora join ... --as <id>`)")
    iv.add_argument("--about", default="",
                    help="default self-description for the joiner")
    iv.add_argument("--url", default=None,
                    help="hub url AS REACHABLE FROM THE REMOTE "
                         "(e.g. http://<lan-ip>:8765 — a loopback url is "
                         "warned about)")
    iv.add_argument("--admin-key", dest="admin_key", default=None,
                    help="admin key (default: $AGORA_ADMIN_KEY, then "
                         "config.json)")
    iv.add_argument("--list", action="store_true",
                    help="list live join tokens (audit; no secrets)")
    iv.add_argument("--revoke", default=None, metavar="TOKEN_ID",
                    help="revoke a token by the public id shown at mint/--list")
    iv.set_defaults(func=cmd_invite)

    sa = _agent_parser("set-about", "set your self-description")
    sa.add_argument("text"); sa.set_defaults(func=cmd_set_about)

    nt = _agent_parser("note", "save a private colleague note")
    nt.add_argument("--about", dest="about_agent", required=True, metavar="AGENT_ID")
    nt.add_argument("text"); nt.set_defaults(func=cmd_note)

    wk = _agent_parser("work", "everything citing one work id: claims, "
                               "decisions, messages (the Option-A stitch)")
    wk.add_argument("item_id", help="ruled work id, e.g. agora-0093")
    wk.set_defaults(func=cmd_work)

    sr = _agent_parser("search", "hub-wide grouped search over everything "
                                 "you can read (the task-context digest)")
    sr.add_argument("terms", nargs="*", default=[],
                    help="free words; optional with --rated (browse by votes)")
    sr.add_argument("--kind", default="", help="message|decision|claim|work|file|agent")
    sr.add_argument("--channel", default="", help="narrow to one channel")
    sr.add_argument("--sender", default="", help="narrow to one sender")
    sr.add_argument("--rated", default="", choices=["", "up", "down", "any"],
                    help="only hits with standing votes (lessons mining)")
    sr.add_argument("--min-votes", dest="min_votes", type=int, default=0)
    sr.add_argument("--sort", default="relevance",
                    choices=["relevance", "recent", "votes"])
    sr.add_argument("--limit", type=int, default=10)
    sr.add_argument("--json", action="store_true", help="raw typed report")
    sr.add_argument("--mode", default="", choices=["", "lexical", "semantic"],
                    help="override (default auto: fuses when the semantic "
                         "index is ready; lexical = words only, semantic = "
                         "meaning only)")
    sr.set_defaults(func=cmd_search)

    rc = _agent_parser("retract", "unsay your own message: redact it "
                                  "everywhere + clear its obligation")
    rc.add_argument("channel", help="the channel the message is in")
    rc.add_argument("message_id", help="the id of YOUR message to retract")
    rc.set_defaults(func=cmd_retract)

    rt = _agent_parser("rate", "cast/revise your ONE live reputation vote "
                               "on a colleague (evidence-based)")
    rt.add_argument("target", help="the colleague being rated")
    rt.add_argument("--channel", required=True,
                    help="the channel you share (scores are per-channel)")
    rt.add_argument("--axis", required=True,
                    choices=["trust", "wisdom", "thorough", "helper"],
                    help="trust=does what it says; wisdom=often right; "
                         "thorough=end-to-end with proofs; helper=improves "
                         "others' work")
    rt.add_argument("--value", required=True, choices=["+1", "1", "-1"],
                    help="+1 or -1 (revising replaces, never stacks)")
    rt.add_argument("--note", default="",
                    help="one-line WHY (on the record, max 280 chars)")
    rt.set_defaults(func=cmd_rate)

    lb = _agent_parser("leaderboard", "reputation leaderboard "
                                      "(--channel C, or hub-wide sum)")
    lb.add_argument("--channel", default=None,
                    help="one channel's board (default: hub-wide)")
    lb.set_defaults(func=cmd_leaderboard)

    mi = _agent_parser("mirror", "export channels to append-only markdown files")
    mi.add_argument("--out", required=True, help="output directory for <channel>.md files")
    mi.add_argument("--watch", action="store_true", help="keep files live via push")
    mi.set_defaults(func=cmd_mirror)

    wt = _agent_parser("watch", "stream new messages (non-blocking trigger)")
    wt.add_argument("--channel", default=None, help="one channel (default: all yours)")
    wt.add_argument("--notify-file", dest="notify_file", default=None,
                    help="append one JSON line per message to this file")
    wt.add_argument("--exec", dest="exec_cmd", default=None,
                    help="shell command to run per message (AGORA_MSG_* in env)")
    wt.add_argument("--pidfile", default=None,
                    help="write this watcher's PID here (removed on exit) so a "
                         "harness can tell a live watcher from a dead one")
    wt.set_defaults(func=cmd_watch)

    # EVERY verb accepts --home: hub selection must not depend on remembering
    # an env-var prefix, and partial coverage would be its own trap (the
    # `--with-hooks` lesson: a flag that exists on one verb but not its
    # sibling reads as a typo). main() maps it onto AGORA_HOME before
    # dispatch, so commands and their child processes all see the same home.
    for sp in set(sub.choices.values()):
        sp.add_argument("--home", default=None, metavar="PATH",
                        help="agora home for this invocation (sets AGORA_HOME; "
                             "default: $AGORA_HOME, else ~/.agora)")
    return p


def main() -> None:
    args = build_parser().parse_args()
    _apply_home(args)                 # --home wins over $AGORA_HOME, if given
    try:
        args.func(args)
    except SystemExit:
        raise
    except BrokenPipeError:
        # A downstream consumer (head, jq -e, a truncating harness) closed the
        # pipe early. Without this handler Python exits 120 (failed stdout
        # flush at shutdown), which scripts misread as a semantic signal.
        # For READER commands the work completed: exit 0. For long-runners
        # (up/watch/mirror/listen) a broken pipe means dying mid-stream: exit 1
        # so a supervisor (or the arming ritual) sees the failure (audit M3).
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(1 if args.cmd in ("up", "watch", "mirror", "listen") else 0)
    except Exception as e:  # noqa: BLE001 — one clean line, not a stack trace
        # Hub refusals (AgoraError) and connection problems reach humans and
        # scripts as a single actionable line; exit 1 keeps it scriptable.
        # (Import from the module: the package __init__ does not re-export it,
        # which used to crash this very handler with an ImportError.)
        from .client.client import AgoraError
        if isinstance(e, AgoraError):
            sys.exit(f"agora {args.cmd} failed: {e}")
        import httpx
        if isinstance(e, httpx.HTTPError):
            sys.exit(f"agora {args.cmd} failed: cannot reach the hub ({e}); "
                     "is it running? (agora status)")
        raise


if __name__ == "__main__":
    main()
