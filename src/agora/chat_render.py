"""Visual layer for `agora chat` — pure string-building, no I/O.

One renderer produces every message block (history, live envelopes, reads),
so the layout is defined once: a dim separator, a colored header line
(time, sender, seq, status badge, trust flags), an optional bold title, and
the body wrapped to the terminal. Preview surfaces (history, live traffic)
cap the body at a few lines with an explicit "/read" hint — long agent
reports must not wall the room; deliberate reads (/read, /fs PATH) render
uncapped (max_lines=None) — the reader explicitly asked for the whole
thing. Colors degrade to plain text when stdout is not a tty.
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

# Preview height for the HUMAN chat surface only (history + live traffic):
# enough to judge relevance, small enough that a busy room stays scannable —
# the full body is one /read away. Agent surfaces (render.py, read_message)
# are unaffected: agents always get full bodies on deliberate reads.
BODY_MAX_LINES = 4


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


def asks_from(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The structured asks declared in a message/envelope `data` payload —
    the machine-tracked questions the 'asks N/M' badge counts. Tolerant by
    design (mirrors the hub's asks_of): only well-formed entries render; a
    malformed payload must never break the human's chat surface."""
    asks = (data or {}).get("asks")
    if not isinstance(asks, list):
        return []
    return [a for a in asks if isinstance(a, dict) and a.get("id") is not None]


def ask_ref(ref: str, ask_id: str) -> str:
    """Insert an ask id into a (possibly channel-qualified) message ref:
    '727' -> '727:1', '7@dm:a--b' -> '7:1@dm:a--b'. The ask id rides the
    LOCAL part because channel names may themselves contain ':' (dm:...)."""
    local, _, at = ref.partition("@")
    return f"{local}:{ask_id}" + (f"@{at}" if at else "")


def ask_lines(s: Style, asks: list[dict[str, Any]],
              pending: list[str] | None, ref: str, width: int,
              declined: list[str] | None = None) -> list[str]:
    """Render a message's asks — each an individually answerable obligation
    item, one bounded line each (protocol caps: 20 asks, short text).

    `pending` is the list of still-unanswered ids when the caller knows it
    (live envelopes carry it; deliberate reads fetch it from the digest),
    or None when unknown (plain history rows) — state marks never guess:
    ○ = pending (yellow, it is owed work), ✓ = answered (dim),
    ✗ = declined (0153 — discharged by refusal, which is NOT an answer),
    · = unknown.
    The trailing hint prints the exact '/reply REF:ID' that answers the
    first open ask, so discharging never requires reading protocol docs."""
    open_ids = None if pending is None else {str(p) for p in pending}
    refused = {str(d) for d in (declined or [])}
    lines: list[str] = []
    hint_id = None
    for a in asks:
        aid = safe(str(a.get("id", "")))
        text = safe(str(a.get("text", "")))
        assignee = safe(str(a.get("assignee", "") or ""))
        if open_ids is None:
            mark, style = "·", None
        elif aid in open_ids:
            mark, style = "○", s.yellow
        elif aid in refused:
            mark, style = "✗", s.dim
        else:
            mark, style = "✓", s.dim
        if hint_id is None and (open_ids is None or aid in open_ids):
            hint_id = aid
        raw = f"{mark} [{aid}] {text}" + (f" → {assignee}" if assignee else "")
        if len(raw) > width - 2:
            raw = raw[:width - 3] + "…"
        lines.append("  " + (style(raw) if style else raw))
    if hint_id is not None:
        lines.append(s.dim(f"  ↳ /reply {ask_ref(ref, hint_id)} TEXT "
                           f"answers [{hint_id}]"))
    return lines


def wrap_body(text: str, width: int, indent: str = "  ",
              max_lines: int | None = BODY_MAX_LINES,
              style: "Style | None" = None) -> tuple[list[str], int]:
    """Render a message body to the terminal, markdown-aware (tables
    aligned, headings styled, lists hanging-indented — see md_render.py;
    operator finding 2026-07-14: raw wrapping turned agents' status tables
    into pipe soup). Keeps paragraph breaks, caps the height
    (max_lines=None removes the cap — for deliberate reads).
    Returns (visible lines, hidden line count)."""
    from .md_render import render_markdown

    text = safe(text)
    lines = render_markdown(text, max(20, width - len(indent)), style)
    if max_lines is None or len(lines) <= max_lines:
        return [indent + l for l in lines], 0
    return [indent + l for l in lines[:max_lines]], len(lines) - max_lines


def message_block(s: Style, *, sender: str, seq: int, status: str,
                  created_at: float, title: str = "", body: str | None = None,
                  body_bytes: int = 0, flags: str = "", ask_progress: str = "",
                  me: str = "", channel: str = "", show_channel: bool = False,
                  max_lines: int | None = BODY_MAX_LINES,
                  asks: list[dict[str, Any]] | None = None,
                  pending_asks: list[str] | None = None,
                  declined_asks: list[str] | None = None) -> str:
    """One message, one layout — used for history, live traffic, and reads.

    `show_channel=True` means the block renders away from its home room
    (a DM or critical landing while you watch another channel). A seq is
    only unique per channel, so a bare '#7' is ambiguous exactly there —
    header and every hint then carry the qualified ref 'SEQ@CHANNEL',
    which /read and /reply resolve from any room. The printed hint must
    always fetch the message it decorates (field bug: a DM's '/read 7'
    hint read the current room's unrelated #7 instead).

    `asks` render as their own list below the body, never capped: they are
    the message's machine-tracked obligations — exactly what the 'asks N/M'
    badge counts — and were previously invisible in chat (field finding:
    an operator could not tell WHAT an open message actually asked)."""
    width = term_width()
    sender, title, channel = safe(sender), safe(title), safe(channel)
    ts = time.strftime("%H:%M", time.localtime(created_at))
    peer = dm_peer(channel, me)
    # DM refs teach the short PEER:SEQ form (a DM has ONE peer, so
    # 'artemis:3' beats '3@dm:artemis--laurent'); both forms resolve.
    if show_channel and channel:
        ref = f"{peer}:{seq}" if peer else f"{seq}@{channel}"
    else:
        ref = str(seq)

    header = f"{s.dim(ts)} {s.sender(sender)} {s.dim(f'#{ref}')} {s.status(status)}"
    if peer is not None:
        header = f"{s.magenta('DM')} {header}"
    if flags:
        header += f"  {s.yellow(f'[{flags}]')}"
    if ask_progress:
        header += f"  {s.yellow(f'asks {ask_progress}')}"

    lines = [s.dim("─" * width), header]
    body_text = body or ""
    if title and not body_text.strip().startswith(title.rstrip("…")):
        lines.append(f"  {s.bold(title)}")
    if body_text:
        visible, hidden = wrap_body(body_text, width, max_lines=max_lines,
                                    style=s)
        lines.extend(visible)
        if hidden:
            lines.append(s.dim(f"  ⋯ {hidden} more line(s) — /read {ref}"))
    elif body_bytes:
        lines.append(s.dim(f"  ({body_bytes} bytes — /read {ref})"))
    if asks:
        lines.extend(ask_lines(s, asks, pending_asks, ref, width,
                               declined=declined_asks))
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
    visible, _ = wrap_body(content, width, max_lines=None, style=s)
    return "\n".join([s.dim("─" * width), header, s.dim("─" * width), *visible])


def charter_block(s: Style, *, scope: str, version: int, updated_by: str,
                  text: str, packaged: bool = False, gated: bool = False,
                  your_receipt: int | None = None) -> str:
    """A charter read (`/charter`), fenced as GOVERNANCE — deliberately not
    `file_block`.

    Provenance is the whole point of a fence (ADR-0002 decision 3, the same
    reason render.render_hub_charter exists beside render_fs_file): a shared
    file is member-authored data, while a charter is operator- or
    owner-authored and admin-key/ownership-gated. Rendering one under the
    other's header would tell the reader — human or model reading over their
    shoulder — the wrong thing about who wrote the words they are about to
    follow."""
    width = term_width()
    scope, updated_by = safe(scope), safe(updated_by)
    who = ("the packaged default" if packaged
           else (updated_by or "the operator"))
    what = ("HUB CHARTER" if scope == "hub" else f"CHARTER {scope}")
    header = (f"{s.yellow(what)} {s.bold('v' + str(version))} {s.dim('·')} "
              + s.dim("by ") + s.sender(who))
    sub = ("who is who: member · owner · delegate · operator"
           if scope == "hub" else "this room's rules, on top of the hub's")
    if gated:
        sub += " · norms_required: posting is GATED on reading it"
    if your_receipt is not None and your_receipt < version:
        sub += f" · your receipt was v{your_receipt}"
    visible, _ = wrap_body(text, width, max_lines=None, style=s)
    return "\n".join([s.dim("═" * width), header, s.dim("  " + sub),
                      s.dim("═" * width), *visible])


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
