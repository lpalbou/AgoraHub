"""`agora runner` — the one process that turns "a seat is wanted" into a seat.

The hub records intent and never starts anything (`docs/architecture.md:340`,
pinned by a test that walks the hub package's AST). This is the other half: a
HUMAN-STARTED, session-bound loop on the machine where the agent should live.
It pulls requests, applies its OWN local policy, and runs the same
`join.run_join()` a human runs today before launching `agora drive` as its own
child.

Why the direction matters, in the operator's own words (dm#24): *"we already
enable agents from remote machines to register to the hub, which is safer,
because it is the agent deciding to join."* That is pull, and this is pull. The
hub opens no connection here, holds no ssh key, knows no absolute path, and
names no binary. This process decides, and everything it will refuse is
decided by the flags a human typed when starting it — never by the request.

NOT hub machinery, and NOT persistent: it dies with the shell that started it,
exactly like `agora drive`. No launchd, no systemd, no login item. A supervision
layer was built here once and deleted hours later
(`docs/backlog/deprecated/0051`); this is deliberately not that.

The five gates, all local, none overridable by the hub or by a request:

1. workspace-root confinement, symlinks resolved
2. harness allowlist — declared to agora, installed here, and not blocked
3. max concurrent seats
4. permission floor — `write`, never `all`, whatever a request asks
5. optional per-request tty approval (`--require-approval`)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config as _config
from .logfmt import emit_log
from .models import SpawnState, validate_spawn_folder

#: The floor a request can never raise. `all` means "no sandbox" on every
#: harness that expresses it, and a request arriving over the wire must not be
#: able to ask for that — the human who started this process chose `--root`
#: precisely to bound what a spawned seat can touch.
PERMISSION_FLOOR = "write"

DEFAULT_POLL_SECONDS = 5.0


class GateRefused(Exception):
    """A local gate said no. The message is the runner's OWN sentence and the
    hub stores it verbatim — it is what the operator reads instead of going to
    find a log on this machine, so it names the thing and the machine."""


@dataclass
class RunnerConfig:
    root: Path
    machine: str = "local"
    url: str = ""
    agent_id: str = ""
    api_key: str = ""
    #: Empty = every harness that is declared AND installed here. A non-empty
    #: allowlist can only ever NARROW that: naming a harness that is not
    #: installed does not make it available.
    allow: tuple[str, ...] = ()
    #: Per-harness model menus, typed by the human at THIS machine (`--models
    #: claude=id,id`). Absent for a harness means this runner has not said, and
    #: a client leaves the model free text; see `harness_capabilities`.
    models: dict[str, tuple[str, ...]] = field(default_factory=dict)
    max_seats: int = 4
    require_approval: bool = True
    poll_seconds: float = DEFAULT_POLL_SECONDS
    vendor_bootstrap: bool = False   # hard off: it mutates global harness config


@dataclass
class Launched:
    seat_id: str
    folder: Path
    pid: int
    process: Any = None


@dataclass
class RunnerState:
    """What this process is currently responsible for. Not persisted: a runner
    that dies takes its children with it and the operator restarts it."""

    live: dict[str, Launched] = field(default_factory=dict)


# -- gates ---------------------------------------------------------------------


def declared_harnesses() -> tuple[str, ...]:
    from .drive import _DRIVE_ADAPTERS
    return tuple(sorted(_DRIVE_ADAPTERS))


def accepted_harnesses(cfg: RunnerConfig) -> tuple[str, ...]:
    """The set this runner ANNOUNCES, computed here and nowhere else.

    There is no hub-side harness enum on purpose: a dropdown populated from a
    constant is a lie the moment `codex` is not installed on this machine, and
    it fails silently as a rejected row minutes later. So the machine that
    would have to run the thing is the one that says what it can run, and both
    clients render that list rather than one of their own (the room made this
    rule three times: unknown store prefix, unknown pickup rung, unknown
    `reason` — an unrecognised name renders verbatim and there is never a
    client-side fallback list).
    """
    from .drive import _DRIVE_ADAPTERS

    names = set(cfg.allow) & set(_DRIVE_ADAPTERS) if cfg.allow else set(_DRIVE_ADAPTERS)
    out = []
    for name in sorted(names):
        binary = getattr(_DRIVE_ADAPTERS[name], "binary", "")
        if binary and shutil.which(binary):
            out.append(name)
    return tuple(out)


def harness_capabilities(names: tuple[str, ...],
                         models: dict[str, tuple[str, ...]] | None = None,
                         ) -> dict[str, dict[str, Any]]:
    """What each announced harness will ACCEPT, read off its own adapter.

    Same rule as `accepted_harnesses`, one level down: the harness list stopped
    clients inventing harnesses, and this stops them inventing the harness's
    KNOBS. `agora` hand-transcribed four adapters' vocabularies into a message
    as the source for a dropdown (`agora-and-wui#251`) and it was already wrong
    by two — `pi` and `abstractcode-tui` missing — in the same evening it had
    argued that a client restating an unpublished contract is drift no suite
    can see. A dropdown built from that message would have made those two
    harnesses unspawnable with reasoning, green on both sides.

    Per harness:
      `reasoning`  — the legal `--reasoning-effort` values. EMPTY means the
                     harness takes no reasoning knob at all (cursor), which is
                     a different fact from "any value" and renders differently.
      `reasoning_advisory` — it accepts the knob and enforces NOTHING (pi), so
                     a seat can ask for `max` and get whatever the vendor does.
      `default_model` — what it drives with when nobody names a model. `None`
                     means the harness resolves its own, so a client must not
                     print a default it does not have.
      `models`     — the menu of model ids for this harness ON THIS MACHINE,
                     and the ONE capability no adapter can compute. It is
                     emitted only when the human running this process typed it
                     (`--models <harness>=<id,id>`), because that human is the
                     only party who knows which models this machine's account
                     can actually drive. Absent means exactly that — nobody has
                     said — and a client leaves the field free text.

    There is still deliberately no adapter-authored model list: no adapter
    enumerates models, and a hardcoded one is the fallback this function exists
    to kill. `--models` is not that list; it is a local operator statement,
    which is the same authority `harnesses` already carries (what is installed
    HERE), one level down. laurent asked for the per-harness list at
    `commons#296`; this is where it can come from without the hub or a client
    inventing one.
    """
    from .drive import _DRIVE_ADAPTERS

    menus = models or {}
    caps: dict[str, dict[str, Any]] = {}
    for name in names:
        adapter = _DRIVE_ADAPTERS.get(name)
        if adapter is None:
            continue
        caps[name] = {
            "reasoning": list(getattr(adapter, "REASONING_VOCAB", ()) or ()),
            "reasoning_advisory": "reasoning" in (
                getattr(adapter, "ADVISORY", frozenset()) or frozenset()),
            "default_model": getattr(adapter, "HARNESS_DEFAULT_MODEL", None),
        }
        if name in menus:
            caps[name]["models"] = list(menus[name])
    return caps


def check_harness(cfg: RunnerConfig, harness: str) -> None:
    """Gate 2. The negative twin of the whole feature: with the binary absent
    the row must reach `rejected` naming the harness AND the machine — never
    `running`, and never a created-folder-and-silence."""
    accepted = accepted_harnesses(cfg)
    if harness in accepted:
        return
    if cfg.allow and harness not in cfg.allow:
        raise GateRefused(
            f"harness '{harness}' is not in this runner's allowlist on "
            f"{cfg.machine} (allowed: {', '.join(cfg.allow) or 'none'})")
    if harness not in declared_harnesses():
        raise GateRefused(
            f"harness '{harness}' is not declared to agora on {cfg.machine} "
            f"(known: {', '.join(declared_harnesses())})")
    raise GateRefused(
        f"harness '{harness}' is not installed on {cfg.machine} — its binary "
        "is not on this machine's PATH")


def resolve_folder(cfg: RunnerConfig, folder: str, seat_id: str) -> Path:
    """Gate 1. `folder` is a HINT relative to this runner's own root; empty
    means `<root>/<seat_id>`. Symlinks are resolved before the containment
    check, because a symlink inside the root pointing out of it is the whole
    attack — `_confined_download_target` in mcp/server.py makes the same
    check for the same reason."""
    try:
        hint = validate_spawn_folder(folder)
    except ValueError as e:
        raise GateRefused(f"{e} (on {cfg.machine})") from e
    root = cfg.root.expanduser().resolve()
    target = (root / (hint or seat_id)).resolve()
    if target != root and root not in target.parents:
        raise GateRefused(
            f"folder '{folder}' resolves outside this runner's root on "
            f"{cfg.machine} — it may only create seats inside {root}")
    if target == root:
        raise GateRefused(
            f"a seat needs its own folder inside {root}, not the root itself")
    return target


def check_capacity(cfg: RunnerConfig, state: RunnerState) -> None:
    """Gate 3. Without it, one bad loop in a client fills a machine."""
    if len(state.live) >= cfg.max_seats:
        raise GateRefused(
            f"{cfg.machine} is at its seat cap ({cfg.max_seats} running) — "
            "stop one, or restart the runner with a higher --max-seats")


def permission_for(options: dict[str, Any]) -> str:
    """Gate 4. A request may ask for less than the floor; it can never ask for
    more. `all` disables the sandbox on every harness that expresses it, and
    nothing arriving over the wire gets to choose that."""
    asked = str((options or {}).get("permissions") or PERMISSION_FLOOR)
    return asked if asked == "read" else PERMISSION_FLOOR


# -- the one request ------------------------------------------------------------


def tty_approve(cfg: RunnerConfig, row: dict[str, Any]) -> bool:
    """Gate 5. Blocking on purpose and only ever on a real tty: a human at
    THIS machine says yes. With no tty there is nobody to ask, and a runner
    that silently self-approves would make `--require-approval` a decoration."""
    if not sys.stdin.isatty():
        raise GateRefused(
            f"--require-approval is on and {cfg.machine} has no tty to ask — "
            "start the runner in a terminal, or run it with "
            "--no-require-approval if this machine is meant to be unattended")
    emit_log(f"AGORA_RUNNER event=approval-request seat={row['seat_id']} "
             f"machine={cfg.machine} harness={row['harness']} "
             f"folder={row.get('folder') or '<root>/' + row['seat_id']} "
             f"requested_by={row.get('requested_by') or '?'}")
    emit_log("AGORA_RUNNER request-preview | mission="
             + json.dumps(str(row.get("mission") or "")[:200]))
    return input("  approve? [y/N] ").strip().lower() in {"y", "yes"}


def default_launcher(cfg: RunnerConfig, seat_id: str, folder: Path,
                     harness: str, permissions: str,
                     model: str = "", reasoning: str = "") -> Launched:
    """Start `agora drive` as a CHILD of this process.

    A child of the runner and NOT of the hub: a `Popen` from inside uvicorn is
    orphaned the moment `agora up --force` SIGTERMs the hub by pid, which is
    the mechanical half of why the hub does not do this.

    `model`/`reasoning` are forwarded ONLY if the row carries them. Before
    they were first-class the operator could set them via `options` and the
    hub would store and echo them while this function never read them — the
    seat ran at the harness default and the row said otherwise. Passing them
    here is what makes the field true rather than decorative.
    """
    argv = [sys.executable, "-m", "agora.cli", "drive", "--as", seat_id,
            "--harness", harness, "--permissions", permissions]
    if model:
        argv += ["--model", model]
    if reasoning:
        argv += ["--reasoning-effort", reasoning]
    if cfg.url:
        argv += ["--url", cfg.url]
    proc = subprocess.Popen(argv, cwd=str(folder), stdin=subprocess.DEVNULL,
                            start_new_session=True)
    return Launched(seat_id=seat_id, folder=folder, pid=proc.pid, process=proc)


def default_joiner(cfg: RunnerConfig, row: dict[str, Any], token: str,
                   folder: Path) -> None:
    """Run the SAME onboarding a human runs today. This is the highest-leverage
    reuse in the design: the runner gains no new power, it just types what a
    person would have typed.

    That reuse is also why the mission reaches the seat's PROMPT and not only
    its hub record — but only as of 2026-08-23, and this docstring claimed it
    before it was true. `run_join` did not mirror the mission; the mirror ran
    from `agora setup` and `agora drive` alone, so every spawned seat got a
    rule file with no standing charge. I had corrected exactly this claim once
    already (`dm#27`) and then wrote it here as fact. Asserting it twice did
    not build it; running the four documented steps against a scratch hub and
    reading the file did.

    `mcp_command` MUST be resolved, never passed as a placeholder. It was
    `""` until 2026-08-23, and `run_join` probes it before redeeming the
    invite — so every spawn died at `mcp-runtime` with `'' is not executable
    on PATH`, a refusal naming no command, on every machine. The feature had
    never worked end to end and 1828 tests said nothing, because the CLI's
    own `agora join` resolves the command at its call site (`cli.py`) and
    nothing exercised THIS one. Found by running the four steps I had been
    handing the operator, which is the only thing that could have found it:
    it is a seam, and both sides passed their own tests."""
    from .join import run_join
    from .mcp.runtime import resolve_mcp_command

    folder.mkdir(parents=True, exist_ok=True)
    run_join(url=cfg.url, token=token, agent_id=row["seat_id"], about="",
             harness=row["harness"], workspace=str(folder), with_hook=True,
             listen=False, mcp_command=resolve_mcp_command(),
             vendor_bootstrap=False)


def handle_request(cfg: RunnerConfig, state: RunnerState, row: dict[str, Any],
                   join_token: str, *,
                   joiner: Callable[..., None] = default_joiner,
                   launcher: Callable[..., Launched] = default_launcher,
                   approver: Callable[..., bool] = tty_approve,
                   report: Callable[[str, str], None] | None = None,
                   ) -> tuple[str, str]:
    """Take one claimed request all the way, and return (state, detail).

    Every exit is a state the operator can read, and every refusal carries this
    runner's own sentence naming the machine. `report` is called for any
    INTERMEDIATE state — today only `awaiting_approval`, which exists because a
    row sitting at `claimed` while a human has not typed `y` shows progress
    that is not happening.
    """
    seat_id = row["seat_id"]
    try:
        check_capacity(cfg, state)
        check_harness(cfg, row["harness"])
        folder = resolve_folder(cfg, row.get("folder") or "", seat_id)
        permissions = permission_for(row.get("options") or {})
    except GateRefused as e:
        return SpawnState.rejected.value, str(e)

    # THE OPERATOR'S OWN REQUEST IS THE APPROVAL (laurent, commons#290). This
    # gate collects a human's consent; when the human who asked IS the hub
    # operator, asking again at this shell demands a second yes from the same
    # person and strands the spawn at `awaiting_approval` until they happen to
    # be watching this terminal — which is exactly how `codex-seat-spawn` sat
    # unspawned. The role comes from the hub with the claim: a runner must not
    # infer authority from an id, and the gate stays live for every requester
    # who is NOT an operator, which is the case that arrives when spawning
    # stops being operator-only.
    if cfg.require_approval and row.get("requested_by_operator"):
        emit_log(f"AGORA_RUNNER event=approval-skipped seat={seat_id} "
                 f"machine={cfg.machine} "
                 f"requested_by={row.get('requested_by') or '?'} "
                 "reason=operator-requested")
    elif cfg.require_approval:
        if report is not None:
            report(SpawnState.awaiting_approval.value,
                   f"awaiting approval on {cfg.machine}")
        try:
            approved = approver(cfg, row)
        except GateRefused as e:
            return SpawnState.rejected.value, str(e)
        if not approved:
            return (SpawnState.rejected.value,
                    f"a human at {cfg.machine} declined this spawn")

    try:
        joiner(cfg, row, join_token, folder)
    except Exception as e:                       # noqa: BLE001 — reported, not swallowed
        return (SpawnState.failed.value,
                f"joining the hub failed on {cfg.machine}: {e}"[:500])
    try:
        launched = launcher(cfg, seat_id, folder, row["harness"], permissions,
                            str(row.get("model") or ""),
                            str(row.get("reasoning") or ""))
    except Exception as e:                       # noqa: BLE001
        return (SpawnState.failed.value,
                f"the seat joined but its driver would not start on "
                f"{cfg.machine}: {e}"[:500])

    state.live[row["id"]] = launched
    return (SpawnState.running.value,
            f"running on {cfg.machine} as pid {launched.pid} in "
            f"{launched.folder}")


def _runner_log(event_log: Callable[[str], None] | None, line: str) -> None:
    """Best-effort runner telemetry: logging must never break supervision."""
    if event_log is None:
        return
    try:
        event_log(line)
    except Exception:
        pass


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _mission_preview(row: dict[str, Any], limit: int = 160) -> tuple[int, str]:
    mission = str(row.get("mission") or "")
    preview = " ".join(mission.split())
    if len(preview) > limit:
        preview = preview[:limit - 1] + "…"
    return len(mission), preview


def reap(state: RunnerState, *,
         event_log: Callable[[str], None] | None = None
         ) -> list[tuple[str, str]]:
    """Children that have exited since the last look, as (spawn_id, detail).

    A driver that dies is a seat that is gone, and a row left at `running`
    would be the hub asserting something it cannot see. The turn timeout is
    also known to leak grandchildren (`drive.py`, measured at 5069s against a
    3600s cap), which is why the runner starts each driver in its own session
    and signals the group rather than the pid.
    """
    done: list[tuple[str, str]] = []
    for spawn_id, launched in list(state.live.items()):
        proc = launched.process
        if proc is None or proc.poll() is None:
            continue
        del state.live[spawn_id]
        _runner_log(
            event_log,
            f"AGORA_RUNNER event=agent-decommissioned spawn={spawn_id} "
            f"seat={launched.seat_id} pid={launched.pid} "
            f"reason=driver-exited returncode={proc.returncode}")
        done.append((spawn_id, f"driver for '{launched.seat_id}' exited with "
                               f"code {proc.returncode}"))
    return done


def stop_seat(state: RunnerState, spawn_id: str, *, grace: float = 5.0,
              reason: str = "stop-requested",
              event_log: Callable[[str], None] | None = None) -> str:
    """End one seat's driver and everything it started."""
    launched = state.live.pop(spawn_id, None)
    if launched is None or launched.process is None:
        return "no live driver here for that request"
    proc = launched.process
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    deadline = time.time() + grace
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
    detail = f"stopped '{launched.seat_id}' (pid {launched.pid})"
    _runner_log(
        event_log,
        f"AGORA_RUNNER event=agent-decommissioned spawn={spawn_id} "
        f"seat={launched.seat_id} pid={launched.pid} reason={reason} "
        f"returncode={proc.poll() if proc.poll() is not None else 'unknown'}")
    return detail


# -- the hub conversation --------------------------------------------------------


class RunnerHub:
    """Every call this process makes to the hub. Small on purpose: it is the
    whole attack surface in the other direction."""

    def __init__(self, url: str, api_key: str, timeout: float = 30.0) -> None:
        import httpx

        self._http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout)

    def announce(self, machine: str, harnesses: tuple[str, ...],
                 capabilities: dict[str, Any] | None = None,
                 poll_seconds: float | None = None) -> dict[str, Any]:
        # This process is the only place the poll interval is known — it is a
        # local flag. Announcing it lets the hub serve a staleness cutoff so
        # no client has to invent one from a default it cannot see.
        r = self._http.post(f"/machines/{machine}/announce",
                            json={"harnesses": list(harnesses),
                                  "capabilities": capabilities or {},
                                  "poll_seconds": poll_seconds})
        r.raise_for_status()
        return r.json()

    def claim(self, machine: str) -> dict[str, Any] | None:
        r = self._http.post("/spawns/claim", json={"machine": machine})
        r.raise_for_status()
        body = r.json()
        return body if body.get("request") else None

    def set_state(self, spawn_id: str, state: str, detail: str = "") -> None:
        r = self._http.post(f"/spawns/{spawn_id}/state",
                            json={"state": state, "detail": detail})
        r.raise_for_status()

    def list_mine(self, machine: str) -> list[dict[str, Any]]:
        r = self._http.get("/spawns", params={"machine": machine,
                                              "active_only": True})
        if r.status_code == 403:
            # A runner is not an operator, and the list view is the operator's.
            # Stop requests reach us through the rows we already hold.
            return []
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._http.close()


def run_once(cfg: RunnerConfig, state: RunnerState, hub: RunnerHub, *,
             event_log: Callable[[str], None] | None = None, **kw) -> str:
    """One turn of the loop: reap, claim, act, report. Returns a one-line
    summary for the runner's own stdout — the human who started it is the only
    audience, and they should be able to read what it did."""
    lines = []
    for spawn_id, detail in reap(state, event_log=event_log):
        hub.set_state(spawn_id, SpawnState.stopped.value, detail)
        lines.append(detail)

    claimed = hub.claim(cfg.machine)
    if claimed is None:
        return "; ".join(lines) or "nothing to do"
    row = claimed["request"]
    # The requester's ROLE rides the claim, not the row — carried onto the row
    # here so every gate reads one object. An older hub omits it and the value
    # is False, which prompts exactly as before: absent authority is never
    # read as authority.
    row["requested_by_operator"] = bool(claimed.get("requested_by_operator"))
    options = row.get("options") if isinstance(row.get("options"), dict) else {}
    asked_permissions = str(options.get("permissions") or PERMISSION_FLOOR)
    effective_permissions = permission_for(options)
    mission_chars, mission_preview = _mission_preview(row)
    _runner_log(
        event_log,
        f"AGORA_RUNNER event=spawn-received spawn={row['id']} "
        f"seat={row['seat_id']} machine={cfg.machine} "
        f"harness={_json_value(row['harness'])} "
        f"folder={_json_value(row.get('folder') or '')} "
        f"model={_json_value(row.get('model') or '')} "
        f"reasoning={_json_value(row.get('reasoning') or '')} "
        f"permissions_requested={_json_value(asked_permissions)} "
        f"permissions_effective={effective_permissions} "
        f"approval={'required' if cfg.require_approval else 'off'} "
        f"channels={_json_value(row.get('channels') or [])} "
        f"requested_by={row.get('requested_by') or '?'} "
        f"mission_chars={mission_chars} "
        f"mission_preview={_json_value(mission_preview)}")

    def report_state(spawn_state: str, detail: str) -> None:
        hub.set_state(row["id"], spawn_state, detail)
        _runner_log(
            event_log,
            f"AGORA_RUNNER event=spawn-state spawn={row['id']} "
            f"seat={row['seat_id']} state={spawn_state} "
            f"detail={_json_value(detail)}")

    result, detail = handle_request(
        cfg, state, row, claimed["join_token"],
        report=report_state, **kw)
    hub.set_state(row["id"], result, detail)
    launched = state.live.get(row["id"])
    if result == SpawnState.running.value and launched is not None:
        _runner_log(
            event_log,
            f"AGORA_RUNNER event=agent-spawned status=running "
            f"spawn={row['id']} seat={row['seat_id']} pid={launched.pid} "
            f"machine={cfg.machine} harness={_json_value(row['harness'])} "
            f"folder={_json_value(str(launched.folder))} "
            f"model={_json_value(row.get('model') or '')} "
            f"reasoning={_json_value(row.get('reasoning') or '')} "
            f"permissions={effective_permissions}")
    else:
        _runner_log(
            event_log,
            f"AGORA_RUNNER event=spawn-finished status={result} "
            f"spawn={row['id']} seat={row['seat_id']} "
            f"detail={_json_value(detail)}")
    lines.append(f"{row['seat_id']}: {result} — {detail}")
    return "; ".join(lines)


# -- CLI --------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True,
                        help="the ONLY directory this runner may create seats "
                             "in (mandatory, never defaulted: a default of "
                             "$HOME or / is how a bounded tool stops being one)")
    parser.add_argument("--machine", default="local",
                        help="the name operators route to (default: local)")
    parser.add_argument("--as", dest="as_agent", default=None,
                        metavar="AGENT_ID",
                        help="this runner's OWN seat id; an admin must have "
                             "named it with `PUT /admin/machines/<m>/runner` "
                             "or no request can be claimed")
    parser.add_argument("--url", default=None)
    parser.add_argument("--harness", dest="allow", action="append", default=[],
                        metavar="NAME",
                        help="narrow the announced set (repeatable). Default: "
                             "every harness declared to agora AND installed "
                             "here. This can only narrow — naming one that is "
                             "not installed does not make it available")
    parser.add_argument("--models", dest="models", action="append", default=[],
                        metavar="HARNESS=ID,ID",
                        help="the model menu clients offer for one harness on "
                             "this machine (repeatable, e.g. "
                             "--models claude=claude-opus-5,claude-sonnet-5). "
                             "No adapter can compute this — you are the only "
                             "party who knows which models this machine's "
                             "account drives. Omit a harness and clients leave "
                             "its model a free-text field; it is a MENU, not a "
                             "gate, and an unlisted model still spawns")
    parser.add_argument("--max-seats", type=int, default=4)
    parser.add_argument("--require-approval", dest="require_approval",
                        action="store_true", default=True,
                        help="ask a human at this terminal before each spawn "
                             "(default: on)")
    parser.add_argument("--no-require-approval", dest="require_approval",
                        action="store_false",
                        help="unattended: the consent is your act of starting "
                             "this runner with a root, an allowlist and a cap")
    parser.add_argument("--poll-seconds", type=float,
                        default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true",
                        help="one iteration and exit (for scripts and tests)")


def parse_model_menus(specs: list[str]) -> dict[str, tuple[str, ...]]:
    """`["claude=a,b"]` -> `{"claude": ("a", "b")}`, or SystemExit.

    A malformed spec exits at config time rather than being skipped: this is a
    hand-typed flag, the operator is at the terminal, and a dropped `--models`
    would be discovered as a missing dropdown an hour later on another machine.
    `claude=` with nothing after it is legal and MEANS something — the empty
    list is 'this runner constrains nothing', which a client renders
    differently from having said nothing at all."""
    menus: dict[str, tuple[str, ...]] = {}
    for spec in specs:
        harness, sep, raw = spec.partition("=")
        harness = harness.strip()
        if not sep or not harness:
            raise SystemExit(
                f"agora runner: --models {spec!r} is not HARNESS=ID,ID — e.g. "
                "--models claude=claude-opus-5,claude-sonnet-5")
        ids: list[str] = []
        for entry in raw.split(","):
            ident = entry.strip()
            if ident and ident not in ids:
                ids.append(ident)
        menus[harness] = tuple(ids)
    return menus


def config_from_args(args: argparse.Namespace) -> RunnerConfig:
    url = args.url or _config.load_config().get("url") or ""
    agent_id = args.as_agent or os.environ.get("AGORA_AGENT_ID") or ""
    if not agent_id:
        raise SystemExit("agora runner: --as <agent-id> is required — a runner "
                         "is a seat with its own key, like every other")
    if not url:
        raise SystemExit("agora runner: no hub url (pass --url or run "
                         "`agora status` to see what this machine is wired to)")
    key = _config.get_cached_key(url, agent_id)
    if not key:
        raise SystemExit(
            f"agora runner: no cached key for '{agent_id}' at {url} — join "
            "this machine first (`agora join …`), then start the runner")
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"agora runner: --root {root} is not a directory")
    return RunnerConfig(root=root, machine=args.machine, url=url,
                        agent_id=agent_id, api_key=key,
                        allow=tuple(args.allow), max_seats=args.max_seats,
                        models=parse_model_menus(getattr(args, "models", [])),
                        require_approval=args.require_approval,
                        poll_seconds=args.poll_seconds)


def main(args: argparse.Namespace) -> int:
    cfg = config_from_args(args)
    state = RunnerState()
    hub = RunnerHub(cfg.url, cfg.api_key)
    accepted = accepted_harnesses(cfg)

    emit_log(f"AGORA_RUNNER event=starting machine={cfg.machine} "
             f"seat={cfg.agent_id} root={json.dumps(str(cfg.root))}")
    emit_log("AGORA_RUNNER config harnesses="
             + (",".join(accepted) or "NONE-INSTALLED")
             + f" max_seats={cfg.max_seats} "
               f"approval={'on' if cfg.require_approval else 'off'}")
    # Say the true thing here rather than only in the docs: `agora setup` also
    # writes OUTSIDE the named folder (harness rule files under $HOME), so
    # "the agent only touches the folder I named" would be false.
    emit_log("AGORA_RUNNER notice | spawned seats run as this user with this "
             "environment; harness wiring may also be written under $HOME")
    # A menu for a harness this machine cannot run would make the hub refuse
    # the WHOLE announce — one typo and the machine says it has no harnesses at
    # all. Drop those here and NAME them: the operator is at this terminal now,
    # and a missing dropdown discovered later reads as a hub defect.
    menus = {h: ids for h, ids in cfg.models.items() if h in accepted}
    for stray in sorted(set(cfg.models) - set(menus)):
        emit_log(f"AGORA_RUNNER notice | --models {stray}=… ignored: this "
                 f"machine does not announce {stray} (installed and allowed: "
                 + (",".join(accepted) or "NONE") + ")")
    try:
        hub.announce(cfg.machine, accepted,
                     harness_capabilities(accepted, menus),
                     poll_seconds=cfg.poll_seconds)
        emit_log(f"AGORA_RUNNER event=announced status=ok "
                 f"machine={cfg.machine} harnesses={len(accepted)}")
    except Exception as e:                       # noqa: BLE001
        emit_log(f"AGORA_RUNNER event=announced status=failed "
                 f"machine={cfg.machine} detail={json.dumps(str(e))}")

    stopping = False

    def _handle_signal(*_a: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        while not stopping:
            try:
                line = run_once(cfg, state, hub, event_log=emit_log)
                if line != "nothing to do":
                    emit_log(f"AGORA_RUNNER event=activity | {line}")
            except Exception as e:               # noqa: BLE001
                emit_log("AGORA_RUNNER event=hub-call status=failed detail="
                         + json.dumps(str(e)))
            if args.once:
                break
            for _ in range(max(1, int(cfg.poll_seconds * 10))):
                if stopping:
                    break
                time.sleep(0.1)
    finally:
        for spawn_id in list(state.live):
            detail = stop_seat(state, spawn_id, reason="runner-shutdown",
                               event_log=emit_log)
            try:
                hub.set_state(spawn_id, SpawnState.stopped.value, detail)
            except Exception:                    # noqa: BLE001
                pass
            emit_log(f"AGORA_RUNNER event=stopped | {detail}")
        hub.close()
        emit_log(f"AGORA_RUNNER event=shutdown machine={cfg.machine} "
                 f"live_seats={len(state.live)}")
    return 0
