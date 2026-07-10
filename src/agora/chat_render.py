"""Visual layer for `agora chat` — pure string-building, no I/O.

One renderer produces every message block (history, live envelopes, reads),
so the layout is defined once: a dim separator, a colored header line
(time, sender, seq, status badge, trust flags), an optional bold title, and
the body wrapped to the terminal and capped at a few lines with an explicit
"/read" hint — long agent reports must not wall the room. Colors degrade to
plain text when stdout is not a tty.
"""

from __future__ import annotations

import re
import shutil
import textwrap
import time
from typing import Any

_PALETTE = ["36", "32", "33", "35", "34", "96", "92", "93", "95", "94"]

# Agent-authored text reaches the OPERATOR'S TERMINAL here. Strip every
# control character except newline and tab (incl. ESC, CR, C1): otherwise an
# agent could emit ANSI sequences that spoof another sender's line, overwrite
# text, or hide an obligation — attribution is this surface's one trust
# anchor (security review M1). Titles are hub-sanitized; bodies, file
# content, and descriptions are verbatim by design, so the render strips.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def safe(text: Any) -> str:
    return _CONTROL.sub("", str(text))

# Status is the message's obligation class — color it accordingly.
_STATUS_CODES = {"open": "33;1", "blocked": "31;1", "reply": "32",
                 "resolved": "36", "fyi": "2"}

BODY_MAX_LINES = 10


class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.enabled else text

    def sender(self, name: str) -> str:
        return self._wrap(_PALETTE[hash(name) % len(_PALETTE)] + ";1", name)

    def status(self, value: str) -> str:
        return self._wrap(_STATUS_CODES.get(value, "0"), value)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def red(self, text: str) -> str:
        return self._wrap("31;1", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def magenta(self, text: str) -> str:
        return self._wrap("35;1", text)

    def on_dark(self, text: str) -> str:
        return self._wrap("48;5;236;97", text) if self.enabled else text


def term_width() -> int:
    return min(shutil.get_terminal_size((100, 24)).columns, 110)


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def dm_peer(channel: str, me: str) -> str | None:
    """`dm:a--b` -> the other participant, or None for ordinary channels."""
    if not channel.startswith("dm:"):
        return None
    ids = channel[3:].split("--")
    others = [i for i in ids if i != me]
    return others[0] if others else me


def wrap_body(text: str, width: int, indent: str = "  ",
              max_lines: int = BODY_MAX_LINES) -> tuple[list[str], int]:
    """Wrap to the terminal, keep paragraph breaks, cap the height.
    Returns (visible lines, hidden line count)."""
    text = safe(text)
    lines: list[str] = []
    for para in text.splitlines():
        lines.extend(textwrap.wrap(para, max(20, width - len(indent)),
                                   break_long_words=False,
                                   break_on_hyphens=False) or [""])
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) <= max_lines:
        return [indent + l for l in lines], 0
    return [indent + l for l in lines[:max_lines]], len(lines) - max_lines


def message_block(s: Style, *, sender: str, seq: int, status: str,
                  created_at: float, title: str = "", body: str | None = None,
                  body_bytes: int = 0, flags: str = "", ask_progress: str = "",
                  me: str = "", channel: str = "", show_channel: bool = False,
                  max_lines: int = BODY_MAX_LINES) -> str:
    """One message, one layout — used for history, live traffic, and reads."""
    width = term_width()
    sender, title, channel = safe(sender), safe(title), safe(channel)
    ts = time.strftime("%H:%M", time.localtime(created_at))
    peer = dm_peer(channel, me)

    header = f"{s.dim(ts)} {s.sender(sender)} {s.dim(f'#{seq}')} {s.status(status)}"
    if peer is not None:
        header = f"{s.magenta('DM')} {header}"
    elif show_channel and channel:
        header += f" {s.dim('in')} {s.cyan(channel)}"
    if flags:
        header += f"  {s.yellow(f'[{flags}]')}"
    if ask_progress:
        header += f"  {s.yellow(f'asks {ask_progress}')}"

    lines = [s.dim("─" * width), header]
    body_text = body or ""
    if title and not body_text.strip().startswith(title.rstrip("…")):
        lines.append(f"  {s.bold(title)}")
    if body_text:
        visible, hidden = wrap_body(body_text, width, max_lines=max_lines)
        lines.extend(visible)
        if hidden:
            lines.append(s.dim(f"  ⋯ {hidden} more line(s) — /read {seq}"))
    elif body_bytes:
        lines.append(s.dim(f"  ({body_bytes} bytes — /read {seq})"))
    return "\n".join(lines)


def channel_table(s: Style, channels: list[dict[str, Any]],
                  unread: dict[str, int], current: str | None, me: str = "",
                  now: float | None = None) -> str:
    """The room directory: channels first, DMs as their own section."""
    now = now or time.time()
    rooms = [c for c in channels if not c["name"].startswith("dm:")]
    dms = [c for c in channels if c["name"].startswith("dm:")]

    header = f"  {'':1} {'channel':<24} {'':7} {'members':>7} {'msgs':>6} {'last':>5} {'unread':>7}"
    lines = [s.dim(header)]

    def row(c: dict[str, Any], display: str) -> str:
        display = safe(display)
        marker = ">" if c["name"] == current else (" " if c["member"] else "·")
        vis = "private" if c["private"] else "public"
        members = c.get("member_count")
        msgs = c.get("last_seq")
        age = fmt_age(now - c["last_at"]) if c.get("last_at") else "-"
        n = unread.get(c["name"], 0)
        name = display[:24]
        if c["name"] == current:
            name_s = s.cyan(f"{name:<24}")
        elif not c["member"]:
            name_s = s.dim(f"{name:<24}")
        else:
            name_s = f"{name:<24}"
        n_s = s.yellow(f"{n:>7}") if n else s.dim(f"{n:>7}")
        return (f"  {marker} {name_s} {s.dim(f'{vis:<7}')} "
                f"{members if members is not None else '?':>7} "
                f"{msgs if msgs is not None else '?':>6} {age:>5} {n_s}")

    for c in rooms:
        lines.append(row(c, c["name"]))
    if dms:
        lines.append(s.dim("  ── direct messages ──"))
        for c in dms:
            lines.append(row(c, f"@{dm_peer(c['name'], me)}"))
    lines.append(s.dim("  > = current   · = public, not joined   (/switch NAME, /dm PEER TEXT)"))
    return "\n".join(lines)


def file_block(s: Style, *, path: str, content: str, version: int,
               updated_by: str, size_bytes: int, channel: str) -> str:
    """A deliberate file read (`/fs PATH`): header card + full wrapped content.
    No height cap — unlike chat traffic, the reader explicitly asked for the
    whole document."""
    width = term_width()
    path, updated_by, channel = safe(path), safe(updated_by), safe(channel)
    header = (f"{s.cyan('FILE')} {s.bold(path)} {s.dim('·')} "
              f"{s.dim(f'v{version} · by ')}{s.sender(updated_by)} "
              f"{s.dim(f'· {size_bytes} bytes · in {channel}')}")
    visible, _ = wrap_body(content, width, max_lines=10_000)
    return "\n".join([s.dim("─" * width), header, s.dim("─" * width), *visible])


def file_event_line(s: Style, *, sender: str, title: str, channel: str,
                    current: str | None,
                    data: dict[str, Any] | None = None) -> str:
    """kind=fs audit messages are change signals, not conversation — render
    one dim line with the edit's size (so "signed a line" and "rewrote the
    doc" look different at a glance) and the retrieval hint."""
    sender, title, channel = safe(sender), safe(title), safe(channel)
    path = title.split(" ", 1)[1] if " " in title else title
    where = "" if channel == current else f" [{channel}]"
    op = title.split(" ")[0].removeprefix("fs:")
    detail = ""
    if data and data.get("version") is not None:
        detail = f" v{data['version']}"
        if data.get("size_bytes") is not None:
            detail += f" · {data['size_bytes']}B"
    return (s.dim(f"  ⬡{where} ") + s.sender(sender)
            + s.dim(f" {op} {path}{detail} — /fs {path}"))


def file_history_table(s: Style, path: str, events: list[dict[str, Any]]) -> str:
    """The file's life, one row per edit, with size deltas — answers "who
    wrote it and who merely amended it" without reading any content."""
    lines = [s.bold(path) + s.dim(" — edit history")]
    prev = 0
    for m in events:
        d = m.get("data") or {}
        size = d.get("size_bytes", 0)
        delta = size - prev
        prev = size
        ts = time.strftime("%H:%M", time.localtime(m["created_at"]))
        sign = "+" if delta >= 0 else ""
        note = "created" if d.get("version") == 1 else f"{sign}{delta}B"
        pad = " " * max(1, 12 - len(m["sender"]))
        lines.append(f"  {s.dim(ts)} v{d.get('version', '?'):<3} "
                     f"{s.sender(m['sender'])}{pad}"
                     f"{d.get('op', 'put'):<7} {size:>7}B  {s.yellow(note)}")
    lines.append(s.dim(f"  /fs {path}@N to read any archived version"))
    return "\n".join(lines)


def presence_rows(s: Style, rows: list[dict[str, Any]],
                  now: float | None = None) -> str:
    now = now or time.time()
    color = {"idle": s.cyan, "working": s.yellow,
             "active": s.bold, "offline": s.dim}
    lines = []
    for r in rows:
        age = fmt_age(now - r["updated_at"]) if r["updated_at"] else "never"
        state = color.get(r["state"], s.dim)(f"{safe(r['state']):<8}")
        agent_id = safe(r["agent_id"])
        lines.append(f"  {s.sender(agent_id)}"
                     f"{' ' * max(1, 17 - len(agent_id))}{state} "
                     f"{s.dim(f'(updated {age})')}")
    return "\n".join(lines)
