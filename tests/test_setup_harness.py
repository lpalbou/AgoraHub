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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from agora.setup_harness import (codex_toml_block, custom_home_env,
                                 install_claude_listener,
                                 register_claude_local, register_codex_global,
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


def _ledger(home, agent="runtime"):
    return json.loads(_ledger_path(home, agent).read_text())


def _shift_last_prompt(home, seconds, agent="runtime"):
    """Simulate time passing by editing the v4 ledger's last_prompt."""
    path = _ledger_path(home, agent)
    led = json.loads(path.read_text())
    led["last_prompt"] = time.time() - seconds
    path.write_text(json.dumps(led))


# ---------------------------------------------------------------------------
# generated hook, executed: obligation gate / floor / guards / noop paths
# ---------------------------------------------------------------------------


def test_hook_obligations_prompt_and_seed_ledger(tmp_path, inbox_stub):
    inbox_stub.messages = [
        {"channel": "commons", "seq": 4, "from": "memory", "status": "open",
         "id": "m4"},
        {"channel": "commons", "seq": 5, "from": "memory", "status": "fyi",
         "id": "m5"},
        {"channel": "dm:runtime--memory", "seq": 2, "from": "memory",
         "status": "reply", "flags": "reply-to-me", "id": "m2"},
    ]
    inbox_stub.owed = {"to_answer": [{"id": "a1"}], "to_consume": [],
                       "waiting_on": []}
    script, home = _hook_env(tmp_path, inbox_stub.url,
                             reprompt_key="followup_message")
    out = _run_hook(script, home)
    assert out.returncode == 0, out.stderr
    prompt = json.loads(out.stdout)["followup_message"]
    # 3 obligations: owed ask a1 + open m4 + flagged m2. The fyi m5 is NOT
    # counted — fyi waits for an organic turn.
    assert "3 obligation(s)" in prompt
    assert "1 unanswered ask(s)" in prompt
    assert "2 open/blocked unread" in prompt
    # Anti-lurk wording (2026-07-13): debts first, ack demoted to a
    # seen-marker — never "review and decide" with ack as the goal.
    assert "settle what you OWE first" in prompt
    assert "ack = seen, not done" in prompt
    assert inbox_stub.requests[0] == "Bearer k1"  # authenticated instant GET
    # v4 declares itself to the hub (stale-client detection, notice hygiene).
    assert all(h for h in inbox_stub.client_headers)

    led = _ledger(home)
    assert led["v"] == 4 and led["attempts"] == 1
    assert len(led["sig"]) == 16
    assert led["last_prompt"] == pytest.approx(time.time(), abs=30)


def test_hook_prompts_decision_block_contract(tmp_path, inbox_stub):
    """The Claude/Codex re-prompt contract, EXECUTED: stdout must be exactly
    one {"decision": "block", "reason": ...} object."""
    inbox_stub.messages = [{"channel": "commons", "seq": 7, "from": "memory",
                            "status": "open", "id": "m7"}]
    for name, kw in [("claude", {}), ("codex", {"noop_output": '""'})]:
        sub = tmp_path / name
        sub.mkdir()
        script, home = _hook_env(sub, inbox_stub.url, **kw)
        out = _run_hook(script, home)
        assert out.returncode == 0, out.stderr
        obj = json.loads(out.stdout)
        assert obj["decision"] == "block"
        assert "1 obligation(s)" in obj["reason"]
        assert _ledger(home)["attempts"] == 1


def test_hook_fyi_never_prompts(tmp_path, inbox_stub):
    """fyi unread — including the hub's synthetic notices — must never cost
    a turn: it waits for the next organic check_inbox."""
    inbox_stub.messages = [
        {"channel": "commons", "seq": 5, "from": "memory", "status": "fyi"},
        {"channel": "commons", "seq": 6, "from": "hub", "status": "fyi",
         "flags": "to-you"},  # hub notices ride from=hub: never a turn
    ]
    script, home = _hook_env(tmp_path, inbox_stub.url,
                             reprompt_key="followup_message")
    out = _run_hook(script, home)
    assert json.loads(out.stdout) == {}


def test_hook_global_floor_gates_even_changed_debt(tmp_path, inbox_stub):
    """One prompt per FLOOR seconds, PERIOD: a brand-new obligation arriving
    seconds after a prompt does not buy another turn (instant delivery is
    the listener's job; the hook is the slow backstop)."""
    inbox_stub.owed = {"to_answer": [{"id": "a1"}], "to_consume": [],
                       "waiting_on": []}
    script, home = _hook_env(tmp_path, inbox_stub.url,
                             reprompt_key="followup_message")
    assert "followup_message" in _run_hook(script, home).stdout

    # New debt lands immediately: sig changed, but the floor is closed.
    inbox_stub.owed = {"to_answer": [{"id": "a1"}, {"id": "a2"}],
                       "to_consume": [], "waiting_on": []}
    assert json.loads(_run_hook(script, home).stdout) == {}

    # 610s later the floor opens: the changed debt prompts, attempts resets.
    _shift_last_prompt(home, 610)
    out = _run_hook(script, home)
    assert "2 obligation(s)" in json.loads(out.stdout)["followup_message"]
    assert _ledger(home)["attempts"] == 1


def test_hook_unchanged_debt_backs_off_to_cap(tmp_path, inbox_stub):
    """Standing UNCHANGED debt re-prompts on exponential backoff
    600s*2^(n-1) capped at 3600s — never at the raw floor rate."""
    inbox_stub.owed = {"to_answer": [{"id": "a1"}], "to_consume": [],
                       "waiting_on": []}
    script, home = _hook_env(tmp_path, inbox_stub.url,
                             reprompt_key="followup_message")
    assert "followup_message" in _run_hook(script, home).stdout  # attempts=1

    # 610s: attempts=1 window is 600s -> due, prompt, attempts -> 2.
    _shift_last_prompt(home, 610)
    assert "followup_message" in _run_hook(script, home).stdout
    assert _ledger(home)["attempts"] == 2

    # 610s again: attempts=2 window is 1200s -> throttled.
    _shift_last_prompt(home, 610)
    assert json.loads(_run_hook(script, home).stdout) == {}

    # 1250s: past the 1200s window -> prompt, attempts -> 3.
    _shift_last_prompt(home, 1250)
    assert "followup_message" in _run_hook(script, home).stdout
    assert _ledger(home)["attempts"] == 3

    # Cap: attempts=30 would be centuries uncapped; 3600s is the ceiling.
    led = _ledger(home)
    led["attempts"] = 30
    led["last_prompt"] = time.time() - 3500
    _ledger_path(home).write_text(json.dumps(led))
    assert json.loads(_run_hook(script, home).stdout) == {}  # 3500 < 3600
    _shift_last_prompt(home, 3700)
    assert "followup_message" in _run_hook(script, home).stdout


def test_hook_cleared_debt_silent_then_new_debt_prompts(tmp_path, inbox_stub):
    """The ledger THROTTLES, it never means handled: once debts clear the
    hook goes silent, and a NEW obligation prompts at the next floor-open
    stop with the decay reset."""
    inbox_stub.owed = {"to_answer": [{"id": "a1"}], "to_consume": [],
                       "waiting_on": []}
    script, home = _hook_env(tmp_path, inbox_stub.url,
                             reprompt_key="followup_message")
    assert "followup_message" in _run_hook(script, home).stdout

    inbox_stub.owed = {"to_answer": [], "to_consume": [], "waiting_on": []}
    assert json.loads(_run_hook(script, home).stdout) == {}
    assert _ledger(home)["sig"] == ""           # cleared debt resets the sig

    inbox_stub.owed = {"to_answer": [{"id": "a9"}], "to_consume": [],
                       "waiting_on": []}
    _shift_last_prompt(home, 610)
    out = _run_hook(script, home)
    assert "1 obligation(s)" in json.loads(out.stdout)["followup_message"]
    assert _ledger(home)["attempts"] == 1       # fresh debt resets the decay


def test_hook_payload_guards_completed_and_loop_count(tmp_path, inbox_stub):
    """Cursor guards: an aborted/errored turn must not breed a follow-up
    (the human just cancelled, or the provider just failed), and loop_count
    >= 2 caps the chain script-side. Both fire BEFORE any hub contact.
    Claude/Codex payloads lack the fields: same script still prompts."""
    inbox_stub.owed = {"to_answer": [{"id": "a1"}], "to_consume": [],
                       "waiting_on": []}
    script, home = _hook_env(tmp_path, inbox_stub.url,
                             reprompt_key="followup_message")

    for payload in ({"status": "aborted"}, {"status": "error"},
                    {"loop_count": 2}, {"loop_count": 7},
                    {"stop_hook_active": True}):
        out = _run_hook(script, home, payload=json.dumps(payload))
        assert json.loads(out.stdout) == {}, payload
    assert inbox_stub.requests == []            # guards fire BEFORE any GET
    assert not _ledger_path(home).exists()

    # A completed turn under the cap prompts normally.
    out = _run_hook(script, home,
                    payload=json.dumps({"status": "completed",
                                        "loop_count": 1}))
    assert "followup_message" in out.stdout
    # And a payload with NEITHER field (Claude/Codex shape) still works.
    sub = tmp_path / "claudeshape"
    sub.mkdir()
    script2, home2 = _hook_env(sub, inbox_stub.url,
                               reprompt_key="followup_message")
    assert "followup_message" in _run_hook(script2, home2, payload="{}").stdout


def test_hook_missing_keys_noops_silently_all_variants(tmp_path, inbox_stub):
    """No credentials -> silent no-op in each harness's output contract,
    without ever contacting the hub or writing state."""
    inbox_stub.messages = [{"channel": "commons", "seq": 5, "status": "open"}]
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
        assert not _ledger_path(home).exists()
    assert inbox_stub.requests == []


def test_hook_hub_unreachable_noops(tmp_path):
    script, home = _hook_env(tmp_path, "http://127.0.0.1:9",  # closed port
                             reprompt_key="followup_message")
    out = _run_hook(script, home)
    assert out.returncode == 0
    assert json.loads(out.stdout) == {}


def test_hook_corrupt_ledger_recovers(tmp_path, inbox_stub):
    """A missing, truncated, garbage, or PRE-v4 ledger must never wedge the
    hook: state restarts clean and a valid v4 ledger is rewritten."""
    inbox_stub.owed = {"to_answer": [{"id": "a1"}], "to_consume": [],
                       "waiting_on": []}
    script, home = _hook_env(tmp_path, inbox_stub.url,
                             reprompt_key="followup_message")
    for garbage in ['not json at all', '[1, 2, 3]',
                    json.dumps({"commons": "what"}),
                    # v3 per-channel shape: restarts clean under v4
                    json.dumps({"commons": {"seq": 5, "attempts": 3,
                                            "last": time.time()}}),
                    json.dumps({"v": 4, "last_prompt": 9e18, "sig": 7,
                                "attempts": "x", "dead_streak": None})]:
        _ledger_path(home).write_text(garbage)
        out = _run_hook(script, home)
        assert "followup_message" in out.stdout, (garbage, out.stderr)
        led = _ledger(home)
        assert led["v"] == 4 and len(led["sig"]) == 16  # sane state rewritten


def test_hook_version_stamp_and_shared_guards():
    for kw in [{}, {"reprompt_key": "followup_message"},
               {"noop_output": '""'}]:
        script = stop_hook_script("http://h:1", "a", **kw)
        assert script.splitlines()[1] == "# agora-hook v4"
        assert "stop_hook_active" in script
        assert "loop_count" in script           # Cursor chain cap, v4
        assert '"completed"' in script          # aborted turns never chain
        assert "X-Agora-Client" in script       # honest client version
        assert "FLOOR" in script                # one global prompt floor
        assert "hook-attempts-" in script
        assert "hook-state" not in script
        assert "wait=" not in script            # instant check, never long-poll


def test_cursor_hook_dead_listener_needs_two_observations():
    """Cursor reception exists only while the agent's own RECEPTION LOOP
    runs — ONLY the Cursor hook carries the broken-loop nag (Claude re-arms
    via its own hooks; Codex has no idle wake). v4: the pidfile is
    legitimately absent ~5s of every listen cycle, so ONE dead observation
    is noise — the nag needs two consecutive dead stops, then obeys the
    global floor like every other prompt."""
    import json as _json
    import os
    import pathlib
    import subprocess
    import tempfile

    cursor = stop_hook_script("http://127.0.0.1:9", "seat",
                              reprompt_key="followup_message",
                              check_listener=True)
    assert "BACKGROUND RECEPTION" in cursor and "listen-{AGENT}.pid" in cursor
    assert "listen --once" in cursor and "^AGORA_WAKE" in cursor
    assert "foreground on real work" in cursor
    assert "never arm a second loop" in cursor  # the ×3-loops lesson
    for other in (stop_hook_script("http://h:1", "a"),
                  stop_hook_script("http://h:1", "a", noop_output='""')):
        assert "os.kill" not in other           # no pidfile probe elsewhere

    script = pathlib.Path(tempfile.mkdtemp()) / "hook.py"
    script.write_text(cursor)
    home = tempfile.mkdtemp()
    (pathlib.Path(home) / "keys.json").write_text(
        _json.dumps({"http://127.0.0.1:9::seat": "k"}))
    env = {**os.environ, "AGORA_HOME": home}

    def run(payload="{}"):
        return subprocess.run(["python3", str(script)], input=payload,
                              capture_output=True, text=True, env=env).stdout

    # First dead observation: NOISE (the listen loop's re-exec gap) -> noop.
    (pathlib.Path(home) / "listen-seat.pid").write_text("999999")
    assert run().strip() == "{}"
    # Second consecutive dead stop: now it is real -> nag.
    out = run()
    assert "BACKGROUND RECEPTION" in _json.loads(out)["followup_message"]
    # Third dead stop seconds later: the global floor holds -> silent.
    assert run().strip() == "{}"

    # A live pid resets the streak: one later dead read is noise again.
    (pathlib.Path(home) / "listen-seat.pid").write_text(str(os.getpid()))
    assert run().strip() == "{}"
    (pathlib.Path(home) / "listen-seat.pid").write_text("999999")
    assert run().strip() == "{}"

    # Guards still dominate: an aborted turn never nags, dead or not.
    assert run('{"status": "aborted"}').strip() == "{}"
    assert run('{"stop_hook_active": true}').strip() == "{}"


# ---------------------------------------------------------------------------
# rule text: arming ritual, informational wake semantics, honest wake notes
# ---------------------------------------------------------------------------


def test_rule_text_cursor_has_background_reception_and_no_watcher_ban(tmp_path):
    setup_cursor(tmp_path, "runtime", "http://hub:8765", "", "agora-mcp",
                 with_hook=False)
    rule = (tmp_path / ".cursor" / "rules" / "agora.mdc").read_text()
    # The .mdc frontmatter is what makes Cursor actually inject the rule; a
    # plain .md is ignored, so this is load-bearing, not cosmetic.
    assert rule.startswith("---\nalwaysApply: true\n---\n")
    assert rule.endswith(rule_text("runtime"))   # the one shared template

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


def test_rule_text_cursor_headless_is_driven(tmp_path):
    """--headless wires a DRIVEN seat: an external watcher owns reception,
    so the rule must forbid in-session listeners entirely (never teach a
    listen loop) and teach the turn contract: settle, ack, END. It must
    also not install the listener-nag stop hook, which would order the
    exact behavior the rule forbids."""
    written = setup_cursor(tmp_path, "runtime", "http://hub:8765", "",
                           "agora-mcp", with_hook=True, headless=True)
    rule = (tmp_path / ".cursor" / "rules" / "agora.mdc").read_text()

    assert "DRIVEN RECEPTION" in rule
    assert "agora listen" in rule                 # named only to forbid it
    assert "NEVER run `agora listen`" in rule
    assert "while true; do agora listen" not in rule   # no loop is ever taught
    assert "END" in rule and "watcher" in rule
    assert "--idle-nudge" not in rule
    # The listener-nag hook must NOT be installed for a driven seat, even
    # with with_hook=True.
    assert not (tmp_path / ".cursor" / "hooks.json").exists()
    assert all("hooks" not in str(p) for p in written)


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
    channel): every setup harness parser takes --channels, and the value
    parses as a comma list."""
    from agora.cli import build_parser

    p = build_parser()
    for harness in ("cursor", "claude", "codex"):
        args = p.parse_args(["setup", harness, "x", "--channels", "a,b",
                             "--workspace", str(tmp_path)])
        assert args.channels == "a,b"


def test_codex_project_config_approves_agora_tools(tmp_path):
    """Without default_tools_approval_mode=approve Codex prompts per TOOL
    NAME on first use — an unattended seat freezes on a dialog at every new
    verb (live 3-seat run, 2026-07-14: whoami, list_channels, check_inbox,
    ... each stalled until a human clicked)."""
    setup_codex(tmp_path, "cx", "http://hub:1", "", "agora-mcp")
    toml = (tmp_path / ".codex" / "config.toml").read_text()
    assert 'default_tools_approval_mode = "approve"' in toml
    # the key must live in the server table, before the env table
    assert toml.index("default_tools_approval_mode") < toml.index("[mcp_servers.agora.env]")


def test_codex_dedicated_rule_teaches_the_standing_loop(tmp_path):
    """--headless (dedicated) codex: the standing wait_for_messages loop IS
    the seat's reachability and the rule must say so. The generic wait-ban
    variant made live seats wait once, end the turn, and go deaf
    (2026-07-14). The default (shared) rule must keep the ban."""
    setup_codex(tmp_path, "cx", "http://hub:1", "", "agora-mcp",
                dedicated=True)
    rule = (tmp_path / "AGENTS.md").read_text()
    assert "wait_for_messages(45)" in rule
    assert "NEVER exit" in rule
    assert "DEDICATED seat" in rule
    assert "do NOT end the turn because nothing arrived" in rule

    other = tmp_path / "shared2"
    other.mkdir()
    setup_codex(other, "cx", "http://hub:1", "", "agora-mcp")
    shared_rule = (other / "AGENTS.md").read_text()
    assert "NEVER wait or poll in the FOREGROUND" in shared_rule
    assert "wait_for_messages(45)" not in shared_rule


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
    assert 'AGORA_API_KEY = "agora_k"' in block
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
             ("AGORA_ABOUT", about)]
    if api_key:
        pairs.append(("AGORA_API_KEY", api_key))
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
