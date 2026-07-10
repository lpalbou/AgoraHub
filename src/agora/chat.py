"""`agora chat` — the human's live window into the hub.

A REPL that makes the human a first-class channel member: a room directory
with stats on entry, live streaming of every message on every channel you
belong to (the current room rendered in full, other rooms as one-line
notices), and posting with the same obligation semantics agents use — plain
text posts `fyi`, `/ask` opens an obligation, `/critical` is the operator's
forced-attention tier.

Design notes:
- This is a HUMAN surface. The nonce-fencing applied to LLM-facing renders
  (see render.py) exists so a model cannot mistake quoted content for
  operator instructions; a human reading a terminal needs attribution, not
  fences, so messages render chat-style with explicit sender/status.
- Everything displayed is acked (triage-seen). Obligations and criticals
  stay pinned server-side until actually read/answered — acking here never
  discharges anything, so the human cannot accidentally "lose" work signals.
- Input uses prompt_toolkit when available (input line survives concurrent
  output); falls back to plain stdin otherwise.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from typing import Any

from .chat_render import (Style, channel_table as _render_channel_table,
                          dm_peer, file_block, file_event_line,
                          file_history_table, fmt_age, message_block,
                          presence_rows, safe, term_width)
from .client import AgoraClient
from .models import Envelope, Status


def channel_table(channels: list[dict[str, Any]], unread: dict[str, int],
                  current: str | None, now: float | None = None) -> str:
    """Plain-style directory (kept for tests and non-tty callers)."""
    return _render_channel_table(Style(False), channels, unread, current,
                                 now=now)


# -- pure helpers (unit-tested) ------------------------------------------------

def derive_title(text: str, limit: int = 80) -> str:
    """A chat message's triage headline: its first line, whitespace-collapsed,
    truncated at a word boundary. Keeps 'the title is what everyone reads'
    true even for humans typing free-form lines."""
    first = " ".join(text.strip().splitlines()[0].split()) if text.strip() else ""
    if len(first) <= limit:
        return first
    cut = first[:limit]
    if " " in cut[40:]:
        cut = cut[:cut.rfind(" ")]
    return cut + "…"


def parse_line(line: str) -> tuple[str, str]:
    """Split a REPL line into (command, argument). Plain text (no leading
    slash) is the implicit 'say' command. A leading '//' escapes a literal
    slash message."""
    stripped = line.strip()
    if stripped.startswith("//"):
        return "say", stripped[1:]
    if stripped.startswith("/"):
        head, _, rest = stripped[1:].partition(" ")
        return head.lower() or "help", rest.strip()
    return "say", stripped


def flags_of(env: Envelope) -> str:
    return ",".join(f for f, on in [
        ("CRITICAL", env.critical), ("escalated", env.escalated),
        ("to-you", env.to_me), ("reply-to-you", env.reply_to_me),
    ] if on)


# -- the app -------------------------------------------------------------------

HELP = """\
plain text          post to the current channel (status=fyi, no obligation)
/ask TEXT           post an open question (creates an obligation, escalates)
/reply SEQ|ID TEXT  reply to a message (discharges its obligation)
/critical TEXT      operator broadcast: pins in every inbox until read
/dm PEER TEXT       private 1:1 message
/dms                your direct conversations (unread, recency)
/fs [PATH]          this room's shared files: list, or read one in full
/fs PATH@N          read archived version N (every edit is kept, with author)
/fs hist PATH       a file's edit history (who wrote, who amended, size deltas)
/channels (/ls)     room directory with stats
/switch NAME (/c)   enter a room (auto-joins public rooms; also /join NAME TOKEN)
/history [N] (/h)   last N messages of this room (default 15)
/digest             open questions / decided / decisions of this room
/members            who is in this room (with self-descriptions)
/who                presence of everyone you share a channel with
/read SEQ|ID        full body of one message (records a read receipt)
/quit (/q)          leave the chat (membership persists)"""


class ChatApp:
    def __init__(self, url: str, api_key: str, agent_id: str,
                 channel: str | None = None) -> None:
        self.client = AgoraClient(url, api_key)
        self.me = agent_id
        self.current = channel
        self.style = Style(enabled=sys.stdout.isatty())
        self._closing = False

    # -- output ---------------------------------------------------------------

    def _print(self, text: str = "") -> None:
        print(text, flush=True)

    def show_envelope(self, env: Envelope) -> None:
        """Live traffic. The current room and DMs render as full blocks;
        other rooms as a one-line notice; file events and joins as one dim
        line; criticals always in full, loudly."""
        s = self.style
        is_dm = dm_peer(env.channel, self.me) is not None
        if env.kind.value == "fs":
            self._print(file_event_line(s, sender=env.sender, title=env.title,
                                        channel=env.channel, current=self.current,
                                        data=env.data))
            return
        if env.kind.value == "system":
            text = safe(env.body or env.title)
            self._print(s.dim(f"  ∙ [{safe(env.channel)}] {text[:100]}"))
            return
        if env.critical:
            self._print(s.red("═" * term_width()))
            self._print(s.red(f" CRITICAL from {env.sender} in {env.channel}"
                              f" — pinned until you /read {env.seq}"))
        elif env.channel != self.current and not is_dm:
            head = safe(env.title or (env.body or "")[:70])
            self._print(s.dim(f"  · [{safe(env.channel)}] ") + s.sender(safe(env.sender))
                        + s.dim(f": {head}"))
            return
        self._print(message_block(
            s, sender=env.sender, seq=env.seq, status=env.status.value,
            created_at=env.created_at, title=env.title, body=env.body,
            body_bytes=env.body_bytes, flags=flags_of(env),
            ask_progress=env.ask_progress, me=self.me, channel=env.channel,
            show_channel=env.channel != self.current))

    def show_message_row(self, m: Any, *, max_lines: int | None = None) -> None:
        kwargs = {} if max_lines is None else {"max_lines": max_lines}
        self._print(message_block(
            self.style, sender=m.sender, seq=m.seq, status=m.status.value,
            created_at=m.created_at, title=m.title, body=m.body,
            me=self.me, channel=m.channel, **kwargs))

    # -- data helpers -----------------------------------------------------------

    async def _channels_with_stats(self) -> list[dict[str, Any]]:
        """Room directory data. Hubs older than the stats fields leave the
        columns empty — fill them client-side for channels we can read, so
        the surface degrades to slower, never to emptier."""
        channels = await self.client.list_channels()

        async def fill(c: dict[str, Any]) -> None:
            if c.get("last_seq") is not None or not c["member"]:
                return
            with contextlib.suppress(Exception):
                info = await self.client.channel_info(c["name"])
                c["member_count"] = len(info.get("members", []))
            with contextlib.suppress(Exception):
                tail = await self._tail(c["name"], 1)
                c["last_seq"] = tail[-1].seq if tail else 0
                c["last_at"] = tail[-1].created_at if tail else None
        await asyncio.gather(*(fill(c) for c in channels))
        return channels

    async def _unread_by_channel(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for env in await self.client.check_inbox():
            counts[env.channel] = counts.get(env.channel, 0) + 1
        return counts

    async def _resolve_id(self, ref: str) -> str | None:
        """Accept a message id (ULID) or a seq number in the current channel."""
        if not ref.isdigit():
            return ref
        rows = await self.client.history(self.current, since=int(ref) - 1, limit=1)
        return rows[0].id if rows and rows[0].seq == int(ref) else None

    # -- commands ---------------------------------------------------------------

    async def cmd_channels(self) -> None:
        channels = await self._channels_with_stats()
        unread = await self._unread_by_channel()
        self._print(_render_channel_table(self.style, channels, unread,
                                          self.current, me=self.me))

    async def cmd_dms(self) -> None:
        """Direct conversations only, newest first."""
        channels = [c for c in await self._channels_with_stats()
                    if c["name"].startswith("dm:") and c["member"]]
        if not channels:
            self._print(self.style.dim("no direct messages yet — /dm PEER TEXT starts one"))
            return
        channels.sort(key=lambda c: c.get("last_at") or 0, reverse=True)
        unread = await self._unread_by_channel()
        now = time.time()
        for c in channels:
            peer = dm_peer(c["name"], self.me)
            n = unread.get(c["name"], 0)
            n_s = self.style.yellow(f"{n} unread") if n else self.style.dim("read")
            age = fmt_age(now - c["last_at"]) if c.get("last_at") else "-"
            switch_hint = self.style.dim(f"/switch {c['name']}")
            self._print(f"  {self.style.magenta('DM')} {self.style.sender(peer)}"
                        f"{' ' * max(1, 20 - len(peer))}"
                        f"{self.style.dim(f'last {age:<5}')} {n_s}   {switch_hint}")

    async def cmd_switch(self, arg: str) -> None:
        name, _, token = arg.partition(" ")
        if not name:
            self._print("usage: /switch CHANNEL   or   /join CHANNEL [INVITE_TOKEN]")
            return
        channels = {c["name"]: c for c in await self.client.list_channels()}
        info = channels.get(name)
        if info is None and not token:
            self._print(f"no such channel: {name} (private ones need "
                        "/join NAME INVITE_TOKEN)")
            return
        if info is None or not info["member"]:
            try:
                await self.client.join_channel(name, invite_token=token.strip() or None)
                self._print(self.style.dim(f"(joined {name})"))
            except Exception as exc:
                self._print(f"cannot join {name}: {exc}")
                return
        self.current = name
        meta = (await self.client.channel_info(name)).get("meta") or {}
        purpose = meta.get("purpose", "")
        s = self.style
        width = term_width()
        peer = dm_peer(name, self.me)
        label = f" DM with {peer} " if peer else f" {name} "
        bar = "─" * max(2, (width - len(label)) // 2)
        self._print(s.cyan(bar + label + bar))
        if purpose:
            self._print(s.dim(f"  {purpose}"))
        await self.cmd_history("5")

    async def _tail(self, channel: str, n: int) -> list[Any]:
        """Last n messages, robust against hubs that don't report last_seq:
        page forward keeping a rolling tail (channels at human scale are a
        few pages at most)."""
        tail: list[Any] = []
        cursor = 0
        while True:
            page = await self.client.history(channel, since=cursor, limit=200)
            if not page:
                return tail[-n:]
            tail = (tail + page)[-n:]
            cursor = page[-1].seq
            if len(page) < 200:
                return tail[-n:]

    async def cmd_history(self, arg: str) -> None:
        if not self.current:
            self._print("no current channel — /switch NAME first")
            return
        n = int(arg) if arg.isdigit() else 15
        for m in await self._tail(self.current, n):
            if m.kind.value == "message":
                self.show_message_row(m)
            elif m.kind.value == "fs":
                self._print(file_event_line(self.style, sender=m.sender,
                                            title=m.title, channel=m.channel,
                                            current=self.current, data=m.data))
        self._print()

    async def cmd_digest(self) -> None:
        if not self.current:
            self._print("no current channel — /switch NAME first")
            return
        d = self.client._json(await self.client._http.get(
            f"/channels/{self.current}/digest"))
        s = self.style
        c = d["counts"]
        self._print(s.dim("─" * term_width()))
        self._print(s.bold(f"digest of {self.current}") + s.dim(
            f" — {c['open_questions']} open · "
            f"{c['decided_shown']}/{c['decided_total']} decided · "
            f"{c['decisions']} recorded decision(s)"))
        for q in d["open_questions"]:
            seq = s.dim(f"#{q['seq']}")
            self._print(f"  {s.yellow('OPEN')} {seq} {s.sender(safe(q['from']))} "
                        f"{s.bold(safe(q['title']))}")
            for a in q["pending_asks"]:
                self._print(s.dim(safe(f"        [{a['id']}] {a['text'][:90]}")))
        for item in d["decided"][:10]:
            how = ("self-resolved" if item.get("self_resolved") else
                   "answered by " + ", ".join(item["answered_by"])
                   if item.get("answered_by") else "resolved")
            self._print(s.dim(safe(f"  done #{item['seq']} {item['title'][:70]} — {how}")))
        for entry in d["decisions"]:
            detail = f"v{entry['version']} by {entry['updated_by']}"
            self._print(f"  {s.cyan('DECISION')} {safe(entry['key'])} {s.dim(safe(detail))}")

    async def cmd_who(self) -> None:
        rows = self.client._json(await self.client._http.get("/presence"))
        self._print(presence_rows(self.style, rows))

    async def cmd_members(self) -> None:
        if not self.current:
            self._print("no current channel — /switch NAME first")
            return
        info = await self.client.channel_info(self.current)
        s = self.style
        for m in info.get("members", []):
            about = safe((m.get("about") or "").strip())
            agent_id = safe(m["agent_id"])
            role = s.yellow(m["role"]) if m["role"] == "owner" else s.dim(m["role"])
            pad = " " * max(1, 17 - len(agent_id))
            self._print(f"  {s.sender(agent_id)}{pad}{role:<16} "
                        f"{s.dim(about[:80])}")

    async def cmd_read(self, ref: str) -> None:
        if not (self.current and ref):
            self._print("usage: /read SEQ|MESSAGE_ID (in a current channel)")
            return
        mid = await self._resolve_id(ref)
        if mid is None:
            self._print(f"no message {ref} in {self.current}")
            return
        for m in await self.client.read(self.current, mid):
            self.show_message_row(m)

    async def cmd_reply(self, arg: str) -> None:
        ref, _, text = arg.partition(" ")
        if not (self.current and ref and text.strip()):
            self._print("usage: /reply SEQ|MESSAGE_ID TEXT")
            return
        mid = await self._resolve_id(ref)
        if mid is None:
            self._print(f"no message {ref} in {self.current}")
            return
        await self.client.post(self.current, text.strip(), title=derive_title(text),
                               status=Status.reply, reply_to=mid)

    async def cmd_post(self, text: str, *, status: Status = Status.fyi,
                       critical: bool = False) -> None:
        if not self.current:
            self._print("no current channel — /switch NAME first")
            return
        if not text:
            return
        try:
            msg = await self.client.post(self.current, text, title=derive_title(text),
                                         status=status, critical=critical)
        except Exception as exc:
            self._print(self.style.red(f"post failed: {exc}"))
            return
        # Always confirm the send (field lesson: no echo read as "not sent"),
        # and be honest about the delivery class: fyi carries no obligation
        # and does not wake idle agents — a question typed as plain text
        # would silently get the weakest delivery there is.
        note = f"(sent #{msg.seq} to {self.current} as {status.value}"
        if critical:
            note += ", CRITICAL — pinned in every inbox until read"
        elif status == Status.fyi:
            note += " — no obligation; expecting answers? use /ask"
        self._print(self.style.dim(note + ")"))

    async def cmd_dm(self, arg: str) -> None:
        peer, _, text = arg.partition(" ")
        if not (peer and text.strip()):
            self._print("usage: /dm PEER TEXT")
            return
        try:
            await self.client.dm(peer, text.strip(), title=derive_title(text))
            self._print(self.style.dim(f"(dm sent to {peer})"))
        except Exception as exc:
            self._print(self.style.red(f"dm failed: {exc}"))

    async def cmd_fs(self, arg: str) -> None:
        """The channel's shared files — the same tree agents use via the
        fs_* MCP tools and `agora fs`. `/fs` lists; `/fs PATH` reads in full
        (a deliberate read, like /read); `/fs hist PATH` shows the edit
        history with size deltas (who wrote vs who amended)."""
        if not self.current:
            self._print("no current channel — /switch NAME first")
            return
        s = self.style
        sub, _, rest = arg.partition(" ")
        if sub == "hist" and rest.strip():
            try:
                events = await self.client.fs_history(self.current, rest.strip())
            except Exception as exc:
                self._print(self.style.red(f"cannot read history: {exc}"))
                return
            if not events:
                self._print(s.dim(f"no history for '{rest.strip()}'"))
                return
            self._print(file_history_table(s, rest.strip(), events))
            return
        if not arg or arg == "ls":
            files = await self.client.fs_list(self.current)
            if not files:
                self._print(s.dim("no shared files in this channel"))
                return
            now = time.time()
            for f in sorted(files, key=lambda f: f.get("updated_at") or 0,
                            reverse=True):
                age = fmt_age(now - f["updated_at"]) if f.get("updated_at") else "-"
                size = f.get("size")
                meta = f"v{f['version']} · {size}ch · {age} · " if size is not None \
                    else f"v{f['version']} · {age} · "
                self._print(f"  {s.bold(f['path'])}  "
                            + s.dim(meta) + s.sender(f["updated_by"]))
                desc = safe(f.get("description", ""))
                if desc:
                    # ~ marks a derived first-line stand-in (writer set none).
                    prefix = "" if f.get("described") else "~ "
                    self._print(s.dim(f"      {prefix}{desc}"))
            self._print(s.dim("  /fs PATH to read · /fs hist PATH for its history"))
            return
        # `/fs PATH@N` reads archived version N (provenance preserved).
        path, _, ver = arg.rpartition("@")
        version = int(ver) if path and ver.isdigit() else None
        if version is None:
            path = arg
        try:
            f = await self.client.fs_read(self.current, path, version=version)
        except Exception as exc:
            self._print(self.style.red(f"cannot read '{arg}': {exc}"))
            return
        self._print(file_block(s, path=f["path"], content=f["content"],
                               version=f["version"], updated_by=f["updated_by"],
                               size_bytes=f["size_bytes"], channel=self.current))

    async def dispatch(self, line: str) -> bool:
        """Execute one REPL line; returns False to quit."""
        cmd, arg = parse_line(line)
        if cmd in ("q", "quit", "exit"):
            return False
        handlers = {
            "say": lambda: self.cmd_post(arg),
            "ask": lambda: self.cmd_post(arg, status=Status.open),
            "critical": lambda: self.cmd_post(arg, critical=True),
            "reply": lambda: self.cmd_reply(arg),
            "channels": self.cmd_channels, "ls": self.cmd_channels,
            "dm": lambda: self.cmd_dm(arg),
            "dms": self.cmd_dms,
            "fs": lambda: self.cmd_fs(arg), "files": lambda: self.cmd_fs(arg),
            "switch": lambda: self.cmd_switch(arg), "c": lambda: self.cmd_switch(arg),
            "join": lambda: self.cmd_switch(arg),
            "history": lambda: self.cmd_history(arg), "h": lambda: self.cmd_history(arg),
            "digest": self.cmd_digest,
            "who": self.cmd_who,
            "members": self.cmd_members,
            "read": lambda: self.cmd_read(arg),
            "ack": lambda: self.client.ack(),
            "help": lambda: self._print(HELP),
        }
        handler = handlers.get(cmd)
        if handler is None:
            self._print(f"unknown command /{cmd} — /help for the list")
            return True
        result = handler()
        if asyncio.iscoroutine(result):
            await result
        return True

    # -- live pump ----------------------------------------------------------------

    async def _pump(self) -> None:
        """Print incoming traffic as it lands; ack everything displayed.
        Acks are triage-seen only: obligations and criticals stay pinned
        server-side until read/answered."""
        while not self._closing:
            try:
                envelopes = await self.client.inbox.wait(timeout=3600.0)
            except asyncio.CancelledError:
                return
            for env in envelopes:
                self.show_envelope(env)
            if envelopes:
                with contextlib.suppress(Exception):
                    await self.client.ack()

    # -- entry ---------------------------------------------------------------------

    async def run(self) -> None:
        s = self.style
        me = await self.client.whoami()
        operator = bool(me.get("operator"))
        channels = await self._channels_with_stats()
        memberships = [c["name"] for c in channels if c["member"]]

        width = term_width()
        self._print(s.cyan("═" * width))
        role = s.yellow(" · operator") if operator else ""
        self._print(f" {s.bold('agora chat')} — {s.sender(self.me)}{role}")
        self._print(s.cyan("═" * width))
        self._print(_render_channel_table(
            s, channels, await self._unread_by_channel(), self.current,
            me=self.me))
        self._print(s.dim("type to talk (posts as fyi) · /ask opens a question"
                          " · /help for all commands\n"))

        if self.current is None and len(memberships) == 1:
            self.current = memberships[0]
        if self.current:
            await self.cmd_switch(self.current)

        await self.client.connect(memberships)
        pump = asyncio.create_task(self._pump())
        try:
            await self._input_loop()
        finally:
            self._closing = True
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            await self.client.close()
            self._print(s.dim("left the chat (memberships persist)"))

    def _prompt_text(self) -> str:
        s = self.style
        room = self.current or "-"
        peer = dm_peer(room, self.me) if self.current else None
        room_label = f"@{peer} (dm)" if peer else room
        return f"{s.sender(self.me)} {s.dim('@')} {s.cyan(room_label)} {s.dim('❯')} "

    async def _input_loop(self) -> None:
        prompt_async = self._make_prompt()
        while True:
            try:
                line = await prompt_async(self._prompt_text())
            except (EOFError, KeyboardInterrupt):
                return
            if line.strip() and not await self.dispatch(line):
                return

    def _make_prompt(self):
        """prompt_toolkit keeps the input line intact under concurrent output
        and renders the ANSI-colored prompt. Plain stdin is the fallback —
        both when the library is missing and when stdin is not a tty."""
        if sys.stdin.isatty():
            try:
                from prompt_toolkit import PromptSession
                from prompt_toolkit.formatted_text import ANSI
                from prompt_toolkit.patch_stdout import patch_stdout

                session = PromptSession()

                async def ask(prompt: str) -> str:
                    with patch_stdout(raw=True):
                        return await session.prompt_async(ANSI(prompt))
                return ask
            except ImportError:
                pass

        async def ask(prompt: str) -> str:
            # input() can't render ANSI reliably through readline; strip it.
            import re
            plain = re.sub(r"\x1b\[[0-9;]*m", "", prompt)
            return await asyncio.to_thread(input, plain)
        return ask


def run_chat(url: str, api_key: str, agent_id: str, channel: str | None = None) -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(ChatApp(url, api_key, agent_id, channel).run())
