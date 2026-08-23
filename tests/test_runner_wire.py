"""`RunnerHub` against a REAL hub — the one seam in the spawn feature that no
test crossed.

THE GAP THIS FILL S (dm#24, 2026-08-23). The feature had two suites and they
passed each other in the dark:

  * `test_spawn_requests.py` drives the hub's routes with its own `TestClient`,
    hand-writing every path and payload;
  * `test_runner.py` drives the runner's decisions with a FAKE hub object —
    `monkeypatch.setattr(R, "RunnerHub", lambda *a, **k: hub)`, the only line
    in the suite that mentions the class.

So `RunnerHub` — the four calls that are the entire conversation between the
two processes — was never executed against anything that could refuse it. Both
sides were green and nothing asserted they spoke the same protocol. That is the
same shape as `default_joiner`'s `mcp_command=""` bug, which shipped for the
same reason and was found by hand, not by the suite: *"it is a seam, and both
sides passed their own tests."*

A wrong path, a renamed payload key, a response field read under the wrong
name, or an authz rule the runner cannot satisfy would all be invisible here
and fatal in the field — and the field is a machine where a human is waiting at
a terminal to type `y`.

What is still injected, deliberately: `joiner` and `launcher`. This file is
about the WIRE. A test that ran the real launcher would start `agora drive` as
a real child, i.e. an LLM harness, from a unit test.
"""

from __future__ import annotations

import pathlib
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from agora import runner as R
from agora.hub.app import create_app
from agora.runner import RunnerConfig, RunnerHub, RunnerState

ADMIN_KEY = "test-admin-key"
MACHINE = "build-box"


@pytest.fixture()
def live_hub(tmp_path: pathlib.Path):
    """A real uvicorn hub on an ephemeral loopback port. `RunnerHub` builds its
    own `httpx.Client` from a url, so there is no transport to inject — the
    only way to exercise it is to give it a real one to talk to."""
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
    yield SimpleNamespace(url=f"http://127.0.0.1:{port}")
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive(), "test hub did not shut down"


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


def _register(url: str, agent_id: str, operator: bool = False) -> str:
    r = httpx.post(f"{url}/agents", json={"id": agent_id, "operator": operator},
                   headers=_admin(), timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


def _wanted(url: str, key: str, **over) -> dict:
    body = dict(seat_id="scribe", mission="write the minutes", harness="claude",
                machine=MACHINE, folder="", channels=[], options={})
    body.update(over)
    r = httpx.post(f"{url}/spawns", json=body,
                   headers={"Authorization": f"Bearer {key}"}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


def _row(url: str, spawn_id: str) -> dict:
    r = httpx.get(f"{url}/spawns/{spawn_id}", headers=_admin(), timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def wired(live_hub, tmp_path: pathlib.Path):
    """An operator, a runner seat named for `build-box`, and the config the
    runner process would have built from its own flags."""
    url = live_hub.url
    operator_key = _register(url, "laurent", operator=True)
    runner_key = _register(url, "runner-bb")
    r = httpx.put(f"{url}/admin/machines/{MACHINE}/runner",
                  json={"agent_id": "runner-bb"}, headers=_admin(), timeout=10)
    assert r.status_code == 200, r.text

    root = tmp_path / "seats"
    root.mkdir()
    cfg = RunnerConfig(root=root, machine=MACHINE, url=url,
                       agent_id="runner-bb", api_key=runner_key,
                       require_approval=False)
    hub = RunnerHub(url, runner_key)
    yield SimpleNamespace(url=url, cfg=cfg, hub=hub, operator_key=operator_key)
    hub.close()


def _launcher(pid: int = 4123, rc: int | None = None):
    class _Proc:
        def __init__(self) -> None:
            self.pid, self.returncode = pid, rc

        def poll(self):
            return rc

    def launch(cfg, seat_id, folder, harness, permissions, model="",
               reasoning=""):
        return R.Launched(seat_id=seat_id, folder=folder, pid=pid,
                          process=_Proc())

    return launch


def _joiner(seen: list):
    def join(cfg, row, token, folder):
        seen.append(token)
        folder.mkdir(parents=True, exist_ok=True)
    return join


def _installed(monkeypatch, *names: str) -> None:
    from agora.drive import _DRIVE_ADAPTERS
    binaries = {getattr(_DRIVE_ADAPTERS[n], "binary", n) for n in names}
    monkeypatch.setattr(R.shutil, "which",
                        lambda b: f"/usr/bin/{b}" if b in binaries else None)


# -- the four calls -------------------------------------------------------------


def test_announce_reaches_the_machines_list_clients_read(wired, monkeypatch):
    """`RunnerHub.announce` -> `GET /machines`. The harness list and its
    capabilities are the source both clients render their dropdowns from, and
    the runner is the only writer. A path or key mismatch here empties every
    dropdown on the fleet while both suites stay green."""
    _installed(monkeypatch, "claude")
    accepted = R.accepted_harnesses(wired.cfg)
    wired.hub.announce(MACHINE, accepted, R.harness_capabilities(accepted))

    machines = httpx.get(f"{wired.url}/machines", headers=_admin(),
                         timeout=10).json()
    mine = [m for m in machines if m["machine"] == MACHINE]
    assert len(mine) == 1, machines
    assert mine[0]["runner"] == "runner-bb"
    assert mine[0]["harnesses"] == list(accepted) == ["claude"]
    assert mine[0]["announced_at"] is not None
    # The capabilities the runner COMPUTED, read back off the wire — not a
    # constant repeated in the test.
    assert mine[0]["capabilities"]["claude"] == \
        R.harness_capabilities(accepted)["claude"]


def test_claim_returns_the_shape_the_runner_unpacks(wired):
    """`RunnerHub.claim` reads `body["request"]` and `body["join_token"]`, and
    treats a falsy `request` as "nothing to do". Both readings are asserted
    against the real response, including the empty case — which is the one the
    runner takes on every idle poll, forever."""
    assert wired.hub.claim(MACHINE) is None          # nothing pending yet

    spawn_id = _wanted(wired.url, wired.operator_key)["id"]
    claimed = wired.hub.claim(MACHINE)
    assert claimed is not None
    assert claimed["request"]["id"] == spawn_id
    assert claimed["request"]["state"] == "claimed"
    assert claimed["join_token"].startswith("agora-join_")
    # And the runner's own row-reads: every key `handle_request` indexes.
    row = claimed["request"]
    for key in ("id", "seat_id", "harness", "folder", "options", "mission"):
        assert key in row, f"handle_request indexes row[{key!r}]"

    assert wired.hub.claim(MACHINE) is None          # single claim


def test_set_state_is_accepted_for_every_state_the_runner_reports(wired):
    """The runner reports `awaiting_approval` through `report`, then one of
    `running`/`rejected`/`failed`, then `stopped` from `reap`. A state word the
    hub refuses would strand a row mid-flight with the seat already running."""
    spawn_id = _wanted(wired.url, wired.operator_key)["id"]
    wired.hub.claim(MACHINE)

    wired.hub.set_state(spawn_id, "awaiting_approval", "awaiting approval")
    assert _row(wired.url, spawn_id)["state"] == "awaiting_approval"
    wired.hub.set_state(spawn_id, "running", "running as pid 1")
    assert _row(wired.url, spawn_id)["state"] == "running"
    wired.hub.set_state(spawn_id, "stopped", "driver exited with code 0")
    assert _row(wired.url, spawn_id)["state"] == "stopped"


def test_list_mine_swallows_the_403_a_runner_is_certain_to_get(wired):
    """`GET /spawns` is the OPERATOR's view and a runner is not an operator, so
    this call 403s BY DESIGN on every real hub. The client returns `[]` rather
    than raising — assert that against the real refusal, because the comment
    saying so is otherwise the only evidence, and an unhandled 403 here kills
    the runner's loop on a machine nobody is watching."""
    _wanted(wired.url, wired.operator_key)
    with httpx.Client(base_url=wired.url, timeout=10) as raw:
        direct = raw.get("/spawns", params={"machine": MACHINE},
                         headers={"Authorization":
                                  f"Bearer {wired.cfg.api_key}"})
    assert direct.status_code == 403, "the premise of this test changed"
    assert wired.hub.list_mine(MACHINE) == []


# -- the whole turn -------------------------------------------------------------


def test_run_once_takes_a_real_row_to_running_and_the_seat_exists(
        wired, monkeypatch):
    """The positive, end to end over the wire: an operator asks, the runner's
    OWN client claims it, the token it was handed mints the wanted seat, and
    the hub — read back, not remembered — says `running` with the runner's own
    sentence.

    This is the assertion `test_a_claimed_request_reaches_running` cannot make:
    there, the hub is a fake that records whatever it is told."""
    _installed(monkeypatch, "claude")
    spawn_id = _wanted(wired.url, wired.operator_key,
                       mission="keep the minutes")["id"]
    tokens: list[str] = []

    line = R.run_once(wired.cfg, RunnerState(), wired.hub,
                      joiner=_joiner(tokens), launcher=_launcher())

    assert "scribe: running" in line
    row = _row(wired.url, spawn_id)
    assert row["state"] == "running"
    assert f"running on {MACHINE} as pid 4123" in row["detail"]

    # The token was real: it mints the wanted seat, with its mission, once.
    joined = httpx.post(f"{wired.url}/join",
                        json={"token": tokens[0], "agent_id": "scribe"},
                        timeout=10)
    assert joined.status_code == 200, joined.text
    assert joined.json()["agent"]["operator"] is False
    seat = {"Authorization": f"Bearer {joined.json()['api_key']}"}
    who = httpx.get(f"{wired.url}/whoami", headers=seat, timeout=10).json()
    assert who["mission"] == "keep the minutes"


def test_a_refused_row_reaches_the_hub_as_rejected_not_as_silence(
        wired, monkeypatch):
    """The negative twin, over the wire. With the binary absent the gate
    refuses locally — and the point is that the REFUSAL travels: the operator
    reads `rejected` plus the runner's own sentence naming the harness and the
    machine, from the hub, without going to find a log on another computer."""
    _installed(monkeypatch)                    # nothing installed
    spawn_id = _wanted(wired.url, wired.operator_key)["id"]

    R.run_once(wired.cfg, RunnerState(), wired.hub,
               joiner=_joiner([]), launcher=_launcher())

    row = _row(wired.url, spawn_id)
    assert row["state"] == "rejected"
    assert "claude" in row["detail"] and MACHINE in row["detail"]


def test_the_real_joiner_redeems_the_token_and_wires_a_workspace(
        wired, tmp_path, monkeypatch):
    """`default_joiner` for real: the last segment of the spawn path that had
    never executed under test.

    Everything above ran with an injected `joiner`, so the runner's onboarding
    call was asserted only by the tests it did not have. That is where
    `mcp_command=""` shipped: `run_join` probes the command before redeeming
    the invite, so every spawn died at `mcp-runtime` with *"'' is not
    executable on PATH"* — a refusal naming no command — while 1828 tests
    stayed green, because the CLI's own `agora join` resolves the command at
    its own call site and nothing exercised THIS one.

    `HOME` and `AGORA_HOME` are redirected into the tmp tree: `run_join` caches
    a key, pins a url and writes harness wiring under `$HOME`, and a test that
    let that reach the real one would edit the machine it runs on.

    The launcher stays injected. The only thing left unexecuted after this test
    is `subprocess.Popen` of `agora drive` itself, which is the LLM harness —
    correctly out of scope for a suite, and covered for its argv by
    `test_the_knobs_reach_the_real_argv_not_just_the_injected_launcher`.
    """
    home = tmp_path / "home"
    (home / ".agora").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGORA_HOME", str(home / ".agora"))
    for var in ("AGORA_URL", "AGORA_ADMIN_KEY", "AGORA_AGENT_ID",
                "AGORA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    _installed(monkeypatch, "claude")

    spawn_id = _wanted(wired.url, wired.operator_key, seat_id="scribe",
                       mission="keep the minutes")["id"]

    R.run_once(wired.cfg, RunnerState(), wired.hub,
               launcher=_launcher())          # joiner NOT injected

    # The row says running, which means the joiner did not raise. Under the
    # empty-mcp_command bug this is `failed` with a refusal naming no command.
    row = _row(wired.url, spawn_id)
    assert row["state"] == "running", row["detail"]

    # The SEAT is real on the hub, minted by the token the runner was handed.
    agents = httpx.get(f"{wired.url}/agents", headers=_admin(),
                       timeout=10).json()
    scribe = [a for a in agents if a["id"] == "scribe"]
    assert scribe, [a["id"] for a in agents]
    assert scribe[0]["operator"] is False

    # And this machine can now act AS that seat: the key was cached under the
    # redirected home, against the same url string used to redeem.
    from agora import config as _cfg
    key = _cfg.get_cached_key(wired.url, "scribe")
    assert key, "run_join redeemed but cached no key"
    who = httpx.get(f"{wired.url}/whoami",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=10).json()
    assert who["id"] == "scribe"
    assert who["mission"] == "keep the minutes"    # it rode the join token

    # The workspace was wired inside the runner's root, and nowhere else.
    folder = wired.cfg.root / "scribe"
    assert folder.is_dir()
    assert any(folder.iterdir()), "joined but wrote no harness footprint"


def test_a_dead_driver_is_reported_stopped_on_the_next_turn(wired, monkeypatch):
    """`reap` -> `set_state(stopped)` over the wire. A row left at `running`
    after its driver died is the hub asserting a seat that is not there, and
    this is the only path that corrects it."""
    _installed(monkeypatch, "claude")
    spawn_id = _wanted(wired.url, wired.operator_key)["id"]
    state = RunnerState()

    R.run_once(wired.cfg, state, wired.hub, joiner=_joiner([]),
               launcher=_launcher(rc=None))
    assert _row(wired.url, spawn_id)["state"] == "running"

    # The child exits between turns.
    state.live[spawn_id].process.returncode = 0
    object.__setattr__(state.live[spawn_id].process, "poll", lambda: 0)
    R.run_once(wired.cfg, state, wired.hub, joiner=_joiner([]),
               launcher=_launcher())

    row = _row(wired.url, spawn_id)
    assert row["state"] == "stopped"
    assert "exited with code 0" in row["detail"]
