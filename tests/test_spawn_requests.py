"""The hub's record that a seat is WANTED (laurent, dm#24).

Design: `plan/spawn-a-seat-from-the-chat.md` in the agora-and-wui vfs. This
file covers the persistence layer and the invariants that must hold whichever
way the operator settles the authz fork (§6) — the state machine, the folder
refusal, single-claim, mission-on-the-token — plus the one non-negotiable that
outranks all of them: the hub package never starts a process.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from agora.db import Database, SpawnTransitionRefused
from agora.models import (
    SPAWN_TERMINAL,
    SPAWN_TRANSITIONS,
    SpawnFolderRefused,
    SpawnState,
    validate_spawn_folder,
)


def _make(db: Database, **over):
    kw = dict(machine="local", seat_id="scribe", mission="write the minutes",
              harness="claude", folder="", channels=["commons"], options={},
              requested_by="laurent")
    kw.update(over)
    return db.create_spawn_request(**kw)


@pytest.fixture()
def db(tmp_path: pathlib.Path) -> Database:
    return Database(str(tmp_path / "hub.db"))


# -- the row ------------------------------------------------------------------

def test_a_request_is_recorded_pending_and_nothing_else_happens(db: Database):
    req = _make(db)
    assert req.state is SpawnState.pending
    assert req.claimed_by == "" and req.claimed_at is None
    assert req.machine == "local"          # required, defaulted, present from v1
    assert req.mission == "write the minutes"
    assert db.get_spawn_request(req.id) == req


def test_list_is_newest_first_and_can_drop_the_terminal_rows(db: Database):
    old = _make(db, seat_id="one")
    new = _make(db, seat_id="two")
    assert [r.id for r in db.list_spawn_requests()] == [new.id, old.id]

    db.set_spawn_state(new.id, SpawnState.rejected, detail="no such harness")
    active = db.list_spawn_requests(active_only=True)
    assert [r.id for r in active] == [old.id]


def test_list_filters_by_machine_because_that_is_how_a_runner_polls(db: Database):
    here = _make(db, machine="local")
    _make(db, machine="build-box")
    assert [r.id for r in db.list_spawn_requests(machine="local")] == [here.id]


# -- the state machine --------------------------------------------------------

def test_the_happy_path_walks_pending_to_running_to_stopped(db: Database):
    req = _make(db)
    claimed = db.claim_spawn_request(machine="local", runner_id="runner-mbp")
    assert claimed.id == req.id
    assert claimed.state is SpawnState.claimed
    assert claimed.claimed_by == "runner-mbp" and claimed.claimed_at is not None

    running = db.set_spawn_state(req.id, SpawnState.running, detail="pid 4123",
                                 by="runner-mbp")
    assert running.state is SpawnState.running
    stopped = db.set_spawn_state(req.id, SpawnState.stopped, by="runner-mbp")
    assert stopped.state is SpawnState.stopped


def test_awaiting_approval_is_reachable_and_is_not_running(db: Database):
    """agora-wui's finding (#179): a request sitting at `claimed` while a human
    has not typed `y` renders as progress that is not happening. It needs its
    own word, and the word carries the machine — the operator's next act is to
    go and find that terminal."""
    req = _make(db, machine="build-box")
    db.claim_spawn_request(machine="build-box", runner_id="runner-bb")
    waiting = db.set_spawn_state(req.id, SpawnState.awaiting_approval,
                                 detail="awaiting approval on build-box",
                                 by="runner-bb")
    assert waiting.state is SpawnState.awaiting_approval
    assert "build-box" in waiting.detail
    assert db.set_spawn_state(req.id, SpawnState.running,
                              by="runner-bb").state is SpawnState.running


def test_a_row_can_never_go_backwards(db: Database):
    """The one that matters: a `running` row returning to `pending` would be
    claimable again, which is one request with two live seats."""
    req = _make(db)
    db.claim_spawn_request(machine="local", runner_id="r")
    db.set_spawn_state(req.id, SpawnState.running, by="r")
    with pytest.raises(SpawnTransitionRefused) as exc:
        db.set_spawn_state(req.id, SpawnState.pending, by="r")
    assert exc.value.status_code == 409
    assert db.get_spawn_request(req.id).state is SpawnState.running


@pytest.mark.parametrize("terminal", sorted(s.value for s in SPAWN_TERMINAL))
def test_every_terminal_state_is_actually_terminal(db: Database, terminal: str):
    req = _make(db)
    if terminal == "stopped":
        db.claim_spawn_request(machine="local", runner_id="r")
        db.set_spawn_state(req.id, SpawnState.running, by="r")
    db.set_spawn_state(req.id, SpawnState(terminal))
    for target in SpawnState:
        if target.value == terminal:
            continue
        with pytest.raises(SpawnTransitionRefused) as exc:
            db.set_spawn_state(req.id, target)
        assert exc.value.status_code == 409
        assert "terminal" in exc.value.detail


def test_every_non_terminal_state_can_always_fail(db: Database):
    """A runner that dies mid-boot must always be able to say so. If this ever
    goes red, some state has become a place a request can get stuck."""
    for state, allowed in SPAWN_TRANSITIONS.items():
        if state in SPAWN_TERMINAL:
            continue
        assert SpawnState.failed in allowed, f"{state.value} cannot fail"


def test_a_second_runner_gets_nothing_rather_than_the_same_row(db: Database):
    _make(db)
    first = db.claim_spawn_request(machine="local", runner_id="runner-a")
    second = db.claim_spawn_request(machine="local", runner_id="runner-b")
    assert first is not None
    assert second is None


def test_a_runner_cannot_move_another_runners_row(db: Database):
    req = _make(db)
    db.claim_spawn_request(machine="local", runner_id="runner-a")
    with pytest.raises(SpawnTransitionRefused) as exc:
        db.set_spawn_state(req.id, SpawnState.running, by="runner-b")
    assert exc.value.status_code == 403


def test_a_runner_polling_another_machine_sees_nothing(db: Database):
    _make(db, machine="build-box")
    assert db.claim_spawn_request(machine="local", runner_id="r") is None


def test_the_detail_is_kept_verbatim_so_a_refusal_names_itself(db: Database):
    req = _make(db, harness="emacs")
    sentence = "harness 'emacs' is not installed on build-box"
    row = db.set_spawn_state(req.id, SpawnState.rejected, detail=sentence)
    assert row.detail == sentence


# -- stop is a request, not a state -------------------------------------------

def test_stop_records_intent_and_leaves_the_state_to_the_runner(db: Database):
    req = _make(db)
    db.claim_spawn_request(machine="local", runner_id="r")
    db.set_spawn_state(req.id, SpawnState.running, by="r")

    asked = db.request_spawn_stop(req.id)
    assert asked.stop_requested_at is not None
    # Still running: only the runner can end a process, and a hub that flipped
    # this to `stopped` would be reporting something it cannot observe.
    assert asked.state is SpawnState.running

    again = db.request_spawn_stop(req.id)          # idempotent
    assert again.stop_requested_at == asked.stop_requested_at

    assert db.set_spawn_state(req.id, SpawnState.stopped,
                              by="r").state is SpawnState.stopped


def test_stopping_an_already_terminal_row_is_refused(db: Database):
    req = _make(db)
    db.set_spawn_state(req.id, SpawnState.rejected, detail="no")
    with pytest.raises(SpawnTransitionRefused) as exc:
        db.request_spawn_stop(req.id)
    assert exc.value.status_code == 409


def test_unknown_ids_are_404_not_a_crash(db: Database):
    assert db.get_spawn_request("nope") is None
    for call in (lambda: db.set_spawn_state("nope", SpawnState.running),
                 lambda: db.request_spawn_stop("nope")):
        with pytest.raises(SpawnTransitionRefused) as exc:
            call()
        assert exc.value.status_code == 404


# -- the folder is a hint, never a path the hub can name ----------------------

@pytest.mark.parametrize("hint", ["projects/minutes", "minutes", "", "  a/b  ",
                                  "/leading-slash-is-stripped-after-check"])
def test_relative_hints_pass(hint: str):
    if hint.startswith("/"):
        pytest.skip("covered by the refusal case")
    validate_spawn_folder(hint)


@pytest.mark.parametrize("hint", [
    "/etc/cron.d",            # absolute
    "~/.ssh",                 # home
    "../../etc",              # escape
    "ok/../../etc",           # escape mid-path
    "C:\\Windows",            # drive-absolute
    "a" * 300,                # cap
])
def test_a_folder_that_names_someone_elses_disk_is_refused(hint: str):
    with pytest.raises(SpawnFolderRefused):
        validate_spawn_folder(hint)


def test_the_refusal_says_what_to_do_about_it():
    with pytest.raises(SpawnFolderRefused) as exc:
        validate_spawn_folder("/srv/agents")
    assert "relative" in str(exc.value)


# -- mission rides the join token ---------------------------------------------

def test_a_seat_minted_from_a_token_arrives_with_its_mission(db: Database):
    """architecture.md measures the mission mirror as the difference between
    7/20 and 20/20 compliance after a compaction. A spawned seat whose mission
    lives only in the hub is a seat that forgets what it is for — so the token
    carries it and redemption applies it."""
    db.create_join_token("tok1", "sec", "scribe", about="", channels=[],
                         created_by="laurent", ttl_seconds=600, max_uses=1,
                         mission="write the minutes; never edit them after")
    info, _ = db.redeem_join_token("tok1", "sec", "scribe", name="",
                                   api_key="k1", about="")
    assert info.id == "scribe"
    assert db.get_mission("scribe") == "write the minutes; never edit them after"


def test_the_redeemer_chooses_its_about_and_never_its_mission(db: Database):
    db.create_join_token("tok2", "sec", "scribe", about="token about",
                         channels=[], created_by="laurent", ttl_seconds=600,
                         max_uses=1, mission="the operator's charge")
    db.redeem_join_token("tok2", "sec", "scribe", name="", api_key="k",
                         about="my own words")
    assert db.get_about("scribe") == "my own words"
    assert db.get_mission("scribe") == "the operator's charge"


def test_a_token_with_no_mission_leaves_the_seat_as_before(db: Database):
    db.create_join_token("tok3", "sec", "scribe", about="", channels=[],
                         created_by="laurent", ttl_seconds=600, max_uses=1)
    db.redeem_join_token("tok3", "sec", "scribe", name="", api_key="k", about="")
    assert db.get_mission("scribe") == ""


def test_a_spawned_seat_can_never_be_an_operator(db: Database):
    """Unchanged by this feature and asserted here anyway: the whole design
    rests on the join token being unable to mint an operator."""
    db.create_join_token("tok4", "sec", "scribe", about="", channels=[],
                         created_by="laurent", ttl_seconds=600, max_uses=1,
                         mission="m")
    info, _ = db.redeem_join_token("tok4", "sec", "scribe", name="",
                                   api_key="k", about="")
    assert info.operator is False


# -- non-negotiable #1 --------------------------------------------------------

#: Modules whose whole purpose is starting a process.
_PROCESS_MODULES = frozenset({"subprocess", "multiprocessing", "pty"})
#: `os` attributes that start one.
_PROCESS_OS_CALLS = ("system", "popen", "fork", "forkpty", "posix_spawn",
                     "posix_spawnp", "execv", "execve", "execl", "execlp",
                     "execvp", "execvpe", "spawnv", "spawnl", "spawnvp")


def _process_starters(path: pathlib.Path) -> list[str]:
    """Every place this file could start a process, by AST — not by grep.

    The first version of this check was a substring scan and it fired on the
    word "subprocess" inside the comment describing the check. A guard that
    cannot tell code from prose teaches people to word around it, so it reads
    the tree instead."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _PROCESS_MODULES:
                    found.append(f"{path.name}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _PROCESS_MODULES:
                found.append(f"{path.name}:{node.lineno} from {node.module}")
        elif isinstance(node, ast.Attribute) and node.attr in _PROCESS_OS_CALLS:
            base = node.value
            if isinstance(base, ast.Name) and base.id == "os":
                found.append(f"{path.name}:{node.lineno} os.{node.attr}")
    return found


def test_the_hub_package_never_starts_a_process():
    """The invariant the whole architecture rests on: `docs/architecture.md`
    line 340 — "The hub never creates turns. Agora never launches, resumes,
    closes, or supervises an agent's session or process." A spawn feature is
    exactly the pressure that erodes it, so the erosion has to go red.
    """
    hub = pathlib.Path(__file__).resolve().parents[1] / "src" / "agora" / "hub"
    offenders = [o for path in sorted(hub.rglob("*.py"))
                 for o in _process_starters(path)]
    assert offenders == [], (
        "the hub package must never start a process — found: "
        + ", ".join(offenders))


def test_that_guard_can_actually_fail(tmp_path: pathlib.Path):
    """The guard's own negative twin: a check whose absent-input case is PASS
    is decoration. Both shapes it must catch, on a file written here."""
    imports = tmp_path / "imports.py"
    imports.write_text("import subprocess\nsubprocess.run(['true'])\n")
    assert _process_starters(imports)

    calls = tmp_path / "calls.py"
    calls.write_text("import os\nos.system('true')\n")
    assert _process_starters(calls)

    prose = tmp_path / "prose.py"
    prose.write_text('"""We never use subprocess or os.system here."""\n')
    assert _process_starters(prose) == []


# -- the wire (runner-side; the operator-side CREATE route awaits the fork) ---

ADMIN_KEY = "test-admin-key"


@pytest.fixture()
def wire():
    from fastapi.testclient import TestClient

    from agora.hub.app import create_app

    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                     rate_per_minute=600.0)
    return TestClient(app)


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


def _register(wire, agent_id: str, operator: bool = False) -> dict:
    r = wire.post("/agents", json={"id": agent_id, "operator": operator},
                  headers=_admin())
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def _request_row(wire, **over) -> str:
    """Create a row through the SERVICE. There is no HTTP create route yet —
    that is the one piece gated on agora-and-wui#178 ask 1 — so a test that
    posted to `/spawns` would be asserting a decision the operator has not
    made."""
    service = wire.app.state.service
    from agora.models import AgentInfo
    kw = dict(seat_id="scribe", mission="write the minutes", harness="claude",
              machine="local", folder="", channels=[], options={})
    kw.update(over)
    row = service.create_spawn_request(
        AgentInfo(id="laurent", operator=True), **kw)
    return row.id


def test_no_runner_registered_means_nothing_can_be_claimed(wire):
    """The refuse-by-default gate, and the honest source of the clients'
    "no runner available on <machine>" empty state."""
    assert wire.get("/machines", headers=_admin()).json() == []
    rogue = _register(wire, "rogue")
    _request_row(wire)
    r = wire.post("/spawns/claim", json={"machine": "local"}, headers=rogue)
    assert r.status_code == 403
    assert "no runner is registered" in r.json()["detail"]


def test_only_the_named_runner_may_claim(wire):
    _register(wire, "runner-mbp")
    rogue = _register(wire, "rogue")
    assert wire.put("/admin/machines/local/runner",
                    json={"agent_id": "runner-mbp"},
                    headers=_admin()).status_code == 200
    _request_row(wire)

    r = wire.post("/spawns/claim", json={"machine": "local"}, headers=rogue)
    assert r.status_code == 403
    assert "runner-mbp" in r.json()["detail"]
    # And the row is untouched — a refused claim consumes nothing.
    service = wire.app.state.service
    assert service.list_spawn_requests()[0].state.value == "pending"


def test_naming_a_runner_requires_the_admin_key(wire):
    _register(wire, "runner-mbp")
    op = _register(wire, "boss", operator=True)
    r = wire.put("/admin/machines/local/runner",
                 json={"agent_id": "runner-mbp"}, headers=op)
    assert r.status_code == 403
    assert "admin key" in r.json()["detail"]


def test_a_runner_must_be_a_real_seat(wire):
    r = wire.put("/admin/machines/local/runner",
                 json={"agent_id": "ghost"}, headers=_admin())
    assert r.status_code == 404


def test_claim_hands_over_one_join_token_and_the_row(wire):
    runner = _register(wire, "runner-mbp")
    wire.put("/admin/machines/local/runner", json={"agent_id": "runner-mbp"},
             headers=_admin())
    spawn_id = _request_row(wire)

    body = wire.post("/spawns/claim", json={"machine": "local"},
                     headers=runner).json()
    assert body["request"]["id"] == spawn_id
    assert body["request"]["state"] == "claimed"
    assert body["request"]["claimed_by"] == "runner-mbp"
    assert body["join_token"].startswith("agora-join_")
    assert body["request"]["join_token_id"]

    # The secret is served exactly once and the hub cannot serve it again.
    row = wire.get(f"/spawns/{spawn_id}", headers=_admin()).json()
    assert "join_token" not in row
    assert body["join_token"] not in str(row)

    # Nothing left to claim.
    assert wire.post("/spawns/claim", json={"machine": "local"},
                     headers=runner).json() == {"request": None}


def test_the_token_from_a_claim_mints_the_wanted_seat_with_its_mission(wire):
    runner = _register(wire, "runner-mbp")
    wire.put("/admin/machines/local/runner", json={"agent_id": "runner-mbp"},
             headers=_admin())
    _request_row(wire, seat_id="scribe", mission="write the minutes")
    token = wire.post("/spawns/claim", json={"machine": "local"},
                      headers=runner).json()["join_token"]

    joined = wire.post("/join", json={"token": token, "agent_id": "scribe"})
    assert joined.status_code == 200, joined.text
    assert joined.json()["agent"]["operator"] is False
    seat = {"Authorization": f"Bearer {joined.json()['api_key']}"}
    assert wire.get("/whoami", headers=seat).json()["mission"] == \
        "write the minutes"

    # Single use: the same token cannot mint a second seat.
    assert wire.post("/join", json={"token": token,
                                    "agent_id": "impostor"}).status_code == 403


def test_the_runner_reports_states_and_cannot_skip_backwards(wire):
    runner = _register(wire, "runner-mbp")
    wire.put("/admin/machines/local/runner", json={"agent_id": "runner-mbp"},
             headers=_admin())
    spawn_id = _request_row(wire)
    wire.post("/spawns/claim", json={"machine": "local"}, headers=runner)

    waiting = wire.post(f"/spawns/{spawn_id}/state",
                        json={"state": "awaiting_approval",
                              "detail": "awaiting approval on local"},
                        headers=runner)
    assert waiting.json()["state"] == "awaiting_approval"
    assert waiting.json()["detail"] == "awaiting approval on local"

    assert wire.post(f"/spawns/{spawn_id}/state", json={"state": "running"},
                     headers=runner).json()["state"] == "running"
    back = wire.post(f"/spawns/{spawn_id}/state", json={"state": "pending"},
                     headers=runner)
    assert back.status_code == 409


def test_an_unknown_state_word_is_refused_by_name(wire):
    runner = _register(wire, "runner-mbp")
    wire.put("/admin/machines/local/runner", json={"agent_id": "runner-mbp"},
             headers=_admin())
    spawn_id = _request_row(wire)
    wire.post("/spawns/claim", json={"machine": "local"}, headers=runner)
    r = wire.post(f"/spawns/{spawn_id}/state", json={"state": "booting"},
                  headers=runner)
    assert r.status_code == 400
    assert "booting" in r.json()["detail"]


def test_stop_is_the_operators_act_and_leaves_the_state_alone(wire):
    runner = _register(wire, "runner-mbp")
    member = _register(wire, "nosy")
    wire.put("/admin/machines/local/runner", json={"agent_id": "runner-mbp"},
             headers=_admin())
    spawn_id = _request_row(wire)
    wire.post("/spawns/claim", json={"machine": "local"}, headers=runner)
    wire.post(f"/spawns/{spawn_id}/state", json={"state": "running"},
              headers=runner)

    assert wire.post(f"/spawns/{spawn_id}/stop",
                     headers=member).status_code == 403
    stopped = wire.post(f"/spawns/{spawn_id}/stop", headers=_admin()).json()
    assert stopped["stop_requested_at"] is not None
    assert stopped["state"] == "running"   # only the runner ends a process

    assert wire.post(f"/spawns/{spawn_id}/state", json={"state": "stopped"},
                     headers=runner).json()["state"] == "stopped"


def test_spawn_rows_are_not_a_member_view(wire):
    member = _register(wire, "nosy")
    _request_row(wire)
    assert wire.get("/spawns", headers=member).status_code == 403


def test_a_taken_seat_id_is_refused_at_request_time(wire):
    from agora.hub.service import HubError
    from agora.models import AgentInfo

    _register(wire, "scribe")
    service = wire.app.state.service
    with pytest.raises(HubError) as exc:
        service.create_spawn_request(AgentInfo(id="laurent", operator=True),
                                     seat_id="scribe", harness="claude")
    assert exc.value.status_code == 409


def test_a_request_without_a_harness_is_refused(wire):
    from agora.hub.service import HubError
    from agora.models import AgentInfo

    service = wire.app.state.service
    with pytest.raises(HubError) as exc:
        service.create_spawn_request(AgentInfo(id="laurent", operator=True),
                                     seat_id="scribe", harness="  ")
    assert exc.value.status_code == 400


def test_an_absolute_folder_is_refused_at_the_service_door(wire):
    from agora.hub.service import HubError
    from agora.models import AgentInfo

    service = wire.app.state.service
    with pytest.raises(HubError) as exc:
        service.create_spawn_request(AgentInfo(id="laurent", operator=True),
                                     seat_id="scribe", harness="claude",
                                     folder="/etc")
    assert exc.value.status_code == 400
