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
    'pattern "^AGORA_WAKE", debounce_ms >= 5000). An unmonitored listener is '
    'SILENT — if you backgrounded this shell without that monitor, kill it '
    'and re-arm WITH the monitor (see the ARMING RITUAL in your agora rule).')


def _announce_armed(source: str, agent_id: str, hub: str, *, once: bool) -> None:
    """The arming moment: the monitor warning on stderr FIRST, then the
    machine-readable `armed` sentinel on stdout — the order the arming
    ritual's self-check relies on (the warning is already in the shell output
    an agent reads when it verifies the armed line). --once keeps stderr for
    the digest alone: that stream IS the wake payload Claude reads
    (asyncRewake shows stderr to the model), its exit-2 wake needs no output
    monitor, and the timeout path is contractually silent."""
    if not once:
        print(ARM_BANNER, file=sys.stderr, flush=True)
    _emit(f"AGORA_LISTEN armed source={source} agent={agent_id} hub={hub}")


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
    --important-only narrows to obligations and attention flags."""
    if event["from"] == agent_id:
        return False
    if not important_only:
        return True
    tokens = {t for t in str(event.get("flags", "")).split(",") if t}
    return bool(tokens & _IMPORTANT_FLAGS) or str(event.get("status")) in ("open", "blocked")


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


def once_digest(events: list[dict[str, Any]]) -> str:
    """--once stderr digest: informational, redacted (counts + channel names).
    Channel names are clamped (Claude shows this stderr to the model verbatim,
    so a crafted name must not smuggle newlines or instructions into it)."""
    chans = sorted({_safe_channel(str(ev["channel"])) for ev in events})
    shown = ", ".join(chans[:_CHANNEL_CAP])
    if len(chans) > _CHANNEL_CAP:
        shown += f" (+{len(chans) - _CHANNEL_CAP} more)"
    return (f"AGORA: you have {len(events)} new message(s) in {shown}. Review and "
            "decide what needs action; reply where a reply is owed; ack what you "
            "have seen.")


def _deliver_wake(batch, agent_id, *, preview: bool, once: bool) -> int | None:
    """Emit the wake sentinel (+ stderr digest and exit-2 in --once mode)."""
    _emit(wake_line(batch, agent_id, preview=preview))
    if once:
        print(once_digest(batch), file=sys.stderr, flush=True)
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
    """SIGTERM/SIGINT -> ListenSignal, so every exit path can emit
    `ended reason=signal` and clean up. Best-effort (main thread only)."""
    import signal

    def _raise(signum, frame):  # noqa: ARG001
        raise ListenSignal()
    with contextlib.suppress(ValueError):  # not the main thread (in-process tests)
        signal.signal(signal.SIGTERM, _raise)
        signal.signal(signal.SIGINT, _raise)


def _heartbeat(pid_path: Path) -> None:
    with contextlib.suppress(OSError):
        os.utime(pid_path)
    _emit(f"AGORA_LISTEN heartbeat ts={int(time.time())}")


def follow_lines(fh, path: Path, *, poll: float = 0.5,
                 stop: Callable[[], bool] = lambda: False) -> Iterator[str | None]:
    """Yield lines appended to an open (END-seeked) handle; None = idle tick.
    Follows by NAME: rotation (inode change), truncation (size shrink) and
    delete-then-recreate reopen at 0 — a fresh file is entirely post-arm."""
    inode, buf = os.fstat(fh.fileno()).st_ino, b""
    try:
        while not stop():
            chunk = fh.read()
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
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
                    continue
            yield None
            time.sleep(poll)
    finally:
        fh.close()


def run_file_mode(path: Path, agent_id: str, hub_url: str, pid_path: Path, *,
                  once: bool = False, max_wait: float | None = None,
                  debounce: float = DEFAULT_DEBOUNCE, important_only: bool = False,
                  preview: bool = False, poll: float = 0.5,
                  heartbeat: float = DEFAULT_HEARTBEAT,
                  stop: Callable[[], bool] = lambda: False) -> int:
    try:
        fh = open(path, "rb")
    except FileNotFoundError:
        _emit("AGORA_LISTEN ended reason=no-notify-file")
        return 1  # forced file mode with nothing to tail must fail LOUDLY
    fh.seek(0, os.SEEK_END)  # attach point: no history replay, ever
    _announce_armed("file", agent_id, hub_url, once=once)
    batcher, last_beat = DebounceBatcher(debounce), time.monotonic()
    deadline = (time.monotonic() + max_wait) if (once and max_wait is not None) else None
    # closing(): the early returns below (--once wake, --max-wait deadline)
    # abandon the generator mid-yield; closing it explicitly runs its finally
    # and releases the file handle deterministically instead of leaning on
    # refcount GC (an implementation detail of CPython, not a contract).
    with contextlib.closing(follow_lines(fh, path, poll=poll, stop=stop)) as lines:
        for item in lines:
            if item is not None:
                event = parse_line(item)
                if event is not None and qualifies(event, agent_id, important_only):
                    batcher.add(event)
            batch = batcher.pop_ready()
            if batch:
                code = _deliver_wake(batch, agent_id, preview=preview, once=once)
                if code is not None:
                    return code
            if heartbeat > 0 and time.monotonic() - last_beat >= heartbeat:
                last_beat = time.monotonic()
                _heartbeat(pid_path)
            # An open debounce window may close just past the deadline: a real
            # wake at the boundary beats a punctual empty exit.
            if deadline is not None and not batcher.pending and time.monotonic() >= deadline:
                return 0
    return 0  # stop() asked us to end (in-process tests)


async def run_ws_mode(url: str, key: str, agent_id: str, pid_path: Path, *,
                      once: bool = False, max_wait: float | None = None,
                      debounce: float = DEFAULT_DEBOUNCE, important_only: bool = False,
                      preview: bool = False, notify_file: str | None = None,
                      heartbeat: float = DEFAULT_HEARTBEAT) -> int:
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
                    return 0
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
        _announce_armed("ws", agent_id, url, once=once)
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
                code = _deliver_wake(batch, agent_id, preview=preview, once=once)
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
               poll: float = 0.5, cwd: Path | None = None) -> int:
    aid, hub = resolve_identity(agent_id, url, Path(cwd) if cwd else Path.cwd())
    home = _config.home()
    src = resolve_source(source, hub, home, aid)
    lock_path = Path(lock).expanduser() if lock else home / f"listen-{aid}.lock"
    pid_path = home / f"listen-{aid}.pid"
    if not acquire_lock(lock_path):
        _emit("AGORA_LISTEN ended reason=already-armed")
        return 0  # idempotent arming: the live instance keeps its lock/pidfile
    shared = dict(once=once, max_wait=max_wait, debounce=debounce,
                  important_only=important_only, preview=preview, heartbeat=heartbeat)
    try:
        # Everything after the lock is acquired lives inside the try: a failure
        # as early as the pidfile write must still release the lock in the
        # finally, or a crashed armer would block re-arming until the stale-pid
        # takeover notices the dead holder.
        arm_signals()
        pid_path.write_text(str(os.getpid()))
        if src == "file":
            return run_file_mode(home / f"{aid}-inbox.log", aid, hub, pid_path,
                                 poll=poll, **shared)
        key = _config.resolve_key(hub, aid)  # cached, else self-register, else exit 1
        return asyncio.run(run_ws_mode(hub, key, aid, pid_path,
                                       notify_file=notify_file, **shared))
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
        for path in (pid_path, lock_path):
            with contextlib.suppress(OSError):
                path.unlink()
