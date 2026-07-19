"""`agora listen` — the session-resident reception primitive.

Turns "a message arrived" into a wake sentinel on stdout for the harness's
wake surface (Cursor monitored shells, Claude asyncRewake exit-2). File mode
tails `<AGORA_HOME>/<id>-inbox.log` read-only from the END (no credentials,
no replay); ws mode reuses the AgoraClient watch core (subscribe, reconnect,
catch-up) seeded at each channel's head for the same no-replay arming.
Sentinels carry hub-validated identifiers ONLY — a doorbell, never the mail
slot (proposal_1 §7). Logic is pure and injectable; IO loops stay thin.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from . import config as _config

DEFAULT_DEBOUNCE = 15.0
DEFAULT_HEARTBEAT = 300.0
_CHANNEL_CAP = 6                       # the wake line stays one short line, always

# Adaptive reception (resource-efficient idle backoff): with --adaptive the
# per-call --max-wait CEILING is chosen by the tool, not the agent, and
# persisted per-seat in listen-<id>.backoff. Because --max-wait is a ceiling
# (a message returns the instant it lands, never at the deadline), widening
# the idle window costs ZERO message latency — it only removes empty loop
# iterations (each = one agent inference). Snap to MIN on a wake so an active
# exchange re-checks tightly; ×2 on each empty timeout up to the cap.
ADAPT_MIN = 60.0                       # tightest window (active): "down to 1mn"
ADAPT_FACTOR = 2.0                     # 60→120→240→480→960→cap: 5-6 idle steps
ADAPT_CAP_DEFAULT = 1200.0             # 20 min; --max-wait overrides the cap
_HUB_UNREACHABLE = 3                   # internal: ws arm gave up (hub down) — do
#                                        NOT widen on it, and map to exit 0
_IMPORTANT_FLAGS = {"to-me", "reply-to-me", "critical", "escalated"}
_FLAG_ORDER = ("to-me", "reply-to-me", "open", "blocked", "critical", "escalated", "dm")

# The sentinel is a single-line, space/comma/'#'-delimited grammar the harness
# monitors with a `^AGORA_WAKE` regex. A channel name is the one identifier in
# it that a peer influences (they pick it at create time), so the doorbell must
# defend its OWN grammar even against a hub that let an unsafe name through or a
# legacy/hand-edited notify file: anything outside this identifier charset —
# newlines (which would forge a second `^AGORA_WAKE` line), the ' ' ',' '#'
# delimiters, control chars, unicode homoglyphs — is neutralized to '?'. ':'
# and '-' stay so real names like `dm:runtime--memory` render intact.
_UNSAFE_CHANNEL = re.compile(r"[^A-Za-z0-9._:-]")


def _safe_channel(name: str) -> str:
    """Clamp a channel name to the sentinel's identifier charset (single-line,
    grammar-safe). Hub-validated slugs pass through unchanged; a crafted or
    legacy name can never forge a sentinel line or inject text into the wake."""
    return _UNSAFE_CHANNEL.sub("?", name)[:64] or "?"


def _emit(line: str) -> None:
    print(line, flush=True)  # harness regexes match line-by-line: never buffer


# The one failure the live integration test surfaced (TEST_REPORT §3 S4) was
# behavioral, not mechanical: an agent backgrounded `agora listen` but forgot
# the output monitor, and the session stayed permanently deaf — the listener
# ran, sentinels flowed, nobody watched. The arming moment therefore SHOUTS
# the requirement once, on STDERR: stdout stays sentinel-only for machine
# consumers, and the banner must never START a line with AGORA_WAKE (Cursor
# matches notify_on_output against both streams, so a careless banner could
# wake what it warns about). One line, because harnesses surface terminal
# output line by line.
ARM_BANNER = (
    'agora listen: wakes reach this session ONLY if THIS shell is monitored '
    'for output matching ^AGORA_WAKE (Cursor Shell tool: notify_on_output, '
    'pattern "^AGORA_WAKE", debounce_ms >= 15000). An unmonitored listener is '
    'SILENT — if you backgrounded this shell without that monitor, stop it '
    'and re-arm WITH the monitor (see BACKGROUND RECEPTION in your agora rule).')


def _announce_armed(source: str, agent_id: str, hub: str, *, once: bool,
                    window: float | None = None) -> None:
    """The arming moment: the monitor warning on stderr FIRST, then the
    machine-readable `armed` sentinel on stdout — the order the arming
    ritual's self-check relies on (the warning is already in the shell output
    an agent reads when it verifies the armed line). --once keeps stderr for
    the digest alone: that stream IS the wake payload Claude reads
    (asyncRewake shows stderr to the model), its exit-2 wake needs no output
    monitor, and the timeout path is contractually silent. `window` (the
    adaptive ceiling in seconds) is appended so the operator and the agent's
    own shell can see the chosen idle window."""
    if not once:
        print(ARM_BANNER, file=sys.stderr, flush=True)
    tail = f" window={int(window)}" if window is not None else ""
    _emit(f"AGORA_LISTEN armed source={source} agent={agent_id} hub={hub}{tail}")


def read_backoff(path: Path, cap: float) -> float:
    """The ceiling to use on THIS call, from the per-seat backoff file. Read
    defensively: missing/corrupt/out-of-range clamps into [MIN, cap] and
    defaults to MIN — corruption always fails toward MORE checks (lower
    latency), never toward deafness. No clock is consulted, so a stale file
    can never mislead the math (only a wake or a clean timeout changes it)."""
    try:
        ceiling = float(json.loads(path.read_text()).get("ceiling", ADAPT_MIN))
    except (OSError, ValueError, TypeError, AttributeError):
        ceiling = ADAPT_MIN
    return max(ADAPT_MIN, min(ceiling, cap))


def next_backoff(current: float, rc: int, cap: float) -> float:
    """The ceiling to persist for the NEXT call. Wake (exit 2) snaps to MIN;
    a clean idle timeout (exit 0) widens ×FACTOR up to the cap; anything else
    (signal, hub-unreachable, error) leaves it unchanged — only a genuine
    'nothing happened for the whole window' earns a widen."""
    if rc == 2:
        return ADAPT_MIN
    if rc == 0:
        return max(ADAPT_MIN, min(current * ADAPT_FACTOR, cap))
    return max(ADAPT_MIN, min(current, cap))


def write_backoff(path: Path, ceiling: float) -> None:
    """Persist the next ceiling atomically (tmp + os.replace) so a crash mid
    write never leaves a torn file the next read would reject."""
    tmp = path.with_suffix(".backoff.tmp")
    with contextlib.suppress(OSError):
        tmp.write_text(json.dumps({"ceiling": ceiling, "updated": int(time.time())}))
        os.replace(tmp, path)


def resolve_identity(agent_id: str | None, url: str | None, cwd: Path) -> tuple[str, str]:
    """(agent_id, hub_url). Id: --as, $AGORA_AGENT_ID, nearest .cursor/mcp.json
    walking UP from cwd; url: --url, $AGORA_URL, same mcp.json, config.json,
    local default. No resolvable id is a loud exit 1."""
    env: dict[str, Any] = {}
    for folder in (cwd, *cwd.parents):
        path = folder / ".cursor" / "mcp.json"
        if path.is_file():
            with contextlib.suppress(ValueError, KeyError, TypeError, OSError):
                found = json.loads(path.read_text())["mcpServers"]["agora"]["env"]
                if isinstance(found, dict):  # malformed configs: keep walking up
                    env = found
                    break
    aid = agent_id or os.environ.get("AGORA_AGENT_ID") or env.get("AGORA_AGENT_ID")
    if not aid:
        raise SystemExit(
            "agora listen: cannot determine the agent id. Pass --as <id>, set "
            "$AGORA_AGENT_ID, or run from a workspace whose .cursor/mcp.json "
            "declares mcpServers.agora.env.AGORA_AGENT_ID.")
    hub = (url or os.environ.get("AGORA_URL") or env.get("AGORA_URL")
           or _config.load_config().get("url") or "http://127.0.0.1:8765")
    return aid, str(hub).rstrip("/")


def resolve_source(source: str, url: str, home: Path, agent_id: str) -> str:
    """auto = file iff the hub is loopback AND the notify file exists."""
    if source in ("file", "ws"):
        return source
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    loopback = host in ("localhost", "::1") or host.startswith("127.")
    return "file" if loopback and (home / f"{agent_id}-inbox.log").exists() else "ws"


def parse_line(raw: str) -> dict[str, Any] | None:
    """One notify line -> event dict; None for junk and liveness-marker lines."""
    try:
        obj = json.loads(raw.strip() or "null")
    except ValueError:
        return None
    if (not isinstance(obj, dict) or "event" in obj  # watch/listen markers
            or not isinstance(obj.get("channel"), str)
            or not isinstance(obj.get("from"), str)):
        return None
    try:
        obj["seq"] = int(obj["seq"])
    except (KeyError, TypeError, ValueError):
        return None
    return obj


def qualifies(event: dict[str, Any], agent_id: str, important_only: bool = False) -> bool:
    """Own messages never wake (hub filters; legacy files may not);
    --important-only means OBLIGATIONS wake, fyi waits: to-me (message `to`
    or a pending ask naming you — the hub folds both into the flag),
    reply-to-me, critical, escalated, AND room-wide open/blocked.

    History, because this flipped twice: 0.10.x dropped bare open/blocked
    (a nine-seat debrief showed broadcast asks in a busy channel waking
    every seat). That narrowing was FALSIFIED in the operator's own test
    (2026-07-14): a room-wide `/ask` woke NOBODY — dead air in the exact
    surface every rule, doc, and skill promised would wake ("obligations,
    not fyi chatter"). An ask that reaches no one is worse than a burst of
    wakes the debounce already coalesces; the code now matches the taught
    contract. Storm control stays where it belongs: debounce (one wake per
    burst), per-ask `to` for precision, and fyi never waking anyone."""
    if event["from"] == agent_id:
        return False
    if not important_only:
        return True
    if str(event.get("status", "")) in ("open", "blocked"):
        return True
    tokens = {t for t in str(event.get("flags", "")).split(",") if t}
    return bool(tokens & _IMPORTANT_FLAGS)


def wake_line(events: list[dict[str, Any]], agent_id: str, *, preview: bool = False) -> str:
    """ONE sentinel per batch, identifiers only; peer-authored titles appear
    only with --preview, neutralized and capped."""
    per_channel: dict[str, int] = {}
    flags: set[str] = set()
    for ev in events:
        chan = ev["channel"]
        per_channel[chan] = max(per_channel.get(chan, 0), int(ev["seq"]))
        flags.update(t for t in str(ev.get("flags", "")).split(",") if t)
        if str(ev.get("status", "")) in ("open", "blocked"):
            flags.add(str(ev["status"]))
        if chan.startswith("dm:"):
            flags.add("dm")
    names = sorted(per_channel)
    parts = [f"AGORA_WAKE agent={agent_id}", f"n={len(events)}",
             "channels=" + ",".join(f"{_safe_channel(c)}#{per_channel[c]}"
                                    for c in names[:_CHANNEL_CAP])]
    if len(names) > _CHANNEL_CAP:
        parts.append(f"more={len(names) - _CHANNEL_CAP}")
    # Self-stating latency: hub mint time rides in the message ULID, so the
    # wake can carry its own hub->wake age. Attribution armor (2026-07-15:
    # a phantom "11-minute latency" cost an hour of forensics, and the woken
    # agent confabulated a cause — an age= stamp would have settled it on
    # sight). Oldest event in the batch = worst-case age; a bare number, so
    # the identifiers-only guarantee holds.
    from .ids import ulid_timestamp
    stamps = [ts for ev in events
              if (ts := ulid_timestamp(str(ev.get("id", "")))) is not None]
    if stamps:
        parts.append(f"age={max(0.0, time.time() - min(stamps)):.1f}s")
    shown = [f for f in _FLAG_ORDER if f in flags]  # enum whitelist, fixed order
    if shown:
        parts.append("flags=" + ",".join(shown))
    if preview:
        title = next((str(ev.get("title") or "") for ev in events if ev.get("title")), "")
        if title:
            from .models import sanitize_text
            from .render import _neutralize
            clean = sanitize_text(_neutralize(title), 80).replace('"', "'")
            parts.append(f'preview="{clean}"')
    return " ".join(parts)


def _owed_snapshot(hub: str, agent_id: str) -> tuple[tuple[int, int] | None, str | None]:
    """One GET /owed -> (counts, signature). counts feed the wake surfaces
    (0079: the woken turn must start knowing what it OWES); the signature is
    the debt as a comparable string (sorted message ids — None when nothing
    is owed or the hub is unknowable). Never raises, never blocks a wake:
    cached key only, short timeout, any failure -> (None, None)."""
    try:
        key = _config.get_cached_key(hub, agent_id)
        if not key:
            return None, None
        import httpx
        # X-Agora-Reception (0098): this /owed poll runs at every arm, so it
        # doubles as the reception heartbeat the hub uses to tell an armed
        # listener from a dead one (DEAF detection). Rides the existing call.
        r = httpx.get(f"{hub.rstrip('/')}/owed",
                      headers={"Authorization": f"Bearer {key}",
                               "X-Agora-Reception": "arm"}, timeout=5.0)
        if r.status_code != 200:
            return None, None
        owed = r.json()
        counts_raw = owed.get("counts", {})
        counts = (int(counts_raw.get("to_answer", 0)),
                  int(counts_raw.get("to_consume", 0)))
        if not (counts[0] or counts[1]):
            return counts, None
        ids = sorted([row.get("id", "") for row in owed.get("to_answer", [])]
                     + [row.get("answer_id", "") for row in owed.get("to_consume", [])])
        return counts, ",".join(ids)
    except Exception:
        return None, None


def _owed_counts(hub: str, agent_id: str) -> tuple[int, int] | None:
    """Back-compat shim over _owed_snapshot (tests and older callers)."""
    return _owed_snapshot(hub, agent_id)[0]


def _sig_path(agent_id: str) -> Path:
    return _config.home() / f"listen-{agent_id}.owedsig"


def _offset_path(agent_id: str) -> Path:
    return _config.home() / f"listen-{agent_id}.offset"


def _read_offset(agent_id: str) -> tuple[int, int] | None:
    """Stored (inode, byte-offset) from the previous --once instance, or
    None. Corruption is treated as absent — a bad offset must never wedge
    reception, only cost a replay-from-END like the pre-0086 behavior."""
    try:
        raw = json.loads(_offset_path(agent_id).read_text())
        return int(raw["inode"]), int(raw["offset"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_offset(agent_id: str, inode: int, offset: int) -> None:
    """Persist the tail position so the next --once instance resumes from
    it instead of END, closing the ~2% per-cycle gap where a NON-obligation
    event (a plain fyi, a gap-missed critical) lands between instances and
    is seen by no one (0086). Best-effort: a write failure just degrades to
    the old resume-from-END, never blocks the wake path."""
    with contextlib.suppress(OSError):
        _offset_path(agent_id).write_text(
            json.dumps({"inode": inode, "offset": offset}))


def _resume_offset(fh, agent_id: str) -> None:
    """Position an open notify-file handle: resume from the stored offset
    when the inode still matches AND the offset is within the current size
    (guards truncation/rotation), else seek to END. The debounce already
    coalesces any replayed burst into one wake, so a slightly stale offset
    costs at most a harmless re-yield, never a storm."""
    stored = _read_offset(agent_id)
    try:
        st = os.fstat(fh.fileno())
    except OSError:
        fh.seek(0, os.SEEK_END)
        return
    if stored is not None and stored[0] == st.st_ino and 0 <= stored[1] <= st.st_size:
        fh.seek(stored[1])  # same file, valid position: replay the blind-spot gap
    else:
        fh.seek(0, os.SEEK_END)  # new/rotated/truncated file, or first arm: no replay


def _record_owed_signature(hub: str, agent_id: str,
                           sig: str | None = None) -> None:
    """Persist the debt signature we last WOKE for, so the arm-time backlog
    check never re-fires for debt a wake already delivered (the seat may
    still be mid-turn settling it when the loop re-arms 5s later)."""
    if sig is None:
        _, sig = _owed_snapshot(hub, agent_id)
    try:
        path = _sig_path(agent_id)
        if sig:
            path.write_text(sig)
        elif path.exists():
            path.unlink()
    except OSError:
        pass


def _backlog_wake_at_arm(hub: str, agent_id: str, *, once: bool) -> int | None:
    """The blind-spot closer: a message that lands BETWEEN two --once listen
    windows (the loop's `sleep 5`, or while the seat is mid-turn) is invisible
    to tail-from-END listeners — no instance ever reads it, and an interactive
    seat has no drive-style sweep to recover it. So arming starts with a debt
    poll: if the seat OWES something and the debt SIGNATURE differs from the
    last one a wake delivered, wake NOW instead of waiting for a fresh event.

    Signature gating (same doctrine as agora drive's sweep): unchanged debt
    never re-wakes — a turn that ran and failed to settle waits for the hub's
    escalation instead of burning a wake per window; settled debt clears the
    signature; any new obligation changes it and wakes. Costs one local HTTP
    GET per arm; any failure means 'no backlog wake', never a blocked arm."""
    if not once:
        return None                      # streaming mode delivers live events
    counts, sig = _owed_snapshot(hub, agent_id)
    if sig is None:
        return None
    last = None
    try:
        last = _sig_path(agent_id).read_text().strip() or None
    except OSError:
        pass
    if sig == last:
        return None
    _record_owed_signature(hub, agent_id, sig)
    counts = counts or (0, 0)
    _emit(f"AGORA_WAKE agent={agent_id} n=0 backlog owed={counts[0] + counts[1]}")
    print(f"AGORA: you OWE {counts[0]} answer(s) and {counts[1]} unconsumed "
          "answer(s) that arrived while no listener was watching. "
          "check_inbox lists them; settle before new work — DO or claim "
          "what is yours, reply where owed, then ack.",
          file=sys.stderr, flush=True)
    return 2


def once_digest(events: list[dict[str, Any]],
                owed: tuple[int, int] | None = None) -> str:
    """--once stderr digest: informational, redacted (counts + channel names).
    Channel names are clamped (Claude shows this stderr to the model verbatim,
    so a crafted name must not smuggle newlines or instructions into it).
    The verb order is deliberate (anti-lurk): DO comes before reply, and ack
    is named last as what it is — a seen-marker that discharges nothing."""
    chans = sorted({_safe_channel(str(ev["channel"])) for ev in events})
    shown = ", ".join(chans[:_CHANNEL_CAP])
    if len(chans) > _CHANNEL_CAP:
        shown += f" (+{len(chans) - _CHANNEL_CAP} more)"
    text = (f"AGORA: you have {len(events)} new message(s) in {shown}. Triage "
            "each: DO or claim what is yours to do; read and use answers to "
            "your own asks; reply where a reply is owed; then ack. Ack means "
            "seen, not done.")
    if owed and (owed[0] or owed[1]):
        text += (f" You currently owe {owed[0]} answer(s) and {owed[1]} "
                 "unconsumed answer(s) to your own asks — check_inbox lists "
                 "them; settle those before new work.")
    return text


def _deliver_wake(batch, agent_id, *, preview: bool, once: bool,
                  hub: str = "") -> int | None:
    """Emit the wake sentinel (+ stderr digest and exit-2 in --once mode)."""
    owed, sig = _owed_snapshot(hub, agent_id) if hub else (None, None)
    line = wake_line(batch, agent_id, preview=preview)
    if owed and (owed[0] or owed[1]):
        # Identifiers-only guarantee holds: a bare integer, no agent text.
        line += f" owed={owed[0] + owed[1]}"
    _emit(line)
    if once:
        # This wake delivers the current debt too (its digest names it), so
        # record the signature: the loop re-arms ~5s later, usually before
        # the woken turn settles anything, and the arm-time backlog check
        # must not re-fire for debt this wake already announced.
        if hub:
            _record_owed_signature(hub, agent_id, sig)
        print(once_digest(batch, owed), file=sys.stderr, flush=True)
        return 2
    return None


class DebounceBatcher:
    """First qualifying event opens a window; the batch pops once it closes —
    exactly one wake per burst. Clock injectable for tests."""

    def __init__(self, debounce_s: float, clock: Callable[[], float] = time.monotonic):
        self._debounce, self._clock = debounce_s, clock
        self._events: list[dict[str, Any]] = []
        self._opened = 0.0

    def add(self, event: dict[str, Any]) -> None:
        if not self._events:
            self._opened = self._clock()
        self._events.append(event)

    def pop_ready(self) -> list[dict[str, Any]] | None:
        if self._events and self._clock() - self._opened >= self._debounce:
            batch, self._events = self._events, []
            return batch
        return None

    @property
    def pending(self) -> bool:
        return bool(self._events)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, someone else's


def _read_lock_pid(path: Path) -> int:
    try:
        return int(path.read_text().strip() or "0")
    except (OSError, ValueError):
        return 0


def acquire_lock(path: Path) -> bool:
    """O_EXCL lock containing our pid; False = a live listener already holds
    it (arming is idempotent). A dead holder's lock is taken over."""
    for _ in range(5):  # bounded takeover retries (racing armers)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            pid = _read_lock_pid(path)
            if pid == 0:
                # Empty/garbled lock: a racing armer sits BETWEEN its O_EXCL
                # create and its pid write. Give it a beat before calling the
                # lock stale, or two "simultaneous" armers can both arm.
                time.sleep(0.05)
                pid = _read_lock_pid(path)
            if pid > 0 and pid_alive(pid):
                return False
            with contextlib.suppress(OSError):
                path.unlink()  # stale: holder is dead (or died pre-write)
    return False


class ListenSignal(BaseException):
    """BaseException: retry loops' `except Exception` must never swallow it."""


def arm_signals() -> None:
    """SIGTERM/SIGINT/SIGHUP -> ListenSignal, so every exit path can emit
    `ended reason=signal`, run the finally, and release the pidfile/lock.
    SIGHUP matters here: a closed terminal (harness teardown) delivers HUP,
    and without it the listener would skip its cleanup and leave a stale
    lock behind. Best-effort (main thread only)."""
    import signal

    def _raise(signum, frame):  # noqa: ARG001
        raise ListenSignal()
    with contextlib.suppress(ValueError):  # not the main thread (in-process tests)
        signal.signal(signal.SIGTERM, _raise)
        signal.signal(signal.SIGINT, _raise)
        with contextlib.suppress(AttributeError):  # SIGHUP is POSIX-only
            signal.signal(signal.SIGHUP, _raise)


def _heartbeat(pid_path: Path) -> None:
    with contextlib.suppress(OSError):
        os.utime(pid_path)
    _emit(f"AGORA_LISTEN heartbeat ts={int(time.time())}")


def follow_lines(fh, path: Path, *, poll: float = 0.5,
                 stop: Callable[[], bool] = lambda: False,
                 pos: dict | None = None) -> Iterator[str | None]:
    """Yield lines appended to an open handle; None = idle tick. Follows by
    NAME: rotation (inode change), truncation (size shrink) and
    delete-then-recreate reopen at 0 — a fresh file is entirely post-arm.

    `pos` (0086): an optional mutable dict the caller owns; kept current
    with {"inode", "offset"} of the tail position so it can persist the
    resume point across --once instances. offset = the byte AFTER the last
    yielded line (buffered-but-unsplit bytes excluded), so a resume neither
    loses a partial trailing line nor re-yields a drained one."""
    inode, buf = os.fstat(fh.fileno()).st_ino, b""
    if pos is not None:
        pos["inode"], pos["offset"] = inode, fh.tell()
    try:
        while not stop():
            chunk = fh.read()
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if pos is not None:
                        pos["offset"] = fh.tell() - len(buf)
                    yield line.decode("utf-8", "replace")
                continue  # drain to EOF before idling
            try:
                st = os.stat(path)
            except OSError:
                # Rotated away and not yet recreated — or transiently
                # unstattable (permission flap, dir being replaced). Either
                # way tail -F semantics apply: keep waiting by NAME rather
                # than dying on a condition that usually heals itself.
                st = None
            if st is not None and (st.st_ino != inode or st.st_size < fh.tell()):
                try:
                    new_fh = open(path, "rb")
                except OSError:
                    # Re-rotation race / transiently unopenable path (e.g. the
                    # name briefly points at something unreadable): keep the OLD
                    # handle and fall through to the idle tick, so a PERSISTENT
                    # failure retries at the poll cadence instead of looping
                    # here without ever yielding — a yield-less spin would
                    # starve heartbeats and burn a core until the path heals.
                    pass
                else:
                    fh.close()
                    fh, inode, buf = new_fh, os.fstat(new_fh.fileno()).st_ino, b""
                    if pos is not None:  # rotated file: new identity, fresh 0
                        pos["inode"], pos["offset"] = inode, 0
                    continue
            yield None
            time.sleep(poll)
    finally:
        fh.close()


def run_file_mode(path: Path, agent_id: str, hub_url: str, pid_path: Path, *,
                  once: bool = False, max_wait: float | None = None,
                  debounce: float = DEFAULT_DEBOUNCE, important_only: bool = False,
                  preview: bool = False, poll: float = 0.5,
                  heartbeat: float = DEFAULT_HEARTBEAT, window: float | None = None,
                  stop: Callable[[], bool] = lambda: False) -> int:
    try:
        fh = open(path, "rb")
    except FileNotFoundError:
        _emit("AGORA_LISTEN ended reason=no-notify-file")
        return 1  # forced file mode with nothing to tail must fail LOUDLY
    # Attach point: resume from the previous instance's persisted offset
    # (0086 — closes the per-cycle blind spot for non-obligation events),
    # falling back to END on a new/rotated/truncated file or first arm.
    _resume_offset(fh, agent_id)
    _announce_armed("file", agent_id, hub_url, once=once, window=window)
    batcher, last_beat = DebounceBatcher(debounce), time.monotonic()
    deadline = (time.monotonic() + max_wait) if (once and max_wait is not None) else None
    # closing(): the early returns below (--once wake, --max-wait deadline)
    # abandon the generator mid-yield; closing it explicitly runs its finally
    # and releases the file handle deterministically instead of leaning on
    # refcount GC (an implementation detail of CPython, not a contract).
    # pos tracks the live tail position; persisted at exit AND per heartbeat
    # so an instance killed by signal mid-window still leaves a resume point
    # (0086). Seed it from the attach position we just seeked to.
    pos: dict[str, int] = {"inode": os.fstat(fh.fileno()).st_ino,
                           "offset": fh.tell()}
    try:
        with contextlib.closing(
                follow_lines(fh, path, poll=poll, stop=stop, pos=pos)) as lines:
            for item in lines:
                if item is not None:
                    event = parse_line(item)
                    if event is not None and qualifies(event, agent_id, important_only):
                        batcher.add(event)
                batch = batcher.pop_ready()
                if batch:
                    code = _deliver_wake(batch, agent_id, preview=preview,
                                         once=once, hub=hub_url)
                    if code is not None:
                        return code
                if heartbeat > 0 and time.monotonic() - last_beat >= heartbeat:
                    last_beat = time.monotonic()
                    _heartbeat(pid_path)
                    _write_offset(agent_id, pos["inode"], pos["offset"])
                # An open debounce window may close just past the deadline: a real
                # wake at the boundary beats a punctual empty exit.
                if deadline is not None and not batcher.pending and time.monotonic() >= deadline:
                    return 0
        return 0  # stop() asked us to end (in-process tests)
    finally:
        # Persist the final tail position for the NEXT --once instance to
        # resume from — the whole point of 0086.
        _write_offset(agent_id, pos["inode"], pos["offset"])


async def run_ws_mode(url: str, key: str, agent_id: str, pid_path: Path, *,
                      once: bool = False, max_wait: float | None = None,
                      debounce: float = DEFAULT_DEBOUNCE, important_only: bool = False,
                      preview: bool = False, notify_file: str | None = None,
                      heartbeat: float = DEFAULT_HEARTBEAT,
                      window: float | None = None) -> int:
    from .client import AgoraClient
    from .client.client import AgoraError  # not re-exported by the package
    from .hub.notify_sink import notify_line
    if notify_file:  # a bad path must fail at ARM time, not swallow the first wake
        try:
            open(notify_file, "a").close()
        except OSError as exc:
            raise SystemExit(f"agora listen: cannot append to --notify-file "
                             f"{notify_file}: {exc}") from exc
    deadline = (time.monotonic() + max_wait) if (once and max_wait is not None) else None
    client = AgoraClient(url, key, agent_id=agent_id)
    beats: asyncio.Task | None = None
    try:
        delay = 0.5
        while True:  # the hub may be down: arming waits (and retries) for it
            try:
                rows = await client.list_channels()
                # Head-seeded cursors: the ws twin of seek-to-END (no replay);
                # reconnect catch-up then covers outage windows only.
                await client.connect([r["name"] for r in rows if r["member"]], since={
                    r["name"]: int(r.get("last_seq") or 0) for r in rows if r["member"]})
                break
            except Exception as exc:
                if isinstance(exc, AgoraError) and exc.status_code in (401, 403):
                    raise SystemExit(f"agora listen: hub rejected the key for "
                                     f"'{agent_id}' ({exc}); fix ~/.agora/keys.json") from exc
                if deadline is not None and time.monotonic() >= deadline:
                    _emit("AGORA_LISTEN ended reason=hub-unreachable")
                    return _HUB_UNREACHABLE  # exit 0, but never widen on it
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
        _announce_armed("ws", agent_id, url, once=once, window=window)
        if heartbeat > 0:
            async def _beats() -> None:
                while True:
                    await asyncio.sleep(heartbeat)
                    _heartbeat(pid_path)
            beats = asyncio.create_task(_beats())
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            envelopes = await client.inbox.wait(timeout=remaining)
            if not envelopes:
                if deadline is not None and time.monotonic() >= deadline:
                    return 0  # --max-wait timeout: silent, exit 0
                continue
            await asyncio.sleep(debounce)  # let the burst settle into one wake
            envelopes += client.inbox.drain()
            lines = [notify_line(env) for env in envelopes]
            if notify_file:  # byte-compatible with hub-written files; best-effort
                with contextlib.suppress(OSError):  # never lose a wake over it
                    with open(notify_file, "a") as nf:
                        nf.writelines(line + "\n" for line in lines)
            events = [json.loads(line) for line in lines]
            batch = [ev for ev in events if qualifies(ev, agent_id, important_only)]
            if batch:
                code = _deliver_wake(batch, agent_id, preview=preview,
                                     once=once, hub=url)
                if code is not None:
                    return code
    finally:
        if beats:
            beats.cancel()
        with contextlib.suppress(Exception):
            await client.close()


def run_listen(*, agent_id: str | None = None, url: str | None = None,
               source: str = "auto", once: bool = False, max_wait: float | None = None,
               debounce: float = DEFAULT_DEBOUNCE, important_only: bool = False,
               preview: bool = False, notify_file: str | None = None,
               lock: str | None = None, heartbeat: float = DEFAULT_HEARTBEAT,
               poll: float = 0.5, adaptive: bool = False,
               idle_nudge: float = 0.0,  # accepted no-op since 0.10.5 (see cli)
               signal_passthrough: bool = False,
               cwd: Path | None = None) -> int:
    aid, hub = resolve_identity(agent_id, url, Path(cwd) if cwd else Path.cwd())
    home = _config.home()
    src = resolve_source(source, hub, home, aid)
    lock_path = Path(lock).expanduser() if lock else home / f"listen-{aid}.lock"
    pid_path = home / f"listen-{aid}.pid"
    backoff_path = home / f"listen-{aid}.backoff"
    # The lock exists to keep TWO PERSISTENT listeners from double-waking a
    # session — that is its only job. A --once reception-loop call needs no
    # lock: file mode is read-only tailing, so a harness-orphaned prior call
    # is harmless (it delivers to a dead terminal), and locking made the LIVE
    # iteration bounce `already-armed` while the orphan held the lock — the
    # starvation the fleet hit. So --once locks ONLY when a lock path is passed
    # explicitly (Claude's hook-armed single-shots do, to dedup duplicate hook
    # firings); the Cursor reception loop passes none and never contends.
    want_lock = (not once) or (lock is not None)
    if want_lock and not acquire_lock(lock_path):
        _emit("AGORA_LISTEN ended reason=already-armed")
        return 0  # idempotent arming: the live instance keeps its lock/pidfile
    # Adaptive: the per-call ceiling comes from the backoff file (--max-wait is
    # the cap, default 20 min). A message returns the instant it lands, so a
    # wide idle window adds no latency — only fewer empty inferences.
    cap = max_wait if (adaptive and max_wait) else ADAPT_CAP_DEFAULT
    effective_wait = read_backoff(backoff_path, cap) if adaptive else max_wait
    shared = dict(once=once, max_wait=effective_wait, debounce=debounce,
                  important_only=important_only, preview=preview,
                  heartbeat=heartbeat, window=effective_wait if adaptive else None)
    try:
        # Everything after the lock is acquired lives inside the try: a failure
        # as early as the pidfile write must still release the lock in the
        # finally, or a crashed armer would block re-arming until the stale-pid
        # takeover notices the dead holder.
        # signal_passthrough: when an EMBEDDING loop (agora drive) owns the
        # process, converting SIGTERM into a return-0 here would turn "kill
        # the driver" into "driver survives, spawns forever" — the caller
        # must see the signal, so we leave the default handlers in place.
        if not signal_passthrough:
            arm_signals()
        pid_path.write_text(str(os.getpid()))
        # Backlog check BEFORE waiting: debt that landed in a listener blind
        # spot (between --once windows) wakes at arm time or never.
        backlog_rc = _backlog_wake_at_arm(hub, aid, once=once)
        if backlog_rc is not None:
            return backlog_rc
        if src == "file":
            rc = run_file_mode(home / f"{aid}-inbox.log", aid, hub, pid_path,
                               poll=poll, **shared)
        else:
            key = _config.resolve_key(hub, aid)  # cached, else self-register, else exit 1
            rc = asyncio.run(run_ws_mode(hub, key, aid, pid_path,
                                         notify_file=notify_file, **shared))
        # Persist the next ceiling BEFORE mapping the internal code: a wake
        # (2) snaps to MIN, a clean idle timeout (0) widens, unreachable (3)
        # leaves it be. Only reached on a normal return — a signal skips it.
        if adaptive:
            write_backoff(backoff_path, next_backoff(effective_wait, rc, cap))
        return 0 if rc == _HUB_UNREACHABLE else rc
    except ListenSignal:
        _emit("AGORA_LISTEN ended reason=signal")
        return 0
    except SystemExit as exc:
        # Post-lock arm failures raised as SystemExit (no resolvable key, a bad
        # --notify-file, hub auth rejection) already print their cause to
        # stderr; add the machine-readable tombstone so a monitored shell (and
        # the arming ritual's self-check) sees the ear end, not just prose.
        if exc.code not in (0, None):
            with contextlib.suppress(Exception):
                _emit("AGORA_LISTEN ended reason=error")
        raise
    except Exception:
        # An unexpected crash must leave a machine-readable tombstone in the
        # monitored shell (the proposal's "ended on any exit path"): without
        # it the terminal shows a bare traceback and a reader cannot tell a
        # dead ear from a quiet one until the next heartbeat never comes.
        # Suppress emit failures (e.g. the crash IS a broken stdout) and
        # re-raise unchanged so cli.py's error reporting stays intact.
        with contextlib.suppress(Exception):
            _emit("AGORA_LISTEN ended reason=error")
        raise
    finally:
        # Without the lock, concurrent --once tailers share the pidfile: a
        # harness-orphaned prior call must NOT unlink the live one's pidfile
        # on its way out (that would blank `agora status` for a live seat), so
        # remove it only if it still holds OUR pid. The lock is only ours to
        # remove when we actually acquired it.
        with contextlib.suppress(OSError):
            if _read_lock_pid(pid_path) == os.getpid():
                pid_path.unlink()
        if want_lock:
            with contextlib.suppress(OSError):
                lock_path.unlink()
