"""New CLI surfaces: `agora create-channel`, the universal `--home` flag,
and the harness-CLI registration step in the join flow.

- create-channel runs end-to-end against a real hub on an ephemeral loopback
  port: a private room lands with its purpose in channel:meta and each
  --invite receives a DM whose token actually REDEEMS; --public rooms are
  joinable with no token.
- --home is accepted by EVERY verb (partial coverage would be the
  `--with-hooks` trap all over again) and maps onto AGORA_HOME before
  dispatch: flag > env > default, and the env var alone keeps working.
- `agora join --harness claude|codex` calls the harness's own `mcp add`
  registration (stubbed here — the real vendor calls are covered in
  test_setup_harness) and reports the outcome in the join ledger.

Nothing here touches the live hub, ~/.agora, or fixed ports.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agora import config as _config
from agora.cli import _apply_home, _port_holder, _preflight_port, build_parser
from agora.hub.app import create_app

ADMIN_KEY = "test-admin-cli-surfaces"


# ---------------------------------------------------------------------------
# fixtures (same pattern as test_join: ephemeral port, isolated home)
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "agora-home"
    monkeypatch.setenv("AGORA_HOME", str(home))
    for var in ("AGORA_URL", "AGORA_ADMIN_KEY", "AGORA_AGENT_ID", "AGORA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture()
def live_hub(tmp_path):
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(db_path=str(tmp_path / "hub.db"), admin_key=ADMIN_KEY,
                     rate_per_minute=600.0)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                              daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("test hub failed to start")
        time.sleep(0.02)
    yield SimpleNamespace(url=f"http://127.0.0.1:{port}", admin=ADMIN_KEY)
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive(), "test hub did not shut down"


def _run_cli(argv: list[str]) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


def _register(url: str, agent_id: str) -> str:
    r = httpx.post(f"{url}/agents", json={"id": agent_id},
                   headers={"Authorization": f"Bearer {ADMIN_KEY}"}, timeout=5)
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# create-channel
# ---------------------------------------------------------------------------


def test_create_channel_private_with_purpose_and_invite(live_hub, isolated_home,
                                                        capsys):
    """The whole surface: owner creation, purpose in channel:meta (what
    describe_channel shows joiners), and an --invite DM whose token REDEEMS."""
    alice_key = _register(live_hub.url, "alice")
    bob_key = _register(live_hub.url, "bob")
    _config.cache_key(live_hub.url, "alice", alice_key)

    _run_cli(["create-channel", "dev", "--as", "alice", "--url", live_hub.url,
              "--purpose", "Build & ship the dev work", "--invite", "bob"])
    out = capsys.readouterr().out
    assert "created channel 'dev'" in out
    assert "private (invite-only)" in out and "owner alice" in out
    assert "purpose: Build & ship the dev work" in out
    assert "invited bob (invite token DM'd)" in out

    # Hub state agrees: private channel, purpose in channel:meta, alice owner.
    info = httpx.get(f"{live_hub.url}/channels/dev/info",
                     headers=_bearer(alice_key), timeout=5).json()
    assert info["channel"]["private"] is True
    assert info["meta"]["purpose"] == "Build & ship the dev work"
    assert any(m["agent_id"] == "alice" and m["role"] == "owner"
               for m in info["members"])

    # Bob's DM carries a REDEEMABLE invite token (the documented sharing
    # pattern: private membership stays the invitee's own act).
    inbox = httpx.get(f"{live_hub.url}/inbox", headers=_bearer(bob_key),
                      timeout=5).json()
    # The DM channel also carries the hub's system note — take alice's DM.
    [dm] = [e for e in inbox
            if e["channel"].startswith("dm:") and e["sender"] == "alice"]
    msgs = httpx.get(
        f"{live_hub.url}/channels/{dm['channel']}/messages/{dm['id']}",
        headers=_bearer(bob_key), timeout=5).json()
    token = re.search(r"invite_token='([^']+)'", msgs[-1]["body"]).group(1)
    joined = httpx.post(f"{live_hub.url}/channels/dev/join",
                        json={"invite_token": token},
                        headers=_bearer(bob_key), timeout=5)
    assert joined.status_code == 200 and joined.json()["joined"] is True


def test_create_channel_public_needs_no_token(live_hub, isolated_home, capsys):
    alice_key = _register(live_hub.url, "ann")
    carol_key = _register(live_hub.url, "carol")
    _config.cache_key(live_hub.url, "ann", alice_key)

    _run_cli(["create-channel", "town", "--public", "--as", "ann",
              "--url", live_hub.url, "--invite", "carol"])
    out = capsys.readouterr().out
    assert "public (anyone may join)" in out
    assert "invited carol (public: DM'd a join pointer)" in out

    # Carol joins with no token at all — that is what public means.
    joined = httpx.post(f"{live_hub.url}/channels/town/join", json={},
                        headers=_bearer(carol_key), timeout=5)
    assert joined.status_code == 200 and joined.json()["joined"] is True
    inbox = httpx.get(f"{live_hub.url}/inbox", headers=_bearer(carol_key),
                      timeout=5).json()
    assert any(e["channel"].startswith("dm:") for e in inbox)


# ---------------------------------------------------------------------------
# --home: one flag instead of the AGORA_HOME=... env prefix
# ---------------------------------------------------------------------------


def test_home_flag_registered_on_every_verb():
    """Partial coverage would be its own trap (the --with-hooks lesson), so
    the option must exist on every subcommand — including future ones, which
    the registration loop picks up automatically."""
    parser = build_parser()
    sub = next(a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction))
    for name, sp in sub.choices.items():
        assert any("--home" in a.option_strings for a in sp._actions), name


def test_home_flag_parses_after_the_verb():
    """The natural spelling is `agora chat --as laurent --home ~/.agora-hub2`
    (flag AFTER the verb) — exactly what a subparser option gives."""
    for argv in (["chat", "--as", "laurent"],
                 ["whoami", "--as", "laurent"],
                 ["invite", "someone"],
                 ["join", "--channel", "c", "--as", "laurent"],
                 ["status"],
                 ["listen"],
                 ["post", "--as", "a", "--channel", "c", "hello"],
                 ["dm", "--as", "a", "--to", "b", "hi"],
                 ["inbox", "--as", "a"],
                 ["create-channel", "dev", "--as", "a"],
                 ["up"]):
        args = build_parser().parse_args([argv[0], "--home", "/x/hub2",
                                          *argv[1:]])
        assert args.home == "/x/hub2", argv[0]


def test_apply_home_flag_wins_env_keeps_working(tmp_path, monkeypatch):
    env_home = tmp_path / "env-home"
    flag_home = tmp_path / "flag-home"
    monkeypatch.setenv("AGORA_HOME", str(env_home))

    # Flag given: it wins for this invocation (and for child processes).
    args = build_parser().parse_args(["whoami", "--as", "x",
                                      "--home", str(flag_home)])
    _apply_home(args)
    assert os.environ["AGORA_HOME"] == str(flag_home)
    assert _config.home() == flag_home

    # No flag: the env var works exactly as before.
    monkeypatch.setenv("AGORA_HOME", str(env_home))
    _apply_home(build_parser().parse_args(["whoami", "--as", "x"]))
    assert os.environ["AGORA_HOME"] == str(env_home)
    assert _config.home() == env_home


def test_apply_home_expands_tilde(monkeypatch):
    monkeypatch.delenv("AGORA_HOME", raising=False)
    args = build_parser().parse_args(["status", "--home", "~/agora-hub2"])
    _apply_home(args)
    assert os.environ["AGORA_HOME"] == str(Path.home() / "agora-hub2")
    monkeypatch.delenv("AGORA_HOME", raising=False)


# ---------------------------------------------------------------------------
# join --harness claude|codex calls the harness-CLI registration
# ---------------------------------------------------------------------------


def test_join_vendor_bootstrap_invokes_harness_registration(live_hub,
                                                            isolated_home,
                                                            tmp_path,
                                                            monkeypatch,
                                                            capsys):
    """The read-side fix must fire from the ONE-paste onboarding too: after
    the project files are written, join calls the vendor's own `mcp add`
    (stubbed here) and reports the outcome in the ledger. A registration
    failure must not fail the join — the files + printed remedy remain."""
    import agora.setup_harness as _sh
    from agora.join import run_join

    calls: list[tuple] = []
    monkeypatch.setattr(
        _sh, "register_claude_local",
        lambda ws, mcp, url, agent, about, api_key=None, home=None:
            calls.append(("claude", str(ws), url, agent, api_key, home))
            or (True, "claude stub registered"))
    monkeypatch.setattr(
        _sh, "register_codex_global",
        lambda mcp, url, agent, about, api_key=None, home=None:
            calls.append(("codex", url, agent, api_key, home))
            or (False, "codex CLI not found on PATH — trust the project"))

    for harness, agent in (("claude", "cl-agent"), ("codex", "cx-agent")):
        minted = httpx.post(f"{live_hub.url}/join-tokens",
                            json={"agent_id": agent},
                            headers=_bearer(ADMIN_KEY), timeout=5).json()
        ws = tmp_path / f"ws-{harness}"
        ws.mkdir()
        result = run_join(url=live_hub.url, token=minted["token"], agent_id=None,
                          about="", harness=harness, workspace=str(ws),
                          with_hook=False, listen=False, mcp_command="agora-mcp",
                          pinned_id=agent, vendor_bootstrap=True)
        assert result.code == 0

    out = capsys.readouterr().out
    assert "claude stub registered" in out
    assert "codex CLI not found on PATH" in out          # failure = ledger line,
    assert "run `codex` here and trust the project" in out  # not a crash

    claude_call = next(c for c in calls if c[0] == "claude")
    assert claude_call[2] == live_hub.url and claude_call[3] == "cl-agent"
    assert claude_call[4] is None                         # bearer stays in cache
    assert claude_call[5] == str(isolated_home)          # custom home threaded
    codex_call = next(c for c in calls if c[0] == "codex")
    assert codex_call[2] == "cx-agent"


def test_setup_all_is_explicit_and_skips_vendor_bootstrap(isolated_home,
                                                          tmp_path,
                                                          monkeypatch,
                                                          capsys):
    """The framework-neutral path is local and deterministic: `agora setup X`
    writes all supported workspace footprints, installs hooks by default, and
    does NOT mutate Claude/Codex user-level bootstrap state unless a single
    harness was explicitly selected."""
    import agora.setup_harness as _sh

    calls: list[str] = []
    monkeypatch.setattr(
        _sh, "register_claude_local",
        lambda *args, **kwargs: calls.append("claude") or (True, "claude stub"))
    monkeypatch.setattr(
        _sh, "register_codex_global",
        lambda *args, **kwargs: calls.append("codex") or (True, "codex stub"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    workspace = tmp_path / "seat"
    workspace.mkdir()
    _run_cli(["setup", "janus", "--workspace", str(workspace), "--harness", "all"])
    out = capsys.readouterr().out

    assert calls == []
    assert (workspace / ".cursor" / "mcp.json").exists()
    assert (workspace / ".cursor" / "hooks.json").exists()
    assert (workspace / ".mcp.json").exists()
    assert (workspace / ".claude" / "settings.json").exists()
    assert (workspace / ".codex" / "config.toml").exists()
    assert (workspace / ".codex" / "hooks.json").exists()
    seat = json.loads((workspace / ".agora" / "seat.json").read_text())
    # `all` = every harness agora can wire with its OWN files alone.
    # abstractcode-tui stays out (needs a server-side grant agora cannot make);
    # opencode and pi are in (project config / bridge extension suffice).
    assert seat["harnesses"] == ["cursor", "claude", "codex", "abstractcode",
                                 "opencode", "pi"]
    assert (workspace / ".abstractcode" / "agora.state.config.json").exists()
    assert seat["default_drive_harness"] is None
    assert "cursor, claude, codex" in out
    assert "start agora protocol" in out


def test_setup_prompt_selects_harness_on_empty_workspace(isolated_home,
                                                         tmp_path,
                                                         monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "3")

    workspace = tmp_path / "seat"
    workspace.mkdir()
    _run_cli(["setup", "janus", "--workspace", str(workspace)])

    assert (workspace / ".codex" / "config.toml").exists()
    seat = json.loads((workspace / ".agora" / "seat.json").read_text())
    assert seat["harnesses"] == ["codex"]
    assert seat["default_drive_harness"] == "codex"
    assert not (workspace / ".cursor" / "mcp.json").exists()
    assert not (workspace / ".mcp.json").exists()


def test_setup_single_harness_writes_default_drive_harness(isolated_home,
                                                           tmp_path,
                                                           monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "seat"
    workspace.mkdir()

    _run_cli(["setup", "laurent", "--workspace", str(workspace),
              "--harness", "claude"])

    seat = json.loads((workspace / ".agora" / "seat.json").read_text())
    assert seat["agent_id"] == "laurent"
    assert seat["harnesses"] == ["claude"]
    assert seat["default_drive_harness"] == "claude"


def test_setup_preflights_all_harnesses_before_any_write(isolated_home,
                                                         tmp_path,
                                                         monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "seat"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".claude" / "settings.json").write_text("{ not json")

    with pytest.raises(SystemExit, match=r"\.claude/settings\.json"):
        _run_cli(["setup", "janus", "--workspace", str(workspace),
                  "--harness", "all"])

    assert not (workspace / ".cursor" / "mcp.json").exists()


def test_setup_preflight_rejects_invalid_mcp_servers_shape(isolated_home,
                                                           tmp_path,
                                                           monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "seat"
    workspace.mkdir()
    (workspace / ".mcp.json").write_text(json.dumps({"mcpServers": []}))

    with pytest.raises(SystemExit, match=r"field 'mcpServers' must be a JSON object"):
        _run_cli(["setup", "janus", "--workspace", str(workspace),
                  "--harness", "claude"])


def test_setup_preflight_rejects_invalid_codex_hooks_shape(isolated_home,
                                                           tmp_path,
                                                           monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "seat"
    (workspace / ".codex").mkdir(parents=True)
    (workspace / ".codex" / "hooks.json").write_text("[]")

    with pytest.raises(SystemExit, match=r"\.codex/hooks\.json must contain a JSON object"):
        _run_cli(["setup", "janus", "--workspace", str(workspace),
                  "--harness", "codex"])


def test_setup_rejects_invalid_agent_id_before_writing(isolated_home,
                                                       tmp_path,
                                                       monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "seat"
    workspace.mkdir()

    with pytest.raises(SystemExit, match="invalid agent id 'Alice!'"):
        _run_cli(["setup", "Alice!", "--workspace", str(workspace),
                  "--harness", "cursor"])

    assert not (workspace / ".cursor" / "mcp.json").exists()


def test_setup_harness_config_is_not_secret_and_needs_no_gitignore(
        live_hub, isolated_home, tmp_path, monkeypatch, capsys):
    minted = _register(live_hub.url, "helios")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    os.system(f"git -C {workspace} init -q")

    _run_cli(["setup", "helios", "--workspace", str(workspace),
              "--harness", "cursor", "--url", live_hub.url,
              "--key", minted])
    out = capsys.readouterr().out
    assert "status: READY" in out
    assert "harness config contains no bearer" in out
    env = json.loads((workspace / ".cursor" / "mcp.json").read_text(
    ))["mcpServers"]["agora"]["env"]
    assert env["AGORA_API_KEY"] == ""
    assert env["AGORA_ADMIN_KEY"] == ""


def test_setup_in_a_nested_folder_needs_no_git_and_gets_no_warning(
        live_hub, isolated_home, tmp_path, monkeypatch, capsys):
    """Zero-search ruling (2026-07-31): the workspace is the folder the command
    runs in. agora no longer probes parent folders for `.git` or warns about
    "the enclosing repo" — whether a folder is a git repo is not agora's
    business, and the old warning hard-crashed (KeyError) for any harness
    outside its three-vendor dict."""
    minted = _register(live_hub.url, "janus")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    seat = repo / "seat"
    seat.mkdir(parents=True)
    os.system(f"git -C {repo} init -q")

    _run_cli(["setup", "janus", "--workspace", str(seat),
              "--harness", "cursor", "--url", live_hub.url, "--key", minted])
    out = capsys.readouterr().out
    assert "status: READY" in out
    assert "WARNING" not in out
    assert "git init" not in out


def test_join_vendor_bootstrap_failure_sets_needs_action(live_hub,
                                                         isolated_home,
                                                         tmp_path,
                                                         monkeypatch,
                                                         capsys):
    import agora.setup_harness as _sh
    from agora.join import encode_artifact

    monkeypatch.setattr(
        _sh, "register_codex_global",
        lambda *args, **kwargs: (False, "codex CLI not found on PATH"))
    minted = httpx.post(f"{live_hub.url}/join-tokens",
                        json={"agent_id": "cx-agent"},
                        headers=_bearer(ADMIN_KEY), timeout=5).json()
    workspace = tmp_path / "ws-codex"
    workspace.mkdir()

    with pytest.raises(SystemExit) as exc:
        _run_cli(["join", encode_artifact(live_hub.url, minted["token"], "cx-agent"),
                  "--workspace", str(workspace), "--harness", "codex",
                  "--vendor-bootstrap"])
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "status: NEEDS ACTION" in out
    assert "Codex vendor bootstrap needs action" in out


# ---------------------------------------------------------------------------
# `agora up` port preflight (agora-0096: the 16h-deaf-room squatter class)
# ---------------------------------------------------------------------------


def test_preflight_free_port_proceeds():
    """A free port: preflight returns cleanly so the bind proceeds."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # freed
    # No holder -> None -> _preflight_port is a no-op (no raise).
    assert _port_holder("127.0.0.1", port) is None
    _preflight_port("127.0.0.1", port, f"http://127.0.0.1:{port}")


def test_preflight_existing_hub_exits_zero(live_hub):
    """An agora hub already on the port is a double-launch, not an error:
    preflight names it and exits 0."""
    port = int(live_hub.url.rsplit(":", 1)[1])
    with pytest.raises(SystemExit) as e:
        _preflight_port("127.0.0.1", port, live_hub.url)
    assert e.value.code == 0


def test_preflight_squatter_refuses_loudly(capsys):  # noqa: F811
    """A NON-hub process on the port (the incident: a static file server)
    is refused with a named diagnosis and a nonzero exit — not a silent
    accept, not a raw bind error."""
    import http.server
    import socketserver
    import threading

    # A plain static server = exactly the squatter class from the incident.
    httpd = socketserver.TCPServer(("127.0.0.1", 0),
                                   http.server.SimpleHTTPRequestHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        assert _port_holder("127.0.0.1", port) is not None  # detected
        with pytest.raises(SystemExit) as e:
            _preflight_port("127.0.0.1", port, f"http://127.0.0.1:{port}")
        assert e.value.code == 3
        assert "REFUSING to start" in capsys.readouterr().err
    finally:
        httpd.shutdown()
        t.join(timeout=5)


# ---------------------------------------------------------------------------
# `agora charter` — one verb, two scopes (0146)
# ---------------------------------------------------------------------------


def _seed_seat(live_hub, agent_id: str) -> str:
    """Register a seat AND cache its key in the isolated home, so `--as` works
    exactly as it does on an operator's machine."""
    key = _register(live_hub.url, agent_id)
    _config.cache_key(live_hub.url, agent_id, key)
    return key


def test_charter_verb_manages_the_hub_scope_end_to_end(live_hub, isolated_home,
                                                       tmp_path, capsys):
    """set -> consult -> change -> re-ack, through the CLI an operator types."""
    _config.save_config(url=live_hub.url, admin_key=live_hub.admin, db_path="")
    key = _seed_seat(live_hub, "seat-a")

    _run_cli(["charter", "show", "--url", live_hub.url])
    out = capsys.readouterr().out
    assert "hub charter v0 (packaged default" in out
    assert "## Delegate" in out and "## Operator" in out

    # Nobody has read it; the seat's own read is what changes that.
    _run_cli(["charter", "receipts", "--url", live_hub.url])
    assert "nobody yet" in capsys.readouterr().out
    httpx.get(f"{live_hub.url}/charter", headers=_bearer(key), timeout=5)
    _run_cli(["charter", "receipts", "--url", live_hub.url])
    assert "seat-a" in capsys.readouterr().out

    # Publish v1 -> the seat is stale until it reads again.
    doc = tmp_path / "roles.md"
    doc.write_text("# roles\nmember, owner, delegate, operator.\n")
    _run_cli(["charter", "set", str(doc), "--url", live_hub.url])
    assert "updated to v1" in capsys.readouterr().out
    _run_cli(["charter", "receipts", "--url", live_hub.url])
    assert "STALE" in capsys.readouterr().out
    httpx.get(f"{live_hub.url}/charter", headers=_bearer(key), timeout=5)
    _run_cli(["charter", "receipts", "--url", live_hub.url])
    assert "current" in capsys.readouterr().out

    _run_cli(["charter", "history", "--url", live_hub.url])
    assert "v1" in capsys.readouterr().out
    # v0 stays readable forever — the packaged default is never lost.
    _run_cli(["charter", "show", "--version", "0", "--as", "seat-a",
              "--url", live_hub.url])
    assert "hub charter v0" in capsys.readouterr().out


def test_charter_set_warns_when_the_operator_text_drops_a_seat_kind(
        live_hub, isolated_home, tmp_path, capsys):
    _config.save_config(url=live_hub.url, admin_key=live_hub.admin, db_path="")
    doc = tmp_path / "thin.md"
    doc.write_text("# house\nEveryone is a member; owners own their rooms.\n")
    _run_cli(["charter", "set", str(doc), "--url", live_hub.url])
    out = capsys.readouterr().out
    assert "never mentions delegate" in out and "never mentions operator" in out
    # And `agora status` says it again, on the surface an operator revisits.
    from agora.cli import _print_governance_drift
    _print_governance_drift(live_hub.url, live_hub.admin)
    assert "hub charter v1 (operator-set)" in capsys.readouterr().out


def test_charter_verb_manages_a_channel_scope_end_to_end(live_hub, isolated_home,
                                                         tmp_path, capsys):
    _config.save_config(url=live_hub.url, admin_key=live_hub.admin, db_path="")
    owner_key = _seed_seat(live_hub, "owner-a")
    member_key = _seed_seat(live_hub, "member-b")
    httpx.post(f"{live_hub.url}/channels", json={"name": "design"},
               headers=_bearer(owner_key), timeout=5)
    token = httpx.post(f"{live_hub.url}/channels/design/invites",
                       json={"agent_id": "member-b"},
                       headers=_bearer(owner_key), timeout=5).json()["invite_token"]
    httpx.post(f"{live_hub.url}/channels/design/join", json={"invite_token": token},
               headers=_bearer(member_key), timeout=5)

    # Born with a charter: nothing to create, only to read.
    _run_cli(["charter", "show", "--channel", "design", "--as", "owner-a",
              "--url", live_hub.url])
    out = capsys.readouterr().out
    assert "'design' charter v1" in out and "Owner: owner-a" in out

    # The member has not read it yet.
    _run_cli(["charter", "receipts", "--channel", "design", "--as", "owner-a",
              "--url", live_hub.url])
    assert "member-b" in capsys.readouterr().out

    doc = tmp_path / "design.md"
    doc.write_text("# design — charter\nReviews name files and lines.\n")
    _run_cli(["charter", "set", str(doc), "--channel", "design", "--as", "owner-a",
              "--url", live_hub.url])
    assert "updated to v2" in capsys.readouterr().out
    _run_cli(["charter", "receipts", "--channel", "design", "--as", "owner-a",
              "--url", live_hub.url])
    out = capsys.readouterr().out
    assert "member-b         member STALE" in out
    assert "owner-a          owner  read" in out

    httpx.get(f"{live_hub.url}/channels/design/charter",
              headers=_bearer(member_key), timeout=5)
    _run_cli(["charter", "receipts", "--channel", "design", "--as", "owner-a",
              "--url", live_hub.url])
    assert "STALE" not in capsys.readouterr().out

    _run_cli(["charter", "history", "--channel", "design", "--as", "owner-a",
              "--url", live_hub.url])
    hist = capsys.readouterr().out
    assert "v1" in hist and "v2" in hist


def test_charter_verb_refuses_ambiguous_invocations(live_hub, isolated_home):
    """Both refusals name the fix rather than failing inside key resolution."""
    _config.save_config(url=live_hub.url, admin_key=live_hub.admin, db_path="")
    with pytest.raises(SystemExit) as e:
        _run_cli(["charter", "set", "--url", live_hub.url])
    assert "usage: agora charter set FILE" in str(e.value)
    with pytest.raises(SystemExit) as e:
        _run_cli(["charter", "show", "--channel", "design", "--url", live_hub.url])
    assert "--as <seat>" in str(e.value)


def test_post_to_flag_is_repeatable_and_comma_splittable():
    """fund1 (2026-08-11): `--to a --to b --to c` silently kept only `c` —
    argparse's plain store action last-wins — so a four-seat commission went
    out addressed to one seat. The flag now appends; both the repeated and
    the comma form must survive parsing intact."""
    args = build_parser().parse_args(
        ["post", "--as", "op", "--channel", "commons",
         "--to", "lead", "--to", "systems", "--to", "gameplay,frontend",
         "body"])
    assert args.to == ["lead", "systems", "gameplay,frontend"]
    # And the no---to shape stays falsy (an unaddressed post).
    args = build_parser().parse_args(
        ["post", "--as", "op", "--channel", "commons", "body"])
    assert not args.to
