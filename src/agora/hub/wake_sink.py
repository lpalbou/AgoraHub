"""Hub-driven wake: turn "message delivered" into "the agent runs a turn".

Field-proven gap (2026-07-09): the hub delivered an urgent directive to six
agents' inboxes and notify files within one second — and all six sat deaf for
an hour, because they are turn-gated harness sessions (cursor-agent CLI) whose
stop-hooks only fire when a turn ENDS. An idle session runs no turns; every
resident process that could have resumed one (watchers, attache) had died with
its terminal. Delivery without a wake is a mailbox, not communication.

The fix follows the same lesson as notify_sink: move the job into the one
process that must exist anyway. The hub already knows every delivery, every
agent's live connections, and every viewer's envelope — so the hub itself now
runs the operator's wake command when a wake-worthy message lands for an agent
with no live push connection.

Trust boundary: the command comes from an operator-authored config file
(~/.agora/wake.json, never writable via any API; refused if world-readable).
The digest reaches the command on STDIN — message content never touches argv
or the shell line. Best-effort by contract: a wake failure never fails a post;
it is logged to wake.log and surfaced by `agora status` as WAKE-FAIL.

Loop safety, layered: per-agent debounce coalesces bursts into one wake; a
sliding-window wake budget (default 12/hour) is the alarm clock's own brake;
the hub's post rate limit and interrupt/critical budgets bound whatever the
woken agent does next. `critical` messages bypass the budget and the
active-skip — forced attention stays forced.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..models import Envelope

_DEFAULTS = {
    "debounce_seconds": 5.0,
    "max_wakes_per_hour": 12,
    # A wake command IS a full agent turn: reading channels, thinking,
    # replying — routinely 10-30 minutes. Killing it mid-turn destroys the
    # work and posts nothing (field incident: five agents woke, worked 10
    # minutes each, and were all killed by a 600s default). The timeout is a
    # hung-process backstop, not a turn budget — keep it generous.
    "command_timeout_seconds": 3600.0,
    # An `active` agent is likely mid-turn; waking would interleave a resume
    # into a running session. The pending wake re-checks on this cadence and
    # fires once the agent rests — deferred, never dropped.
    "recheck_seconds": 60.0,
}


def wake_worthy(env: Envelope) -> bool:
    """Wake only for traffic the inbox itself treats as demanding: forced
    attention, addressed work, obligations, or hub-escalated rot. Plain fyi
    broadcasts wait for the agent's next natural turn."""
    return bool(env.critical or env.to_me or env.reply_to_me or env.escalated
                or env.status.value in ("open", "blocked"))


def load_wake_config(path: Path) -> dict[str, Any] | None:
    """Operator-authored wake map. Refuse (with a loud warning) if the file is
    readable by group/other — it contains shell commands the hub will run."""
    if not path.exists():
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        print(f"agora: {path} is group/world-readable (mode {oct(mode)}); "
              "wake DISABLED — chmod 600 it and restart", file=sys.stderr)
        return None
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        print(f"agora: cannot read {path}: {exc}; wake DISABLED", file=sys.stderr)
        return None
    if not isinstance(config.get("agents"), dict):
        print(f"agora: {path} has no 'agents' map; wake DISABLED", file=sys.stderr)
        return None
    return config


class _Budget:
    """Sliding-window cap — the alarm clock must not become the loop."""

    def __init__(self, max_per_hour: int) -> None:
        self.max_per_hour = max_per_hour
        self._times: list[float] = []

    def allow(self) -> bool:
        now = time.time()
        self._times = [t for t in self._times if now - t < 3600.0]
        if len(self._times) >= self.max_per_hour:
            return False
        self._times.append(now)
        return True


class WakeSink:
    """Debounced, budgeted subprocess wakes, one lane per configured agent.

    Thread model: `consider()` is called from request worker threads; each
    agent's pending wake is a threading.Timer that re-arms on further traffic
    within the debounce window, so a burst becomes one wake carrying the full
    inbox digest.
    """

    def __init__(self, config: dict[str, Any], *,
                 digest_fn: Callable[[str], str],
                 has_connection: Callable[[str], bool],
                 state_fn: Callable[[str], str],
                 log_path: str | Path | None = None) -> None:
        defaults = {**_DEFAULTS, **(config.get("defaults") or {})}
        self.debounce = float(defaults["debounce_seconds"])
        self.timeout = float(defaults["command_timeout_seconds"])
        self.recheck = float(defaults["recheck_seconds"])
        self.agents: dict[str, dict[str, Any]] = config["agents"]
        self.digest_fn = digest_fn            # agent_id -> rendered inbox digest
        self.has_connection = has_connection  # live push connection?
        self.state_fn = state_fn              # presence state string
        self.log_path = Path(log_path) if log_path else None
        self._budgets = {aid: _Budget(int(spec.get("max_wakes_per_hour",
                                                   defaults["max_wakes_per_hour"])))
                         for aid, spec in self.agents.items()}
        self._timers: dict[str, threading.Timer] = {}
        self._pending_critical: set[str] = set()
        self._running: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        # Operator visibility: last outcome per agent, served by /admin/status.
        self.last_result: dict[str, dict[str, Any]] = {}

    def configured(self, agent_id: str) -> bool:
        return agent_id in self.agents

    # -- delivery-time decision (cheap; called per member per message) --------

    def consider(self, agent_id: str, envelope: Envelope) -> None:
        spec = self.agents.get(agent_id)
        if spec is None or not wake_worthy(envelope):
            return
        if self.has_connection(agent_id):
            return  # a live push consumer (watch/runner/chat) already has it
        with self._lock:
            if envelope.critical:
                self._pending_critical.add(agent_id)
            self._arm(agent_id, self.debounce)

    def _arm(self, agent_id: str, delay: float) -> None:
        """(Re)start the agent's pending-wake timer. Caller holds the lock."""
        timer = self._timers.get(agent_id)
        if timer is not None:
            timer.cancel()  # re-arm: coalesce the burst into one wake
        timer = threading.Timer(delay, self._fire, args=(agent_id,))
        timer.daemon = True
        self._timers[agent_id] = timer
        timer.start()

    # -- the wake itself (runs on the timer thread) -----------------------------

    def _fire(self, agent_id: str) -> None:
        with self._lock:
            self._timers.pop(agent_id, None)
            critical = agent_id in self._pending_critical
        # Conditions are checked at FIRE time, not delivery time, and a busy
        # agent DEFERS the wake instead of dropping it — otherwise a message
        # arriving just after a turn ends leaves the agent deaf until the
        # next delivery event (field-review hole).
        if self.has_connection(agent_id):
            return  # a live consumer appeared meanwhile; push has it
        if not critical and self.state_fn(agent_id) == "active":
            with self._lock:
                if agent_id not in self._timers:  # newer arrival may have re-armed
                    self._arm(agent_id, self.recheck)
            return
        # One wake session at a time per agent: a wake command IS an agent
        # turn (often minutes long); spawning a second into the same
        # workspace would race the first. Defer — the running session's own
        # inbox check covers whatever arrived meanwhile, and the re-check
        # fires the wake if it somehow doesn't.
        running = self._running.get(agent_id)
        if running is not None and running.poll() is None:
            with self._lock:
                if agent_id not in self._timers:
                    self._arm(agent_id, self.recheck)
            return
        with self._lock:
            self._pending_critical.discard(agent_id)
        if not critical and not self._budgets[agent_id].allow():
            self._record(agent_id, ok=False, note="wake budget exhausted")
            return
        try:
            digest = self.digest_fn(agent_id)
        except Exception as exc:
            self._record(agent_id, ok=False, note=f"digest failed: {exc}")
            return
        command = self.agents[agent_id]["command"]
        try:
            # Own process group: on timeout the WHOLE tree dies (killing only
            # the `sh -c` wrapper would orphan the harness underneath it).
            proc = subprocess.Popen(
                command, shell=True, start_new_session=True,
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, text=True)
            self._running[agent_id] = proc
            _, stderr = proc.communicate(input=digest, timeout=self.timeout)
            ok = proc.returncode == 0
            self._record(agent_id, ok=ok, exit_code=proc.returncode,
                         note=(stderr or "")[-400:] if not ok else "")
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            self._record(agent_id, ok=False, note=f"timed out after {self.timeout}s")
        except OSError as exc:
            self._record(agent_id, ok=False, note=str(exc))
        finally:
            self._running.pop(agent_id, None)

    # -- restart resilience -----------------------------------------------------

    def sweep(self, pending_agents: list[str]) -> None:
        """Re-arm wakes after a hub restart: pending debounce timers die with
        the process, so agents whose obligations were already waiting would
        stay deaf until the NEXT delivery (field incident: 'i relaunched and
        it changed nothing'). Called once at startup with every configured
        agent that has undischarged wake-worthy work and no live connection."""
        with self._lock:
            for agent_id in pending_agents:
                if agent_id in self.agents and agent_id not in self._timers:
                    self._arm(agent_id, self.debounce)

    def _record(self, agent_id: str, *, ok: bool, exit_code: int | None = None,
                note: str = "") -> None:
        entry = {"ts": time.time(), "agent_id": agent_id, "ok": ok,
                 "exit_code": exit_code, "note": note}
        self.last_result[agent_id] = entry
        if self.log_path is not None:
            try:
                with open(self.log_path, "a") as fh:
                    fh.write(json.dumps(entry) + "\n")
            except OSError:
                pass  # best-effort; the in-memory result still feeds status
        if not ok:
            print(f"agora: wake for '{agent_id}' failed: {note or exit_code}",
                  file=sys.stderr)


def default_wake_path() -> Path:
    return Path(os.environ.get("AGORA_HOME",
                               str(Path.home() / ".agora"))) / "wake.json"
