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
    # Deliberately an OLDER protocol than this build speaks: taking a port
    # over asks whether the holder is an agora hub AT ALL, never which
    # version it speaks (agora.is_agora_protocol, not protocol_warning).
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


def test_force_takes_over_a_port_that_changed_hands(monkeypatch, capsys):
    """The 0.14.0 field-test failure: lsof named pid A, but the socket was
    owned by pid B. --force killed A, asked only "is the port free?", and
    refused — falsely blaming A for surviving while B kept serving. The
    takeover must re-resolve the holder and take B over too."""
    kills: list[tuple[int, int]] = []

    def holder(host, port):
        if (2222, signal.SIGTERM) in kills:
            return None                     # B is down: port free
        if (1111, signal.SIGTERM) in kills:
            return (2222, "agora up")       # A gone, B still owns the socket
        return (1111, "agora up")           # the pid lsof happened to list

    monkeypatch.setattr(cli, "_port_holder", holder)
    monkeypatch.setattr(httpx, "get", _hub_healthz)
    monkeypatch.setattr(cli.os, "kill",
                        lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    cli._preflight_port("127.0.0.1", 8765, _URL, force=True)  # returns: freed
    assert kills == [(1111, signal.SIGTERM), (2222, signal.SIGTERM)]
    err = capsys.readouterr().err
    assert "pid 1111" in err and "pid 2222" in err   # both takeovers narrated
    assert "starting fresh" in err
    assert "survived" not in err                     # nobody falsely blamed


def test_force_refuses_when_a_new_holder_is_a_squatter(monkeypatch, capsys):
    """If the process that inherits the port is NOT a hub, the takeover
    stops there — a squatter is never signaled, even mid-force."""
    kills: list[tuple[int, int]] = []
    hub_answers = {"on": True}

    def holder(host, port):
        if kills:
            hub_answers["on"] = False       # what took over is not a hub
            return (777, "python -m http.server")
        return (1111, "agora up")

    def healthz(*a, **k):
        if hub_answers["on"]:
            return _hub_healthz()
        raise httpx.ConnectError("nothing answers")

    monkeypatch.setattr(cli, "_port_holder", holder)
    monkeypatch.setattr(httpx, "get", healthz)
    monkeypatch.setattr(cli.os, "kill",
                        lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit) as ex:
        cli._preflight_port("127.0.0.1", 8765, _URL, force=True)
    assert ex.value.code == 3
    assert kills == [(1111, signal.SIGTERM)]         # 777 never signaled
    err = capsys.readouterr().err
    assert "NOT an agora" in err and "777" in err


def test_force_gives_up_on_a_respawning_supervisor(monkeypatch, capsys):
    """A supervisor handing the port to a fresh hub every round must not
    spin forever: bounded rounds, then a refusal naming the pids killed."""
    kills: list[tuple[int, int]] = []

    def holder(host, port):
        return (3000 + len(kills), "agora up")       # always a NEW hub pid

    monkeypatch.setattr(cli, "_port_holder", holder)
    monkeypatch.setattr(httpx, "get", _hub_healthz)
    monkeypatch.setattr(cli.os, "kill",
                        lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit) as ex:
        cli._preflight_port("127.0.0.1", 8765, _URL, force=True)
    assert ex.value.code == 3
    assert len(kills) == cli._FORCE_MAX_ROUNDS       # bounded, not infinite
    err = capsys.readouterr().err
    assert "kept changing hands" in err and "respawning" in err


def test_force_with_unidentifiable_pid_refuses(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_port_holder", lambda h, p: (0, ""))
    monkeypatch.setattr(httpx, "get", _hub_healthz)
    with pytest.raises(SystemExit) as ex:
        cli._preflight_port("127.0.0.1", 8765, _URL, force=True)
    assert ex.value.code == 3
    assert "unidentifiable" in capsys.readouterr().err
