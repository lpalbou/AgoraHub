"""setup-cursor / setup-claude / setup-codex: project-scoped wiring, v4 hooks.

What must hold: configs land in the harness's documented project-scope
locations (never global), re-runs refresh in place without duplicating agora
entries or clobbering FOREIGN hooks, hook command paths are absolute, and the
generated v4 stop-hook — executed here as a real subprocess against a stubbed
hub `/owed` + `/inbox` — prompts ONLY for obligations (owed debts and
open/blocked unread, never fyi), enforces one global prompt floor across all
branches, obeys the harness payload guards (completed turns only, loop_count
cap, stop_hook_active), needs two consecutive dead observations before the
listener nag, and noops silently on missing key / unreachable hub.
"""

import json
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from agora.setup_harness import (HOOK_EVENTS, _WAKE_CURSOR_NO_HOOK,
                                 codex_toml_block,
                                 custom_home_env,
                                 install_claude_listener,
                                 register_claude_local, register_codex_global,
                                 rule_text, setup_claude, setup_codex,
                                 setup_cursor,
                                 upsert_marked_section, write_mcp_json)

# ---------------------------------------------------------------------------
# harness: a tiny stub hub serving GET /inbox (never the live hub — the
# server binds an ephemeral loopback port and dies with the test)
# ---------------------------------------------------------------------------


class _InboxHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        stub = self.server.stub
        stub.requests.append(self.headers.get("Authorization", ""))
        stub.client_headers.append(self.headers.get("X-Agora-Client", ""))
        path = self.path.partition("?")[0]
        if path == "/inbox":
            payload = stub.messages
        elif path == "/owed":
            payload = stub.owed
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # keep pytest output clean
        pass


@pytest.fixture()
def inbox_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _InboxHandler)
    stub = SimpleNamespace(messages=[], requests=[], client_headers=[],
                           owed={"to_answer": [], "to_consume": [],
                                 "waiting_on": []},
                           url=f"http://127.0.0.1:{server.server_address[1]}")
    server.stub = stub
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield stub
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _shift_last_prompt(home, seconds, agent="runtime"):
    """Simulate time passing by editing the v4 ledger's last_prompt."""
    path = _ledger_path(home, agent)
    led = json.loads(path.read_text())
    led["last_prompt"] = time.time() - seconds
    path.write_text(json.dumps(led))


# ---------------------------------------------------------------------------
# generated hook, executed: obligation gate / floor / guards / noop paths
# ---------------------------------------------------------------------------


def test_rule_text_cursor_has_background_reception_and_no_watcher_ban(tmp_path):
    setup_cursor(tmp_path, "runtime", "http://hub:8765", "", "agora-mcp",
                 with_hook=False)
    rule = (tmp_path / ".cursor" / "rules" / "agora.mdc").read_text()
    # The .mdc frontmatter is what makes Cursor actually inject the rule; a
    # plain .md is ignored, so this is load-bearing, not cosmetic.
    assert rule.startswith("---\nalwaysApply: true\n---\n")
    assert rule.endswith(rule_text("runtime", wake=_WAKE_CURSOR_NO_HOOK))

    # BACKGROUND RECEPTION: a monitored background listener shell is
    # reception on Cursor — a foreground blocking wait serializes the seat
    # behind others' messages (fleet failure, 2026-07-13).
    assert "BACKGROUND RECEPTION" in rule and "FIRST turn" in rule
    assert ("while true; do agora listen --once --as runtime --important-only "
            "--max-wait 240; sleep 5; done") in rule
    # The withdrawn initiative heartbeat must never be taught again (c2095):
    assert "--idle-nudge" not in rule
    # Initiative rides claims now, not synthetic wakes.
    assert "ONE live claim" in rule and "idle=1" not in rule
    # fyi chatter must not wake a seat (0080 watcher audit: traffic-driven
    # burn); obligations still do, and fyi drains at the next check_inbox.
    assert "not for fyi chatter" in rule
    assert "block_until_ms 0" in rule
    assert "never park your foreground" in rule

    # The tuned-wake contract: anchored pattern + debounce, both named as
    # load-bearing (an unanchored pattern matches the listener's own banner;
    # instant re-arm storms wakes on a burst).
    assert "^AGORA_WAKE" in rule and "notify_on_output" in rule
    assert "15000" in rule and "unanchored" in rule
    assert "matches the listener's own banner" in rule

    # The v1 lies are gone: watcher ban, push-not-pull promise, attaché.
    assert "never start a watcher" not in rule
    assert "push, not pull" not in rule
    assert "attach" not in rule.lower()          # attaché/attache both

    # Foreground waits stay banned across the board.
    assert "NEVER wait or poll in the FOREGROUND" in rule
    assert "wait_for_messages" in rule


def test_rule_text_cursor_reception_is_ordered_and_bounded(tmp_path):
    """The arming step must be copy-executable and safe: inbox first, ONE
    background listener shell as the wake source, ack discipline named (an
    unacked inbox is what makes wakes feel spammy), and a stop condition on
    hard errors (a tight error loop is worse than deafness)."""
    setup_cursor(tmp_path, "runtime", "http://hub:8765", "", "agora-mcp",
                 with_hook=False)
    rule = (tmp_path / ".cursor" / "rules" / "agora.mdc").read_text()

    assert rule.index("check_inbox") < rule.index("agora listen --once")
    assert "ONE background shell" in rule
    assert "ack_inbox` what you triaged" in rule
    assert "clears NOTHING you owe" in rule
    assert "stop the loop shell" in rule and "error loop is worse" in rule


def test_kickoff_is_the_three_word_boot(capsys):
    """The kickoff is 'start agora protocol', nothing more (operator
    finding, 2026-07-15): setup installs the skill per harness, the skill
    owns the boot, and a paragraph restating the rule was noise with drift
    risk — the retired long prompt once taught a flag the rule had dropped
    (c2095 drift class). The retired generator survives only as a shim."""
    from agora.cli import _print_kickoff
    from agora.setup_harness import kickoff_prompt

    for harness in ("cursor", "claude", "codex"):
        _print_kickoff(harness)
        out = capsys.readouterr().out
        assert "start agora protocol" in out
        assert "check_inbox" not in out          # no restated boot steps
        assert "AGORA_WAKE" not in out           # no respelled commands

    assert kickoff_prompt("x", "http://h:1", standing_loop=True) == \
        "start agora protocol"


def test_rule_text_cursor_loop_never_says_kill(tmp_path):
    """Regression (2026-07-13 fleet incident): the old rule told seats to
    kill the lock holder on already-armed, which caused cross-seat `kill`
    sprees (every listener looks identical by name) and supervisor wars. The
    rule must now forbid killing and treat already-armed as self-resolving."""
    setup_cursor(tmp_path, "runtime", "http://hub:8765", "", "agora-mcp",
                 with_hook=False)
    rule = (tmp_path / ".cursor" / "rules" / "agora.mdc").read_text()

    assert "NEVER pgrep or kill" in rule
    assert "kill it once" not in rule            # the old harmful imperative is gone
    assert "winding" in rule                     # already-armed = your own prior call
    assert "never kill anything" in rule
    # The default (non-headless) loop stays the bounded fixed window.
    assert "--adaptive" not in rule


def test_rule_text_cursor_is_mode_free(tmp_path):
    """The mode-free rule (2026-07-28): --headless and the default write
    BYTE-IDENTICAL wiring — the folder no longer encodes the mode; the
    running driver is the mode. One rule carries BOTH branches: the DRIVEN
    TURN contract (prompt-marked turns never arm listeners) and the
    INTERACTIVE arming ritual, plus the refusal teaching
    (driver-owns-reception = never retry). The hook installs in both cases
    because its nag is now driver-aware."""
    written_plain = setup_cursor(tmp_path, "runtime", "http://hub:8765", "",
                                 "agora-mcp", with_hook=True, headless=False)
    rule_plain = (tmp_path / ".cursor" / "rules" / "agora.mdc").read_text()
    written_headless = setup_cursor(tmp_path, "runtime", "http://hub:8765", "",
                                    "agora-mcp", with_hook=True, headless=True)
    rule_headless = (tmp_path / ".cursor" / "rules" / "agora.mdc").read_text()

    assert rule_plain == rule_headless            # the flag changes nothing
    assert [str(p) for p in written_plain] == [str(p) for p in written_headless]
    rule = rule_headless
    # Both branches present, honestly labeled.
    assert "DRIVEN TURN" in rule and "INTERACTIVE SESSION" in rule
    assert "AGORA WAKE" in rule and "AGORA WORK CHUNK" in rule
    assert "NEVER run `agora listen`" in rule     # the driven contract
    assert "while true; do agora listen" in rule  # the interactive ritual
    assert "driver-owns-reception" in rule        # the refusal teaching
    # Continuation is taught to every seat (RULE_TEMPLATE, all harnesses).
    assert "INITIATIVE & CONTINUATION" in rule
    assert "SUPERSEDE" in rule                    # re-read the record first
    # The hook installs for BOTH modes now (its nag is driver-aware).
    assert (tmp_path / ".cursor" / "hooks.json").exists()


def test_install_skill_writes_and_refreshes_each_harness(tmp_path):
    """`agora setup` must leave ZERO manual skill copies (operator finding,
    2026-07-14: a four-cp install block in the guide was unacceptable):
    install_skill drops the packaged SKILL.md + agora_protocol.py into the
    harness's skills dir, and re-running REFRESHES them (stale copies are
    the drift class the single-source rule exists for)."""
    from agora.setup_harness import _SKILL_DIRS, install_skill

    for harness in ("cursor", "claude", "codex"):
        detail = install_skill(harness, home=tmp_path)
        target = tmp_path / _SKILL_DIRS[harness]
        assert "installed" in detail
        assert (target / "SKILL.md").read_text().startswith("---")
        assert "start agora protocol" in (target / "SKILL.md").read_text()
        assert (target / "agora_protocol.py").exists()
        # refresh: a stale local edit is overwritten by the packaged copy
        (target / "SKILL.md").write_text("stale")
        install_skill(harness, home=tmp_path)
        assert (target / "SKILL.md").read_text() != "stale"


def test_setup_parsers_accept_channels_placement(tmp_path):
    """Placement is part of wiring (field incident 2026-07-14: a seat wired
    without placement improvised at boot and squatted a busy public
    channel): the agent-first setup parser and the legacy compatibility shape
    both take --channels, and the value parses as a comma list."""
    from agora.cli import build_parser

    p = build_parser()
    args = p.parse_args(["setup", "x", "--channels", "a,b",
                         "--workspace", str(tmp_path)])
    assert args.channels == "a,b" and args.target == "x"
    for harness in ("cursor", "claude", "codex"):
        compat = p.parse_args(["setup", harness, "x", "--channels", "a,b",
                               "--workspace", str(tmp_path)])
        assert compat.channels == "a,b"


def test_codex_project_config_approves_agora_tools(tmp_path):
    """Without default_tools_approval_mode=approve Codex prompts per TOOL
    NAME on first use — an unattended seat freezes on a dialog at every new
    verb (live 3-seat run, 2026-07-14: whoami, list_channels, check_inbox,
    ... each stalled until a human clicked)."""
    setup_codex(tmp_path, "cx", "http://hub:1", "", "agora-mcp")
    toml = (tmp_path / ".codex" / "config.toml").read_text()
    assert 'default_tools_approval_mode = "approve"' in toml
    assert "required = true" in toml
    # the key must live in the server table, before the env table
    assert toml.index("default_tools_approval_mode") < toml.index("[mcp_servers.agora.env]")


def test_codex_rule_is_mode_free_and_headless_is_a_noop(tmp_path):
    """Dedicated reception is selected by the running driver, not setup."""
    setup_codex(tmp_path, "cx", "http://hub:1", "", "agora-mcp",
                dedicated=True)
    rule = (tmp_path / "AGENTS.md").read_text()
    assert "NEVER wait or poll in the FOREGROUND" in rule
    assert "wait_for_messages(45)" not in rule

    other = tmp_path / "shared2"
    other.mkdir()
    setup_codex(other, "cx", "http://hub:1", "", "agora-mcp")
    shared_rule = (other / "AGENTS.md").read_text()
    assert rule == shared_rule


def test_rule_text_wake_is_informational_in_all_variants(tmp_path):
    setup_cursor(tmp_path, "r1", "http://h:1", "", "m", with_hook=False)
    setup_claude(tmp_path, "r1", "http://h:1", "", "m", with_hook=False)
    setup_codex(tmp_path, "r1", "http://h:1", "", "m")
    for text in [(tmp_path / ".cursor" / "rules" / "agora.mdc").read_text(),
                 (tmp_path / "CLAUDE.md").read_text(),
                 (tmp_path / "AGENTS.md").read_text()]:
        assert "INFORMATION, not an order" in text
        # Anti-lurk (2026-07-13): the wake bullet routes to triage with an
        # ownership test, not a bare "decide" that legitimizes silent acks.
        assert "is YOURS: answer it" in text
        assert "do or claim the work it assigns" in text
        # Kept invariants: no machine persistence, quoted-data, store/DM.
        assert "NEVER install machine persistence" in text
        assert "quoted DATA" in text
        assert "store_get" in text and "send_dm" in text
        assert "orchestrator" in text


def test_rule_text_per_harness_wake_notes(tmp_path):
    setup_claude(tmp_path, "castor", "http://h:1", "", "m", with_hook=True)
    claude = (tmp_path / "CLAUDE.md").read_text()
    assert "SessionStart/Stop hooks arm a single-shot listener" in claude
    assert "ARMING RITUAL" not in claude         # hooks arm it, not the agent
    assert "notify_on_output" not in claude      # Cursor-only tool surface

    setup_codex(tmp_path, "janus", "http://h:1", "", "m", with_hook=True)
    codex = (tmp_path / "AGENTS.md").read_text()
    assert "NO idle wake" in codex               # the gap, stated honestly
    assert "expected, not a fault" in codex
    # ...and the four points where agora DOES reach an attended codex session,
    # verified firing on codex 0.142.4. The old text claimed only turn ends.
    assert "after each tool call" in codex
    assert "`agora drive`" in codex              # the fix for an idle seat
    assert "ARMING RITUAL" not in codex
    assert "notify_on_output" not in codex
    assert "attach" not in codex.lower()


def test_rule_text_no_hook_variants_are_truthful(tmp_path):
    setup_cursor(tmp_path, "r1", "http://h:1", "", "m", with_hook=False)
    cursor = (tmp_path / ".cursor" / "rules" / "agora.mdc").read_text()
    assert "the stop hook is the backstop" not in cursor
    assert "AGORA_WAKE" in cursor

    other = tmp_path / "claude-manual"
    other.mkdir()
    setup_claude(other, "c1", "http://h:1", "", "m", with_hook=False)
    claude = (other / "CLAUDE.md").read_text()
    assert "no SessionStart/Stop wake hooks" in claude
    assert "messages wait for your next turn" in claude

    third = tmp_path / "codex-manual"
    third.mkdir()
    setup_codex(third, "j1", "http://h:1", "", "m", with_hook=False)
    codex = (third / "AGENTS.md").read_text()
    assert "stop hook drains bursts" not in codex
    assert "messages wait for your next turn" in codex


# ---------------------------------------------------------------------------
# cursor installer: absolute path, merge preserves foreign hooks
# ---------------------------------------------------------------------------


def test_setup_cursor_uses_the_shared_generators(tmp_path):
    """setup-cursor goes through the same module as claude/codex (one rule
    template, one stop-hook generator) — the drift-prone cli.py copies died."""
    written = setup_cursor(tmp_path, "runtime", "http://hub:8765", "the kernel",
                           "/usr/local/bin/agora-mcp", with_hook=True)
    mcp = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert mcp["mcpServers"]["agora"]["env"]["AGORA_AGENT_ID"] == "runtime"

    hooks = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    [entry] = hooks["hooks"]["stop"]
    assert entry["loop_limit"] == 3 and entry["timeout"] == 30
    # ABSOLUTE command path: hook commands resolve against the launch dir,
    # not the hooks file (the deployed-fleet relative-path trap).
    assert entry["command"].startswith("/")
    # ONE hook implementation for every harness — `agora hook`, not a script
    # generated as a string literal. `--cursor` selects Cursor's
    # followup_message shape over hookSpecificOutput/decision.
    assert " hook Stop --as runtime " in entry["command"]
    assert " --cursor" in entry["command"]
    assert not (tmp_path / ".cursor" / "hooks" / "agora_wait.sh").exists()
    assert len(written) == 3


def test_cursor_hooks_json_merge_preserves_foreign_hooks(tmp_path):
    """Re-running setup replaces ONLY agora_wait entries: foreign stop hooks,
    other events, and a user-set version survive; nothing duplicates."""
    hooks_path = tmp_path / ".cursor" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(json.dumps({
        "version": 3,
        "hooks": {
            "stop": [{"command": "my_stop.sh", "timeout": 5},
                     # stale agora entry from an earlier generation:
                     {"command": ".cursor/hooks/agora_wait.sh",
                      "timeout": 10, "loop_limit": 3}],
            "beforeShellExecution": [{"command": "guard.sh"}],
        },
    }))
    for _ in range(2):
        setup_cursor(tmp_path, "runtime", "http://hub:8765", "", "agora-mcp",
                     with_hook=True)
    hooks = json.loads(hooks_path.read_text())
    assert hooks["version"] == 3                       # user's value kept
    assert hooks["hooks"]["beforeShellExecution"] == [{"command": "guard.sh"}]
    stop = hooks["hooks"]["stop"]
    assert stop[0] == {"command": "my_stop.sh", "timeout": 5}
    agora = [e for e in stop if "agora hook " in e["command"]]
    assert len(agora) == 1                             # replaced, not stacked
    assert agora[0]["command"].startswith("/")         # absolute now
    assert agora[0]["timeout"] == 30 and agora[0]["loop_limit"] == 3
    # The stale previous-generation entry is GONE, not left beside the new one.
    assert not any("agora_wait" in e["command"] for e in stop)


# ---------------------------------------------------------------------------
# claude installer: stop hook + NEW single-shot listener (SessionStart/Stop)
# ---------------------------------------------------------------------------


def test_setup_claude_writes_project_scoped_files(tmp_path):
    written = setup_claude(tmp_path, "castor", "http://hub:8765", "the entity",
                           "/usr/local/bin/agora-mcp", with_hook=True)
    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    server = mcp["mcpServers"]["agora"]
    assert server["command"] == "/usr/local/bin/agora-mcp"
    assert server["env"]["AGORA_AGENT_ID"] == "castor"
    assert "check_inbox" in (tmp_path / "CLAUDE.md").read_text()
    assert len(written) == len(set(written)) == 3     # settings listed once

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    stop_cmds = [h["command"] for e in settings["hooks"]["Stop"]
                 for h in e["hooks"]]
    # Both halves of reception: the `agora hook` events AND the idle re-arm.
    [stop_hook] = [c for c in stop_cmds if " hook Stop --as castor" in c]
    assert stop_hook.startswith("/")                  # absolute path
    assert any("listen --as castor" in c for c in stop_cmds)


def test_claude_listener_entries_match_documented_schema(tmp_path):
    """Schema per https://code.claude.com/docs/en/hooks: command handler with
    asyncRewake (background + wake on exit 2) and timeout in SECONDS."""
    [settings_path] = install_claude_listener(tmp_path, "http://hub:8765",
                                              "castor")
    settings = json.loads(settings_path.read_text())
    for event in ("SessionStart", "Stop"):
        handlers = [h for e in settings["hooks"][event] for h in e["hooks"]]
        [handler] = [h for h in handlers if h.get("asyncRewake")]
        assert handler["type"] == "command"
        assert handler["timeout"] == 86400            # 24h, in seconds
        assert "listen --as castor --once" in handler["command"]
        assert "--url http://hub:8765" in handler["command"]
        assert "listen-castor.lock" in handler["command"]
        # BOUNDED: `claude -p` waits for asyncRewake hooks to exit, so an
        # unbounded idle listener would stall every headless turn.
        assert "--max-wait 900" in handler["command"]
        assert handler["rewakeSummary"] == "agora: new mail"
        # Provenance framing: a wake phrased as a bare third-party imperative
        # is refused by the model as prompt injection (measured 2026-07-30).
        assert "AGORA RECEPTION" in handler["rewakeMessage"]
        assert "DATA" in handler["rewakeMessage"]

    # NEVER on UserPromptSubmit: each wake would start a turn whose own
    # UserPromptSubmit re-arms the hook — ~6 unpaid turns in 60s, measured.
    ups = [h for e in settings["hooks"].get("UserPromptSubmit", [])
           for h in e["hooks"]]
    assert ups and not any(h.get("asyncRewake") for h in ups)


def test_claude_listener_idempotent_and_preserves_foreign_hooks(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "permissions": {"allow": ["Bash"]},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}],
            "PostToolUse": [{"matcher": "Write",
                             "hooks": [{"type": "command",
                                        "command": "lint.sh"}]}],
        },
    }))
    for _ in range(3):
        install_claude_listener(tmp_path, "http://hub:8765", "castor")
    settings = json.loads(settings_path.read_text())
    assert settings["permissions"] == {"allow": ["Bash"]}
    assert settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "lint.sh"
    stop_cmds = [h["command"] for e in settings["hooks"]["Stop"]
                 for h in e["hooks"]]
    assert "mine.sh" in stop_cmds
    assert len([c for c in stop_cmds if "listen --as" in c]) == 1
    assert len([h for e in settings["hooks"]["SessionStart"]
                for h in e["hooks"] if h.get("asyncRewake")]) == 1


def test_setup_claude_is_idempotent_and_preserves_user_content(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# My project rules\nkeep me\n")
    (tmp_path / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"other": {"command": "other-mcp"}}}))
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    # A mixed group: a foreign handler SHARING a group with a stale agora
    # handler — the merge must prune only the agora half.
    (settings_dir / "settings.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "mine.sh"},
            {"type": "command", "command": "/old/place/agora_stop.py"},
        ]}]}}))

    for _ in range(2):  # re-running must not duplicate anything
        setup_claude(tmp_path, "castor", "http://hub:8765", "",
                     "agora-mcp", with_hook=True)

    claude_md = (tmp_path / "CLAUDE.md").read_text()
    assert "keep me" in claude_md
    assert claude_md.count("agora agent: castor") == 1

    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert set(mcp["mcpServers"]) == {"other", "agora"}

    settings = json.loads((settings_dir / "settings.json").read_text())
    stop_cmds = [h["command"] for e in settings["hooks"]["Stop"]
                 for h in e["hooks"]]
    assert len([c for c in stop_cmds if " hook Stop --as" in c]) == 1
    assert "/old/place/agora_stop.py" not in stop_cmds  # stale one replaced
    assert "mine.sh" in stop_cmds                       # foreign survives
    assert len([c for c in stop_cmds if "listen --as" in c]) == 1


def test_setup_claude_no_hook_removes_prior_agora_hooks(tmp_path):
    setup_claude(tmp_path, "castor", "http://hub:8765", "", "agora-mcp",
                 with_hook=True)
    setup_claude(tmp_path, "castor", "http://hub:8765", "", "agora-mcp",
                 with_hook=False)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    stop_cmds = [h["command"] for e in settings["hooks"]["Stop"]
                 for h in e.get("hooks", []) if isinstance(h, dict)]
    assert not any(" hook Stop --as" in cmd for cmd in stop_cmds)
    assert not any("listen --as" in cmd for cmd in stop_cmds)
    session_cmds = [h["command"] for e in settings["hooks"]["SessionStart"]
                    for h in e.get("hooks", []) if isinstance(h, dict)]
    assert not any("listen --as" in cmd for cmd in session_cmds)
    assert not (tmp_path / ".claude" / "hooks" / "agora_stop.py").exists()


# ---------------------------------------------------------------------------
# codex: config.toml, honest wake note, hook merge
# ---------------------------------------------------------------------------


def test_setup_codex_writes_project_config_and_agents_md(tmp_path):
    setup_codex(tmp_path, "janus", "http://hub:8765", "the door", "agora-mcp")
    toml_text = (tmp_path / ".codex" / "config.toml").read_text()
    assert "[mcp_servers.agora]" in toml_text
    assert 'AGORA_AGENT_ID = "janus"' in toml_text
    agents_md = (tmp_path / "AGENTS.md").read_text()
    assert "agora agent: janus" in agents_md
    assert "no idle wake" in agents_md    # the codex gap, stated honestly

    # Re-run: the workspace converges to the new request instead of leaving a
    # stale Agora-owned table behind, and AGENTS.md is not duplicated.
    setup_codex(tmp_path, "janus", "http://hub:8765", "", "agora-mcp")
    refreshed = (tmp_path / ".codex" / "config.toml").read_text()
    assert refreshed != toml_text
    assert 'AGORA_ABOUT = ""' in refreshed
    assert 'AGORA_AGENT_ID = "janus"' in refreshed
    assert (tmp_path / "AGENTS.md").read_text().count("agora agent: janus") == 1


def test_setup_codex_replaces_interleaved_agora_tables_without_duplication(tmp_path):
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[mcp_servers.agora]\n'
        'command = "old"\n\n'
        '[mcp_servers.other]\n'
        'command = "foreign"\n\n'
        '[mcp_servers.agora.env]\n'
        'AGORA_AGENT_ID = "old"\n'
    )
    setup_codex(tmp_path, "bob", "http://hub:8765", "", "agora-mcp")
    text = config_path.read_text()
    assert text.count("[mcp_servers.agora]") == 1
    assert text.count("[mcp_servers.agora.env]") == 1
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["agora"]["env"]["AGORA_AGENT_ID"] == "bob"
    assert parsed["mcp_servers"]["other"]["command"] == "foreign"


def test_setup_codex_replaces_quoted_agora_table_names(tmp_path):
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "[mcp_servers.'agora'] # keep this comment\n"
        'command = "old"\n\n'
        '[mcp_servers.other]\n'
        'command = "foreign"\n\n'
        "[mcp_servers.'agora'.env] # env comment\n"
        'AGORA_AGENT_ID = "old"\n'
    )
    setup_codex(tmp_path, "bob", "http://hub:8765", "", "agora-mcp")
    text = config_path.read_text()
    assert text.count("[mcp_servers.agora]") == 1
    assert text.count("[mcp_servers.agora.env]") == 1
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["agora"]["env"]["AGORA_AGENT_ID"] == "bob"
    assert parsed["mcp_servers"]["other"]["command"] == "foreign"


def test_setup_codex_with_hook_writes_stop_hook(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "hooks.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"type": "command", "command": "other.py"}]}}))
    for _ in range(2):  # idempotent: no duplicate agora entry on re-run
        setup_codex(tmp_path, "janus", "http://hub:8765", "", "agora-mcp",
                    with_hook=True)
    hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    # All four reception events are declared, each as a MATCHER GROUP. The old
    # flat-handler shape registered ZERO hooks in Codex with no warning at all
    # — the single reason in-session Codex reception never fired.
    assert set(hooks["hooks"]) >= set(HOOK_EVENTS)
    for event in HOOK_EVENTS:
        agora = [g for g in hooks["hooks"][event]
                 if any("agora hook " in h.get("command", "")
                        for h in g.get("hooks", []))]
        assert len(agora) == 1, (event, agora)      # idempotent across re-runs
        handler = agora[0]["hooks"][0]
        assert handler["type"] == "command"
        assert handler["command"].startswith("/")   # absolute, never PATH
        assert f" hook {event} " in handler["command"]
        # FROZEN: for Codex these bytes are the trust hash.
        assert handler["timeout"] == 10
        assert set(handler) == {"type", "command", "timeout"}
    # foreign entries are preserved untouched
    assert {"type": "command", "command": "other.py"} in hooks["hooks"]["Stop"]
    # the generated script from earlier releases is gone, not left to rot
    assert not (tmp_path / ".codex" / "hooks" / "agora_stop.py").exists()


def test_setup_codex_no_hook_removes_prior_agora_hook(tmp_path):
    setup_codex(tmp_path, "janus", "http://hub:8765", "", "agora-mcp",
                with_hook=True)
    setup_codex(tmp_path, "janus", "http://hub:8765", "", "agora-mcp",
                with_hook=False)
    hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    for event in HOOK_EVENTS:
        for group in hooks["hooks"].get(event, []):
            for handler in group.get("hooks", []):
                assert "agora hook " not in handler.get("command", "")
    assert not (tmp_path / ".codex" / "hooks" / "agora_stop.py").exists()


def test_codex_toml_block_quotes_special_characters():
    block = codex_toml_block("agora-mcp", "http://h:1", "a", 'says "hi"\\path')
    assert '"says \\"hi\\"\\\\path"' in block  # JSON escaping is valid TOML


# ---------------------------------------------------------------------------
# credential placement: keys.json is the sole bearer source. Harness config
# carries only non-secret identity/home data, even when old callers still pass
# the source-compatible api_key argument.
# ---------------------------------------------------------------------------


def test_write_mcp_json_explicitly_clears_inherited_credentials(tmp_path):
    """Workspace MCP config carries no bearer and masks stale shell keys."""
    path = tmp_path / "mcp.json"
    write_mcp_json(path, "agora-mcp", "http://hub:8765", "runtime", "the kernel")
    expected = json.dumps({"mcpServers": {"agora": {
        "command": "agora-mcp",
        "env": {"AGORA_URL": "http://hub:8765", "AGORA_AGENT_ID": "runtime",
                "AGORA_ABOUT": "the kernel", "AGORA_API_KEY": "",
                "AGORA_ADMIN_KEY": ""},
    }}}, indent=2) + "\n"
    assert path.read_text() == expected
    assert path.stat().st_mode & 0o077 != 0    # default perms, not clamped


def test_setup_cursor_no_hook_removes_prior_agora_hook(tmp_path):
    setup_cursor(tmp_path, "runtime", "http://hub:8765", "", "agora-mcp",
                 with_hook=True)
    setup_cursor(tmp_path, "runtime", "http://hub:8765", "", "agora-mcp",
                 with_hook=False)
    hooks = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    entries = hooks["hooks"]["stop"]
    assert not any("agora hook " in e.get("command", "") for e in entries)
    assert not (tmp_path / ".cursor" / "hooks" / "agora_wait.sh").exists()
    assert not (tmp_path / ".cursor" / "hooks" / "agora_wait.sh").exists()


def test_write_mcp_json_never_embeds_api_key_and_preserves_other_servers(tmp_path):
    path = tmp_path / "mcp.json"
    (tmp_path / "mcp.json").write_text(json.dumps(
        {"mcpServers": {
            "other": {"command": "other-mcp"},
            "agora": {
                "command": "stale-agora-mcp",
                "env": {"AGORA_API_KEY": "agora_legacy"},
            },
        }}))
    write_mcp_json(path, "agora-mcp", "http://hub:8765", "castor", "",
                   api_key="agora_secret123")
    config = json.loads(path.read_text())
    env = config["mcpServers"]["agora"]["env"]
    assert env["AGORA_API_KEY"] == ""
    assert env["AGORA_ADMIN_KEY"] == ""
    assert env["AGORA_URL"] == "http://hub:8765"
    assert config["mcpServers"]["other"] == {"command": "other-mcp"}  # merged
    assert path.stat().st_mode & 0o077 != 0    # no secret-driven permission clamp


def test_setup_cursor_and_claude_never_thread_api_key(tmp_path):
    for name, fn, mcp_rel in [("cursor", setup_cursor, ".cursor/mcp.json"),
                              ("claude", setup_claude, ".mcp.json")]:
        ws = tmp_path / name
        ws.mkdir()
        fn(ws, "castor", "http://hub:8765", "", "agora-mcp", False,
           api_key="agora_k1")
        mcp_path = ws / mcp_rel
        env = json.loads(mcp_path.read_text())["mcpServers"]["agora"]["env"]
        assert env["AGORA_API_KEY"] == "", name
        assert env["AGORA_ADMIN_KEY"] == "", name


def test_codex_toml_masks_inherited_api_key_without_a_bearer(tmp_path):
    block = codex_toml_block("agora-mcp", "http://h:1", "janus", "",
                             api_key="agora_k2")
    assert 'AGORA_API_KEY = ""' in block
    assert 'AGORA_ADMIN_KEY = ""' in block
    assert "agora_k2" not in block
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[mcp_servers.agora]\ncommand = "stale"\n\n'
        '[mcp_servers.agora.env]\nAGORA_API_KEY = "agora_legacy"\n'
    )
    setup_codex(tmp_path, "janus", "http://h:1", "", "agora-mcp",
                api_key="agora_k2")
    rendered = config_path.read_text()
    assert 'AGORA_API_KEY = ""' in rendered
    assert 'AGORA_ADMIN_KEY = ""' in rendered
    assert "agora_legacy" not in rendered and "agora_k2" not in rendered


def test_upsert_marked_section_replaces_only_the_marked_block(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("intro\n")
    upsert_marked_section(path, "v1 content")
    upsert_marked_section(path, "v2 content")
    text = path.read_text()
    assert "intro" in text and "v2 content" in text and "v1 content" not in text


# ---------------------------------------------------------------------------
# custom home placement: a non-default AGORA_HOME must ride the env block
# (harness-spawned processes do not inherit the operator's shell env), and
# the default-home output must stay byte-identical
# ---------------------------------------------------------------------------


def test_custom_home_env_only_reports_non_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AGORA_HOME", raising=False)
    assert custom_home_env() is None
    # An EXPLICIT default is still the default — nothing worth embedding.
    monkeypatch.setenv("AGORA_HOME", str(Path.home() / ".agora"))
    assert custom_home_env() is None
    monkeypatch.setenv("AGORA_HOME", str(tmp_path / "hub2"))
    assert custom_home_env() == str(tmp_path / "hub2")


def test_write_mcp_json_and_toml_embed_home_only_when_given(tmp_path):
    path = tmp_path / "mcp.json"
    write_mcp_json(path, "agora-mcp", "http://h:1", "a", "", home="/x/hub2")
    env = json.loads(path.read_text())["mcpServers"]["agora"]["env"]
    assert env["AGORA_HOME"] == "/x/hub2"

    block = codex_toml_block("agora-mcp", "http://h:1", "a", "",
                             api_key="agora_k", home="/x/hub2")
    assert 'AGORA_HOME = "/x/hub2"' in block
    assert 'AGORA_API_KEY = ""' in block
    assert 'AGORA_ADMIN_KEY = ""' in block
    assert "agora_k" not in block
    assert "AGORA_HOME" not in codex_toml_block("agora-mcp", "http://h:1",
                                                "a", "")


def test_setup_writers_embed_the_ambient_custom_home(tmp_path, monkeypatch):
    """The second-hub trap: wired under AGORA_HOME=~/.agora-hub2, the spawned
    MCP server must read hub2's keys.json — so the env block carries the
    custom home. Under the default home nothing is added (config output
    unchanged for the common case)."""
    monkeypatch.setenv("AGORA_HOME", str(tmp_path / "hub2"))
    ws = tmp_path / "ws"
    ws.mkdir()
    setup_cursor(ws, "r1", "http://h:1", "", "agora-mcp", with_hook=False)
    env = json.loads((ws / ".cursor" / "mcp.json").read_text()
                     )["mcpServers"]["agora"]["env"]
    assert env["AGORA_HOME"] == str(tmp_path / "hub2")
    setup_codex(ws, "r1", "http://h:1", "", "agora-mcp")
    assert (f'AGORA_HOME = "{tmp_path / "hub2"}"'
            in (ws / ".codex" / "config.toml").read_text())

    monkeypatch.delenv("AGORA_HOME", raising=False)
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    setup_claude(ws2, "r1", "http://h:1", "", "agora-mcp", with_hook=False)
    env = json.loads((ws2 / ".mcp.json").read_text()
                     )["mcpServers"]["agora"]["env"]
    assert "AGORA_HOME" not in env


# ---------------------------------------------------------------------------
# harness-CLI registration: the read-side fix. Claude Code gates a project
# .mcp.json behind trust + /mcp approval; Codex loads .codex/config.toml only
# for trusted projects. The vendors' own `mcp add` CLIs land the server where
# it is read WITHOUT those gates — verify the documented calls are built,
# and that a missing/failing binary degrades to (False, remedy), never raises.
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Records subprocess.run-style calls; returns a canned returncode."""

    def __init__(self, returncode: int = 0):
        self.calls: list[tuple[list, dict]] = []
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=self.returncode,
                               stdout="", stderr="harness said no")


def _env_flags(flag: str, url: str, agent: str, about: str,
               api_key: str | None = None, home: str | None = None) -> list:
    pairs = [("AGORA_URL", url), ("AGORA_AGENT_ID", agent),
             ("AGORA_ABOUT", about), ("AGORA_API_KEY", ""),
             ("AGORA_ADMIN_KEY", "")]
    del api_key
    if home:
        pairs.append(("AGORA_HOME", home))
    return [part for k, v in pairs for part in (flag, f"{k}={v}")]


def test_register_claude_local_builds_documented_call(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which",
                        lambda name: "/opt/bin/claude" if name == "claude" else None)
    runner = _FakeRunner()
    ok, detail = register_claude_local(
        tmp_path, "/x/agora-mcp", "http://h:1", "castor", "the entity",
        api_key="agora_k", home="/x/hub2", runner=runner)
    assert ok and "local scope" in detail

    (rm_argv, rm_kw), (add_argv, add_kw) = runner.calls
    # Stale entry removed first (`claude mcp add` refuses to overwrite)...
    assert rm_argv == ["/opt/bin/claude", "mcp", "remove", "--scope", "local",
                       "agora"]
    # ...then added at LOCAL scope — and BOTH calls anchored to the
    # workspace: local entries are keyed by the working directory.
    assert add_argv == ["/opt/bin/claude", "mcp", "add", "--scope", "local",
                        "agora",
                        *_env_flags("-e", "http://h:1", "castor", "the entity",
                                    api_key="agora_k", home="/x/hub2"),
                        "--", "/x/agora-mcp"]
    assert rm_kw["cwd"] == add_kw["cwd"] == str(tmp_path)


def test_register_codex_global_builds_documented_call(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which",
                        lambda name: "/opt/bin/codex" if name == "codex" else None)
    runner = _FakeRunner()
    ok, detail = register_codex_global("/x/agora-mcp", "http://h:1", "janus",
                                       "", runner=runner)
    assert ok and "globally" in detail
    [(argv, _kwargs)] = runner.calls   # re-add replaces: no remove needed
    assert argv == ["/opt/bin/codex", "mcp", "add", "agora",
                    *_env_flags("--env", "http://h:1", "janus", ""),
                    "--", "/x/agora-mcp"]


def test_register_helpers_degrade_when_binary_missing(tmp_path, monkeypatch):
    """No harness binary -> (False, printed remedy) and NO subprocess call —
    setup/join must keep working on machines without claude/codex."""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    def never_called(*_a, **_k):
        raise AssertionError("runner must not run without a binary")

    ok, detail = register_claude_local(tmp_path, "m", "http://h:1", "a", "",
                                       runner=never_called)
    assert not ok and "/mcp" in detail
    ok, detail = register_codex_global("m", "http://h:1", "a", "",
                                       runner=never_called)
    assert not ok and "trust the project" in detail


def test_register_helpers_degrade_on_failure_and_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")
    ok, detail = register_claude_local(tmp_path, "m", "http://h:1", "a", "",
                                       runner=_FakeRunner(returncode=1))
    assert not ok and "harness said no" in detail and "/mcp" in detail
    ok, detail = register_codex_global("m", "http://h:1", "a", "",
                                       runner=_FakeRunner(returncode=1))
    assert not ok and "trust the project" in detail

    def boom(*_a, **_k):
        raise OSError("spawn failed")

    ok, detail = register_claude_local(tmp_path, "m", "http://h:1", "a", "",
                                       runner=boom)
    assert not ok and "spawn failed" in detail
    ok, detail = register_codex_global("m", "http://h:1", "a", "",
                                       runner=boom)
    assert not ok and "spawn failed" in detail


def test_setup_abstractcode_writes_the_agora_contract(tmp_path):
    """AbstractCode composes a project AGENTS.md into its system prompt
    (abstractcode/project_context.py). Setup wrote only the MCP block, so an
    in-session seat had all 43 tools and NO contract: nothing told it what it
    owed, when to look, or that peer text is data."""
    from agora.setup_harness import setup_abstractcode

    written = setup_abstractcode(tmp_path, "ac", "http://hub:8765", "",
                                 "agora-mcp", with_hook=True)
    agents_md = tmp_path / "AGENTS.md"
    assert agents_md in written and agents_md.exists()
    text = agents_md.read_text()
    assert "agora agent: ac" in text
    assert "check_inbox" in text
    # Honest about the surface it actually has: no hook API, so reception
    # happens when the agent looks, and an idle seat needs `agora drive`.
    assert "no hook or idle-wake surface" in text
    assert "agora drive" in text
    # Idempotent, and a user's own AGENTS.md content survives.
    agents_md.write_text("# my notes\nkeep me\n" + text)
    setup_abstractcode(tmp_path, "ac", "http://hub:8765", "", "agora-mcp",
                       with_hook=True)
    again = agents_md.read_text()
    assert "keep me" in again
    assert again.count("agora agent: ac") == 1


def test_setup_abstractcode_tui_writes_only_what_agora_owns(tmp_path):
    """agora writes its OWN wiring and nothing else.

    An earlier version pinned a workflow bundle into the vendor's preferences
    file. That encoded another product's internals inside agora AND silently
    overrode an operator's choice for that workspace. agora is a protocol: how a
    seat reaches its tools is the framework's business, and how it is configured
    is the operator's.
    """
    from agora.setup_harness import setup_abstractcode_tui

    written = setup_abstractcode_tui(tmp_path, "actui", "http://hub:8765", "",
                                     "agora-mcp", with_hook=True)
    assert written == [tmp_path / "AGENTS.md"]
    # No vendor configuration is touched at all.
    assert not (tmp_path / ".abstractcode-tui").exists()

    contract = (tmp_path / "AGENTS.md").read_text()
    assert "agora agent: actui" in contract
    assert "check_inbox" in contract
    assert "no hook or idle-wake surface" in contract   # honest about reception
    for leak in ("basic-agent", "react-agent", "bundle_id",
                 "ABSTRACT_ENABLE", "gateway-side"):
        assert leak not in contract


def test_agora_never_carries_another_frameworks_internals():
    """agora is a communication protocol, not a member of any one framework.

    It may map a vendor's CLI shape (this one spells the model flag `-m`) and it
    may install its OWN wiring (MCP server, rule text, hook command). It may not
    carry a vendor's workflow/bundle names, its internal tool registry, or its
    server's env-var names — that is agora shipping someone else's
    implementation, and it goes stale silently.

    Regression guard for the 0.12.59 layering fix.
    """
    import agora

    root = Path(agora.__file__).parent
    leaks = ("ABSTRACT_ENABLE", "AGORA_API_KEY__", "basic-agent",
             "react-agent", "multiagent-cod", "gateway-side")
    offenders = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            for leak in leaks:
                if leak in line:
                    offenders.append(f"{path.relative_to(root)}:{line_no} {leak}")
    assert offenders == [], (
        "vendor internals leaked into agora:\n  " + "\n  ".join(offenders))
