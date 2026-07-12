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
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agora import config as _config
from agora.cli import _apply_home, build_parser
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


def test_join_claude_and_codex_invoke_harness_registration(live_hub,
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
        code = run_join(url=live_hub.url, token=minted["token"], agent_id=None,
                        about="", harness=harness, workspace=str(ws),
                        with_hook=False, listen=False, mcp_command="agora-mcp",
                        pinned_id=agent)
        assert code == 0

    out = capsys.readouterr().out
    assert "claude stub registered" in out
    assert "codex CLI not found on PATH" in out          # failure = ledger line,
    assert "run `codex` here and trust the project" in out  # not a crash

    claude_call = next(c for c in calls if c[0] == "claude")
    assert claude_call[2] == live_hub.url and claude_call[3] == "cl-agent"
    assert claude_call[4].startswith("agora_")           # minted key threaded
    assert claude_call[5] == str(isolated_home)          # custom home threaded
    codex_call = next(c for c in calls if c[0] == "codex")
    assert codex_call[2] == "cx-agent"
