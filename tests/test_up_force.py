"""`agora up --force` — take the port over from a VERIFIED hub, never from
a squatter.

What we want: after `agora up --force` in a terminal, the newest installed
hub is the one serving and its logs are in THAT terminal. The kill is
gated on /healthz answering as an agora hub: an unverified process is
never signaled, force or not (the squatter class stays a refusal).
"""

from __future__ import annotations

import signal

import httpx
import pytest

from agora import cli

_URL = "http://127.0.0.1:8765"


class _Resp:
    def __init__(self, body):
        self.status_code = 200
        self._body = body

    def json(self):
        return self._body


def _hub_healthz(*a, **k):
    return _Resp({"ok": True, "protocol": "agora/0.3", "version": "9.9.9"})


def _no_hub(*a, **k):
    raise httpx.ConnectError("nothing answers")


def test_force_kills_verified_hub_and_proceeds(monkeypatch, capsys):
    calls = {"kills": []}

    def holder(host, port):
        # Held until the first signal lands, then free.
        return None if calls["kills"] else (4242, "agora up")

    monkeypatch.setattr(cli, "_port_holder", holder)
    monkeypatch.setattr(httpx, "get", _hub_healthz)
    monkeypatch.setattr(cli.os, "kill",
                        lambda pid, sig: calls["kills"].append((pid, sig)))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    cli._preflight_port("127.0.0.1", 8765, _URL, force=True)   # returns: freed
    assert calls["kills"] == [(4242, signal.SIGTERM)]
    err = capsys.readouterr().err
    assert "taking over port 8765" in err and "starting fresh" in err


def test_force_escalates_to_sigkill_when_term_ignored(monkeypatch):
    calls = {"kills": []}

    def holder(host, port):
        # Survives SIGTERM; frees only after SIGKILL.
        return None if (4242, signal.SIGKILL) in calls["kills"] \
            else (4242, "agora up")

    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(cli, "_port_holder", holder)
    monkeypatch.setattr(httpx, "get", _hub_healthz)
    monkeypatch.setattr(cli.os, "kill",
                        lambda pid, sig: calls["kills"].append((pid, sig)))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.time, "monotonic", monotonic)
    cli._preflight_port("127.0.0.1", 8765, _URL, force=True)
    assert calls["kills"] == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_force_never_kills_a_squatter(monkeypatch, capsys):
    kills = []
    monkeypatch.setattr(cli, "_port_holder",
                        lambda h, p: (777, "python -m http.server"))
    monkeypatch.setattr(httpx, "get", _no_hub)
    monkeypatch.setattr(cli.os, "kill",
                        lambda pid, sig: kills.append((pid, sig)))
    with pytest.raises(SystemExit) as ex:
        cli._preflight_port("127.0.0.1", 8765, _URL, force=True)
    assert ex.value.code == 3
    assert kills == []                          # never signaled
    err = capsys.readouterr().err
    assert "NOT an agora" in err and "UNVERIFIED" in err


def test_no_force_healthy_hub_still_friendly_exit0(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_port_holder", lambda h, p: (4242, "agora up"))
    monkeypatch.setattr(httpx, "get", _hub_healthz)
    with pytest.raises(SystemExit) as ex:
        cli._preflight_port("127.0.0.1", 8765, _URL, force=False)
    assert ex.value.code == 0
    assert "agora up --force" in capsys.readouterr().err   # teaches the takeover


def test_force_with_unidentifiable_pid_refuses(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_port_holder", lambda h, p: (0, ""))
    monkeypatch.setattr(httpx, "get", _hub_healthz)
    with pytest.raises(SystemExit) as ex:
        cli._preflight_port("127.0.0.1", 8765, _URL, force=True)
    assert ex.value.code == 3
    assert "unidentifiable" in capsys.readouterr().err
