from __future__ import annotations

import argparse

from fastapi.testclient import TestClient

from agora import runner as runner_mod
from agora.db import Database
from agora.hub.app import create_app
from agora.hub.service import HubService
from agora.logfmt import emit_log
from agora.runner import RunnerConfig


def test_hub_logs_high_value_lifecycle_events(tmp_path):
    events: list[str] = []
    service = HubService(Database(":memory:"), rate_per_minute=600,
                         event_log=events.append)
    operator, _ = service.register_agent("alice", "Alice", operator=True)
    member, _ = service.register_agent("bob", "Bob")
    service.create_channel(operator, "design", private=False)
    service.join_channel(member, "design", None)
    service.leave_channel(member, "design")
    service.set_agent_role(operator, "bob", True)
    service.archive_channel(operator, "design")

    assert any("event=seat-registered seat=alice role=operator" in row
               for row in events)
    assert any("event=channel-created channel=design owner=alice private=false"
               in row for row in events)
    assert any("event=channel-joined channel=design seat=bob" in row
               for row in events)
    assert any("event=channel-left channel=design seat=bob" in row
               for row in events)
    assert any("event=role-changed seat=bob role=operator by=alice" in row
               for row in events)
    assert any("event=channel-archived channel=design by=alice" in row
               for row in events)


def test_hub_logs_runner_and_spawn_state_changes():
    events: list[str] = []
    service = HubService(Database(":memory:"), rate_per_minute=600,
                         event_log=events.append)
    operator, _ = service.register_agent("alice", "Alice", operator=True)
    runner, _ = service.register_agent("runner-bb", "Runner")
    service.set_machine_runner("build-box", runner.id)
    service.announce_harnesses(runner, "build-box", ["claude"])
    request = service.create_spawn_request(
        operator, seat_id="scribe", mission="write minutes",
        harness="claude", machine="build-box")
    claimed = service.claim_spawn_request(runner, "build-box")
    assert claimed is not None
    service.set_spawn_state(runner, request.id, "running", "pid 123")

    assert any("event=runner-assigned machine=build-box runner=runner-bb"
               in row for row in events)
    assert any("event=runner-announced machine=build-box runner=runner-bb"
               in row for row in events)
    assert any("event=spawn-requested" in row and "seat=scribe" in row
               for row in events)
    assert any("event=spawn-claimed" in row and "seat=scribe" in row
               for row in events)
    assert any("event=spawn-state" in row and "state=running" in row
               for row in events)


def test_join_token_registration_is_not_silent():
    events: list[str] = []
    service = HubService(Database(":memory:"), event_log=events.append)
    token = service.create_join_token(agent_id="remote-seat")
    service.redeem_join_token(token["token"])
    assert any("event=seat-registered seat=remote-seat role=member "
               "source=join-token" in row for row in events)


def test_hub_ready_and_shutdown_use_timestamped_operator_sink(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("agora.logfmt._timestamp", lambda: "now")
    app = create_app(db_path=str(tmp_path / "hub.db"), admin_key="admin",
                     dark_watch_seconds=0, vote_watch_seconds=0,
                     event_log=emit_log)
    with TestClient(app):
        pass
    lines = capsys.readouterr().out.splitlines()
    assert all(line.startswith("[now] | AGORA_HUB ") for line in lines)
    assert any("event=ready" in line for line in lines)
    assert any("event=stopping" in line for line in lines)


class _RunnerHub:
    def announce(self, machine, harnesses, capabilities=None):
        return {}

    def claim(self, machine):
        return None

    def close(self):
        pass


def test_runner_startup_announce_and_shutdown_are_timestamped(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("agora.logfmt._timestamp", lambda: "now")
    root = tmp_path / "seats"
    root.mkdir()
    cfg = RunnerConfig(root=root, machine="build-box", url="http://hub",
                       agent_id="runner-bb", api_key="key",
                       require_approval=False)
    monkeypatch.setattr(runner_mod, "config_from_args", lambda _args: cfg)
    monkeypatch.setattr(runner_mod, "RunnerHub", lambda *_a, **_kw: _RunnerHub())
    monkeypatch.setattr(runner_mod, "accepted_harnesses",
                        lambda _cfg: ("claude", "codex"))
    monkeypatch.setattr(runner_mod, "harness_capabilities", lambda _names: {})

    assert runner_mod.main(argparse.Namespace(once=True)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines and all(line.startswith("[now] | AGORA_RUNNER ")
                         for line in lines)
    assert any("event=starting" in line for line in lines)
    assert any("event=announced status=ok" in line for line in lines)
    assert any("event=shutdown" in line for line in lines)
