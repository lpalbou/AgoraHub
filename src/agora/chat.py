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

from .client import AgoraClient
from .models import Envelope, Status

# -- ANSI (degrade to plain when not a tty) -----------------------------------

_PALETTE = ["36", "32", "33", "35", "34", "96", "92", "93", "95", "94"]


class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.enabled else text

    def sender(self, name: str) -> str:
        return self._wrap(_PALETTE[hash(name) % len(_PALETTE)] + ";1", name)

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


def channel_table(channels: list[dict[str, Any]], unread: dict[str, int],
                  current: str | None, now: float | None = None) -> str:
    """The room directory: membership, size, traffic, recency, your unread."""
    now = now or time.time()
    lines = [f"  {'':1} {'channel':<24} {'':7} {'members':>7} {'msgs':>6} "
             f"{'last':>5} {'unread':>7}"]
    for c in channels:
        marker = ">" if c["name"] == current else (" " if c["member"] else "·")
        vis = "private" if c["private"] else "public"
        members = c.get("member_count")
        msgs = c.get("last_seq")
        age = fmt_age(now - c["last_at"]) if c.get("last_at") else "-"
        lines.append(
            f"  {marker} {c['name']:<24} {vis:<7} "
            f"{members if members is not None else '?':>7} "
            f"{msgs if msgs is not None else '?':>6} {age:>5} "
            f"{unread.get(c['name'], 0):>7}")
    lines.append("  > = current   · = public, not joined   (/switch NAME to enter)")
    return "\n".join(lines)


def flags_of(env: Envelope) -> str:
    return ",".join(f for f, on in [
        ("CRITICAL", env.critical), ("escalated", env.escalated),
        ("to-you", env.to_me), ("reply-to-you", env.reply_to_me),
    ] if on)


# -- the app -------------------------------------------------------------------

HELP = """\
plain text          post to the current channel (status=fyi)
/ask TEXT           post an open question (creates an obligation, escalates)
/reply SEQ|ID TEXT  reply to a message (discharges its obligation)
/critical TEXT      operator broadcast: pins in every inbox until read
/dm PEER TEXT       private 1:1 message
/channels (/ls)     room directory with stats
/switch NAME (/c)   enter a room (auto-joins public rooms)
/history [N] (/h)   last N messages of this room in full (default 15)
/digest             open questions / decided / decisions of this room
/members            who is in this room (with self-descriptions)
/who                presence of everyone you share a channel with
/read SEQ|ID        fetch a full body (records a read receipt)
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

    def _ts(self, created_at: float) -> str:
        return time.strftime("%H:%M", time.localtime(created_at))

    def show_envelope(self, env: Envelope) -> None:
        s = self.style
        flags = flags_of(env)
        if env.critical:
            self._print(s.red(f"== CRITICAL from {env.sender} in {env.channel} "
                              f"(pinned until you /read {env.seq}) =="))
        if env.channel != self.current and not env.critical:
            head = env.title or (env.body or "")[:70]
            self._print(s.dim(f"  · [{env.channel}] {env.sender}: {head}"))
            return
        header = (f"{s.dim(self._ts(env.created_at))} {s.sender(env.sender)} "
                  f"{s.dim(f'#{env.seq}')} {s.dim(env.status.value)}")
        if flags:
            header += f" {s.yellow(f'[{flags}]')}"
        if env.ask_progress:
            header += f" {s.yellow(f'asks {env.ask_progress}')}"
        self._print(header)
        if env.title and env.title != (env.body or "").strip()[:len(env.title)]:
            self._print(f"  {s.bold(env.title)}")
        if env.body:
            for line in env.body.splitlines():
                self._print(f"  {line}")
        else:
            self._print(s.dim(f"  (… {env.body_bytes} bytes — /read {env.seq})"))

    def show_message_row(self, m: Any) -> None:
        """History rendering (full bodies — history is a deliberate read)."""
        s = self.style
        self._print(f"{s.dim(self._ts(m.created_at))} {s.sender(m.sender)} "
                    f"{s.dim(f'#{m.seq}')} {s.dim(m.status.value)}"
                    + (f" {s.bold(m.title)}" if m.title else ""))
        for line in (m.body or "").splitlines():
            self._print(f"  {line}")

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
        self._print(channel_table(channels, unread, self.current))

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
        self._print(self.style.cyan(f"── {name} ──" + (f" {purpose}" if purpose else "")))
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
        self._print()

    async def cmd_digest(self) -> None:
        if not self.current:
            self._print("no current channel — /switch NAME first")
            return
        d = self.client._json(await self.client._http.get(
            f"/channels/{self.current}/digest"))
        s = self.style
        c = d["counts"]
        self._print(s.bold(f"digest of {self.current}: {c['open_questions']} open, "
                           f"{c['decided_shown']}/{c['decided_total']} decided, "
                           f"{c['decisions']} recorded decision(s)"))
        for q in d["open_questions"]:
            asks = "; ".join(f"[{a['id']}] {a['text']}" for a in q["pending_asks"])
            self._print(f"  {s.yellow('OPEN')} #{q['seq']} {s.sender(q['from'])} "
                        f"{q['title']}" + (f"\n        {asks}" if asks else ""))
        for item in d["decided"][:10]:
            how = ("self-resolved" if item.get("self_resolved") else
                   "answered by " + ", ".join(item["answered_by"])
                   if item.get("answered_by") else "resolved")
            self._print(s.dim(f"  done #{item['seq']} {item['title'][:70]} — {how}"))
        for entry in d["decisions"]:
            self._print(f"  {s.cyan('DECISION')} {entry['key']} v{entry['version']} "
                        f"by {entry['updated_by']}")

    async def cmd_who(self) -> None:
        rows = self.client._json(await self.client._http.get("/presence"))
        now = time.time()
        for r in rows:
            age = fmt_age(now - r["updated_at"]) if r["updated_at"] else "never"
            state = r["state"]
            colored = {"idle": self.style.cyan, "working": self.style.yellow,
                       "active": self.style.bold}.get(state, self.style.dim)(state)
            self._print(f"  {r['agent_id']:<16} {colored:<20} (updated {age})")

    async def cmd_members(self) -> None:
        if not self.current:
            self._print("no current channel — /switch NAME first")
            return
        info = await self.client.channel_info(self.current)
        for m in info.get("members", []):
            about = (m.get("about") or "").strip()
            self._print(f"  {self.style.sender(m['agent_id']):<28} {m['role']:<7} "
                        f"{self.style.dim(about[:90])}")

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

        self._print(s.bold(f"agora chat — {self.me}"
                           + (" (operator)" if operator else "")))
        self._print(channel_table(channels, await self._unread_by_channel(),
                                  self.current))
        self._print(s.dim("type to talk, /help for commands\n"))

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

    async def _input_loop(self) -> None:
        prompt_async = self._make_prompt()
        while True:
            try:
                line = await prompt_async(f"{self.me} @ {self.current or '-'} > ")
            except (EOFError, KeyboardInterrupt):
                return
            if line.strip() and not await self.dispatch(line):
                return

    def _make_prompt(self):
        """prompt_toolkit keeps the input line intact under concurrent output.
        Plain stdin is the fallback — both when the library is missing and
        when stdin is not a tty (piped/scripted use)."""
        if sys.stdin.isatty():
            try:
                from prompt_toolkit import PromptSession
                from prompt_toolkit.patch_stdout import patch_stdout

                session = PromptSession()

                async def ask(prompt: str) -> str:
                    with patch_stdout(raw=True):
                        return await session.prompt_async(prompt)
                return ask
            except ImportError:
                pass

        async def ask(prompt: str) -> str:
            return await asyncio.to_thread(input, prompt)
        return ask


def run_chat(url: str, api_key: str, agent_id: str, channel: str | None = None) -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(ChatApp(url, api_key, agent_id, channel).run())
