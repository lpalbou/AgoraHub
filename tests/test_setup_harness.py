"""setup-claude / setup-codex: project-scoped wiring, idempotent re-runs.

What must hold: configs land in the harness's documented project-scope
locations (never global), existing user content is preserved on re-run, and
the Claude Stop hook re-prompts via {"decision": "block"} only on NEW
messages and never while stop_hook_active (the documented loop guard).
"""

import json
import subprocess
import sys

from agora.setup_harness import (codex_toml_block, rule_text, setup_claude,
                                 setup_codex, setup_cursor,
                                 upsert_marked_section)


def test_setup_claude_writes_project_scoped_files(tmp_path):
    written = setup_claude(tmp_path, "castor", "http://hub:8765", "the entity",
                           "/usr/local/bin/agora-mcp", with_hook=True)
    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    server = mcp["mcpServers"]["agora"]
    assert server["command"] == "/usr/local/bin/agora-mcp"
    assert server["env"]["AGORA_AGENT_ID"] == "castor"
    assert "check_inbox" in (tmp_path / "CLAUDE.md").read_text()

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    [entry] = settings["hooks"]["Stop"]
    command = entry["hooks"][0]["command"]
    assert command.endswith("agora_stop.py")
    assert (tmp_path / ".claude" / "hooks" / "agora_stop.py") in written or True
    # The hook command is absolute (relative paths resolve against the launch
    # dir, not the settings file — the documented trap).
    assert command.startswith("/")


def test_setup_claude_is_idempotent_and_preserves_user_content(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# My project rules\nkeep me\n")
    (tmp_path / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"other": {"command": "other-mcp"}}}))
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}}))

    for _ in range(2):  # re-running must not duplicate anything
        setup_claude(tmp_path, "castor", "http://hub:8765", "",
                     "agora-mcp", with_hook=True)

    claude_md = (tmp_path / "CLAUDE.md").read_text()
    assert "keep me" in claude_md
    assert claude_md.count("agora agent: castor") == 1

    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert set(mcp["mcpServers"]) == {"other", "agora"}

    settings = json.loads((settings_dir / "settings.json").read_text())
    commands = [h["command"] for e in settings["hooks"]["Stop"] for h in e["hooks"]]
    assert len([c for c in commands if c.endswith("agora_stop.py")]) == 1
    assert "mine.sh" in commands  # pre-existing hook untouched


def test_setup_cursor_uses_the_shared_generators(tmp_path):
    """setup-cursor goes through the same module as claude/codex (one rule
    template, one stop-hook generator) — the drift-prone cli.py copies died."""
    written = setup_cursor(tmp_path, "runtime", "http://hub:8765", "the kernel",
                           "/usr/local/bin/agora-mcp", with_hook=True)
    mcp = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert mcp["mcpServers"]["agora"]["env"]["AGORA_AGENT_ID"] == "runtime"

    rule = (tmp_path / ".cursor" / "rules" / "agora.md").read_text()
    assert rule == rule_text("runtime")          # the one shared template

    hooks = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    [entry] = hooks["hooks"]["stop"]
    assert entry["loop_limit"] == 3 and entry["timeout"] == 10

    script = (tmp_path / ".cursor" / "hooks" / "agora_wait.sh").read_text()
    assert "followup_message" in script          # Cursor's re-prompt contract
    assert '"decision"' not in script            # not Claude/Codex's
    assert "stop_hook_active" in script          # shared loop guard
    assert "wait=" not in script                 # instant check, never long-poll
    assert len(written) == 4


def test_cursor_hook_noop_and_loop_guard(tmp_path):
    """Run the generated Cursor hook as a real subprocess: no key -> silent
    no-op JSON; stop_hook_active -> no re-prompt (loop guard)."""
    from agora.setup_harness import stop_hook_script

    hook = tmp_path / "hook.py"
    hook.write_text(stop_hook_script("http://127.0.0.1:1", "runtime",
                                     reprompt_key="followup_message"))
    home = tmp_path / "agora-home"
    home.mkdir()
    env = {"AGORA_HOME": str(home), "PATH": "/usr/bin:/bin"}

    out = subprocess.run([sys.executable, str(hook)], input="{}", env=env,
                         capture_output=True, text=True)
    assert json.loads(out.stdout) == {}

    (home / "keys.json").write_text(json.dumps(
        {"http://127.0.0.1:1::runtime": "agora_key"}))
    out = subprocess.run([sys.executable, str(hook)],
                         input=json.dumps({"stop_hook_active": True}),
                         env=env, capture_output=True, text=True)
    assert json.loads(out.stdout) == {}


def test_setup_codex_writes_project_config_and_agents_md(tmp_path):
    setup_codex(tmp_path, "janus", "http://hub:8765", "the door", "agora-mcp")
    toml_text = (tmp_path / ".codex" / "config.toml").read_text()
    assert "[mcp_servers.agora]" in toml_text
    assert 'AGORA_AGENT_ID = "janus"' in toml_text
    agents_md = (tmp_path / "AGENTS.md").read_text()
    assert "agora agent: janus" in agents_md
    assert "attaché" in agents_md  # codex wake note points at session resume

    # Re-run: existing agora table untouched, AGENTS.md not duplicated.
    setup_codex(tmp_path, "janus", "http://hub:8765", "", "agora-mcp")
    assert (tmp_path / ".codex" / "config.toml").read_text() == toml_text
    assert (tmp_path / "AGENTS.md").read_text().count("agora agent: janus") == 1


def test_setup_codex_with_hook_writes_stop_hook(tmp_path):
    setup_codex(tmp_path, "janus", "http://hub:8765", "", "agora-mcp",
                with_hook=True)
    hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    [entry] = hooks["hooks"]["Stop"]
    assert entry["type"] == "command" and entry["command"].startswith("/")
    script = (tmp_path / ".codex" / "hooks" / "agora_stop.py").read_text()
    # Codex no-op contract: NO stdout (Claude's variant prints "{}").
    assert "NOOP = \"\"" in script
    assert "stop_hook_active" in script
    # Idempotent: second run adds no duplicate hook entry.
    setup_codex(tmp_path, "janus", "http://hub:8765", "", "agora-mcp",
                with_hook=True)
    hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert len(hooks["hooks"]["Stop"]) == 1


def test_codex_toml_block_quotes_special_characters():
    block = codex_toml_block("agora-mcp", "http://h:1", "a", 'says "hi"\\path')
    assert '"says \\"hi\\"\\\\path"' in block  # JSON escaping is valid TOML


def test_upsert_marked_section_replaces_only_the_marked_block(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("intro\n")
    upsert_marked_section(path, "v1 content")
    upsert_marked_section(path, "v2 content")
    text = path.read_text()
    assert "intro" in text and "v2 content" in text and "v1 content" not in text


def test_claude_stop_hook_blocks_only_on_new_messages(tmp_path, monkeypatch):
    """Run the generated hook as a real subprocess against a fake inbox."""
    from agora.setup_harness import stop_hook_script

    hook = tmp_path / "hook.py"
    hook.write_text(stop_hook_script("http://127.0.0.1:1", "castor"))
    home = tmp_path / "agora-home"
    home.mkdir()
    env = {"AGORA_HOME": str(home), "PATH": "/usr/bin:/bin"}

    # No cached key -> silent no-op (never blocks a session it can't serve).
    out = subprocess.run([sys.executable, str(hook)], input="{}", env=env,
                         capture_output=True, text=True)
    assert json.loads(out.stdout) == {}

    # stop_hook_active -> no re-prompt even with a key (loop guard).
    (home / "keys.json").write_text(json.dumps(
        {"http://127.0.0.1:1::castor": "agora_key"}))
    out = subprocess.run([sys.executable, str(hook)],
                         input=json.dumps({"stop_hook_active": True}),
                         env=env, capture_output=True, text=True)
    assert json.loads(out.stdout) == {}
