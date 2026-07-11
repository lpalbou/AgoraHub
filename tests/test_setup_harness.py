"""setup-cursor / setup-claude / setup-codex: project-scoped wiring, v2 hooks.

What must hold: configs land in the harness's documented project-scope
locations (never global), re-runs refresh in place without duplicating agora
entries or clobbering FOREIGN hooks, hook command paths are absolute, and the
generated v2 stop-hook — executed here as a real subprocess against a stubbed
hub `/inbox` — prompts on fresh seqs, throttles standing unread on exponential
backoff via the attempt ledger, noops on empty inbox / stop_hook_active /
missing key, and never lets the ledger block a prompt once the server-side
ack cursor (the only truth) says something new is unread.
"""

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agora.setup_harness import (codex_toml_block, install_claude_listener,
                                 rule_text, setup_claude, setup_codex,
                                 setup_cursor, stop_hook_script,
                                 upsert_marked_section, write_mcp_json)

# ---------------------------------------------------------------------------
# harness: a tiny stub hub serving GET /inbox (never the live hub — the
# server binds an ephemeral loopback port and dies with the test)
# ---------------------------------------------------------------------------


class _InboxHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        stub = self.server.stub
        stub.requests.append(self.headers.get("Authorization", ""))
        if self.path.partition("?")[0] == "/inbox":
            body = json.dumps(stub.messages).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *_args):  # keep pytest output clean
        pass


@pytest.fixture()
def inbox_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _InboxHandler)
    stub = SimpleNamespace(messages=[], requests=[],
                           url=f"http://127.0.0.1:{server.server_address[1]}")
    server.stub = stub
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield stub
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _hook_env(tmp_path, url, agent="runtime", with_key=True, **script_kw):
    """Write the GENERATED hook script plus an isolated AGORA_HOME; return
    (script_path, home_path)."""
    script = tmp_path / "hook.py"
    script.write_text(stop_hook_script(url, agent, **script_kw))
    home = tmp_path / "agora-home"
    home.mkdir(exist_ok=True)
    if with_key:
        (home / "keys.json").write_text(json.dumps({f"{url}::{agent}": "k1"}))
    return script, home


def _run_hook(script, home, payload="{}"):
    env = {"AGORA_HOME": str(home), "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, str(script)], input=payload,
                          env=env, capture_output=True, text=True, timeout=30)


def _ledger_path(home, agent="runtime"):
    return home / f"hook-attempts-{agent}.json"


def _shift_last(home, channel, seconds, agent="runtime"):
    """Simulate time passing by editing the ledger's `last` timestamp."""
    path = _ledger_path(home, agent)
    ledger = json.loads(path.read_text())
    ledger[channel]["last"] = time.time() - seconds
    path.write_text(json.dumps(ledger))


# ---------------------------------------------------------------------------
# generated hook, executed: fresh-seq prompt / backoff / noop paths
# ---------------------------------------------------------------------------


def test_hook_fresh_seq_prompts_and_seeds_ledger(tmp_path, inbox_stub):
    inbox_stub.messages = [
        {"channel": "commons", "seq": 4, "from": "memory"},
        {"channel": "commons", "seq": 5, "from": "memory"},
        {"channel": "dm:runtime--memory", "seq": 2, "from": "memory"},
    ]
    script, home = _hook_env(tmp_path, inbox_stub.url,
                             reprompt_key="followup_message")
    out = _run_hook(script, home)
    assert out.returncode == 0
    prompt = json.loads(out.stdout)["followup_message"]
    assert "3 unread" in prompt and "2 channel(s)" in prompt
    assert "(3 new)" in prompt
    # Informational wording: review-and-decide, never a bare command.
    assert "decide what needs action" in prompt
    assert "listener is armed" in prompt
    assert inbox_stub.requests[0] == "Bearer k1"  # authenticated instant GET

    ledger = json.loads(_ledger_path(home).read_text())
    assert ledger["commons"] == pytest.approx(
        {"seq": 5, "attempts": 1, "last": ledger["commons"]["last"]})
    assert ledger["commons"]["last"] == pytest.approx(time.time(), abs=30)
    assert ledger["dm:runtime--memory"]["seq"] == 2


def test_hook_fresh_seq_prompts_decision_block_contract(tmp_path, inbox_stub):
    """The Claude/Codex re-prompt contract, EXECUTED (the fresh-seq test above
    only runs Cursor's followup_message variant): stdout must be exactly one
    {"decision": "block", "reason": ...} object."""
    inbox_stub.messages = [{"channel": "commons", "seq": 7, "from": "memory"}]
    for name, kw in [("claude", {}), ("codex", {"noop_output": '""'})]:
        sub = tmp_path / name
        sub.mkdir()
        script, home = _hook_env(sub, inbox_stub.url, **kw)
        out = _run_hook(script, home)
        assert out.returncode == 0, out.stderr
        obj = json.loads(out.stdout)
        assert obj["decision"] == "block"
        assert "1 unread" in obj["reason"] and "(1 new)" in obj["reason"]
        assert "listener is armed" in obj["reason"]
        assert json.loads(_ledger_path(home).read_text())["commons"]["seq"] == 7


def test_hook_backoff_throttles_then_reprompts(tmp_path, inbox_stub):
    """Standing unread: silent while the window is open, re-prompt after
    120s*2^(n-1). Time is simulated by editing the ledger's timestamps."""
    inbox_stub.messages = [{"channel": "commons", "seq": 5}]
    script, home = _hook_env(tmp_path, inbox_stub.url,
                             reprompt_key="followup_message")
    assert "followup_message" in _run_hook(script, home).stdout  # seeds ledger

    # Immediately after the prompt: window (120s) open -> noop.
    out = _run_hook(script, home)
    assert json.loads(out.stdout) == {}

    # 130s later (edited): due again -> re-prompt, 0 new, attempts -> 2.
    _shift_last(home, "commons", 130)
    out = _run_hook(script, home)
    assert "(0 new)" in json.loads(out.stdout)["followup_message"]
    assert json.loads(_ledger_path(home).read_text())["commons"]["attempts"] == 2

    # 130s after THAT prompt: attempts=2 window is 240s -> still throttled.
    _shift_last(home, "commons", 130)
    assert json.loads(_run_hook(script, home).stdout) == {}

    # 250s: past the 240s window -> re-prompt, attempts -> 3.
    _shift_last(home, "commons", 250)
    assert "followup_message" in _run_hook(script, home).stdout
    assert json.loads(_ledger_path(home).read_text())["commons"]["attempts"] == 3


def test_hook_backoff_caps_at_1800s(tmp_path, inbox_stub):
    """Uncapped, attempts=30 would mean a ~2-century window; the cap keeps
    standing unread re-prompting at least every 30 minutes."""
    inbox_stub.messages = [{"channel": "commons", "seq": 5}]
    script, home = _hook_env(tmp_path, inbox_stub.url,
                             reprompt_key="followup_message")
    _ledger_path(home).write_text(json.dumps(
        {"commons": {"seq": 5, "attempts": 30, "last": time.time() - 1700}}))
    assert json.loads(_run_hook(script, home).stdout) == {}  # 1700 < 1800

    _ledger_path(home).write_text(json.dumps(
        {"commons": {"seq": 5, "attempts": 30, "last": time.time() - 1900}}))
    assert "followup_message" in _run_hook(script, home).stdout  # 1900 >= cap


def test_hook_empty_inbox_noops_and_leaves_ledger_alone(tmp_path, inbox_stub):
    inbox_stub.messages = []
    script, home = _hook_env(tmp_path, inbox_stub.url,
                             reprompt_key="followup_message")
    sentinel = json.dumps({"commons": {"seq": 9, "attempts": 3, "last": 1.0}})
    _ledger_path(home).write_text(sentinel)
    out = _run_hook(script, home)
    assert json.loads(out.stdout) == {}
    assert _ledger_path(home).read_text() == sentinel  # untouched, byte-same

    # And with no ledger at all: noop must not create one.
    _ledger_path(home).unlink()
    _run_hook(script, home)
    assert not _ledger_path(home).exists()


def test_hook_stop_hook_active_noops_without_touching_hub(tmp_path, inbox_stub):
    inbox_stub.messages = [{"channel": "commons", "seq": 5}]
    script, home = _hook_env(tmp_path, inbox_stub.url,
                             reprompt_key="followup_message")
    out = _run_hook(script, home, payload=json.dumps({"stop_hook_active": True}))
    assert json.loads(out.stdout) == {}
    assert inbox_stub.requests == []           # guard fires BEFORE the GET
    assert not _ledger_path(home).exists()


def test_hook_missing_keys_noops_silently_all_variants(tmp_path, inbox_stub):
    """No credentials -> silent no-op in each harness's output contract,
    without ever contacting the hub or writing state."""
    inbox_stub.messages = [{"channel": "commons", "seq": 5}]
    for name, kw, expected in [
        ("cursor", {"reprompt_key": "followup_message"}, "{}"),
        ("claude", {}, "{}"),
        ("codex", {"noop_output": '""'}, ""),
    ]:
        sub = tmp_path / name
        sub.mkdir()
        script, home = _hook_env(sub, inbox_stub.url, with_key=False, **kw)
        out = _run_hook(script, home)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == expected
        assert out.stderr == ""
    assert inbox_stub.requests == []


def test_hook_hub_unreachable_noops(tmp_path):
    script, home = _hook_env(tmp_path, "http://127.0.0.1:9",  # closed port
                             reprompt_key="followup_message")
    out = _run_hook(script, home)
    assert out.returncode == 0
    assert json.loads(out.stdout) == {}


def test_hook_ledger_never_blocks_after_ack(tmp_path, inbox_stub):
    """The ledger THROTTLES, it never means handled: once the agent acks
    (unread empties), a NEW seq must prompt immediately even though the
    channel's backoff window is still wide open."""
    inbox_stub.messages = [{"channel": "commons", "seq": 5}]
    script, home = _hook_env(tmp_path, inbox_stub.url,
                             reprompt_key="followup_message")
    assert "followup_message" in _run_hook(script, home).stdout
    assert json.loads(_run_hook(script, home).stdout) == {}  # throttled now

    inbox_stub.messages = []                    # agent ack'd: unread empty
    assert json.loads(_run_hook(script, home).stdout) == {}

    inbox_stub.messages = [{"channel": "commons", "seq": 6}]  # new delivery
    out = _run_hook(script, home)               # window open, but seq is FRESH
    prompt = json.loads(out.stdout)["followup_message"]
    assert "(1 new)" in prompt
    ledger = json.loads(_ledger_path(home).read_text())
    assert ledger["commons"]["seq"] == 6
    assert ledger["commons"]["attempts"] == 1   # fresh resets the decay


def test_hook_corrupt_ledger_recovers(tmp_path, inbox_stub):
    """A missing, truncated, or garbage ledger must never wedge the hook:
    everything counts as fresh, and a valid ledger is rewritten."""
    inbox_stub.messages = [{"channel": "commons", "seq": 5}]
    script, home = _hook_env(tmp_path, inbox_stub.url,
                             reprompt_key="followup_message")
    for garbage in ['not json at all', '[1, 2, 3]',
                    json.dumps({"commons": "what"}),
                    json.dumps({"commons": {"seq": "NaN", "attempts": "x"}}),
                    json.dumps({"commons": {"seq": 5, "attempts": 1e12,
                                            "last": time.time() + 9e9}})]:
        _ledger_path(home).write_text(garbage)
        out = _run_hook(script, home)
        assert "followup_message" in out.stdout, (garbage, out.stderr)
        ledger = json.loads(_ledger_path(home).read_text())
        assert ledger["commons"]["seq"] == 5    # sane state rewritten


def test_hook_version_stamp_and_shared_guards():
    for kw in [{}, {"reprompt_key": "followup_message"},
               {"noop_output": '""'}]:
        script = stop_hook_script("http://h:1", "a", **kw)
        assert script.splitlines()[1] == "# agora-hook v2"
        assert "stop_hook_active" in script
        assert "hook-attempts-" in script       # v2 ledger, not v1 hook-state
        assert "hook-state" not in script
        assert "wait=" not in script            # instant check, never long-poll


# ---------------------------------------------------------------------------
# rule text: arming ritual, informational wake semantics, honest wake notes
# ---------------------------------------------------------------------------


def test_rule_text_cursor_has_arming_ritual_and_no_watcher_ban(tmp_path):
    setup_cursor(tmp_path, "runtime", "http://hub:8765", "", "agora-mcp",
                 with_hook=False)
    rule = (tmp_path / ".cursor" / "rules" / "agora.md").read_text()
    assert rule == rule_text("runtime")          # the one shared template

    # ARMING RITUAL: monitored background listen, then check_inbox, verify
    # armed, re-arm at boundaries if dead.
    assert "ARMING RITUAL" in rule and "FIRST TURN" in rule
    assert "agora listen --as runtime" in rule
    assert "MONITORED BACKGROUND" in rule and "AGORA_WAKE" in rule
    assert "60s" in rule
    assert "THEN call `check_inbox`" in rule
    assert "AGORA_LISTEN armed" in rule
    assert "re-arm" in rule

    # The v1 lies are gone: watcher ban, push-not-pull promise, attaché.
    assert "never start a watcher" not in rule
    assert "push, not pull" not in rule
    assert "attach" not in rule.lower()          # attaché/attache both

    # Foreground-waiting ban survives the reversal.
    assert "FOREGROUND" in rule and "wait_for_messages" in rule


def test_rule_text_cursor_monitor_is_mandatory_with_exact_args(tmp_path):
    """The live-test failure mode: a listener backgrounded WITHOUT the output
    monitor runs fine and stays deaf. The rule must declare the monitor
    MANDATORY and spell out the EXACT tool arguments — an agent must be able
    to copy them rather than improvise."""
    setup_cursor(tmp_path, "runtime", "http://hub:8765", "", "agora-mcp",
                 with_hook=False)
    rule = (tmp_path / ".cursor" / "rules" / "agora.md").read_text()

    assert "MANDATORY" in rule
    # The one true arming call, verbatim (pattern ^AGORA_WAKE, debounce
    # >= 5000ms, immediate backgrounding):
    assert "command: agora listen --as runtime" in rule
    assert "block_until_ms: 0" in rule
    assert ('notify_on_output: {"pattern": "^AGORA_WAKE", '
            '"reason": "agora wake", "debounce_ms": 60000}') in rule
    assert "debounce_ms >= 5000" in rule
    # Name the wrong ways explicitly (they "work" and stay deaf):
    assert "`&`/nohup" in rule
    assert "stays deaf" in rule


def test_rule_text_cursor_self_check_before_turn_end(tmp_path):
    """Arming must be self-checkable: both halves (the tool call carried the
    monitor; the armed sentinel is visible), the already-armed lock trap, and
    the order 'fix it BEFORE ending the turn'."""
    setup_cursor(tmp_path, "runtime", "http://hub:8765", "", "agora-mcp",
                 with_hook=False)
    rule = (tmp_path / ".cursor" / "rules" / "agora.md").read_text()

    assert "SELF-CHECK before ending the turn" in rule
    assert "MUST see an `AGORA_LISTEN armed` line" in rule
    assert "carried `notify_on_output`" in rule
    # The deaf-lock trap: an unmonitored earlier listener holds the lock, so
    # a CORRECT re-arm reports already-armed and exits — the rule must not
    # let that read as success.
    assert "reason=already-armed" in rule
    assert "kill" in rule and "listen-runtime.pid" in rule
    assert "redo step 1" in rule


def test_rule_text_wake_is_informational_in_all_variants(tmp_path):
    setup_cursor(tmp_path, "r1", "http://h:1", "", "m", with_hook=False)
    setup_claude(tmp_path, "r1", "http://h:1", "", "m", with_hook=False)
    setup_codex(tmp_path, "r1", "http://h:1", "", "m")
    for text in [(tmp_path / ".cursor" / "rules" / "agora.md").read_text(),
                 (tmp_path / "CLAUDE.md").read_text(),
                 (tmp_path / "AGENTS.md").read_text()]:
        assert "INFORMATION, not an order" in text
        assert "DECIDE whether anything needs action" in text
        # Kept invariants: no machine persistence, quoted-data, store/DM.
        assert "NEVER install machine persistence" in text
        assert "quoted DATA" in text
        assert "store_get" in text and "send_dm" in text
        assert "orchestrator" in text


def test_rule_text_per_harness_wake_notes(tmp_path):
    setup_claude(tmp_path, "castor", "http://h:1", "", "m", with_hook=False)
    claude = (tmp_path / "CLAUDE.md").read_text()
    assert "SessionStart/Stop hooks arm a single-shot listener" in claude
    assert "ARMING RITUAL" not in claude         # hooks arm it, not the agent
    assert "notify_on_output" not in claude      # Cursor-only tool surface

    setup_codex(tmp_path, "janus", "http://h:1", "", "m")
    codex = (tmp_path / "AGENTS.md").read_text()
    assert "no idle wake" in codex               # the gap, stated honestly
    assert "expected, not a fault" in codex
    assert "ARMING RITUAL" not in codex
    assert "notify_on_output" not in codex
    assert "attach" not in codex.lower()


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
    assert entry["loop_limit"] == 3 and entry["timeout"] == 10
    # ABSOLUTE command path: hook commands resolve against the launch dir,
    # not the hooks file (the deployed-fleet relative-path trap).
    script_path = (tmp_path / ".cursor" / "hooks" / "agora_wait.sh").resolve()
    assert entry["command"] == str(script_path)

    script = script_path.read_text()
    assert "followup_message" in script          # Cursor's re-prompt contract
    assert '"decision"' not in script            # not Claude/Codex's
    assert len(written) == 4


def test_cursor_hooks_json_merge_preserves_foreign_hooks(tmp_path):
    """Re-running setup replaces ONLY agora_wait entries: foreign stop hooks,
    other events, and a user-set version survive; nothing duplicates."""
    hooks_path = tmp_path / ".cursor" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(json.dumps({
        "version": 3,
        "hooks": {
            "stop": [{"command": "my_stop.sh", "timeout": 5},
                     # stale v1 agora entry (the relative-path trap):
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
    agora = [e for e in stop if "agora_wait" in e["command"]]
    assert len(agora) == 1                             # replaced, not stacked
    assert agora[0]["command"].startswith("/")         # absolute now
    assert agora[0]["timeout"] == 10 and agora[0]["loop_limit"] == 3


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
    assert len(written) == len(set(written)) == 4     # settings listed once

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    stop_cmds = [h["command"] for e in settings["hooks"]["Stop"]
                 for h in e["hooks"]]
    # Both halves of reception: the stop-hook backstop AND the Stop re-arm.
    [stop_hook] = [c for c in stop_cmds if c.endswith("agora_stop.py")]
    assert stop_hook.startswith("/")                  # absolute path
    assert any("listen --as castor" in c for c in stop_cmds)


def test_claude_listener_entries_match_documented_schema(tmp_path):
    """Schema per https://code.claude.com/docs/en/hooks: command handler with
    asyncRewake (background + wake on exit 2) and timeout in SECONDS."""
    [settings_path] = install_claude_listener(tmp_path, "http://hub:8765",
                                              "castor")
    settings = json.loads(settings_path.read_text())
    for event in ("SessionStart", "Stop"):
        [handler] = [h for e in settings["hooks"][event] for h in e["hooks"]]
        assert handler["type"] == "command"
        assert handler["asyncRewake"] is True
        assert handler["timeout"] == 86400            # 24h, in seconds
        assert "listen --as castor --once" in handler["command"]
        assert "--url http://hub:8765" in handler["command"]
        assert "listen-castor.lock" in handler["command"]


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
                for h in e["hooks"]]) == 1


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
    assert len([c for c in stop_cmds if c.endswith("agora_stop.py")]) == 1
    assert "/old/place/agora_stop.py" not in stop_cmds  # stale one replaced
    assert "mine.sh" in stop_cmds                       # foreign survives
    assert len([c for c in stop_cmds if "listen --as" in c]) == 1


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

    # Re-run: existing agora table untouched, AGENTS.md not duplicated.
    setup_codex(tmp_path, "janus", "http://hub:8765", "", "agora-mcp")
    assert (tmp_path / ".codex" / "config.toml").read_text() == toml_text
    assert (tmp_path / "AGENTS.md").read_text().count("agora agent: janus") == 1


def test_setup_codex_with_hook_writes_stop_hook(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "hooks.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"type": "command", "command": "other.py"}]}}))
    for _ in range(2):  # idempotent: no duplicate agora entry on re-run
        setup_codex(tmp_path, "janus", "http://hub:8765", "", "agora-mcp",
                    with_hook=True)
    hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    entries = hooks["hooks"]["Stop"]
    assert {"type": "command", "command": "other.py"} in entries  # foreign kept
    agora = [e for e in entries if "agora_stop" in e["command"]]
    assert len(agora) == 1
    assert agora[0]["command"].startswith("/") and agora[0]["timeout"] == 10
    script = (tmp_path / ".codex" / "hooks" / "agora_stop.py").read_text()
    # Codex no-op contract: NO stdout (Claude's variant prints "{}").
    assert 'NOOP = ""' in script
    assert "stop_hook_active" in script


def test_codex_toml_block_quotes_special_characters():
    block = codex_toml_block("agora-mcp", "http://h:1", "a", 'says "hi"\\path')
    assert '"says \\"hi\\"\\\\path"' in block  # JSON escaping is valid TOML


# ---------------------------------------------------------------------------
# credential placement: the env block is the only channel that survives the
# harness env scrub, so an api_key must land there — and the keyless output
# must stay byte-identical (local zero-config onboarding untouched)
# ---------------------------------------------------------------------------


def test_write_mcp_json_keyless_output_is_byte_identical(tmp_path):
    """No key -> the EXACT file the previous version wrote: same env keys,
    no AGORA_API_KEY, no permission clamp. Local onboarding must not change."""
    path = tmp_path / "mcp.json"
    write_mcp_json(path, "agora-mcp", "http://hub:8765", "runtime", "the kernel")
    expected = json.dumps({"mcpServers": {"agora": {
        "command": "agora-mcp",
        "env": {"AGORA_URL": "http://hub:8765", "AGORA_AGENT_ID": "runtime",
                "AGORA_ABOUT": "the kernel"},
    }}}, indent=2) + "\n"
    assert path.read_text() == expected
    assert path.stat().st_mode & 0o077 != 0    # default perms, not clamped


def test_write_mcp_json_embeds_api_key_and_clamps_0600(tmp_path):
    path = tmp_path / "mcp.json"
    (tmp_path / "mcp.json").write_text(json.dumps(
        {"mcpServers": {"other": {"command": "other-mcp"}}}))
    write_mcp_json(path, "agora-mcp", "http://hub:8765", "castor", "",
                   api_key="agora_secret123")
    config = json.loads(path.read_text())
    env = config["mcpServers"]["agora"]["env"]
    assert env["AGORA_API_KEY"] == "agora_secret123"
    assert env["AGORA_URL"] == "http://hub:8765"
    assert config["mcpServers"]["other"] == {"command": "other-mcp"}  # merged
    assert path.stat().st_mode & 0o077 == 0    # a secret-bearing file is 0600


def test_setup_cursor_and_claude_thread_api_key(tmp_path):
    for name, fn, mcp_rel in [("cursor", setup_cursor, ".cursor/mcp.json"),
                              ("claude", setup_claude, ".mcp.json")]:
        ws = tmp_path / name
        ws.mkdir()
        fn(ws, "castor", "http://hub:8765", "", "agora-mcp", False,
           api_key="agora_k1")
        mcp_path = ws / mcp_rel
        env = json.loads(mcp_path.read_text())["mcpServers"]["agora"]["env"]
        assert env["AGORA_API_KEY"] == "agora_k1", name
        assert mcp_path.stat().st_mode & 0o077 == 0, name


def test_codex_toml_api_key_line_and_chmod(tmp_path):
    block = codex_toml_block("agora-mcp", "http://h:1", "janus", "",
                             api_key="agora_k2")
    assert 'AGORA_API_KEY = "agora_k2"' in block
    assert "AGORA_API_KEY" not in codex_toml_block("agora-mcp", "http://h:1",
                                                   "janus", "")
    setup_codex(tmp_path, "janus", "http://h:1", "", "agora-mcp",
                api_key="agora_k2")
    config_path = tmp_path / ".codex" / "config.toml"
    assert 'AGORA_API_KEY = "agora_k2"' in config_path.read_text()
    assert config_path.stat().st_mode & 0o077 == 0


def test_upsert_marked_section_replaces_only_the_marked_block(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("intro\n")
    upsert_marked_section(path, "v1 content")
    upsert_marked_section(path, "v2 content")
    text = path.read_text()
    assert "intro" in text and "v2 content" in text and "v1 content" not in text
