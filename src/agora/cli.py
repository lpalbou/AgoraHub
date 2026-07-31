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
import sys
import time
from pathlib import Path

from . import config as _config
from .hook import HOOK_EVENTS as _HOOK_EVENTS
from .setup_harness import SUPPORTED_HARNESSES
from . import db_locate as _db_locate
from .agent_id import validate_agent_id
from .models import NOTICE_KINDS


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
    daemons die): name the pid+command and exit 3."""
    holder = _port_holder(host, port)
    if holder is None:
        return  # free: proceed to bind
    # Something listens. Is it a real agora hub?
    is_hub, version = False, "?"
    try:
        import httpx
        r = httpx.get(f"{url}/healthz", timeout=3.0)
        body = r.json()
        if r.status_code == 200 and body.get("protocol", "").startswith("agora/"):
            is_hub, version = True, body.get("version", "?")
    except Exception:
        pass  # not an agora hub (or not answering) — a squatter path below
    pid, cmd = holder
    if is_hub and not force:
        print(f"an agora hub is ALREADY running at {url} "
              f"(version {version}) — nothing to do. "
              "Stop it first if you meant to restart, or take the port "
              "over with `agora up --force`.", file=sys.stderr)
        raise SystemExit(0)
    if is_hub and force:
        if not pid:
            print(f"REFUSING to start: a hub answers at {url} but its pid "
                  "is unidentifiable (lsof missing or opaque) — nothing "
                  "safe to kill. Stop it by hand and retry.",
                  file=sys.stderr)
            raise SystemExit(3)
        import signal
        print(f"--force: taking over port {port} from the running agora "
              f"hub (version {version}, pid {pid}) — SIGTERM, then "
              "SIGKILL if it lingers.", file=sys.stderr)
        for sig, grace in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass  # already gone
            except PermissionError:
                print(f"REFUSING to start: pid {pid} is not ours to kill "
                      "(EPERM) — another user owns that hub.",
                      file=sys.stderr)
                raise SystemExit(3)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if _port_holder(host, port) is None:
                    print(f"--force: port {port} is free; starting fresh.",
                          file=sys.stderr)
                    return
                time.sleep(0.25)
        print(f"REFUSING to start: pid {pid} survived SIGTERM and SIGKILL "
              f"and port {port} is still held — inspect it by hand.",
              file=sys.stderr)
        raise SystemExit(3)
    who = f"pid {pid} ({cmd})" if pid else "an unidentified process"
    print(
        f"REFUSING to start: port {port} is held by {who} — NOT an agora "
        f"hub. This is exactly the silent-squatter class that left the room "
        f"deaf for 16h (a stray static file server on the hub port). Free "
        f"the port (kill {pid or 'that pid'}) and retry, or start on a "
        f"different port with --port. (--force never kills an UNVERIFIED "
        f"process — only a hub that answers /healthz as agora.)",
        file=sys.stderr)
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
        if r.status_code == 200 and r.json().get(
                "protocol", "").startswith("agora/"):
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
    # Pin WS keepalive explicitly: connection-derived presence relies on dead
    # sockets being detected within a bounded window (audit M4). Defaults can
    # differ per uvicorn/ws backend; make the bound deliberate.
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                ws_ping_interval=20.0, ws_ping_timeout=20.0)


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
            "  reception: `agora drive abstractcode` owns unattended wakes; "
            "AbstractCode exposes no native hook registration surface, so the "
            "generic --with-hook default requires no extra hook file."
        )
        lines.append(
            "  launch: run `abstractcode --state-file "
            ".abstractcode/agora.state.json --skill agora-channels` for an "
            "interactive session, or `agora drive abstractcode` unattended."
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
                              api_key=api_key, dedicated=headless)
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
    if headless:
        lines.append("  --headless is a deprecated no-op (wiring is "
                     "mode-free). Dedicated quickstart: "
                     f"cd {workspace} && agora drive")
    lines.append("  note: interactive Codex has no idle-wake surface; with "
                 "the Stop hook it drains bursts at turn ends, otherwise "
                 "messages wait for the next turn. Use `agora drive` for an "
                 "unattended seat.")
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
    if headless and (len(harnesses) != 1
                     or harnesses[0] not in ("cursor", "codex", "abstractcode")):
        sys.exit("agora setup: --headless requires exactly one harness "
                 "(`--harness cursor|codex|abstractcode`)")
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
    collaboration — cannot target operators or other delegates). Grants
    expire (default 7d, cap 30d)."""
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
            print(f"{d['agent_id']:<16} {'+'.join(d['powers']):<32} until {until}{note}")
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
                 "[--ttl 7d] [--note TEXT]   (or --list / --revoke AGENT)")
    try:
        ttl = parse_ttl(args.ttl) if args.ttl else None
    except ValueError as e:
        sys.exit(str(e))
    r = httpx.put(f"{url}/admin/delegation", headers=headers, timeout=10.0,
                  json={"agent_id": args.agent,
                        "powers": [p.strip() for p in args.powers.split(",") if p.strip()],
                        "ttl_seconds": ttl, "note": args.note or ""})
    if r.status_code != 200:
        sys.exit(f"delegation failed: {r.status_code} {r.text}")
    g = r.json()
    until = time.strftime("%Y-%m-%d %H:%M", time.localtime(g["expires_at"]))
    print(f"delegated {'+'.join(g['powers'])} to {g['agent_id']} until {until} "
          "(announced in hub-alerts; visible in every whoami)")


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
                   json={"id": args.agent, "about": args.about or ""},
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
                print(f"  {r['channel']}#{r['seq']} from {r['from']} "
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
                print(f"  {r['channel']}#{r['seq']} from {r['from']} — {r['q'][:100]}")
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
    """Consult and edit a channel's shared virtual filesystem — the network-
    accessible 'book' that lets agents on different machines share an editable
    workspace without a shared disk. Sub-verbs: ls / read / write / rm / hist."""
    async def go(c, a):
        if a.fs_action != "ls" and not a.path:
            raise SystemExit(f"'agora fs {a.fs_action}' requires a path argument")
        if a.fs_action == "ls":
            for f in await c.fs_list(a.channel, a.prefix or ""):
                desc = f.get("description", "")
                print(f"{f['version']:>4}  {f['updated_by']:<12}  {f['path']}"
                      + (f"  — {desc}" if desc else ""))
        elif a.fs_action == "read":
            print((await c.fs_read(a.channel, a.path,
                                   version=a.version))["content"])
        elif a.fs_action == "write":
            content = sys.stdin.read() if a.file == "-" else Path(a.file).read_text()
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
            # Typed consumption (agora-0118): canonical `sender`, never the
            # deprecated `from` alias the 0.4 bump removes.
            print("YOU OWE (ack clears none of this):")
            for row in owed.to_answer[:10]:
                naming = (f" asks naming you: {row.asks_naming_you}"
                          if row.asks_naming_you else "")
                esc = ", ESCALATED" if row.escalated else ""
                print(f"- ANSWER {row.channel}#{row.seq} from {row.sender}"
                      f" (pending {row.pending_asks},{naming}"
                      f" {row.age_minutes}m{esc}) — read id={row.id},"
                      " reply with answers=[...], DO or claim assigned work")
            for row in owed.to_consume[:10]:
                print(f"- CONSUME {row.channel}#{row.answer_seq}:"
                      f" {row.answered_by} answered YOUR ask {row.your_asks}"
                      f" ({row.age_minutes}m ago) — read id={row.answer_id}"
                      " and use it, or close your thread")
            print()
        if owed and owed.counts.to_close:
            print("ADVISORY — your open threads, fully answered:")
            for row in owed.to_close[:10]:
                print(f"- CLOSE {row.channel}#{row.seq}: "
                      f"{row.answered_by} answered ({row.age_minutes}m ago)"
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
        to = [x.strip() for x in a.to.split(",")] if a.to else []
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
        m = await c.dm(a.to, a.body, title=a.title or "", status=Status(a.status),
                       urgency=Urgency(a.urgency), attachments=attachments,
                       asks=asks)
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
        # Snapshot each channel's virtual filesystem into a SEPARATE tree
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
    from .drive import run_drive

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
    digest = (data.get("report_digest") if isinstance(data, dict) else {}) or {}
    for row in digest.get("delegates") or []:
        age = row.get("period_age_minutes")
        age_s = f"{age:.0f}m" if age is not None else "-"
        if digest.get("paused"):
            flag = "paused (fleet dark)"
        elif row.get("replied"):
            flag = "replied"
        elif row.get("missed_alerted"):
            flag = "MISSED"
        elif row.get("overdue"):
            flag = "overdue"
        else:
            flag = "pending"
        print(f"digest: {row.get('delegate', '?')} period={age_s} {flag}")
    print(f"\n{'agent':<16} {'state':<8} {'listener':<9} {'driver':<8} "
          f"{'unread':>6} {'pending':>7}  oldest-pending")
    # The hub can only see what CONTACTS it: an open-but-idle IDE tab makes no
    # calls, so it honestly reads offline even though it will respond at its
    # next prompt. Spell that out or every operator misreads the table.
    legend = ("  states: idle/working = live push connection | active = made an "
              "authenticated call <10m ago |\n  offline = no contact (an open but "
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
        "codex": ("DEPRECATED no-op: Codex setup is mode-free; run "
                  "`cd <workspace> && agora drive` for a dedicated seat"),
        "abstractcode": ("DEPRECATED no-op: AbstractCode setup is mode-free; "
                         "run `cd <workspace> && agora drive abstractcode`"),
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
                    help="deprecated compatibility hint; requires exactly one "
                         "harness (`cursor`, `codex`, or `abstractcode`)")
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

    dg = sub.add_parser("delegate", help="grant/list/revoke delegation "
                                         "(verifiable hub state; powers: "
                                         "ruling,operational,reporting,moderation)")
    dg.add_argument("agent", nargs="?", default=None)
    dg.add_argument("--powers", default=None,
                    help="comma-separated subset of "
                         "ruling,operational,reporting,moderation")
    dg.add_argument("--ttl", default=None, help="e.g. 7d, 48h (default 7d, cap 30d)")
    dg.add_argument("--note", default="", help="shown in the grant announcement")
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
    po.add_argument("--title", default=""); po.add_argument("--to", default="")
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
    dm.add_argument("body")
    dm.set_defaults(func=cmd_dm)

    ak = _agent_parser("ack", "advance your triage cursor")
    ak.add_argument("--channel", required=True); ak.add_argument("--seq", type=int, required=True)
    ak.set_defaults(func=cmd_ack)

    fs = _agent_parser("fs", "channel virtual filesystem: ls/read/write/rm/hist")
    fs.add_argument("--channel", required=True)
    fs.add_argument("fs_action", choices=["ls", "read", "write", "rm", "hist"])
    fs.add_argument("path", nargs="?", default=None, help="file path (omit for ls)")
    fs.add_argument("--prefix", default=None, help="ls: only paths under this prefix")
    fs.add_argument("--file", default="-", help="write: read content from this file ('-' = stdin)")
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
