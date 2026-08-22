"""The hub's record that a seat is WANTED (laurent, dm#24).

Design: `plan/spawn-a-seat-from-the-chat.md` in the agora-and-wui vfs. This
file covers the persistence layer and the invariants that must hold whichever
way the operator settles the authz fork (§6) — the state machine, the folder
refusal, single-claim, mission-on-the-token — plus the one non-negotiable that
outranks all of them: the hub package never starts a process.
"""

from __future__ import annotations

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

def test_the_hub_package_never_starts_a_process():
    """The invariant the whole architecture rests on: `docs/architecture.md`
    line 340 — "The hub never creates turns. Agora never launches, resumes,
    closes, or supervises an agent's session or process." A spawn feature is
    exactly the pressure that erodes it, so the erosion has to go red.

    Falsified: add `import subprocess` to any file under src/agora/hub/ and
    this test fails.
    """
    hub = pathlib.Path(__file__).resolve().parents[1] / "src" / "agora" / "hub"
    forbidden = ("subprocess", "os.system", "os.exec", "os.spawn", "os.fork",
                 "pty.spawn", "multiprocessing")
    offenders: list[str] = []
    for path in sorted(hub.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert offenders == [], (
        "the hub package must never start a process — found: "
        + ", ".join(offenders))
