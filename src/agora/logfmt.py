"""Small, terminal-friendly formatting for operator-facing Agora logs."""

from __future__ import annotations

from datetime import datetime
import os
import sys
import threading
from typing import TextIO


_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_CYAN = "\x1b[36m"
_BLUE = "\x1b[34m"
_MAGENTA = "\x1b[35m"
_YELLOW = "\x1b[33m"
_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_EMIT_LOCK = threading.Lock()


def _timestamp() -> str:
    """Local, timezone-aware wall clock with millisecond precision."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _use_color(stream: TextIO) -> bool:
    return ("NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
            and bool(getattr(stream, "isatty", lambda: False)()))


def _colored_record(line: str) -> str:
    """Color the trusted source/outcome fields, never peer-authored text."""
    source, separator, rest = line.partition(" ")
    trusted = line.split(" | ", 1)[0]
    source_color = _BLUE
    if source in {"AGORA_DRIVE", "AGORA_RUNNER"}:
        source_color = _CYAN
    elif source == "AGORA_HUB":
        source_color = _MAGENTA
    elif source == "AGORA_WAKE":
        source_color = _YELLOW
    detail_color = ""
    if ("status=error" in trusted or "status=failed" in trusted
            or "status=rejected" in trusted or "state=failed" in trusted
            or "state=rejected" in trusted or " warn" in trusted
            or "reason=error" in trusted or "unreachable" in trusted):
        detail_color = _RED
    elif ("status=ok" in trusted or "status=running" in trusted
          or "state=running" in trusted
          or "event=ready" in trusted or "event=seat-registered" in trusted
          or "event=channel-created" in trusted
          or "event=channel-joined" in trusted
          or "event=role-changed" in trusted
          or "event=agent-spawned" in trusted or " recovered" in trusted):
        detail_color = _GREEN
    elif ("state=backoff" in trusted or "state=parked" in trusted
          or "event=channel-left" in trusted or "event=channel-archived" in trusted
          or "event=seat-retired" in trusted or "event=stopped" in trusted
          or "event=agent-decommissioned" in trusted
          or "state=stopped" in trusted
          or "BLOCKED" in trusted):
        detail_color = _YELLOW
    tail = f"{detail_color}{rest}{_RESET}" if detail_color and rest else rest
    return f"{source_color}{source}{_RESET}{separator}{tail}"


def emit_log(line: str, *, stream: TextIO | None = None) -> None:
    """Print records with a timestamp at the beginning of every physical line."""
    target = stream or sys.stdout
    color = _use_color(target)
    records = str(line).splitlines() or [""]
    rendered = []
    for record in records:
        stamp = f"[{_timestamp()}]"
        if color:
            record = _colored_record(record)
            separator = f"{_DIM} | {_RESET}"
        else:
            separator = " | "
        rendered.append(f"{stamp}{separator}{record}")
    with _EMIT_LOCK:
        print("\n".join(rendered), file=target, flush=True)
