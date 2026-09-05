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
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

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
    # `app` is yielded too: uvicorn runs in a THREAD of this process, so the
    # service object is reachable for the one thing HTTP cannot express — a
    # spawn requested by a NON-operator. `POST /spawns` is operator-only by
    # design, and the approval gate's negative half is exactly that case.
    yield SimpleNamespace(url=f"http://127.0.0.1:{port}", app=app)
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
    yield SimpleNamespace(url=url, cfg=cfg, hub=hub, operator_key=operator_key,
                          app=live_hub.app)
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


def test_an_announced_model_menu_reaches_the_clients_dropdown(wired, monkeypatch):
    """`--models claude=…` -> `GET /machines`, over a real socket.

    laurent asked for a per-harness model list (`commons#296`) and agora-wui's
    consumer was already written against `models: [...]`. Before this, the hub
    answered such an announce `200` and dropped the key — a runner believing it
    had announced a menu, a client rendering free text, and nothing red."""
    _installed(monkeypatch, "claude")
    accepted = R.accepted_harnesses(wired.cfg)
    menus = R.parse_model_menus(["claude=claude-opus-5,claude-sonnet-5"])
    wired.hub.announce(MACHINE, accepted, R.harness_capabilities(accepted, menus))

    machines = httpx.get(f"{wired.url}/machines", headers=_admin(),
                         timeout=10).json()
    caps = next(m for m in machines if m["machine"] == MACHINE)["capabilities"]
    assert caps["claude"]["models"] == ["claude-opus-5", "claude-sonnet-5"]


def test_the_three_menu_states_stay_three_on_the_wire(wired, monkeypatch):
    """Absent, `[]` and a list mean three different things to an operator:
    *this runner has not said*, *it constrains nothing*, and the menu. Folding
    absent into `[]` would make every un-configured harness claim it had been
    considered."""
    _installed(monkeypatch, "claude")
    accepted = R.accepted_harnesses(wired.cfg)

    def announced(menus):
        wired.hub.announce(MACHINE, accepted,
                           R.harness_capabilities(accepted, menus))
        machines = httpx.get(f"{wired.url}/machines", headers=_admin(),
                             timeout=10).json()
        row = next(m for m in machines if m["machine"] == MACHINE)
        return row["capabilities"]["claude"]

    assert "models" not in announced({})
    assert announced(R.parse_model_menus(["claude="]))["models"] == []
    assert announced({"claude": ("m-1",)})["models"] == ["m-1"]


def test_a_knob_the_hub_does_not_serve_is_refused_by_name(wired, monkeypatch):
    """The defect that made the menu necessary, as a permanent guard.

    `_clean_capabilities` used to rebuild each row from three keys, so ANY
    other knob was accepted with a 200 and silently absent from the echo. The
    runner and hub ship from one package: a refusal naming the key is printed
    by the runner's own startup log, where a drop is printed nowhere."""
    _installed(monkeypatch, "claude")
    accepted = R.accepted_harnesses(wired.cfg)
    caps = R.harness_capabilities(accepted)
    caps["claude"]["tempreature"] = 0.7          # a plausible typo, not nonsense

    with pytest.raises(httpx.HTTPStatusError) as raised:
        wired.hub.announce(MACHINE, accepted, caps)
    detail = raised.value.response.json()["detail"]
    assert "tempreature" in detail and "models" in detail
    assert raised.value.response.status_code == 400


def test_a_model_id_is_refused_rather_than_truncated(wired, monkeypatch):
    """Same rule as `default_model`: a sliced model id is a string that looks
    like a model and is not one, and this one would land in a dropdown as a
    choosable option."""
    _installed(monkeypatch, "claude")
    accepted = R.accepted_harnesses(wired.cfg)
    caps = R.harness_capabilities(accepted, {"claude": ("m" * 400,)})

    with pytest.raises(httpx.HTTPStatusError) as raised:
        wired.hub.announce(MACHINE, accepted, caps)
    assert raised.value.response.status_code == 400
    machines = httpx.get(f"{wired.url}/machines", headers=_admin(),
                         timeout=10).json()
    assert not [m for m in machines if m["machine"] == MACHINE
                and m["harnesses"]], "a refused announce stored nothing"


def test_an_unlisted_model_still_spawns_because_the_menu_is_not_a_gate(wired,
                                                                      monkeypatch):
    """The asymmetry with `reasoning`, asserted rather than only documented.

    The hub refuses a reasoning level the machine did not announce — the
    machine said it cannot express it. The model menu is hand-typed by a human
    at that machine, so enforcing it would refuse working models with a
    hub-authored 'not allowed'. agora-wui renders a dropdown from it; nobody
    may render it as a permission."""
    _installed(monkeypatch, "claude")
    accepted = R.accepted_harnesses(wired.cfg)
    wired.hub.announce(MACHINE, accepted,
                       R.harness_capabilities(accepted, {"claude": ("m-1",)}))

    row = _wanted(wired.url, wired.operator_key, model="not-on-the-menu")
    assert row["model"] == "not-on-the-menu"


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


def test_the_shape_a_console_actually_posts_omits_keys_it_does_not_empty_them(
        wired, monkeypatch):
    """The console's payload, not the suite's — asked for by agora-wui through
    `agora-and-wui#647` ask 1 (their `spawn-end-to-end#5`).

    Every other test in this file goes through `_wanted`, which posts
    `mission=""`, `folder=""`, `channels=[]`, `options={}`. agora-wui's Spawn
    panel OMITS a field the operator left blank and never sends `options` at
    all, so no test here had ever posted the shape laurent's own click
    produces.

    Today the two shapes collapse before anything reads them, because every
    `CreateSpawn` field carries a default — so this is green on arrival and it
    is not a defect report. What it buys is the CONTRACT: absent≡empty is a
    property of those defaults and nothing asserted it. Write
    `folder: str | None = None`, drop a default, or add validation that tells
    absent from empty, and the console breaks on laurent's first spawn while
    this entire suite stays green.

    Both mutants below were RUN, and each reddened this test alone with the
    other 15 green:
      * `CreateSpawn.mission: str` (no default) — the post 422s at line 3 of
        the body below;
      * `CreateSpawn.folder: str | None = None` — `POST /spawns` raises
        `AttributeError: 'NoneType' has no attribute 'strip'` inside
        `models.validate_spawn_folder`. Sharper than the `null` echo I
        predicted: the console's own click 500s on the hub, and every test
        that sends `folder=""` is untouched.
    """
    _installed(monkeypatch, "claude")
    # Exactly the keys a console sends for a seat with no mission typed: no
    # `mission`, no `folder`, no `channels`, no `options`.
    body = {"seat_id": "scribe", "harness": "claude", "machine": MACHINE}
    posted = httpx.post(f"{wired.url}/spawns", json=body, timeout=10,
                        headers={"Authorization":
                                 f"Bearer {wired.operator_key}"})
    assert posted.status_code == 200, posted.text
    spawn_id = posted.json()["id"]

    tokens: list[str] = []
    line = R.run_once(wired.cfg, RunnerState(), wired.hub,
                      joiner=_joiner(tokens), launcher=_launcher())

    # 1. It ran — the omitted keys reached `handle_request`, which indexes
    #    every one of them, without a KeyError or a None where a str is used.
    assert "scribe: running" in line
    row = _row(wired.url, spawn_id)
    assert row["state"] == "running", row

    # 2. The omitted keys read back off the hub as the EMPTY forms the runner
    #    and both clients index, never as null.
    assert row["mission"] == "" and row["folder"] == ""
    assert row["channels"] == [] and row["options"] == {}

    # 3. And the absence travels the whole way: the seat mints, with an empty
    #    mission rather than a missing one.
    joined = httpx.post(f"{wired.url}/join", timeout=10,
                        json={"token": tokens[0], "agent_id": "scribe"})
    assert joined.status_code == 200, joined.text
    who = httpx.get(f"{wired.url}/whoami", timeout=10,
                    headers={"Authorization":
                             f"Bearer {joined.json()['api_key']}"}).json()
    assert who["id"] == "scribe" and who["mission"] == ""


def _approval_spy() -> tuple[list, Any]:
    """An approver that must NEVER be called on the operator's own request.

    It returns False rather than True on purpose: if the gate ever consults it
    the row is REJECTED, so a regression fails loudly on the row state as well
    as on the spy — two independent assertions of the same defect."""
    calls: list = []

    def approve(cfg, row):
        calls.append(row.get("seat_id"))
        return False

    return calls, approve


def test_the_operators_own_request_is_the_approval_end_to_end(wired, monkeypatch):
    """laurent's overnight commission (`dm#83`, via `agora-and-wui#642` ask 1):
    request in -> runner claims -> `requested_by_operator` fires -> the tty
    prompt is SKIPPED -> the seat is on the roster. ONE run, across the process
    boundary, with approval genuinely ON.

    THIS IS THE TEST THE FEATURE DID NOT HAVE, and its absence is the whole
    story: the fix lives in two files in two processes — `service.py` puts
    `requested_by_operator` on the claim, `runner.py:364` reads it — and each
    half had a green test against a double of the other. `codex-seat-spawn` sat
    at `awaiting_approval` in front of a terminal nobody was watching while
    both suites passed.

    Mutant that must redden it: delete the `requested_by_operator` read at
    runner.py:364. Then the spy is called, the row is rejected, and both
    assertions fire.
    """
    _installed(monkeypatch, "claude")
    # Approval ON — the shipped default, and the state the whole gate is about.
    cfg = replace(wired.cfg, require_approval=True)
    calls, approve = _approval_spy()

    spawn_id = _wanted(wired.url, wired.operator_key,
                       mission="keep the minutes")["id"]
    tokens: list[str] = []
    line = R.run_once(cfg, RunnerState(), wired.hub, joiner=_joiner(tokens),
                      launcher=_launcher(), approver=approve)

    # 1. The prompt never happened, with approval on.
    assert calls == [], f"the operator was asked to approve their own spawn: {calls}"
    # 2. The row is running — not awaiting_approval, not rejected — read back
    #    off the hub rather than off the runner's own return value.
    row = _row(wired.url, spawn_id)
    assert row["state"] == "running", row
    assert "scribe: running" in line
    # 3. The hub really did say the requester was an operator. Asserted on the
    #    CLAIM payload, because that field crossing the wire is the seam.
    assert row["requested_by"] == "laurent"
    # 4. The seat exists: the token mints it, with its mission.
    joined = httpx.post(f"{wired.url}/join",
                        json={"token": tokens[0], "agent_id": "scribe"},
                        timeout=10)
    assert joined.status_code == 200, joined.text
    seat = {"Authorization": f"Bearer {joined.json()['api_key']}"}
    who = httpx.get(f"{wired.url}/whoami", headers=seat, timeout=10).json()
    assert who["id"] == "scribe" and who["mission"] == "keep the minutes"
    assert who["operator"] is False


def test_the_gate_stays_live_for_a_requester_who_is_not_an_operator(wired,
                                                                   monkeypatch):
    """The negative twin, and the one that makes the test above mean something.

    A skip that fired for everyone would pass every assertion in the positive
    test while removing the gate entirely — so this asserts the prompt DOES
    happen for a non-operator requester, over the same wire, in the same shape.
    Spawning is operator-only today and will not stay that way (laurent, `dm#83`:
    *"later on, we will want delegates to be able to spawn seats"*); a machine
    you do not own must still be able to refuse.

    `POST /spawns` is operator-only, so the row is created through the service
    the threaded hub is actually serving — the only way to express a
    non-operator requester at all.
    """
    _installed(monkeypatch, "claude")
    from agora.models import AgentInfo

    _register(wired.url, "someone-else")
    service = wired.app.state.service
    row = service.create_spawn_request(
        AgentInfo(id="someone-else", operator=False),
        seat_id="scribe", mission="", harness="claude", machine=MACHINE,
        folder="", channels=[], options={})

    cfg = replace(wired.cfg, require_approval=True)
    calls, approve = _approval_spy()
    R.run_once(cfg, RunnerState(), wired.hub, joiner=_joiner([]),
               launcher=_launcher(), approver=approve)

    assert calls == ["scribe"], "a non-operator requester skipped the tty gate"
    # And the decline is honoured: refused at the machine, said out loud.
    refused = _row(wired.url, row.id)
    assert refused["state"] == "rejected"
    assert MACHINE in refused["detail"]


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
