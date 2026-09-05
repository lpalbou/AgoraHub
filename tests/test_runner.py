"""`agora runner` — the five local gates and the three states that prove v1.

The design (`plan/spawn-a-seat-from-the-chat.md` §8) names three tests as the
ones that prove this feature, and says the positive one is decoration without
its negative twin:

  1. positive: a claimed request reaches `running` with the seat joined;
  2. the twin: with the harness binary ABSENT the row reaches `rejected`
     naming the harness and the machine — not `running`, and not a
     created-folder-and-silence;
  3. `--require-approval` with nobody answering must sit at
     `awaiting_approval` naming the machine — not `claimed`, not `running`.

No real process is started here. `joiner` and `launcher` are injected, which
is the same seam the runner uses in production — the point of the test is the
DECISION, and a test that shelled out to a harness would be testing the
harness.
"""

from __future__ import annotations

import argparse
import pathlib

import pytest

from agora import runner as R
from agora.runner import GateRefused, RunnerConfig, RunnerState


@pytest.fixture()
def cfg(tmp_path: pathlib.Path) -> RunnerConfig:
    root = tmp_path / "seats"
    root.mkdir()
    return RunnerConfig(root=root, machine="build-box", url="http://hub",
                        agent_id="runner-bb", api_key="k",
                        require_approval=False)


def _row(**over) -> dict:
    row = {"id": "01SPAWN", "seat_id": "scribe", "machine": "build-box",
           "mission": "write the minutes", "harness": "claude", "folder": "",
           "channels": [], "options": {}, "requested_by": "laurent"}
    row.update(over)
    return row


class _FakeProc:
    def __init__(self, pid: int = 4123, rc: int | None = None) -> None:
        self.pid = pid
        self.returncode = rc
        self._rc = rc

    def poll(self):
        return self._rc


def _launcher(*, pid: int = 4123, rc: int | None = None):
    calls: list[dict] = []

    def launch(cfg, seat_id, folder, harness, permissions,
               model="", reasoning=""):
        calls.append({"seat_id": seat_id, "folder": folder,
                      "harness": harness, "permissions": permissions,
                      "model": model, "reasoning": reasoning})
        return R.Launched(seat_id=seat_id, folder=folder, pid=pid,
                          process=_FakeProc(pid, rc))

    launch.calls = calls          # type: ignore[attr-defined]
    return launch


def _joiner():
    calls: list[dict] = []

    def join(cfg, row, token, folder):
        calls.append({"row": row, "token": token, "folder": folder})
        folder.mkdir(parents=True, exist_ok=True)

    join.calls = calls            # type: ignore[attr-defined]
    return join


def _installed(monkeypatch, *names: str) -> None:
    """Pretend exactly these harnesses have their binary on PATH."""
    from agora.drive import _DRIVE_ADAPTERS

    binaries = {getattr(_DRIVE_ADAPTERS[n], "binary", n) for n in names}
    monkeypatch.setattr(R.shutil, "which",
                        lambda b: f"/usr/bin/{b}" if b in binaries else None)


# -- 1. the positive ----------------------------------------------------------

def test_a_claimed_request_reaches_running(cfg, monkeypatch):
    _installed(monkeypatch, "claude")
    state = RunnerState()
    join, launch = _joiner(), _launcher()

    result, detail = R.handle_request(cfg, state, _row(), "agora-join_x.y",
                                      joiner=join, launcher=launch)

    assert result == "running"
    assert "build-box" in detail and "4123" in detail
    # The seat joined with the token before anything was launched.
    assert join.calls[0]["token"] == "agora-join_x.y"
    assert launch.calls[0]["seat_id"] == "scribe"
    # And the folder is the runner's own default, inside its root.
    assert launch.calls[0]["folder"] == cfg.root / "scribe"
    assert state.live["01SPAWN"].pid == 4123


def test_a_folder_hint_is_honoured_inside_the_root(cfg, monkeypatch):
    _installed(monkeypatch, "claude")
    launch = _launcher()
    R.handle_request(cfg, RunnerState(), _row(folder="projects/minutes"),
                     "t", joiner=_joiner(), launcher=launch)
    assert launch.calls[0]["folder"] == cfg.root / "projects" / "minutes"


def test_model_and_reasoning_reach_the_driver(cfg, monkeypatch):
    """The piece without which first-class fields are decoration.

    Before this, `model` could be sent as an `options` key: the hub accepted
    it, stored it, and echoed it back on the row — and the runner read only
    `permissions` out of `options`, so the seat ran at the harness default
    while the row said otherwise. A confirmation that lies is worse than an
    omission that is silent, so the assertion is that the value REACHES THE
    LAUNCHER, not that the field exists.
    """
    _installed(monkeypatch, "claude")
    launch = _launcher()
    R.handle_request(cfg, RunnerState(),
                     _row(model="gpt-5.4", reasoning="high"), "t",
                     joiner=_joiner(), launcher=launch)
    assert launch.calls[0]["model"] == "gpt-5.4"
    assert launch.calls[0]["reasoning"] == "high"


def test_the_knobs_reach_the_real_argv_not_just_the_injected_launcher(
        cfg, monkeypatch):
    """The seam, and it caught me.

    `test_model_and_reasoning_reach_the_driver` exercises the INJECTED
    launcher, so deleting the argv lines in `default_launcher` leaves it
    green — measured, not supposed. The value has to be asserted where it
    becomes a process argument, which is the only place the harness can see
    it. Same absent-input-passes shape as the capabilities announce earlier.
    """
    _installed(monkeypatch, "claude")
    argv_seen: list[list[str]] = []

    class _P:
        pid = 77
        returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(R.subprocess, "Popen",
                        lambda argv, **kw: (argv_seen.append(argv), _P())[1])
    R.handle_request(cfg, RunnerState(),
                     _row(model="gpt-5.4", reasoning="high"), "t",
                     joiner=_joiner())

    argv = argv_seen[0]
    assert argv[argv.index("--model") + 1] == "gpt-5.4"
    assert argv[argv.index("--reasoning-effort") + 1] == "high"


def test_an_absent_knob_is_not_forwarded_as_empty(cfg, monkeypatch):
    """`--model ""` is not the same request as no `--model`: the first asks
    the harness for a model named empty-string, the second lets it resolve its
    own. A row that carries neither must produce an argv that carries
    neither."""
    _installed(monkeypatch, "claude")
    argv_seen: list[list[str]] = []

    class _P:
        pid = 99
        returncode = None

        def poll(self):
            return None

    def fake_popen(argv, **kw):
        argv_seen.append(argv)
        return _P()

    monkeypatch.setattr(R.subprocess, "Popen", fake_popen)
    R.handle_request(cfg, RunnerState(), _row(), "t", joiner=_joiner())

    argv = argv_seen[0]
    assert "--model" not in argv and "--reasoning-effort" not in argv


# -- 2. the negative twin, without which the above is decoration --------------

def test_a_missing_binary_rejects_naming_the_harness_and_the_machine(
        cfg, monkeypatch):
    _installed(monkeypatch)            # nothing installed at all
    state = RunnerState()
    join, launch = _joiner(), _launcher()

    result, detail = R.handle_request(cfg, state, _row(harness="claude"), "t",
                                      joiner=join, launcher=launch)

    assert result == "rejected"
    assert "claude" in detail and "build-box" in detail
    # Not a created-folder-and-silence: nothing was joined, nothing launched,
    # and no directory appeared.
    assert join.calls == [] and launch.calls == []
    assert not (cfg.root / "scribe").exists()
    assert state.live == {}


def test_an_undeclared_harness_says_what_it_knows(cfg, monkeypatch):
    _installed(monkeypatch, "claude")
    result, detail = R.handle_request(cfg, RunnerState(),
                                      _row(harness="emacs"), "t",
                                      joiner=_joiner(), launcher=_launcher())
    assert result == "rejected"
    assert "emacs" in detail and "claude" in detail


def test_the_allowlist_can_only_narrow(cfg, monkeypatch):
    _installed(monkeypatch, "claude", "codex")
    cfg.allow = ("claude",)
    assert R.accepted_harnesses(cfg) == ("claude",)

    result, detail = R.handle_request(cfg, RunnerState(),
                                      _row(harness="codex"), "t",
                                      joiner=_joiner(), launcher=_launcher())
    assert result == "rejected"
    assert "allowlist" in detail and "build-box" in detail


def test_naming_an_uninstalled_harness_does_not_conjure_it(cfg, monkeypatch):
    """An allowlist NARROWS. It is not a way to declare something present."""
    _installed(monkeypatch)
    cfg.allow = ("claude",)
    assert R.accepted_harnesses(cfg) == ()


# -- 3. the state agora-wui found (#179) --------------------------------------

def test_require_approval_reports_awaiting_approval_before_asking(
        cfg, monkeypatch):
    """The row must say it is waiting, and say where — the operator's next act
    is to walk to that terminal. Delete the approval branch and this goes red
    while the other two stay green."""
    _installed(monkeypatch, "claude")
    cfg.require_approval = True
    reported: list[tuple[str, str]] = []
    join, launch = _joiner(), _launcher()

    def never_answers(cfg_, row):
        # Stand-in for a human who has not typed anything: by the time we are
        # here the hub has ALREADY been told, which is the whole point.
        assert reported == [("awaiting_approval", "awaiting approval on build-box")]
        return False

    result, detail = R.handle_request(
        cfg, RunnerState(), _row(), "t", joiner=join, launcher=launch,
        approver=never_answers, report=lambda s, d: reported.append((s, d)))

    assert reported[0][0] == "awaiting_approval"
    assert "build-box" in reported[0][1]
    assert result == "rejected" and "declined" in detail
    assert join.calls == [] and launch.calls == []


def test_approval_granted_runs(cfg, monkeypatch):
    _installed(monkeypatch, "claude")
    cfg.require_approval = True
    result, _ = R.handle_request(cfg, RunnerState(), _row(), "t",
                                 joiner=_joiner(), launcher=_launcher(),
                                 approver=lambda c, r: True,
                                 report=lambda s, d: None)
    assert result == "running"


def test_no_tty_refuses_rather_than_self_approving(cfg, monkeypatch):
    """A runner that quietly approved itself would make --require-approval a
    decoration. With no human to ask, it says so."""
    monkeypatch.setattr(R.sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(GateRefused) as exc:
        R.tty_approve(cfg, _row())
    assert "no tty" in str(exc.value) and "build-box" in str(exc.value)


def test_the_operators_own_request_is_not_re_asked_at_the_shell(cfg, monkeypatch):
    """laurent, commons#290: "when the human operator request the spawn, you
    should never ask again in the shell".

    This gate collects a HUMAN's consent. When the human who asked is the hub
    operator, a second yes at this terminal adds nothing and costs everything:
    `codex-seat-spawn` sat at `awaiting_approval` because nobody was watching
    the shell it was asking in.

    RED CASE: drop the `requested_by_operator` branch and this fails — the
    approver is called and the row reports `awaiting_approval`.
    """
    _installed(monkeypatch, "claude")
    cfg.require_approval = True
    reported: list[tuple[str, str]] = []
    join, launch = _joiner(), _launcher()

    def must_not_be_asked(cfg_, row):        # pragma: no cover - the assertion
        raise AssertionError("the operator was asked to approve twice")

    result, _ = R.handle_request(
        cfg, RunnerState(), _row(requested_by_operator=True), "t",
        joiner=join, launcher=launch, approver=must_not_be_asked,
        report=lambda s, d: reported.append((s, d)))

    assert result == "running"
    assert launch.calls, "the seat must actually start"
    assert not any(s == "awaiting_approval" for s, _ in reported)


def test_a_non_operator_request_is_still_gated(cfg, monkeypatch):
    """The skip is scoped to the operator's OWN request and nothing wider.

    Spawning is operator-only today and will not stay that way; when a
    delegate or a peer can ask, the human at this machine must still get to
    refuse. Absent authority is never read as authority either — an older hub
    sends no flag at all, which lands here.
    """
    _installed(monkeypatch, "claude")
    cfg.require_approval = True
    asked: list[str] = []

    def approver(cfg_, row):
        asked.append(row["seat_id"])
        return False

    for row in (_row(requested_by_operator=False), _row()):   # explicit, absent
        result, detail = R.handle_request(
            cfg, RunnerState(), row, "t", joiner=_joiner(),
            launcher=_launcher(), approver=approver,
            report=lambda s, d: None)
        assert result == "rejected" and "declined" in detail

    assert asked == ["scribe", "scribe"], "both must reach the human"


# -- the other gates ----------------------------------------------------------

def test_a_folder_escaping_the_root_is_refused(cfg):
    for hint in ("../outside", "/etc", "~/.ssh", "a/../../etc"):
        with pytest.raises(GateRefused):
            R.resolve_folder(cfg, hint, "scribe")


def test_a_symlink_out_of_the_root_is_refused(cfg, tmp_path):
    """The check resolves symlinks first: a link INSIDE the root pointing out
    of it is the whole attack, and a string comparison would pass it."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (cfg.root / "sneaky").symlink_to(outside)
    with pytest.raises(GateRefused) as exc:
        R.resolve_folder(cfg, "sneaky/seat", "scribe")
    assert "outside" in str(exc.value)


def test_the_root_itself_is_not_a_seat_folder(cfg):
    with pytest.raises(GateRefused):
        R.resolve_folder(cfg, ".", "scribe")


def test_the_seat_cap_holds(cfg, monkeypatch):
    _installed(monkeypatch, "claude")
    cfg.max_seats = 1
    state = RunnerState()
    R.handle_request(cfg, state, _row(), "t", joiner=_joiner(),
                     launcher=_launcher())
    result, detail = R.handle_request(cfg, state, _row(id="02", seat_id="two"),
                                      "t", joiner=_joiner(),
                                      launcher=_launcher())
    assert result == "rejected"
    assert "cap" in detail and "build-box" in detail


def test_a_request_can_never_ask_for_more_permission_than_the_floor():
    assert R.permission_for({"permissions": "all"}) == "write"
    assert R.permission_for({}) == "write"
    assert R.permission_for({"permissions": "read"}) == "read"   # less is fine


# -- failures are states, not silence -----------------------------------------

def test_a_join_that_fails_reports_failed_and_says_where(cfg, monkeypatch):
    _installed(monkeypatch, "claude")

    def boom(*a, **kw):
        raise RuntimeError("token expired")

    result, detail = R.handle_request(cfg, RunnerState(), _row(), "t",
                                      joiner=boom, launcher=_launcher())
    assert result == "failed"
    assert "build-box" in detail and "token expired" in detail


def test_a_driver_that_will_not_start_is_distinguishable_from_a_bad_join(
        cfg, monkeypatch):
    _installed(monkeypatch, "claude")

    def boom(*a, **kw):
        raise OSError("no such file")

    result, detail = R.handle_request(cfg, RunnerState(), _row(), "t",
                                      joiner=_joiner(), launcher=boom)
    assert result == "failed"
    assert "joined but its driver would not start" in detail


def test_a_dead_driver_is_reaped_rather_than_left_running(cfg, monkeypatch):
    _installed(monkeypatch, "claude")
    state = RunnerState()
    R.handle_request(cfg, state, _row(), "t", joiner=_joiner(),
                     launcher=_launcher(rc=None))
    assert R.reap(state) == []              # still alive

    state.live["01SPAWN"].process._rc = 1   # the driver exited
    state.live["01SPAWN"].process.returncode = 1
    events: list[str] = []
    done = R.reap(state, event_log=events.append)
    assert [d[0] for d in done] == ["01SPAWN"]
    assert "exited with code 1" in done[0][1]
    assert state.live == {}
    assert any("event=agent-decommissioned" in row
               and "seat=scribe" in row and "returncode=1" in row
               for row in events)


def test_explicit_stop_logs_agent_decommission(cfg, monkeypatch):
    state = RunnerState(live={
        "01SPAWN": R.Launched("scribe", cfg.root / "scribe", 4123,
                               _FakeProc(pid=4123, rc=0))
    })
    monkeypatch.setattr(R.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(R.os, "killpg", lambda pid, sig: None)
    events: list[str] = []
    R.stop_seat(state, "01SPAWN", reason="operator-request",
                event_log=events.append)
    assert events == [
        "AGORA_RUNNER event=agent-decommissioned spawn=01SPAWN "
        "seat=scribe pid=4123 reason=operator-request returncode=0"
    ]


# -- the announced set --------------------------------------------------------

def test_the_announced_set_is_what_is_installed_here(cfg, monkeypatch):
    _installed(monkeypatch, "claude", "codex")
    assert set(R.accepted_harnesses(cfg)) == {"claude", "codex"}
    _installed(monkeypatch)
    assert R.accepted_harnesses(cfg) == ()


def test_the_announced_knobs_are_read_off_the_adapters_not_transcribed():
    """The reason this function exists, in one assertion (laurent, #258).

    `agora` hand-typed four adapters' reasoning vocabularies into a message as
    the source for a client dropdown and omitted `pi` and `abstractcode-tui`.
    Anything a human or a model retypes drifts; this reads the adapters. The
    test names the two that were MISSED, so the regression it guards is the
    one that actually happened rather than a hypothetical one.
    """
    from agora.drive import _DRIVE_ADAPTERS

    caps = R.harness_capabilities(("claude", "pi", "abstractcode-tui",
                                   "cursor"))
    for name in ("claude", "pi", "abstractcode-tui", "cursor"):
        assert caps[name]["reasoning"] == list(
            _DRIVE_ADAPTERS[name].REASONING_VOCAB)

    # cursor takes NO reasoning knob: empty, and that is a fact, not a gap.
    assert caps["cursor"]["reasoning"] == []
    # pi accepts the knob and enforces nothing — a client must not imply a
    # guarantee the harness does not make.
    assert caps["pi"]["reasoning_advisory"] is True
    assert caps["claude"]["reasoning_advisory"] is False


def test_no_adapter_may_conjure_a_model_menu(monkeypatch):
    """The menu comes from the HUMAN at this machine and from nowhere else.

    laurent asked for a per-harness model list (`commons#296`). The one source
    that must never appear is a list hardcoded in the package: no adapter
    enumerates models, so a constant here would be exactly the invented
    dropdown `harness_capabilities` was written to kill. An un-configured
    harness therefore carries NO `models` key — absent means *nobody has
    said*, which a client renders as free text."""
    caps = R.harness_capabilities(("claude", "cursor"))
    assert "models" not in caps["claude"]
    assert "models" not in caps["cursor"]

    caps = R.harness_capabilities(("claude", "cursor"),
                                  {"claude": ("m-1", "m-2")})
    assert caps["claude"]["models"] == ["m-1", "m-2"]
    assert "models" not in caps["cursor"], "one harness's menu is not another's"


@pytest.mark.parametrize("spec,expected", [
    ("claude=a,b", {"claude": ("a", "b")}),
    ("claude= a , b ,", {"claude": ("a", "b")}),      # typed by a human
    ("claude=a,a", {"claude": ("a",)}),               # one entry, one option
    ("claude=", {"claude": ()}),                      # constrains nothing
])
def test_a_model_menu_is_parsed_as_typed(spec, expected):
    assert R.parse_model_menus([spec]) == expected


@pytest.mark.parametrize("spec", ["claude", "=a,b", ""])
def test_a_malformed_menu_exits_rather_than_being_skipped(spec):
    """Loud at config time: the operator is at this terminal now, and a
    silently dropped `--models` is discovered as a missing dropdown an hour
    later, on a machine nobody is sitting at."""
    with pytest.raises(SystemExit) as exited:
        R.parse_model_menus([spec])
    assert "HARNESS=ID,ID" in str(exited.value)


def test_a_menu_for_a_harness_this_machine_cannot_run_is_dropped_and_named(
        cfg, monkeypatch, capsys):
    """The hub refuses capabilities for an un-announced harness — correctly,
    since a knob for an unspawnable harness is a dead control. But it refuses
    the WHOLE announce, so one typo here would make the machine report no
    harnesses at all. Drop it before it is sent, and say so by name."""
    _installed(monkeypatch, "claude")
    hub = _FakeHub()
    monkeypatch.setattr(R, "RunnerHub", lambda *a, **k: hub)
    monkeypatch.setattr(R, "config_from_args", lambda _a: cfg)
    cfg.models = {"claude": ("m-1",), "codex": ("m-2",)}
    R.main(argparse.Namespace(once=True))

    _machine, _harnesses, caps = hub.announced
    assert caps["claude"]["models"] == ["m-1"]
    assert "codex" not in caps, "an unannounced harness would 400 the announce"
    logged = capsys.readouterr().out
    assert "--models codex=" in logged and "does not announce codex" in logged


def test_an_unknown_harness_name_cannot_conjure_a_capability_row():
    """No hub-side enum here either: a name with no adapter yields no row,
    rather than an empty row that renders as "this harness has no knobs"."""
    assert R.harness_capabilities(("nonesuch",)) == {}


def test_the_runner_announces_the_knobs_and_not_only_the_names(cfg,
                                                               monkeypatch):
    """The seam. Every hub-side test for `capabilities` passes with a runner
    that never sends them — so without this, the whole feature could go quiet
    and only the clients would find out, which is the absent-input-passes
    defect this repo keeps catching one layer at a time.
    """
    _installed(monkeypatch, "claude", "cursor")
    hub = _FakeHub()
    monkeypatch.setattr(R, "RunnerHub", lambda *a, **k: hub)
    monkeypatch.setattr(R, "config_from_args", lambda _a: cfg)
    R.main(argparse.Namespace(once=True))

    machine, harnesses, caps = hub.announced
    assert machine == cfg.machine
    assert set(harnesses) == {"claude", "cursor"}
    assert caps["claude"]["reasoning"], "claude's vocabulary was not announced"
    assert caps["cursor"]["reasoning"] == []


# -- one turn of the loop -----------------------------------------------------

class _FakeHub:
    def __init__(self, claimed=None):
        self._claimed = claimed
        self.states: list[tuple[str, str, str]] = []
        self.announced = None

    def announce(self, machine, harnesses, capabilities=None,
                 poll_seconds=None):
        self.announced = (machine, tuple(harnesses), capabilities or {})
        self.announced_poll_seconds = poll_seconds
        return {}

    def close(self):
        pass

    def claim(self, machine):
        out, self._claimed = self._claimed, None
        return out

    def set_state(self, spawn_id, state, detail=""):
        self.states.append((spawn_id, state, detail))


def test_run_once_claims_acts_and_reports(cfg, monkeypatch):
    _installed(monkeypatch, "claude")
    request = _row(folder="projects/minutes", channels=["design", "ops"],
                   options={"permissions": "all"}, model="gpt-5.4",
                   reasoning="high", mission="write the minutes\nbe concise")
    hub = _FakeHub({"request": request, "join_token": "agora-join_SECRET"})
    events: list[str] = []
    line = R.run_once(cfg, RunnerState(), hub, joiner=_joiner(),
                      launcher=_launcher(), event_log=events.append)
    assert hub.states[-1][:2] == ("01SPAWN", "running")
    assert "scribe" in line
    request_log = next(row for row in events if "event=spawn-received" in row)
    assert 'harness="claude"' in request_log
    assert 'folder="projects/minutes"' in request_log
    assert 'model="gpt-5.4"' in request_log and 'reasoning="high"' in request_log
    assert 'permissions_requested="all"' in request_log
    assert "permissions_effective=write" in request_log
    assert 'channels=["design","ops"]' in request_log
    assert "mission_chars=28" in request_log and "mission_preview=" in request_log
    launched = next(row for row in events if "event=agent-spawned" in row)
    assert "pid=4123" in launched and "permissions=write" in launched
    assert "agora-join_SECRET" not in "\n".join(events)


def test_run_once_with_nothing_pending_says_so_and_writes_nothing(cfg):
    hub = _FakeHub(None)
    assert R.run_once(cfg, RunnerState(), hub) == "nothing to do"
    assert hub.states == []


# -- the seam every test above stubs (2026-08-23) ------------------------------
#
# `joiner` is injected in all eleven tests above, which is right for testing
# the DECISION — and it meant `default_joiner`, the real one, was never run.
# It passed `mcp_command=""` to `run_join`, which probes that command before
# redeeming the invite, so EVERY spawn died at `mcp-runtime` with `'' is not
# executable on PATH` — a refusal naming no command, on every machine.
#
# 1828 tests were green. The feature had never worked end to end once. Both
# sides of the seam passed their own tests: the CLI's `agora join` resolves
# the command at its own call site, and the runner's gates all pass with a
# fake joiner. Found by running the four steps I had been handing the
# operator, which is the only thing that could have found it.

def test_default_joiner_passes_a_REAL_mcp_command_not_a_placeholder(
        cfg, tmp_path, monkeypatch):
    """The falsification is one character: put `mcp_command=""` back in
    `default_joiner` and this goes red. Nothing else in this file does."""
    from agora.mcp import runtime as _rt

    seen: dict = {}
    monkeypatch.setattr("agora.join.run_join",
                        lambda **kw: seen.update(kw))
    monkeypatch.setattr(_rt, "resolve_mcp_command", lambda: "/opt/bin/agora-mcp")

    R.default_joiner(cfg, _row(), "agora-join_tok", tmp_path / "scribe")

    assert seen["mcp_command"] == "/opt/bin/agora-mcp"
    # ...and the guard that states WHY, so a future refactor that reintroduces
    # an empty default is caught even if the resolver is stubbed differently.
    assert seen["mcp_command"], "run_join probes this; empty fails every spawn"


def test_the_resolver_names_a_command_even_when_it_finds_NOTHING(monkeypatch):
    """The other half of the seam, and the FALLBACK branch specifically.

    `default_joiner` is only as good as what it calls, and the branch that
    matters is the one on a machine where nothing is installed: the resolver
    must still name SOMETHING, because the probe's diagnostic quotes it and
    `'' is not executable on PATH` tells the reader nothing to act on. That
    unreadable refusal is exactly what the placeholder bug produced.

    WRITTEN TWICE. The first version just asserted `resolve_mcp_command()` is
    non-empty, and it passed whether the fallback was `"agora-mcp"` or `""` —
    because this machine HAS `agora-mcp` on PATH, so the fallback never ran.
    A green check over a branch it never reached. Emptying PATH and the two
    sibling lookups is what makes the falsification bite."""
    from agora.mcp import runtime as _rt

    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(_rt.sys, "argv", ["pytest"])
    # Both sibling probes must miss too, or a candidate is found and the
    # fallback is skipped again — the mistake this docstring records.
    monkeypatch.setattr(_rt.Path, "is_file", lambda self: False)

    assert _rt.resolve_mcp_command().strip(), \
        "a probe failure must be able to name the command it could not run"
