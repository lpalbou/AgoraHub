"""Continuation + mode-free driving (2026-07-28, four adversarial reviews).

What we want, as the operator stated it: agents FINISH WHAT THEY START —
continue their tasks until completion, answer messages when they must, and
before taking work up again, re-check the record for messages that
superseded the task. And `cd folder && agora drive` must be the whole
launch gesture: the folder no longer encodes headless-ness; the running
driver is the mode, enforced structurally (one reception owner per seat),
never by rule text alone.

These tests pin the structural halves: the driver-ownership refusal in
listen, the one-driver lock, claim-gated work chains with strike parking,
the held-wake budget fix, and the hub's owner-declared claim-due pings.
"""

from __future__ import annotations

import time

import pytest

import agora.drive as drive_mod
import agora.listen as listen_mod
from agora.drive import (BOOT_PROMPT, WAKE_PROMPT, WORK_BOOT_PROMPT,
                         WORK_PROMPT, WORK_STRIKES, Driver)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_HOME", str(tmp_path))
    return tmp_path


def _driver(home, spawn, **kw):
    return Driver("worker", "http://127.0.0.1:1", spawn=spawn, **kw)


# -- prompts: static, with reception and work kept separate --------------------

def test_prompts_are_static_and_separate_reception_from_work():
    """Reception settles messages; only initiative prompts continue work."""
    for prompt in (WAKE_PROMPT, BOOT_PROMPT, WORK_BOOT_PROMPT, WORK_PROMPT):
        assert "{" not in prompt and "}" not in prompt
    for prompt in (WORK_BOOT_PROMPT, WORK_PROMPT):
        assert "supersede" in prompt
        assert "re-read" in prompt
    assert "Do NOT advance unrelated claims" in WAKE_PROMPT
    assert "routine progress receipts" in BOOT_PROMPT
    assert "ONLY per-slice receipt" in WORK_PROMPT
    assert "addressed structured ask" in WORK_PROMPT
    assert WORK_PROMPT.startswith("AGORA WORK CHUNK")
    assert WAKE_PROMPT.startswith("AGORA WAKE")


# -- one reception owner per seat (listen refuses under a live driver) --------

def test_listen_refuses_while_driver_owns_seat(home, monkeypatch, capsys):
    """A listener armed while a LIVE driver owns the seat is refused
    STATELESSLY: tombstone + guidance, exit 0, and NO listener state
    written (pidfile/offset/owedsig untouched) — the starvation guard."""
    (home / "drive-worker.pid").write_text("99999")
    monkeypatch.setattr(listen_mod, "pid_alive", lambda pid: True)
    rc = listen_mod.run_listen(agent_id="worker", url="http://127.0.0.1:1",
                               source="file", once=True, max_wait=0.01)
    out = capsys.readouterr()
    assert rc == 0
    assert "AGORA_LISTEN ended reason=driver-owns-reception" in out.out
    assert "STOP this listener" in out.err
    assert not (home / "listen-worker.pid").exists()
    assert not (home / "listen-worker.offset").exists()
    assert not (home / "listen-worker.owedsig").exists()


def _listen_swallow_exit(**kw):
    """run_listen, with a possible loud SystemExit (e.g. forced file mode
    without a notify file) mapped to its exit code — these tests only
    assert whether the DRIVER GUARD fired, not the downstream arming."""
    try:
        return listen_mod.run_listen(**kw)
    except SystemExit as ex:
        return ex.code


def test_listen_ignores_dead_or_stale_driver_pid(home, monkeypatch, capsys):
    """A dead driver pid (crash) or an ancient file (reboot pid-reuse)
    never blocks arming — the takeover is silent."""
    pid_file = home / "drive-worker.pid"
    pid_file.write_text("99999")
    monkeypatch.setattr(listen_mod, "pid_alive", lambda pid: False)
    _listen_swallow_exit(agent_id="worker", url="http://127.0.0.1:1",
                         source="file", once=True, max_wait=0.01)
    out = capsys.readouterr()
    # Proceeded PAST the guard (whatever file mode then did about the
    # missing notify file, the refusal tombstone never appeared).
    assert "driver-owns-reception" not in out.out


def test_driver_call_bypasses_own_guard(home, monkeypatch, capsys):
    """The driver's own embedded listen must never refuse itself."""
    (home / "drive-worker.pid").write_text("99999")
    monkeypatch.setattr(listen_mod, "pid_alive", lambda pid: True)
    _listen_swallow_exit(agent_id="worker", url="http://127.0.0.1:1",
                         source="file", once=True, max_wait=0.01,
                         driver_call=True)
    out = capsys.readouterr()
    assert "driver-owns-reception" not in out.out


# -- one driver per seat -------------------------------------------------------

def test_second_driver_refuses_live_holder(home, monkeypatch):
    (home / "drive-worker.pid").write_text("99999")
    monkeypatch.setattr(drive_mod, "pid_alive", lambda pid: True)
    d = _driver(home, lambda p, s: ("s", True))
    with pytest.raises(SystemExit) as ex:
        d._acquire_drive_pid()
    assert "already" in str(ex.value)


def test_dead_driver_pid_taken_over(home, monkeypatch):
    (home / "drive-worker.pid").write_text("99999")
    monkeypatch.setattr(drive_mod, "pid_alive", lambda pid: False)
    d = _driver(home, lambda p, s: ("s", True))
    d._acquire_drive_pid()   # no raise: dead holder taken over
    import os
    assert (home / "drive-worker.pid").read_text() == str(os.getpid())


def test_force_never_takes_over_live_driver(home, monkeypatch):
    """--force must NOT run alongside a LIVE driver (review F1: the old
    holder never re-reads the pidfile, so an overwrite doubles every
    turn). A live holder refuses regardless; force only bypasses the
    interactive-listener guard."""
    (home / "drive-worker.pid").write_text("99999")
    monkeypatch.setattr(drive_mod, "pid_alive", lambda pid: True)
    d = _driver(home, lambda p, s: ("s", True), force=True)
    with pytest.raises(SystemExit) as ex:
        d._acquire_drive_pid()
    assert "cannot take over a LIVE driver" in str(ex.value)


def test_previous_driver_listen_pidfile_not_misdiagnosed(home, monkeypatch):
    """A crashed driver leaves a FRESH listen pidfile holding ITS pid; the
    next driver must not misread it as an interactive tab (review F1)."""
    (home / "listen-worker.pid").write_text("77777")
    monkeypatch.setattr(drive_mod, "pid_alive", lambda pid: True)
    d = _driver(home, lambda p, s: ("s", True))
    d._check_foreign_listener(prev_driver_pid=77777)   # no raise


def test_fresh_interactive_listener_refuses_drive(home, monkeypatch):
    """The dual-surface guard: a FRESH, live listen pidfile (an interactive
    tab's loop) refuses `agora drive` with guidance; --force overrides."""
    (home / "listen-worker.pid").write_text("99999")
    monkeypatch.setattr(drive_mod, "pid_alive", lambda pid: True)
    d = _driver(home, lambda p, s: ("s", True))
    with pytest.raises(SystemExit) as ex:
        d._check_foreign_listener()
    assert "two reception surfaces" in str(ex.value)
    d2 = _driver(home, lambda p, s: ("s", True), force=True)
    d2._check_foreign_listener()   # no raise


def test_stale_interactive_listener_ignored(home, monkeypatch):
    import os
    pidfile = home / "listen-worker.pid"
    pidfile.write_text("99999")
    old = time.time() - 3600.0
    os.utime(pidfile, (old, old))
    monkeypatch.setattr(drive_mod, "pid_alive", lambda pid: True)
    d = _driver(home, lambda p, s: ("s", True))
    d._check_foreign_listener()    # no raise: stale = not a live surface


# -- initiative: claim-gated work chains ---------------------------------------

def _run_loop(d, listen_rcs, monkeypatch):
    """Drive Driver.run with a scripted run_listen; returns spawn calls."""
    rcs = iter(listen_rcs)

    def fake_listen(**kw):
        try:
            return next(rcs)
        except StopIteration:
            raise SystemExit(0)   # end the loop deterministically

    monkeypatch.setattr(drive_mod, "run_listen", fake_listen)
    with pytest.raises(SystemExit):
        d.run(max_turns=None)


def test_initiative_off_idle_never_spawns(home, monkeypatch):
    calls = []
    d = _driver(home, lambda p, s: (calls.append(p) or "s", True))
    monkeypatch.setattr(d, "_claim_snapshot",
                        lambda: ("commons", "claim:x", 1))
    _run_loop(d, [0, 0, 0], monkeypatch)
    assert calls == []            # no initiative flag -> idle spawns nothing


def test_chain_spawns_work_turns_while_claim_progresses(home, monkeypatch):
    """A live claim whose version advances (receipts land) chains work
    chunks at idle boundaries; the chain uses WORK_PROMPT after boot."""
    calls = []
    d = _driver(home, lambda p, s: (calls.append(p) or "s", True),
                initiative=True)
    versions = iter([1, 1, 2, 2, 3, 3, 4, 4])
    monkeypatch.setattr(d, "_claim_snapshot",
                        lambda: ("commons", "claim:x", next(versions, 99)))
    _run_loop(d, [0, 0, 0], monkeypatch)
    assert len(calls) == 3
    assert calls[0] == WORK_BOOT_PROMPT       # fresh work session orients + works
    assert calls[1] == WORK_PROMPT
    assert calls[2] == WORK_PROMPT


def test_receiptless_chunks_park_the_chain(home, monkeypatch):
    """WORK_STRIKES chunks that leave the claim row untouched (version
    frozen) park the chain: further idle boundaries spawn nothing. A
    version bump (any row touch) resumes — strikes key on the version."""
    calls = []
    d = _driver(home, lambda p, s: (calls.append(p) or "s", True),
                initiative=True)
    monkeypatch.setattr(d, "_claim_snapshot",
                        lambda: ("commons", "claim:x", 7))   # frozen version
    _run_loop(d, [0] * (WORK_STRIKES + 3), monkeypatch)
    assert len(calls) == WORK_STRIKES         # parked after the strikes
    calls.clear()
    monkeypatch.setattr(d, "_claim_snapshot",
                        lambda: ("commons", "claim:x", 8))   # row touched
    _run_loop(d, [0], monkeypatch)
    assert len(calls) == 1                    # chain resumed


def test_obligation_preempts_chain(home, monkeypatch):
    """rc=2 between chunks always drives a RECEPTION turn, never a work
    chunk — answering outranks continuing."""
    calls = []
    d = _driver(home, lambda p, s: (calls.append(p) or "s", True),
                initiative=True)
    versions = iter(range(1, 50))
    monkeypatch.setattr(d, "_claim_snapshot",
                        lambda: ("commons", "claim:x", next(versions)))
    _run_loop(d, [0, 2, 0], monkeypatch)
    assert calls[0] == WORK_BOOT_PROMPT       # work chunk (fresh session)
    assert calls[1] == BOOT_PROMPT            # reception has its own session
    assert calls[2] == WORK_PROMPT            # chain resumes after


def test_work_budget_is_a_separate_pool(home, monkeypatch):
    """Work chunks never consume the reception budget: with the work pool
    exhausted, an obligation wake still spawns a reception turn."""
    calls = []
    d = _driver(home, lambda p, s: (calls.append(p) or "s", True),
                initiative=True, work_budget=1)
    versions = iter(range(1, 50))
    monkeypatch.setattr(d, "_claim_snapshot",
                        lambda: ("commons", "claim:x", next(versions)))
    _run_loop(d, [0, 0, 2], monkeypatch)
    # chunk 1 spends the whole work pool; idle 2 parks; the wake still runs.
    assert calls[0] == WORK_BOOT_PROMPT
    assert calls[1] == BOOT_PROMPT
    assert len(calls) == 2


def test_budget_parked_wake_is_held_not_lost(home, monkeypatch):
    """The old 300s park was deaf AND consumed the wake (the listener had
    recorded the owed signature, so the debt waited for escalation). The
    held-wake flag converts it into a turn the moment the budget window
    slides — no deaf sleep, no consumed-wake stall."""
    calls = []
    d = _driver(home, lambda p, s: (calls.append(p) or "s", True),
                turn_budget=1)
    # Exhaust the reception budget with one real turn.
    assert d.run_turn() is True
    assert len(calls) == 1
    # A wake arrives while parked: held, not spawned...
    _run_loop(d, [2, 0], monkeypatch)
    assert len(calls) == 1
    assert d._pending_wake is True
    # ...budget window slides: the next idle boundary runs the held wake.
    d._turn_times.clear()
    _run_loop(d, [0], monkeypatch)
    assert len(calls) == 2


def test_held_wake_blocks_new_work_chunks(home, monkeypatch):
    """While a wake is HELD (budget-parked), idle boundaries must not start
    work chunks: a chunk could pin the seat for up to --work-timeout while
    a human's debt sits at its exact release point. Reception outranks
    work; the moment the window slides, the held wake runs first."""
    calls = []
    d = _driver(home, lambda p, s: (calls.append(p) or "s", True),
                initiative=True, turn_budget=1)
    versions = iter(range(1, 50))
    monkeypatch.setattr(d, "_claim_snapshot",
                        lambda: ("commons", "claim:x", next(versions)))
    assert d.run_turn() is True               # exhaust the reception budget
    _run_loop(d, [2, 0, 0], monkeypatch)      # wake held; two idle boundaries
    assert d._pending_wake is True
    assert all(not p.startswith("AGORA WORK CHUNK") for p in calls[1:])
    d._turn_times.clear()                     # budget window slides
    _run_loop(d, [0], monkeypatch)
    assert d._pending_wake is False           # held wake ran at the boundary
    # The held wake ran BEFORE any new work chunk was allowed to start.
    work_first = next((i for i, p in enumerate(calls)
                       if p.startswith("AGORA WORK CHUNK")), len(calls))
    assert calls.index(WAKE_PROMPT) < work_first


def test_budget_held_wake_rechecks_at_exact_release(home, monkeypatch):
    """A held debt caps the next blocking listen at budget release instead
    of inheriting the ordinary 20-minute idle delay."""
    now = {"value": 4600.0}
    monkeypatch.setattr(drive_mod.time, "time", lambda: now["value"])
    calls = []
    windows = []
    d = _driver(home, lambda p, s: (calls.append(p) or "s", True),
                turn_budget=1, max_wait=1200.0)
    d._turn_times = [1010.0]                    # capacity returns in 10s
    rcs = iter([2, 0])

    def fake_listen(**kw):
        windows.append(kw["max_wait"])
        rc = next(rcs)
        if rc == 0:
            now["value"] = 4610.0              # exact rolling-window edge
        return rc

    monkeypatch.setattr(drive_mod, "run_listen", fake_listen)
    d.run(max_turns=1)
    assert calls == [BOOT_PROMPT]
    assert windows == [1200.0, 10.0]


def test_broadcast_storm_stops_without_work_feedback(home, monkeypatch):
    """Pure room-wide wakes use the small fuse and every spawned prompt is
    reception-only; eight seats running this contract cannot manufacture a
    new claim-progress wake from the reception path."""
    calls = []
    d = _driver(home, lambda p, s: (calls.append(p) or "s", True),
                turn_budget=250, broadcast_turn_budget=2)
    _run_loop(d, [listen_mod._DRIVER_BROADCAST_WAKE] * 3, monkeypatch)
    assert calls == [BOOT_PROMPT, WAKE_PROMPT]
    assert all("status=fyi" not in prompt for prompt in calls)
    assert d._pending_wake is True
    assert d._pending_wake_has_debt is False


def test_addressed_debt_bypasses_exhausted_broadcast_fuse(home, monkeypatch):
    """Human/addressed work is protected up to the high hard ceiling."""
    calls = []
    d = _driver(home, lambda p, s: (calls.append(p) or "s", True),
                turn_budget=250, broadcast_turn_budget=1)
    assert d.run_turn(broadcast=True) is True          # exhaust noise fuse
    _run_loop(d, [2], monkeypatch)                     # addressed/forced wake
    assert calls == [BOOT_PROMPT, WAKE_PROMPT]


def test_pending_wake_cleared_by_normal_wake(home, monkeypatch):
    """Review F2: a held wake must be CLEARED when a normal wake runs a
    turn (that turn drains the whole inbox) — otherwise the stale flag
    spawns a spurious turn at the next idle boundary forever."""
    calls = []
    d = _driver(home, lambda p, s: (calls.append(p) or "s", True),
                turn_budget=1)
    assert d.run_turn() is True                  # spends the whole budget
    _run_loop(d, [2], monkeypatch)               # wake while parked: held
    assert d._pending_wake is True and len(calls) == 1
    d._turn_times.clear()                        # budget window slides
    _run_loop(d, [0], monkeypatch)               # loop-top runs held wake...
    assert d._pending_wake is False              # ...and clears the flag
    assert len(calls) == 2                       # later idle spawned nothing


def test_wake_key_prefers_owed_signature(home):
    """Poison-ledger key: owed signature (debt identity) when present —
    rotation-proof and ws-mode-meaningful; size fallback otherwise."""
    d = _driver(home, lambda p, s: ("s", True))
    (home / "worker-inbox.log").write_text("xyz")
    size_key = d._wake_key()
    assert size_key == "3"
    (home / "listen-worker.owedsig").write_text("id1,id2!3")
    sig_key = d._wake_key()
    assert sig_key != size_key and len(sig_key) == 12


# -- hub: owner-declared claim-due pings ----------------------------------------

@pytest.fixture()
def hub():
    from agora.db import Database
    from agora.hub.service import HubService
    svc = HubService(Database(":memory:"), rate_per_minute=600.0)
    svc.register_agent("alice", "Alice")
    svc.register_agent("bob", "Bob")
    svc.create_channel(_agent("alice"), "room", private=False)
    svc.join_channel(_agent("bob"), "room", invite_token=None)
    return svc


def _agent(agent_id):
    from agora.models import AgentInfo
    return AgentInfo(id=agent_id, name=agent_id)


def _age_row(svc, channel, key, seconds):
    """Backdate a store row's updated_at (tests own the clock)."""
    svc.db._conn.execute(
        "UPDATE store SET updated_at = updated_at - ? "
        "WHERE channel = ? AND key = ?", (seconds, channel, key))
    svc.db._conn.commit()


def _pings(svc, channel="room"):
    """STANDING pings, through the same authoritative-close filter the
    sweep itself uses (a hub 'resolved' reply closes its own episode; the
    original row stays in raw open_obligations by design)."""
    out = []
    for (ch, _owner), msgs in svc._standing_claim_pings().items():
        if ch == channel:
            out.extend(msgs)
    return out


def test_no_cadence_never_pings(hub):
    """The doctrine line: rows WITHOUT owner-declared cadence never ping —
    the hub surfaces debts agents authored, it never authors work."""
    hub.store_set(_agent("alice"), "room", "claim:big-task",
                  {"owner": "alice"})
    _age_row(hub, "room", "claim:big-task", 10 * 86400)
    assert hub._claim_due_sweep() == []
    assert _pings(hub) == []


def test_cadence_idle_posts_one_standing_ping(hub):
    hub.store_set(_agent("alice"), "room", "claim:big-task",
                  {"owner": "alice", "cadence_minutes": 60})
    _age_row(hub, "room", "claim:big-task", 7200)   # 2h idle > 60m cadence
    out = hub._claim_due_sweep()
    assert any(a.startswith("claim-due:room/alice:") for a in out)
    pings = _pings(hub)
    assert len(pings) == 1
    ping = pings[0]
    assert ping.to == ["alice"] and ping.status.value == "open"
    assert "CLAIMS DUE" in ping.body
    assert "supersede" in ping.body                 # the re-read teaching
    # Same due set: the sweep posts nothing new (standing-ping discipline).
    assert hub._claim_due_sweep() == []
    assert len(_pings(hub)) == 1


def test_row_touch_clears_standing_ping(hub):
    hub.store_set(_agent("alice"), "room", "claim:big-task",
                  {"owner": "alice", "cadence_minutes": 60})
    _age_row(hub, "room", "claim:big-task", 7200)
    hub._claim_due_sweep()
    assert len(_pings(hub)) == 1
    # The owner touches the row (the receipt): next sweep closes the ping.
    hub.store_set(_agent("alice"), "room", "claim:big-task",
                  {"owner": "alice", "cadence_minutes": 60,
                   "status": "step 2 done"})
    out = hub._claim_due_sweep()
    assert "claim-due:room/alice:cleared" in out
    assert _pings(hub) == []


def test_done_and_parked_rows_never_ping(hub):
    hub.store_set(_agent("alice"), "room", "claim:a",
                  {"owner": "alice", "cadence_minutes": 60, "done": True})
    hub.store_set(_agent("alice"), "room", "claim:b",
                  {"owner": "alice", "cadence_minutes": 60,
                   "status": "parked"})
    for key in ("claim:a", "claim:b"):
        _age_row(hub, "room", key, 7200)
    assert hub._claim_due_sweep() == []
    assert _pings(hub) == []


def test_cadence_zero_declares_off(hub):
    hub.store_set(_agent("alice"), "room", "claim:c",
                  {"owner": "alice", "cadence_minutes": 0})
    _age_row(hub, "room", "claim:c", 10 * 86400)
    assert hub._claim_due_sweep() == []


def test_cadence_validation_rejects_junk(hub):
    from agora.hub.service import HubError
    with pytest.raises(HubError):
        hub.store_set(_agent("alice"), "room", "claim:d",
                      {"owner": "alice", "cadence_minutes": "soon"})
    with pytest.raises(HubError):
        hub.store_set(_agent("alice"), "room", "claim:d",
                      {"owner": "alice", "cadence_minutes": -5})
    with pytest.raises(HubError):   # bools are not minutes (float(True)=1.0)
        hub.store_set(_agent("alice"), "room", "claim:d",
                      {"owner": "alice", "cadence_minutes": True})


def test_cadence_is_owner_declared_only(hub):
    """Review F7 (doctrine enforcement): a PEER may not set or change the
    cadence on someone else's claim — the hub pings on schedules their
    AUTHOR declared. The owner may; unrelated fields stay peer-writable."""
    from agora.hub.service import HubError
    hub.store_set(_agent("alice"), "room", "claim:f", {"owner": "alice"})
    with pytest.raises(HubError) as ex:
        hub.store_set(_agent("bob"), "room", "claim:f",
                      {"owner": "alice", "cadence_minutes": 60})
    assert "OWNER-declared" in str(ex.value)
    hub.store_set(_agent("alice"), "room", "claim:f",
                  {"owner": "alice", "cadence_minutes": 60})   # owner: fine
    # A peer touching the row WITHOUT changing the cadence stays legal
    # (steward hygiene): same value passes the unchanged-check.
    hub.store_set(_agent("bob"), "room", "claim:f",
                  {"owner": "alice", "cadence_minutes": 60,
                   "note": "steward touch"})


def test_claim_due_sweep_silent_under_hub_pause(hub, monkeypatch):
    """The doctrine paragraph promises pause silences claim pings; the
    sweep must gate on hub_paused (review V3-1)."""
    hub.store_set(_agent("alice"), "room", "claim:g",
                  {"owner": "alice", "cadence_minutes": 60})
    _age_row(hub, "room", "claim:g", 7200)
    monkeypatch.setattr(hub, "hub_paused", lambda: {"state": "paused"})
    assert hub._claim_due_sweep() == []
    assert _pings(hub) == []
    monkeypatch.setattr(hub, "hub_paused", lambda: None)
    assert hub._claim_due_sweep() != []          # unpaused: fires normally


def test_cadence_floor_clamps_spam(hub):
    """cadence_minutes: 1 is clamped to the 30-minute floor: a row idle 5
    minutes never pings even with a 1-minute declared cadence."""
    hub.store_set(_agent("alice"), "room", "claim:e",
                  {"owner": "alice", "cadence_minutes": 1})
    _age_row(hub, "room", "claim:e", 300)
    assert hub._claim_due_sweep() == []


def test_claimless_seat_surfaces_unchanged(hub):
    """The additive guarantee: a seat holding no claim sees nothing new —
    the sweep posts nothing anywhere."""
    assert hub._claim_due_sweep() == []
    assert hub.db.open_obligations(["room"]) == []
