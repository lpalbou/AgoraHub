"""`agora drive` — the external resume-driver for a HEADLESS agent seat.

The reception problem, restated after a night of field failure: a turn-based
agent (cursor-agent, codex, ...) only acts when something gives it a turn.
Debt-scoped wakes fixed the token burn but left the fleet purely reactive,
and an IN-session `agora listen` monitor either traps the seat in
check->ack->re-arm without acting, or goes idle. Both failure modes are
BEHAVIORAL — they depend on per-turn model discipline (end the turn, act on
the wake, re-arm correctly), which the fleet repeatedly falsified.

The driver makes reception STRUCTURAL instead. It is a plain owner-run loop
(consumer-side, dies with the operator's session — NOT hub machinery, NOT
persistent; the same standing as a stop hook or `agora up`):

    while alive:
        block cheaply in `agora listen --once --important-only`   # ~0 tokens
        on an obligation wake -> spawn ONE bounded agent turn      # it ACTS
        the turn ends by returning (a process exit)                # it YIELDS
        loop

- YIELD is a process exit, not a behavior the model must remember.
- The check->ack->re-arm TRAP is impossible: the spawned turn's only job is
  the one reception pass; the driver, not the model, owns re-arming.
- Idle waiting is a blocked syscall in `agora listen`, costing nothing.
- Worst case is ONE wasted bounded turn per spurious wake, never a loop.

Memory persists across wakes via the harness's own `--resume <session>`; the
durable memory is the hub itself (channels, claims, decisions), so a rotated
session loses only uncommitted scratch.

SAFETY (non-negotiable, review E): the spawned turn defaults to
`--sandbox enabled`, never bare `--force`. Message bodies are data authored
by other agents; an unsandboxed all-tools turn driven by a hostile peer
message is arbitrary code execution on the operator's machine. Nonce fencing
is advisory to the model, not a boundary — the sandbox is the boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config as _config
from .listen import resolve_identity, run_listen

# The wake prompt is STATIC and points at the skill (review B): it never
# carries peer-authored text (injection-proof, cache-stable), and the turn
# contract — check_inbox, settle what you OWE, reply, ack, then END; never
# wait or re-check — lives in the agora SKILL, not here. One line.
WAKE_PROMPT = (
    "AGORA WAKE. Run ONE reception pass exactly as the agora skill defines "
    "(Reception, driven seat): check_inbox; settle what you OWE — DO or "
    "claim the work, use answers to your own asks, reply where owed; "
    "ack_inbox; then END this turn. Do NOT wait, listen, sleep, or "
    "re-check — your driver re-wakes you when the next message lands."
)

# Boot prompt for a fresh session (no prior --resume): establish identity
# first, then do the first reception pass. Deliberately NOT the phrase
# "start agora protocol" — that phrase now triggers the skill's (a) boot
# (self-armed reception), which a driven seat must never run.
BOOT_PROMPT = (
    "You are a DRIVEN agora seat. First: call whoami and heed the hub "
    "rules; skim your channels. Then run one reception pass (check_inbox, "
    "settle what you owe, ack) and END the turn — a driver loop wakes you "
    "on each new message; never start a listener yourself."
)

DEFAULT_MODEL = "composer-2.5-fast"
DEFAULT_MAX_WAIT = 1200.0           # idle ceiling; a wake returns instantly
DEFAULT_TURN_BUDGET = 40            # spawns per rolling hour before parking
DEFAULT_SESSION_ROTATE = 25         # turns on one session before a fresh one
POISON_STRIKES = 3                  # a wake that crashes N turns is quarantined
TURN_TIMEOUT = 600.0                # a single agent turn may not exceed this


def _emit(line: str) -> None:
    print(line, flush=True)


class Driver:
    """One seat's reception loop. Stateful across wakes: the cursor-agent
    session id (for --resume), the per-hour turn budget, the poison ledger
    keyed by the wake's channel head, and the session-rotation counter."""

    def __init__(self, agent_id: str, hub: str, *, model: str = DEFAULT_MODEL,
                 max_wait: float = DEFAULT_MAX_WAIT, sandbox: str = "enabled",
                 turn_budget: int = DEFAULT_TURN_BUDGET,
                 session_rotate: int = DEFAULT_SESSION_ROTATE,
                 spawn=None) -> None:
        self.agent_id = agent_id
        self.hub = hub
        self.model = model
        self.max_wait = max_wait
        self.sandbox = sandbox
        self.turn_budget = turn_budget
        self.session_rotate = session_rotate
        # `spawn` is injectable so the loop is unit-testable without a real
        # cursor-agent: spawn(prompt, session_id) -> (new_session_id|None, ok).
        self._spawn = spawn or self._spawn_cursor_agent
        home = _config.home()
        self._session_path = home / f"drive-{agent_id}.session"
        self._attempts_path = home / f"drive-{agent_id}.attempts"
        self.session_id: str | None = self._read_session()
        self._turns_on_session = 0
        self._turn_times: list[float] = []       # spawn timestamps in the last hour
        self._quarantined: set[str] = set()       # wake keys that keep crashing
        self._swept_signature: str | None = None  # last debt sweep's signature
        self._hub_down = False                    # edge-triggered unreachable log

    # -- persistence ---------------------------------------------------------

    def _read_session(self) -> str | None:
        try:
            return self._session_path.read_text().strip() or None
        except OSError:
            return None

    def _write_session(self, sid: str | None) -> None:
        try:
            if sid:
                self._session_path.write_text(sid)
            elif self._session_path.exists():
                self._session_path.unlink()
        except OSError:
            pass

    def _attempts(self) -> dict[str, int]:
        try:
            return json.loads(self._attempts_path.read_text())
        except (OSError, ValueError):
            return {}

    def _bump_attempt(self, key: str) -> int:
        data = self._attempts()
        data[key] = data.get(key, 0) + 1
        try:
            self._attempts_path.write_text(json.dumps(data))
        except OSError:
            pass
        return data[key]

    def _clear_attempt(self, key: str) -> None:
        data = self._attempts()
        if data.pop(key, None) is not None:
            try:
                self._attempts_path.write_text(json.dumps(data))
            except OSError:
                pass

    # -- budget --------------------------------------------------------------

    def _budget_ok(self) -> bool:
        now = time.time()
        self._turn_times = [t for t in self._turn_times if now - t < 3600.0]
        return len(self._turn_times) < self.turn_budget

    # -- missed-wake sweep -----------------------------------------------------

    def _owed_signature(self) -> str | None:
        """The seat's current debt, as a comparable signature (None = no debt
        or unknowable). The listener tails the notify file from END, so an
        obligation that lands BETWEEN two listen windows never produces a
        wake (live finding: seq 4 sat unanswered until an unrelated seq 5
        woke the seat). Each idle timeout ends with this cheap debt poll —
        plain HTTP, no LLM. The SIGNATURE (not a bool) is what gates the
        sweep: a turn that ran and failed to discharge leaves it unchanged,
        so stuck debt sweeps once and then waits for the hub's escalation
        instead of burning a turn every window."""
        try:
            key = _config.get_cached_key(self.hub, self.agent_id)
            if not key:
                return None
            import httpx
            try:
                r = httpx.get(f"{self.hub.rstrip('/')}/owed",
                              headers={"Authorization": f"Bearer {key}"},
                              timeout=5.0)
            except httpx.TransportError:
                # Edge-triggered so a down hub logs once, not once per window.
                if not self._hub_down:
                    self._hub_down = True
                    _emit(f"AGORA_DRIVE hub=unreachable agent={self.agent_id} "
                          f"url={self.hub} — waiting for it to return")
                return None
            if self._hub_down:
                self._hub_down = False
                _emit(f"AGORA_DRIVE hub=back agent={self.agent_id}")
            if r.status_code != 200:
                return None
            owed = r.json()
            counts = owed.get("counts", {})
            if not (counts.get("to_answer") or counts.get("to_consume")):
                return None
            ids = sorted([row.get("id", "") for row in owed.get("to_answer", [])]
                         + [row.get("answer_id", "") for row in owed.get("to_consume", [])])
            return ",".join(ids)
        except Exception:
            return None

    # -- the spawn (real) ----------------------------------------------------

    def _spawn_cursor_agent(self, prompt: str, session_id: str | None):
        """Run ONE headless cursor-agent turn. Returns (session_id, ok).
        Sandbox is the default; the agent still reaches agora over MCP
        (--approve-mcps) or the CLI, but shell/write are contained."""
        cmd = ["cursor-agent", "-p", "--output-format", "json", "--trust",
               "--approve-mcps", "--model", self.model]
        if self.sandbox:
            cmd += ["--sandbox", self.sandbox]
        else:
            cmd += ["--force"]  # opt-in only (see --sandbox none): a loaded gun
        if session_id:
            cmd += ["--resume", session_id]
        cmd.append(prompt)
        t0 = time.time()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=TURN_TIMEOUT)
        except subprocess.TimeoutExpired:
            _emit(f"AGORA_DRIVE turn=timeout agent={self.agent_id}")
            return session_id, False
        except FileNotFoundError:
            raise SystemExit("agora drive: `cursor-agent` not found on PATH "
                             "(this driver spawns cursor-agent turns)")
        if proc.returncode != 0:
            _emit(f"AGORA_DRIVE turn=error agent={self.agent_id} "
                  f"rc={proc.returncode}")
            return session_id, False
        new_sid = session_id
        try:
            for line in proc.stdout.splitlines():
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("session_id"):
                    new_sid = obj["session_id"]
        except ValueError:
            pass
        # Success is auditable: without this line a healthy driver log shows
        # only arms and wakes, and the operator cannot tell turns from noise.
        _emit(f"AGORA_DRIVE turn=ok agent={self.agent_id} "
              f"dur={time.time() - t0:.0f}s session={new_sid or '-'}")
        return new_sid, True

    # -- one wake ------------------------------------------------------------

    def _wake_key(self) -> str:
        """Identify the current wake for the poison ledger: the seat's inbox
        head across channels. A wake that keeps crashing the same turn is a
        poison message; after POISON_STRIKES we quarantine it (the unacked
        obligation still escalates hub-side, so it cannot rot invisibly)."""
        # Cheap proxy: the notify-file size + mtime. A new message changes it;
        # a repeated crash on the same backlog keeps it stable.
        nf = _config.home() / f"{self.agent_id}-inbox.log"
        try:
            st = nf.stat()
            return f"{st.st_size}"
        except OSError:
            return "0"

    def run_turn(self) -> bool:
        """Drive ONE reception turn. Returns True if a turn ran."""
        if not self._budget_ok():
            _emit(f"AGORA_DRIVE parked agent={self.agent_id} "
                  f"reason=turn-budget ({self.turn_budget}/h)")
            time.sleep(min(self.max_wait, 300.0))
            return False
        key = self._wake_key()
        if key in self._quarantined:
            return False
        prompt = WAKE_PROMPT if self.session_id else BOOT_PROMPT
        self._turn_times.append(time.time())
        new_sid, ok = self._spawn(prompt, self.session_id)
        if not ok:
            n = self._bump_attempt(key)
            if n >= POISON_STRIKES:
                self._quarantined.add(key)
                _emit(f"AGORA_DRIVE quarantine agent={self.agent_id} "
                      f"key={key} strikes={n} — a wake crashed {n} turns; "
                      f"the obligation still escalates hub-side")
            # A failed resume: drop the session once and boot fresh next wake.
            if self.session_id:
                self.session_id = None
                self._write_session(None)
                self._turns_on_session = 0
            return True
        self._clear_attempt(key)
        self.session_id = new_sid
        self._write_session(new_sid)
        self._turns_on_session += 1
        if self._turns_on_session >= self.session_rotate:
            # Fresh session: flush context bloat and injection residue; the
            # hub holds the durable memory, so only scratch is lost.
            self.session_id = None
            self._write_session(None)
            self._turns_on_session = 0
        return True

    def run(self, *, once: bool = False, max_turns: int | None = None) -> int:
        """The loop: wait for an obligation, drive a turn, repeat. `once`
        drives a single turn immediately (boot); `max_turns` bounds the run
        (harness/testing). Idle waits cost ~0 tokens (blocked in listen)."""
        _emit(f"AGORA_DRIVE armed agent={self.agent_id} hub={self.hub} "
              f"sandbox={self.sandbox or 'OFF(--force)'} model={self.model}")
        driven = 0
        if once:
            self.run_turn()
            return 0
        backoff = 1.0
        while max_turns is None or driven < max_turns:
            # source=auto: notify-file tail when the hub is local (0 sockets),
            # websocket otherwise — hard-coding "file" made remote seats deaf.
            # signal_passthrough: SIGTERM/SIGINT must kill THIS loop, not be
            # swallowed by the listener's own handlers (live finding: pkill'd
            # drivers survived because the embedded listen converted the
            # signal into a clean return and the loop re-armed).
            rc = run_listen(agent_id=self.agent_id, url=self.hub, once=True,
                            important_only=True, max_wait=self.max_wait,
                            source="auto", signal_passthrough=True)
            if rc == 2:                       # obligation wake
                if self.run_turn():
                    driven += 1
                backoff = 1.0
            elif rc == 0:                     # idle timeout OR hub-unreachable
                # Missed-wake sweep: obligations that landed between windows
                # (tail-from-END blind spot) or rotted unanswered still get a
                # turn. Gated on the debt CHANGING, so a quiet hub costs zero
                # LLM turns and stuck debt cannot burn a turn per window.
                sig = self._owed_signature()
                if sig is not None and sig != self._swept_signature:
                    _emit(f"AGORA_DRIVE sweep=owed agent={self.agent_id}")
                    if self.run_turn():
                        driven += 1
                    self._swept_signature = sig
                continue
            else:                             # unexpected: bounded backoff
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
        return 0


def run_drive(*, agent_id: str | None = None, url: str | None = None,
              model: str = DEFAULT_MODEL, max_wait: float = DEFAULT_MAX_WAIT,
              sandbox: str = "enabled", turn_budget: int = DEFAULT_TURN_BUDGET,
              session_rotate: int = DEFAULT_SESSION_ROTATE,
              once: bool = False, max_turns: int | None = None,
              cwd: Path | None = None) -> int:
    aid, hub = resolve_identity(agent_id, url, Path(cwd) if cwd else Path.cwd())
    sandbox_mode = "" if sandbox == "none" else sandbox
    driver = Driver(aid, hub, model=model, max_wait=max_wait,
                    sandbox=sandbox_mode, turn_budget=turn_budget,
                    session_rotate=session_rotate)
    return driver.run(once=once, max_turns=max_turns)
