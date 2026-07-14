"""`agora` — the one-command front door.

    agora up                         # start the hub with sane, persistent defaults
    agora setup cursor <agent-id>    # wire the CURRENT folder as that agent (one step)
    agora status                     # is the hub up? who am I configured as?

`agora up` picks a stable db (~/.agora/agora.db) and a stable admin key
(generated once, saved to ~/.agora/config.json) so nothing needs to be
remembered or passed around. `setup cursor` writes .cursor/mcp.json + a rule
into a workspace; the MCP server self-registers by agent id on first use, so
there are no keys to copy.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import sys
import time
from pathlib import Path

from . import config as _config


def _resolve_mcp_command() -> str:
    """Absolute path to the agora-mcp executable so Cursor (a GUI app that may
    not inherit the shell PATH) can always find it. Falls back to the bare name."""
    found = shutil.which("agora-mcp")
    if found:
        return found
    sibling = Path(sys.argv[0]).resolve().parent / "agora-mcp"  # next to `agora`
    if sibling.exists():
        return str(sibling)
    return "agora-mcp"


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
        os.environ["AGORA_HOME"] = str(Path(home).expanduser())

DEFAULT_PORT = 8765


def _default_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def cmd_up(args: argparse.Namespace) -> None:
    import uvicorn

    from .hub.app import create_app

    home = _config.home()
    cfg = _config.load_config()
    db_path = args.db or cfg.get("db_path") or str(home / "agora.db")
    admin_key = os.environ.get("AGORA_ADMIN_KEY") or cfg.get("admin_key") or secrets.token_hex(16)
    url = _default_url(args.port)
    _config.save_config(url=url, admin_key=admin_key, db_path=db_path)

    # Hub-written notify files: the hub maintains <id>-inbox.log for every
    # local agent itself, so no watcher processes, supervisors or OS services
    # are ever needed on the hub's machine. --notify-dir '' disables.
    notify_dir = args.notify_dir if args.notify_dir is not None else str(home)

    print(f"agora hub → {url}")
    print(f"  db:     {db_path}")
    print(f"  config: {_config.home() / 'config.json'} (admin key saved; agents self-register)")
    if notify_dir:
        print(f"  notify: {notify_dir}/<agent>-inbox.log (hub-written; nothing to run)")
    # Paste-safe hints (no <angle brackets>: the shell reads `<x>` as a
    # redirect). Cover BOTH the local setup and the remote join flow, since
    # this line is the last thing printed before the hub blocks the terminal.
    print("  local agent:   agora setup cursor AGENT_ID   (run in its workspace)")
    print(f"  remote agent:  agora invite AGENT_ID --url {url}   "
          "(mints a one-paste `agora join ...` line for the other machine)")
    app = create_app(db_path=db_path, admin_key=admin_key,
                     rate_per_minute=args.rate_per_minute,
                     notify_dir=notify_dir or None,
                     notify_rotate_mb=args.notify_rotate_mb)
    # Pin WS keepalive explicitly: connection-derived presence relies on dead
    # sockets being detected within a bounded window (audit M4). Defaults can
    # differ per uvicorn/ws backend; make the bound deliberate.
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                ws_ping_interval=20.0, ws_ping_timeout=20.0)


def _setup_key(url: str, agent_id: str, about: str,
               key_flag: str | None) -> str | None:
    """The agent key a setup command should cache AND embed: seed an
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


def _print_key_placement(written_config: Path) -> None:
    """One consistent ledger line wherever a per-agent key was embedded."""
    print(f"  key: cached in {_config.home() / 'keys.json'} and embedded in "
          f"{written_config} (0600)")
    print("  keep that file out of version control (gitignore it).")


def _print_kickoff(agent_id: str, url: str, *, standing_loop: bool,
                   harness: str = "cursor") -> None:
    """A rule only reaches a harness session's context INSIDE a turn, so a
    just-launched idle session never arms itself — it needs one kick-off turn.
    Print the paste-ready first-turn prompt so the operator can start the agent
    without hand-writing it (the standing-loop variant on harnesses with no
    event wake; the arm-then-end variant otherwise)."""
    from .setup_harness import kickoff_prompt
    print("\nTo start this agent, paste this as its FIRST message:\n")
    print(kickoff_prompt(agent_id, url, standing_loop=standing_loop,
                         harness=harness))


def cmd_setup(args: argparse.Namespace) -> None:
    """One setup verb, three harnesses: `agora setup cursor|claude|codex <id>`.
    The old `setup-cursor|setup-claude|setup-codex` names remain as deprecated
    aliases (simplicity audit: onboarding had two spellings of the same
    selector — `join --harness X` vs `setup-X`)."""
    if getattr(args, "deprecated_alias", None):
        print(f"note: `agora {args.deprecated_alias}` still works but is "
              f"deprecated — prefer `agora setup {args.harness} {args.agent}` "
              "(same flags).")
    if args.harness != "cursor" and getattr(args, "headless", False):
        print("note: --headless applies to the cursor harness only; ignored.")
    dispatch = {"cursor": cmd_setup_cursor, "claude": cmd_setup_claude,
                "codex": cmd_setup_codex}
    dispatch[args.harness](args)


def cmd_setup_cursor(args: argparse.Namespace) -> None:
    """Wire a workspace as a Cursor agent: project `.cursor/mcp.json`, the
    shared etiquette rule, and optionally the shared stop-hook (Cursor's
    followup_message output contract). One generator serves all harnesses —
    see setup_harness.py."""
    from .setup_harness import setup_cursor

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        sys.exit(f"workspace not found: {workspace}")
    url = _hub_url(args)  # honors $AGORA_URL (the silent-127.0.0.1 trap fix)
    api_key = _setup_key(url, args.agent, args.about or "", args.key)
    headless = getattr(args, "headless", False)
    written = setup_cursor(workspace, args.agent, url, args.about or "",
                           _resolve_mcp_command(), args.with_hook,
                           api_key=api_key, headless=headless)
    kind = "Cursor, headless" if headless else "Cursor"
    print(f"configured '{workspace.name}' as agora agent '{args.agent}' ({kind}):")
    for path in written:
        print(f"  wrote {path}")
    if api_key:
        _print_key_placement(written[0])
        print("Open this folder in Cursor. The agent authenticates immediately.")
    else:
        print("Open this folder in Cursor. The agent self-registers on first tool use.")
    _warn_if_not_project_root(workspace, args.agent)
    _print_kickoff(args.agent, url, standing_loop=False, harness="cursor")


def cmd_setup_claude(args: argparse.Namespace) -> None:
    """Wire a workspace as a Claude Code agent: project-scoped `.mcp.json`
    (a file Claude only loads after workspace trust + a one-time /mcp
    approval), etiquette in CLAUDE.md, optionally the Stop hook — PLUS a
    `claude mcp add --scope local` registration so the server connects
    without any approval step at all."""
    from . import setup_harness as _sh

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        sys.exit(f"workspace not found: {workspace}")
    url = _hub_url(args)
    api_key = _setup_key(url, args.agent, args.about or "", args.key)
    written = _sh.setup_claude(workspace, args.agent, url, args.about or "",
                               _resolve_mcp_command(), args.with_hook,
                               api_key=api_key)
    print(f"configured '{workspace.name}' as agora agent '{args.agent}' (Claude Code):")
    for path in written:
        print(f"  wrote {path}")
    if api_key:
        _print_key_placement(written[0])
    registered, detail = _sh.register_claude_local(
        workspace, _resolve_mcp_command(), url, args.agent, args.about or "",
        api_key=api_key, home=_sh.custom_home_env())
    print(f"  {detail}")
    if registered:
        print("Run `claude` in this folder — the 'agora' MCP server is "
              "already registered for you.")
    else:
        print("Run `claude` in this folder and approve the 'agora' MCP "
              "server (/mcp).")
    _warn_if_not_project_root(workspace, args.agent)
    _print_kickoff(args.agent, url, standing_loop=False, harness="claude")


def cmd_setup_codex(args: argparse.Namespace) -> None:
    """Wire a workspace as a Codex CLI agent: project-scoped
    `.codex/config.toml` (loaded only once the project is trusted) and
    etiquette in AGENTS.md — PLUS a `codex mcp add` registration in the
    always-loaded global registry so the server is visible immediately."""
    from . import setup_harness as _sh

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        sys.exit(f"workspace not found: {workspace}")
    url = _hub_url(args)
    api_key = _setup_key(url, args.agent, args.about or "", args.key)
    written = _sh.setup_codex(workspace, args.agent, url, args.about or "",
                              _resolve_mcp_command(), with_hook=args.with_hook,
                              api_key=api_key)
    print(f"configured '{workspace.name}' as agora agent '{args.agent}' (Codex CLI):")
    for path in written:
        print(f"  wrote {path}")
    config_path = workspace / ".codex" / "config.toml"
    if api_key and config_path in written:
        _print_key_placement(config_path)
    elif api_key:
        # Pre-existing agora table: setup leaves TOML untouched by design, so
        # the fresh key landed only in keys.json. Say so instead of implying
        # the embed happened.
        print(f"  key: cached in {_config.home() / 'keys.json'} (existing "
              f"[mcp_servers.agora] table in {config_path} left untouched — "
              "delete it and re-run to embed the key)")
    registered, detail = _sh.register_codex_global(
        _resolve_mcp_command(), url, args.agent, args.about or "",
        api_key=api_key, home=_sh.custom_home_env())
    print(f"  {detail}")
    print("Run `codex` in this folder"
          + (" (trusting the project when prompted pins this workspace's "
             "identity)." if registered
             else " and trust the project when prompted."))
    if args.with_hook:
        print("Then review/approve the Stop hook once via /hooks (re-approve "
              "if the hook file ever changes).")
    # Codex has no idle-wake surface, so be honest here instead of promising
    # push (session resumes are forbidden by the "hub never creates turns"
    # boundary).
    print("Note: Codex has no idle-wake surface; the Stop hook drains bursts at "
          "turn ends, otherwise messages wait for the next turn (that is "
          "expected). Harnesses with a wake surface use `agora listen`.")
    _warn_if_not_project_root(workspace, args.agent)
    # Codex has no event wake, so reachability needs a standing loop (only in a
    # session no human shares — it deliberately waits).
    _print_kickoff(args.agent, url, standing_loop=True)


def _warn_if_not_project_root(workspace: Path, agent_id: str) -> None:
    """Harnesses anchor per-project config at the project root (git root for
    CLI harnesses) — warn when this folder isn't one (same trap as Cursor)."""
    if (workspace / ".git").exists():
        return
    git_root = next((p for p in workspace.parents if (p / ".git").exists()), None)
    if git_root is not None:
        print(f"note: '{workspace}' is not a git root; CLI harnesses may anchor"
              f" at '{git_root}' and ignore this folder's config. Prefer a"
              " folder that is its own repo, or use the terminal CLI"
              f" (`agora inbox --as {agent_id}`) which needs no MCP config.")


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
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    print(f"agent '{args.agent}' registered at {url} (operator=false)")
    print(f"  api_key: {payload['api_key']}")
    print("shown exactly ONCE (the hub stores only its hash). On the agent's "
          "machine:")
    print(f"  agora seed-key {args.agent} --url {url} --key <that key>")
    print(f"  (or: agora setup cursor {args.agent} --url {url} --key <that key>)")


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
    sent to the hub (the hub makes no LLM calls)."""
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


def cmd_channels(args):
    async def go(c, a):
        for ch in await c.list_channels():
            mark = "*" if ch["member"] else " "
            vis = "public" if not ch["private"] else "private"
            print(f" {mark} {ch['name']:32} {vis}")
        print("\n (* = you are a member)")
    _run_agent_cmd(args, go)


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
            if a.public:
                await c.dm(invitee,
                           f"Channel '{a.name}' is open — join it with "
                           f"join_channel(channel={a.name!r}).",
                           title=f"join {a.name}")
                print(f"  invited {invitee} (public: DM'd a join pointer)")
            else:
                token = await c.create_invite(a.name, agent_id=invitee)
                await c.dm(invitee,
                           f"You are invited to channel '{a.name}'. Join with "
                           f"join_channel(channel={a.name!r}, "
                           f"invite_token={token!r}).",
                           title=f"invite to {a.name}")
                print(f"  invited {invitee} (invite token DM'd)")
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
        if owed and (owed["counts"]["to_answer"] or owed["counts"]["to_consume"]):
            print("YOU OWE (ack clears none of this):")
            for row in owed["to_answer"][:10]:
                naming = (f" asks naming you: {row['asks_naming_you']}"
                          if row.get("asks_naming_you") else "")
                esc = ", ESCALATED" if row.get("escalated") else ""
                print(f"- ANSWER {row['channel']}#{row['seq']} from {row['from']}"
                      f" (pending {row['pending_asks']},{naming}"
                      f" {row['age_minutes']}m{esc}) — read id={row['id']},"
                      " reply with answers=[...], DO or claim assigned work")
            for row in owed["to_consume"][:10]:
                print(f"- CONSUME {row['channel']}#{row['answer_seq']}:"
                      f" {row['answered_by']} answered YOUR ask {row['your_asks']}"
                      f" ({row['age_minutes']}m ago) — read id={row['answer_id']}"
                      " and use it, or close your thread")
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
        # --ask "1:question text" (repeatable) -> numbered asks on an open/blocked msg
        asks = None
        if a.ask:
            asks = []
            for spec in a.ask:
                aid, _, text = spec.partition(":")
                asks.append({"id": aid.strip(), "text": text.strip()})
        # --answer 1,3 -> ask ids this reply discharges
        answers = [x.strip() for x in a.answer.split(",")] if a.answer else None
        m = await c.post(a.channel, a.body, title=a.title or "",
                         status=Status(a.status), urgency=Urgency(a.urgency),
                         to=to, critical=a.critical, data=data, reply_to=a.reply_to,
                         asks=asks, answers=answers)
        print(f"posted to {a.channel} as {args.as_agent}: seq {m.seq}, id {m.id}")
    _run_agent_cmd(args, go)


def cmd_dm(args):
    from .models import Status, Urgency

    async def go(c, a):
        m = await c.dm(a.to, a.body, title=a.title or "", status=Status(a.status),
                       urgency=Urgency(a.urgency))
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
        from .join import decode_artifact, run_join
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
        code = run_join(url=url, token=token, agent_id=args.as_agent,
                        about=args.about or "", harness=args.harness,
                        workspace=args.workspace, with_hook=args.with_hook,
                        listen=args.listen, mcp_command=_resolve_mcp_command(),
                        pinned_id=pinned, expires_hint=expires)
        if code:
            sys.exit(code)
        agent_id = pinned or args.as_agent
        if args.harness and args.harness != "none" and agent_id:
            # Codex has no event wake → standing loop; others arm-then-end.
            _print_kickoff(agent_id, url,
                           standing_loop=(args.harness == "codex"),
                           harness=args.harness)
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


def cmd_drive(args: argparse.Namespace) -> None:
    """The external resume-driver for a HEADLESS seat: block cheaply in
    `agora listen --once --important-only`, and on an obligation wake spawn
    ONE bounded `cursor-agent -p --resume` turn that acts and yields by
    returning. Reception becomes structural (yield = process exit; the
    check->ack->re-arm trap is impossible). Owner-run, session-bound, never
    hub machinery. See drive.py."""
    from .drive import run_drive

    sys.exit(run_drive(
        agent_id=args.as_agent, url=args.url, model=args.model,
        max_wait=args.max_wait, sandbox=args.sandbox,
        turn_budget=args.turn_budget, session_rotate=args.session_rotate,
        once=args.once, max_turns=args.max_turns))


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

    # With the admin key (same machine as `agora up`) also show the per-agent
    # overview. DARK = offline with obligations pending — the dead-agent
    # alarm, as a table row instead of a subsystem.
    admin_key = cfg.get("admin_key")
    if not admin_key:
        return
    try:
        rows = httpx.get(f"{url}/admin/status", timeout=5,
                         headers={"Authorization": f"Bearer {admin_key}"}).json()
    except Exception:
        return
    if not isinstance(rows, list):
        return
    print(f"\n{'agent':<16} {'state':<8} {'listener':<9} {'unread':>6} "
          f"{'pending':>7}  oldest-pending")
    # The hub can only see what CONTACTS it: an open-but-idle IDE tab makes no
    # calls, so it honestly reads offline even though it will respond at its
    # next prompt. Spell that out or every operator misreads the table.
    legend = ("  states: idle/working = live push connection | active = made an "
              "authenticated call <10m ago |\n  offline = no contact (an open but "
              "idle IDE tab reads offline; it acts at its next prompt/turn)\n"
              "  listener: armed = live `agora listen` pidfile | STALE = pidfile "
              "but dead/old | - = none")
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
        print(f"{row['agent_id']:<16} {row['state']:<8} {listener:<9} "
              f"{row['unread']:>6} {row['pending_obligations']:>7}  {oldest_s}{flag}")
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
    up.add_argument("--db", default=os.environ.get("AGORA_DB"))
    up.add_argument("--rate-per-minute", type=float, default=60.0)
    up.add_argument("--notify-dir", default=None,
                    help="dir for hub-written <agent>-inbox.log files "
                         "(default: ~/.agora; '' disables)")
    up.add_argument("--notify-rotate-mb", dest="notify_rotate_mb", type=float,
                    default=8.0,
                    help="rotate a notify file above N MB to <file>.1 "
                         "(default 8; 0 disables rotation)")
    up.set_defaults(func=cmd_up)

    _KEY_HELP = ("operator-minted agent key (from `agora register`): seeds the "
                 "local key cache and is embedded in the harness config — the "
                 "admin key is then never needed on this machine")

    def _setup_common_args(sp, *, headless: bool) -> None:
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
                        help="also install the turn-end stop hook (a backstop "
                             "that re-prompts if reception breaks). Default: "
                             "no hook.")
        if headless:
            sp.add_argument("--headless", action="store_true",
                            help="dedicated Cursor seat (no human sharing the "
                                 "tab): the background listener widens its idle "
                                 "window to 1200s to save empty iterations. Do "
                                 "NOT use for a human-shared tab")

    st = sub.add_parser("setup",
                        help="wire a workspace as an agora agent: "
                             "setup cursor|claude|codex <id>")
    st_sub = st.add_subparsers(dest="harness", required=True)
    for h in ("cursor", "claude", "codex"):
        sp = st_sub.add_parser(h, help=f"wire this workspace for {h}")
        _setup_common_args(sp, headless=(h == "cursor"))
        sp.set_defaults(func=cmd_setup, harness=h)

    # Deprecated aliases (one release, per the simplicity audit): same flags,
    # same handlers, a one-line nudge toward `agora setup <harness>`.
    for h in ("cursor", "claude", "codex"):
        alias = sub.add_parser(f"setup-{h}")
        _setup_common_args(alias, headless=(h == "cursor"))
        alias.set_defaults(func=cmd_setup, harness=h,
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

    ln = sub.add_parser("listen", help="session-resident listener: emit AGORA_WAKE "
                                       "sentinels when new messages arrive")
    ln.add_argument("--as", dest="as_agent", default=None, metavar="AGENT_ID",
                    help="agent id (default: $AGORA_AGENT_ID, else the nearest "
                         ".cursor/mcp.json walking up from cwd)")
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

    dr = sub.add_parser("drive",
                        help="external resume-driver for a HEADLESS seat: "
                             "wait on obligations, spawn one bounded "
                             "cursor-agent turn per wake (owner-run, "
                             "session-bound)")
    dr.add_argument("--as", dest="as_agent", default=None, metavar="AGENT_ID",
                    help="agent id (default: $AGORA_AGENT_ID, else the nearest "
                         ".cursor/mcp.json)")
    dr.add_argument("--url", default=None)
    dr.add_argument("--model", default="composer-2.5-fast",
                    help="cursor-agent model for driven turns "
                         "(default: composer-2.5-fast)")
    dr.add_argument("--max-wait", dest="max_wait", type=float, default=1200.0,
                    help="idle ceiling for each listen window (a wake returns "
                         "instantly regardless; default 1200)")
    dr.add_argument("--sandbox", choices=["enabled", "disabled", "none"],
                    default="enabled",
                    help="spawn sandbox: enabled (DEFAULT — contained shell/"
                         "write, safe for unattended peer-driven turns), "
                         "disabled (cursor-agent's own default), or none "
                         "(--force, all tools, NO container — only in a "
                         "throwaway VM)")
    dr.add_argument("--turn-budget", dest="turn_budget", type=int, default=40,
                    help="max spawned turns per rolling hour before parking "
                         "(runaway-loop bound; default 40)")
    dr.add_argument("--session-rotate", dest="session_rotate", type=int,
                    default=25,
                    help="turns on one cursor-agent session before rotating "
                         "to a fresh one (context-bloat + injection-residue "
                         "flush; default 25)")
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
    po.add_argument("--ask", action="append", metavar="ID:TEXT",
                    help="a numbered ask (repeatable), e.g. --ask '1:confirm the payload cap?'")
    po.add_argument("--answer", default=None, metavar="IDS",
                    help="comma-separated ask ids this reply discharges, e.g. --answer 1,3")
    po.add_argument("body")
    po.set_defaults(func=cmd_post)

    dm = _agent_parser("dm", "send a private 1:1 message")
    dm.add_argument("--to", required=True)
    dm.add_argument("--status", default="fyi", choices=["open", "reply", "fyi", "blocked", "resolved"])
    dm.add_argument("--urgency", default="inbox", choices=["inbox", "next_turn", "interrupt"])
    dm.add_argument("--title", default=""); dm.add_argument("body")
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
    jn.add_argument("--harness", choices=["cursor", "claude", "codex", "none"],
                    default="cursor",
                    help="onboarding: workspace wiring to install "
                         "(default cursor; none = register + cache key only)")
    jn.add_argument("--workspace", default=".",
                    help="onboarding: workspace folder (default: cwd)")
    jn.add_argument("--with-hook", action="store_true",
                    help="onboarding: also install the turn-end stop hook "
                         "(reception backstop). Default: no hook.")
    jn.add_argument("--listen", action="store_true",
                    help="onboarding: arm a FOREGROUND `agora listen "
                         "--source ws` after wiring (headless nodes)")
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
