"""Is a runner still up?

Until 2026-08-24 nothing on this hub could answer that. `list_machines` served
`announced_at`, a stamp written once at runner startup, so a runner that
announced and then died presented IDENTICALLY to one alive and idle — same
timestamp, same harness list. A spawn request then sat at `pending` with no
surface anywhere able to say whether anything was listening (delegate,
agora-and-wui#558, from laurent's spawn that never ran).

The evidence already existed and was thrown away: the runner's claim poll is an
authenticated call every `poll_seconds` whether or not there is work. These
tests pin that it is kept, and that keeping it does not blur the two facts the
clients already render differently.
"""
from __future__ import annotations

import time

from agora.db import Database
from agora.hub.service import HubService


def _hub() -> tuple[HubService, object, object]:
    service = HubService(Database(":memory:"), rate_per_minute=600)
    operator, _ = service.register_agent("alice", "Alice", operator=True)
    runner, _ = service.register_agent("runner-bb", "Runner")
    service.set_machine_runner("build-box", runner.id)
    return service, operator, runner


def _machine(service: HubService, name: str = "build-box") -> dict:
    return next(r for r in service.list_machines() if r["machine"] == name)


def test_an_idle_claim_poll_is_the_heartbeat():
    """The poll that finds NOTHING is the one that proves life.

    RED CASE: delete the `touch_machine_runner` call from
    `claim_spawn_request` and this fails — `last_seen_at` stays pinned at the
    announce stamp however long the runner keeps polling.
    """
    service, _operator, runner = _hub()
    service.announce_harnesses(runner, "build-box", ["claude"])
    announced = _machine(service)["last_seen_at"]
    assert announced is not None

    time.sleep(0.01)
    assert service.claim_spawn_request(runner, "build-box") is None  # idle poll

    row = _machine(service)
    assert row["last_seen_at"] > announced
    # The startup stamp must NOT move with it, or the two facts collapse.
    assert row["announced_at"] == announced


def test_a_silent_runner_is_distinguishable_from_an_idle_one():
    """The whole point: two runners, same announce, different liveness."""
    service, _operator, runner = _hub()
    service.announce_harnesses(runner, "build-box", ["claude"])
    dead = _machine(service)

    time.sleep(0.01)
    service.claim_spawn_request(runner, "build-box")
    alive = _machine(service)

    assert dead["announced_at"] == alive["announced_at"]
    assert dead["harnesses"] == alive["harnesses"]
    assert alive["last_seen_at"] > dead["last_seen_at"]


def test_a_poll_never_manufactures_an_announcement():
    """`announced_at: null` means no runner ever announced — a DIFFERENT fact
    from "announced with no harness installed", and the clients render them
    differently. A heartbeat that minted a row would silently convert one into
    the other.

    RED CASE: make `touch_machine_runner` create a row when none exists and
    this fails on the `announced_at is None` assertion.
    """
    service, _operator, runner = _hub()          # registered, never announced
    assert service.claim_spawn_request(runner, "build-box") is None

    row = _machine(service)
    assert row["announced_at"] is None
    assert row["harnesses"] == []
    assert row["last_seen_at"] is None


def test_a_machine_with_no_runner_row_still_reports_both_fields():
    """An absent meta row must answer both questions, not raise or omit."""
    service, _operator, _runner = _hub()
    row = _machine(service)
    assert row["announced_at"] is None
    assert row["last_seen_at"] is None
    assert row["stale_after_seconds"] is None


def test_the_cutoff_is_the_runners_own_interval_not_a_hub_constant():
    """`last_seen_at` alone forces every client to invent a staleness cutoff,
    and two clients inventing different ones disagree about one machine in
    front of one operator (agora-wui, agora-and-wui#571). Only the runner
    knows its poll interval — it is a local flag — so it announces it and the
    hub serves the cutoff.

    RED CASE: hard-code the multiple against a constant instead of the
    announced interval and the 60s runner below still reports 60, not 720.
    """
    service, _operator, runner = _hub()
    service.announce_harnesses(runner, "build-box", ["claude"],
                               poll_seconds=5.0)
    assert _machine(service)["stale_after_seconds"] == 60.0

    # A runner that polls every minute must not be called dead after twelve
    # seconds of a faster machine's cadence.
    service.announce_harnesses(runner, "build-box", ["claude"],
                               poll_seconds=60.0)
    assert _machine(service)["stale_after_seconds"] == 720.0


def test_an_older_runner_announces_no_interval_and_the_hub_invents_none():
    """"This machine has not said" is a real answer and a default is not.

    RED CASE: default `poll_seconds` to DEFAULT_POLL_SECONDS when absent and
    this fails — the hub would be serving a cutoff nobody announced, which is
    the invented constant the field exists to remove.
    """
    service, _operator, runner = _hub()
    service.announce_harnesses(runner, "build-box", ["claude"])  # older runner

    row = _machine(service)
    assert row["announced_at"] is not None      # it DID announce
    assert row["stale_after_seconds"] is None   # it did not say how often


def test_a_nonsense_interval_is_refused_at_the_door():
    """A cutoff is rendered as fact, so a runner cannot announce 0 or -1 and
    have every client call a live machine permanently silent."""
    from agora.hub.service import HubError

    service, _operator, runner = _hub()
    for bad in (0, -5.0, 10**6):
        try:
            service.announce_harnesses(runner, "build-box", ["claude"],
                                       poll_seconds=bad)
        except HubError as e:
            assert e.status_code == 400
        else:                                    # pragma: no cover
            raise AssertionError(f"poll_seconds={bad} was accepted")
