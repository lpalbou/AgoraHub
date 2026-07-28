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

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config as _config
from .listen import pid_alive, resolve_identity, run_listen

# The wake prompt is STATIC and points at the skill (review B): it never
# carries peer-authored text (injection-proof, cache-stable), and the turn
# contract — check_inbox, settle what you OWE, reply, ack, then END; never
# wait or re-check — lives in the agora SKILL, not here. The CONTINUATION
# clause (2026-07-28, operator principle "agents finish what they start"):
# a turn that owes nothing further advances the seat's ONE live claim a
# bounded unit before ending — after re-reading the record, because a newer
# message may have canceled, refined, or superseded the task.
WAKE_PROMPT = (
    "AGORA WAKE. Run ONE reception pass exactly as the agora skill defines "
    "(Reception, driven seat): check_inbox; settle what you OWE — DO or "
    "claim the work, use answers to your own asks, reply where owed; "
    "ack_inbox. Then, if you hold a live claim and owe nothing further: "
    "re-read the claim row and any newer messages touching that task FIRST "
    "(a newer message may cancel, refine, or supersede it — the record "
    "outranks your memory; adjust or park on the record if so), then "
    "advance it ONE bounded unit and post a progress receipt with "
    "evidence, or post blocked naming the blocker. Then END this turn. "
    "Do NOT wait, listen, sleep, or re-check — your driver re-wakes you "
    "when the next message lands."
)

# Boot prompt for a fresh session (no prior --resume): establish identity
# first, then do the first reception pass. Deliberately NOT the phrase
# "start agora protocol" — that phrase now triggers the skill's (a) boot
# (self-armed reception), which a driven seat must never run.
BOOT_PROMPT = (
    "You are a DRIVEN agora seat. First: call whoami and heed the hub "
    "rules; skim your channels. Then run one reception pass (check_inbox, "
    "settle what you owe, ack); if you hold a live claim and owe nothing "
    "further, advance it one bounded unit (re-read its row and newer "
    "messages first — they may supersede it) and post the receipt. Then "
    "END the turn — a driver loop wakes you on each new message; never "
    "start a listener yourself."
)

# The work prompt (--initiative): STATIC like the others — no hub or peer
# text is ever interpolated. Continuation is a LOOP property of the driver
# (chunks chain at short listen windows; any obligation preempts the next
# chunk at the arm between them), never a model posture — the shape the
# 0083/0085 falsifications left standing. Supersession check is FIRST:
# before continuing, the turn re-reads the claim row and newer messages,
# because the operator or a peer may have canceled/refined/replaced the
# task while the seat was heads-down.
WORK_PROMPT = (
    "AGORA WORK CHUNK. No new obligation is waiting; you hold the live "
    "claim in your home channel — continue THAT work. FIRST re-read the "
    "claim row and any newer messages touching the task: a newer message "
    "may have canceled, refined, or superseded it (the record outranks "
    "your memory) — if so, adjust or park on the record instead of "
    "continuing blind. Otherwise do ONE bounded slice toward completion, "
    "stop at a safe checkpoint (workspace consistent: commit or stash), "
    "overwrite your claim row with a one-line progress receipt naming "
    "what is done and what is next, and END this turn — the driver "
    "re-wakes you for the next slice. Finished, blocked, or not worth "
    "continuing? Write done/blocked/parked on the row, post the receipt "
    "or blocker, and END. Do NOT check the inbox again, wait, listen, or "
    "start watchers — reception is the driver's job between slices."
)

DEFAULT_MODEL = "composer-2.5-fast"
DEFAULT_MAX_WAIT = 1200.0           # idle ceiling; a wake returns instantly
DEFAULT_TURN_BUDGET = 40            # spawns per rolling hour before parking
DEFAULT_SESSION_ROTATE = 25         # turns on one session before a fresh one
POISON_STRIKES = 3                  # a wake that crashes N turns is quarantined
TURN_TIMEOUT = 600.0                # a single agent turn may not exceed this
DRIVE_CHAIN_WAIT = 20.0             # listen window between chained work chunks:
#                                     the arm IS the receive point, so any
#                                     obligation preempts the next chunk here
DEFAULT_WORK_BUDGET = 12            # work chunks per rolling hour (--initiative);
#                                     wall clock limits honest chains to ~6/h,
#                                     this pool only bites degenerate churn
WORK_STRIKES = 3                    # receipt-less chunks per claim VERSION
#                                     before the chain parks (a row touch =
#                                     the receipt; a version bump resets)
_LISTENER_FRESH_S = 600.0           # a listen pidfile younger than this marks
#                                     a live interactive surface (tab loops
#                                     rewrite it every <=245s)
_DRIVER_STALE_S = 7200.0            # a drive pidfile older than this never
#                                     blocks anyone (reboot pid-reuse guard)


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
                 initiative: bool = False,
                 work_timeout: float = TURN_TIMEOUT,
                 work_budget: int = DEFAULT_WORK_BUDGET,
                 force: bool = False,
                 spawn=None) -> None:
        self.agent_id = agent_id
        self.hub = hub
        self.model = model
        self.max_wait = max_wait
        self.sandbox = sandbox
        self.turn_budget = turn_budget
        self.session_rotate = session_rotate
        self.initiative = initiative
        # Cap: a chunk longer than half the driver-staleness bound would
        # let a second driver "take over" mid-chunk (review F8).
        self.work_timeout = min(work_timeout, _DRIVER_STALE_S / 2)
        self.work_budget = work_budget
        self.force = force
        # `spawn` is injectable so the loop is unit-testable without a real
        # cursor-agent: spawn(prompt, session_id) -> (new_session_id|None, ok).
        self._spawn = spawn or self._spawn_cursor_agent
        home = _config.home()
        self._session_path = home / f"drive-{agent_id}.session"
        self._attempts_path = home / f"drive-{agent_id}.attempts"
        # THE driver-ownership signal (2026-07-28 unification): while this
        # file holds a LIVE pid, `agora listen` refuses to arm a second
        # reception surface for the seat, the stop hook stays quiet, and
        # `agora status` shows a `driver` column — one file, one truth.
        self._drive_pid_path = home / f"drive-{agent_id}.pid"
        self.session_id: str | None = self._read_session()
        self._turns_on_session = 0
        self._turn_times: list[float] = []       # spawn timestamps in the last hour
        self._quarantined: set[str] = set()       # wake keys that keep crashing
        self._turn_timeout = TURN_TIMEOUT         # per-spawn cap (work turns raise it)
        self._work_times: list[float] = []        # work-chunk spawns, rolling hour
        self._work_strikes: dict[str, int] = {}   # claim-version -> receipt-less chunks
        self._chain_live = False                  # a work chain is running
        self._pending_wake = False                # a budget-parked wake is HELD

    # -- one driver per seat (the ownership file) ------------------------------

    def _acquire_drive_pid(self) -> int:
        """One driver per seat: refuse while a LIVE driver holds the pidfile
        (double drivers double every turn and fork the --resume session); a
        dead or ancient holder (crash, reboot with pid reuse) is taken over
        silently. A LIVE holder refuses even under --force — two live
        drivers is never what anyone wants, and the previous driver never
        re-reads the file, so an overwrite would RUN ALONGSIDE it, not
        replace it (review F1): stopping the live one is the operator's
        act. Returns the previous holder's pid (0 if none) so the
        foreign-listener check can ignore that driver's own embedded
        listen pidfile."""
        try:
            pid = int(self._drive_pid_path.read_text().strip() or "0")
            mtime = self._drive_pid_path.stat().st_mtime
        except (OSError, ValueError):
            pid, mtime = 0, 0.0
        recent = (time.time() - mtime) < _DRIVER_STALE_S
        if pid > 0 and pid != os.getpid() and pid_alive(pid) and recent:
            raise SystemExit(
                f"agora drive: a driver for '{self.agent_id}' is already "
                f"running (pid {pid}, {self._drive_pid_path}). One driver "
                f"per seat — stop that one yourself (kill {pid}) and "
                "re-run; --force cannot take over a LIVE driver (it would "
                "run alongside it, doubling every turn).")
        with contextlib.suppress(OSError):
            self._drive_pid_path.write_text(str(os.getpid()))
        return pid

    def _touch_drive_pid(self) -> None:
        with contextlib.suppress(OSError):
            os.utime(self._drive_pid_path)

    def _clear_drive_pid(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            if int(self._drive_pid_path.read_text().strip() or "0") == os.getpid():
                self._drive_pid_path.unlink()

    def _preflight_spawner(self) -> None:
        """Refuse a missing cursor-agent AT ARM, not at the first 3am wake
        (the old FileNotFoundError killed the driver with the obligation
        undelivered). Injected spawns (tests, custom harnesses) skip it."""
        if self._spawn != self._spawn_cursor_agent:
            return
        import shutil
        if shutil.which("cursor-agent") is None:
            raise SystemExit(
                "agora drive: `cursor-agent` not found on PATH — this "
                "driver spawns cursor-agent turns. Install the Cursor CLI "
                "first.")

    def _check_foreign_listener(self, prev_driver_pid: int = 0) -> None:
        """An interactive tab's listener and a driver share the seat's
        offset/owedsig files and starve each other (the dual-surface
        hazard). A listen pidfile that is FRESH marks a live interactive
        surface even when its recorded pid is momentarily dead (tab loops
        re-exec every ~245s): refuse a live one, warn on a fresh-but-dead
        one, ignore stale ones — and ignore the PREVIOUS DRIVER's own
        embedded listen (its pidfile carries the driver pid; without the
        exemption a crashed driver's fresh listen pidfile would misdiagnose
        as an interactive tab, review F1)."""
        pidfile = _config.home() / f"listen-{self.agent_id}.pid"
        try:
            pid = int(pidfile.read_text().strip() or "0")
            age = time.time() - pidfile.stat().st_mtime
        except (OSError, ValueError):
            return
        if (pid <= 0 or pid == os.getpid() or pid == prev_driver_pid
                or age > _LISTENER_FRESH_S):
            return
        if pid_alive(pid) and not self.force:
            raise SystemExit(
                f"agora drive: an interactive listener for "
                f"'{self.agent_id}' is live (pid {pid}, touched "
                f"{age:.0f}s ago) — two reception surfaces on one seat "
                "starve each other (shared offset/owedsig). Close that "
                "session or its reception shell and retry (a just-closed "
                "tab drains within ~4 min), or pass --force if you know "
                "it is gone.")
        if not pid_alive(pid):
            _emit(f"AGORA_DRIVE warn=recent-listener agent={self.agent_id} "
                  "(a listener surface was active moments ago; if an "
                  "interactive tab is open for this seat, close it)")

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
                                  timeout=self._turn_timeout)
        except subprocess.TimeoutExpired as exc:
            # Salvage the session id from the partial stdout: discarding it
            # forced a fresh boot after every timeout, losing the very
            # context a long task needs (review 2026-07-28).
            out = exc.stdout or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            sid = session_id
            for line in out.splitlines():
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and obj.get("session_id"):
                        sid = obj["session_id"]
                except ValueError:
                    pass
            _emit(f"AGORA_DRIVE turn=timeout agent={self.agent_id}")
            return sid, False
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
        """Identify the current wake for the poison ledger: a wake that keeps
        crashing the same turn is a poison message; after POISON_STRIKES we
        quarantine it (the unacked obligation still escalates hub-side, so
        it cannot rot invisibly).

        Primary key: the owed SIGNATURE the listener just recorded
        (listen-<id>.owedsig) — the debt's identity. The old size-only key
        collided after notify-file rotation and degraded to a constant '0'
        for ws-mode seats, quarantining every future wake after three bad
        turns (review 2026-07-28). Fallbacks keep the old behavior."""
        sig = _config.home() / f"listen-{self.agent_id}.owedsig"
        try:
            content = sig.read_text().strip()
            if content:
                return hashlib.sha256(content.encode()).hexdigest()[:12]
        except OSError:
            pass
        nf = _config.home() / f"{self.agent_id}-inbox.log"
        try:
            return f"{nf.stat().st_size}"
        except OSError:
            return "0"

    def run_turn(self) -> bool:
        """Drive ONE reception turn. Returns True if a turn ran."""
        if not self._budget_ok():
            # No sleep here (the old 300s nap was a deaf window): the LOOP
            # holds the wake (_pending_wake) and keeps listening; direct
            # callers just get the False.
            _emit(f"AGORA_DRIVE parked agent={self.agent_id} "
                  f"reason=turn-budget ({self.turn_budget}/h)")
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

    # -- initiative: claim-gated work chunks (--initiative, 2026-07-28) -------

    def _claim_snapshot(self) -> tuple[str, str, int] | None:
        """(channel, key, version) of the seat's live claim, or None. Read
        with the cached key over EXISTING endpoints (precedent: listen's
        /owed poll). Any failure returns None — initiative fails toward
        silence, never toward burn. A row whose status word says done/
        parked/blocked (or done:true) is not continuable work."""
        api_key = _config.get_cached_key(self.hub, self.agent_id)
        if not api_key:
            return None
        import urllib.parse

        import httpx
        hdrs = {"Authorization": f"Bearer {api_key}"}
        base = self.hub.rstrip("/")
        terminal = {"done", "shipped", "delivered", "complete", "completed",
                    "closed", "landed", "merged", "released", "resolved",
                    "parked", "paused", "blocked", "on-hold", "onhold",
                    "hold", "deferred"}
        try:
            chans = httpx.get(f"{base}/channels", headers=hdrs,
                              timeout=5.0).json()
            for ch in chans if isinstance(chans, list) else []:
                name = ch.get("name") if isinstance(ch, dict) else None
                if not name or not ch.get("member", True):
                    continue
                rows = httpx.get(f"{base}/channels/{name}/store",
                                 headers=hdrs, timeout=5.0).json()
                for row in rows if isinstance(rows, list) else []:
                    k = str(row.get("key", ""))
                    if not k.startswith("claim:"):
                        continue
                    entry = httpx.get(
                        f"{base}/channels/{name}/store/"
                        f"{urllib.parse.quote(k, safe=':')}",
                        headers=hdrs, timeout=5.0).json()
                    value = entry.get("value")
                    if not isinstance(value, dict):
                        continue
                    if value.get("owner") != self.agent_id or value.get("done"):
                        continue
                    status = str(value.get("status")
                                 or value.get("state") or "").strip().lower()
                    first = (status.split()[0].rstrip(".,;:!—-")
                             if status.split() else "")
                    if first in terminal:
                        continue
                    return name, k, int(entry.get("version", 0))
        except Exception:
            return None
        return None

    def _work_budget_ok(self) -> bool:
        now = time.time()
        self._work_times = [t for t in self._work_times if now - t < 3600.0]
        return len(self._work_times) < self.work_budget

    def run_work_turn(self) -> bool:
        """Spawn ONE bounded work chunk (WORK_PROMPT, --work-timeout cap).
        Shares session persistence/rotation with reception turns but NOT
        the wake poison ledger — a failing chunk must never quarantine the
        inbox head and deafen reception (composition bug, review
        2026-07-28); chunk failures are bounded by the per-version strike
        ledger in _chain_step instead."""
        prompt = WORK_PROMPT if self.session_id else BOOT_PROMPT
        self._work_times.append(time.time())
        self._turn_timeout = self.work_timeout
        try:
            new_sid, ok = self._spawn(prompt, self.session_id)
        finally:
            self._turn_timeout = TURN_TIMEOUT
        if not ok:
            # A failed resume: drop the session once and boot fresh next time.
            if self.session_id:
                self.session_id = None
                self._write_session(None)
                self._turns_on_session = 0
            return True
        self.session_id = new_sid
        self._write_session(new_sid)
        self._turns_on_session += 1
        if self._turns_on_session >= self.session_rotate:
            self.session_id = None
            self._write_session(None)
            self._turns_on_session = 0
        return True

    def _chain_step(self) -> bool:
        """One initiative step at an idle boundary: spawn a work chunk when
        the seat holds a live, progressing claim. Continuation is a LOOP
        property — chunks chain at DRIVE_CHAIN_WAIT listen windows and any
        obligation preempts at the arm between them — never a model
        posture. Strikes are keyed on the claim row's CAS VERSION: a chunk
        that ends without touching the row is a strike; WORK_STRIKES parks
        the chain (recoverable — any row touch mints a fresh version);
        parking is never the wake quarantine."""
        snap = self._claim_snapshot()
        if snap is None:
            self._chain_live = False
            return False
        channel, key, version = snap
        ck = f"{channel}/{key}@{version}"
        if self._work_strikes.get(ck, 0) >= WORK_STRIKES:
            if self._chain_live:
                _emit(f"AGORA_DRIVE initiative=parked agent={self.agent_id} "
                      f"key={ck} reason=no-receipt ({WORK_STRIKES} chunks "
                      "left the claim row untouched; a row touch resumes)")
            self._chain_live = False
            return False
        if not self._work_budget_ok():
            if self._chain_live:
                _emit(f"AGORA_DRIVE initiative=parked agent={self.agent_id} "
                      f"reason=work-budget ({self.work_budget}/h)")
            self._chain_live = False
            return False
        ran = self.run_work_turn()
        after = self._claim_snapshot()
        if (after is not None and after[0] == channel and after[1] == key
                and after[2] == version):
            self._work_strikes[ck] = self._work_strikes.get(ck, 0) + 1
        self._chain_live = ran and after is not None
        return ran

    def run(self, *, once: bool = False, max_turns: int | None = None) -> int:
        """The loop: wait for an obligation, drive a turn, repeat; with
        --initiative, idle boundaries additionally chain claim-gated work
        chunks. `once` drives a single turn immediately (boot); `max_turns`
        bounds the run (harness/testing). Idle waits cost ~0 tokens
        (blocked in listen)."""
        self._preflight_spawner()
        # Order matters (review F1): claim the driver seat FIRST — its
        # refusal names the real conflict (a live driver) — then check for
        # an interactive listener, ignoring the previous driver's own one.
        prev_pid = self._acquire_drive_pid()
        self._check_foreign_listener(prev_driver_pid=prev_pid)
        _emit(f"AGORA_DRIVE armed agent={self.agent_id} hub={self.hub} "
              f"sandbox={self.sandbox or 'OFF(--force)'} model={self.model}"
              + (f" initiative=on work_budget={self.work_budget}/h"
                 if self.initiative else ""))
        driven = 0
        try:
            if once:
                self.run_turn()
                return 0
            backoff = 1.0
            while max_turns is None or driven < max_turns:
                self._touch_drive_pid()
                # source=auto: notify-file tail when the hub is local (0
                # sockets), websocket otherwise — hard-coding "file" made
                # remote seats deaf. signal_passthrough: SIGTERM/SIGINT must
                # kill THIS loop, not be swallowed by the listener's own
                # handlers. Missed-wake recovery is INSIDE run_listen:
                # arming starts with a debt poll (signature-gated), so an
                # obligation that landed mid-turn wakes at the next arm —
                # which, while a chain is live, is at most DRIVE_CHAIN_WAIT
                # away: obligations always preempt the next chunk.
                window = (DRIVE_CHAIN_WAIT
                          if (self.initiative and self._chain_live)
                          else self.max_wait)
                rc = run_listen(agent_id=self.agent_id, url=self.hub,
                                once=True, important_only=True,
                                max_wait=window, source="auto",
                                signal_passthrough=True, driver_call=True)
                if rc == 2:                   # obligation wake (live or backlog)
                    if self._budget_ok():
                        # This turn drains the WHOLE inbox, held debt
                        # included — a still-set flag would spawn a
                        # spurious turn at the next idle (review F2).
                        self._pending_wake = False
                        if self.run_turn():
                            driven += 1
                        backoff = 1.0
                    else:
                        # Budget-parked: HOLD the wake instead of sleeping
                        # deaf for 300s. The listener already recorded the
                        # owed signature, so without this flag the debt
                        # would wait for hub escalation (consumed-wake
                        # stall, review 2026-07-28); the flag converts it
                        # into a turn the moment the budget window slides.
                        self._pending_wake = True
                        _emit(f"AGORA_DRIVE parked agent={self.agent_id} "
                              f"reason=turn-budget ({self.turn_budget}/h) "
                              "wake=held")
                elif rc == 0:                 # idle timeout OR hub-unreachable
                    if self._pending_wake and self._budget_ok():
                        self._pending_wake = False
                        if self.run_turn():
                            driven += 1
                        continue
                    if self.initiative:
                        if self._chain_step():
                            driven += 1
                else:                         # unexpected: bounded backoff
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
            return 0
        finally:
            self._clear_drive_pid()


def run_drive(*, agent_id: str | None = None, url: str | None = None,
              model: str = DEFAULT_MODEL, max_wait: float = DEFAULT_MAX_WAIT,
              sandbox: str = "enabled", turn_budget: int = DEFAULT_TURN_BUDGET,
              session_rotate: int = DEFAULT_SESSION_ROTATE,
              initiative: bool = False, work_timeout: float = TURN_TIMEOUT,
              work_budget: int = DEFAULT_WORK_BUDGET, force: bool = False,
              once: bool = False, max_turns: int | None = None,
              cwd: Path | None = None) -> int:
    aid, hub = resolve_identity(agent_id, url, Path(cwd) if cwd else Path.cwd())
    sandbox_mode = "" if sandbox == "none" else sandbox
    driver = Driver(aid, hub, model=model, max_wait=max_wait,
                    sandbox=sandbox_mode, turn_budget=turn_budget,
                    session_rotate=session_rotate, initiative=initiative,
                    work_timeout=work_timeout, work_budget=work_budget,
                    force=force)
    return driver.run(once=once, max_turns=max_turns)
