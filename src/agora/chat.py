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
import re
import sys
import time
from typing import Any

from .chat_render import (BODY_MAX_LINES, Style, asks_from,
                          channel_table as _render_channel_table,
                          dm_peer, file_block, file_event_line,
                          file_history_table, fmt_age, message_block,
                          presence_rows, safe, term_width)
from .client import AgoraClient
from .models import Envelope, Status, dm_channel_name
from .vote import (DEFAULT_VOTE_TTL, VOTE_DATA_KEY, VOTE_RESULT_KEY,
                   VoteChair, build_vote_post, parse_vote_arg,
                   result_ballots, split_ttl, tally_ballots, vote_block,
                   vote_info, watch_votes)


def channel_table(channels: list[dict[str, Any]], unread: dict[str, int],
                  current: str | None, now: float | None = None) -> str:
    """Plain-style directory (kept for tests and non-tty callers)."""
    return _render_channel_table(Style(False), channels, unread, current,
                                 now=now)


def _summarize_sync(url: str, api_key: str, agent_id: str, llm: dict[str, Any],
                    channel: str | None, agent: str | None) -> str:
    """Run the (blocking) summarizer with its OWN client in a fresh event loop
    — called via asyncio.to_thread so the chat's live pump never blocks on the
    model. Kept module-level so it is trivially unit-testable."""
    from .client import AgoraClient
    from .summarize import summarize

    async def _go() -> str:
        client = AgoraClient(url, api_key, agent_id=agent_id)
        try:
            return await summarize(client, llm, channel=channel, agent=agent)
        finally:
            await client.close()

    return asyncio.run(_go())


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


_MENTION_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9_.-]*)")


def parse_group(arg: str) -> tuple[str, list[str]]:
    """Parse a `/group` line into (title, members) — operator dm 24: free
    text with @mentions anywhere. The @mentions become the roster (order
    kept, duplicates dropped, case folded to the hub's lowercase ids); the
    text with mentions stripped becomes the topic title. Both halves may
    interleave: `/group Fix voice @gateway @core then verify with @entity`
    -> ("Fix voice then verify with", [gateway, core, entity])."""
    members: list[str] = []
    for m in _MENTION_RE.findall(arg):
        m = m.lower()
        if m not in members:
            members.append(m)
    title = " ".join(_MENTION_RE.sub(" ", arg).split()).strip(" ,;:-")
    return title, members


def group_slug(title: str, taken: set[str]) -> str:
    """A channel slug derived from the topic title: lowercase, dashes for
    runs of non-slug characters, capped, uniqued against existing rooms
    with -2/-3... (create_channel refuses spaces/slashes/controls, so the
    slug must be born clean)."""
    base = re.sub(r"[^a-z0-9_.-]+", "-", title.lower()).strip("-.") or "group"
    base = base[:40].rstrip("-.")
    slug, n = base, 1
    while slug in taken:
        n += 1
        slug = f"{base}-{n}"
    return slug


def flags_of(env: Envelope) -> str:
    return ",".join(f for f, on in [
        ("CRITICAL", env.critical), ("escalated", env.escalated),
        ("to-you", env.to_me), ("reply-to-you", env.reply_to_me),
    ] if on)


# -- the app -------------------------------------------------------------------

# Two Ctrl-C within this window = "really quit"; a single one only clears
# the input line (the operator's reflex gesture must not tear the room down).
CTRL_C_QUIT_WINDOW = 2.0

HELP = """\
plain text          post to the current channel (status=fyi, no obligation)
/ask TEXT           post an open question (creates an obligation, escalates)
/ask @seat TEXT     ask ONE agent specifically: the named seat is flagged,
                    pinned, and owes the answer (several @seats allowed;
                    works in channels and DMs)
/reply REF TEXT     reply to a message (discharges its obligation, posts
                    into the referenced message's channel)
/reply REF:N TEXT   formally answer ask N of that message (727:1 or 727:1,2
                    — the asks a block lists and the 'asks N/M' badge counts)
/critical TEXT      operator broadcast: pins in every inbox until read
/dm PEER TEXT       private 1:1 message (fyi — for an OWED answer, open the
                    dm and /ask). /dm PEER opens the conversation;
                    /dm PEER:N reads message N (same as /read PEER:N)
/dms                your direct conversations (unread, recency)
/group TEXT @a @b   spin up a FOCUSED private room from one line: the text
                    becomes the room's name+purpose+opening ask, the
                    @mentioned seats get invites — keeps deep work out of
                    the big rooms (mentions can sit anywhere in the text)
/owed               your debts: asks awaiting YOUR answer, answers to your
                    asks awaiting consumption, and who you are waiting on
/board              follow the work: pending on you / queue / proposals /
                    in progress / review / decisions — hub-derived table
/fs [PATH]          this room's shared files: list, or read one in full
/fs PATH@N          read archived version N (every edit is kept, with author)
/fs hist PATH       a file's edit history (who wrote, who amended, size deltas)
/channels (/ls)     room directory with stats
/switch NAME (/c)   enter a room (auto-joins public rooms; also /join NAME
                    TOKEN). DMs by peer alone: /switch dm:agency — or just
                    /dm agency
/history [N] (/h)   last N messages of this room (default 15)
/digest             open questions / decided / decisions of this room
/summary [TARGET]   LLM summary (situation / pending / done / blocked) of the
                    whole hub from your view, a CHANNEL, or an @agent. Needs
                    an endpoint set once: `agora llm --base-url ... --model ...`
/vote TOPIC | A | B open a BLIND vote (ballots arrive as DMs to you, so no
                    voter sees another's choice; add options with |).
                    Auto-publishes on deadline (default 30m, lead with
                    30m/2h/1d to override) or once every member has voted
/tally REF [close]  the vote's state: chair-only counts while it runs;
                    'close' publishes the full result early
/members            who is in this room (with self-descriptions)
/who                presence of everyone you share a channel with
/read REF           full body of one message (records a read receipt)
                    REF: SEQ or ID in this room; SEQ@CHANNEL from any room
                    (seqs repeat across channels); DMs: PEER:SEQ, e.g.
                    /read artemis:3 (CHANNEL:SEQ works too)
/kick AGENT         remove from THIS room, rejoin refused for 15m
                    (--time 30mn/2h overrides; --target hub = full hub
                    lockout, operator only; trailing words = the reason)
/ban AGENT          same but forever (--time makes it a timed ban);
                    /unban AGENT [--target hub] lifts either early
/quiet              toggle quiet mode (default ON: resolved closes and
                    replies not addressed to you collapse to a counter —
                    they stay in /read, /history, /digest)
/delegate AGENT --power reporting[,operational,ruling,moderation]
                    operator: grant delegation (verifiable in whoami);
                    /delegate AGENT --revoke lifts; /delegate lists
/quit (/q)          leave the chat (membership persists)
Ctrl-C              clear the input line (twice within 2s to quit)"""


class ChatApp:
    def __init__(self, url: str, api_key: str, agent_id: str,
                 channel: str | None = None) -> None:
        self.client = AgoraClient(url, api_key)
        self.me = agent_id
        self.current = channel
        self.style = Style(enabled=sys.stdout.isatty())
        self._closing = False
        self._last_interrupt = float("-inf")  # monotonic time of the last Ctrl-C
        # Quiet mode (default ON): collapse bookkeeping traffic that carries
        # no debt for this seat — the operator-flood fix (2026-07-14: a
        # post-restart debt-clearing wave scrolled the room away faster than
        # a human could read).
        self.quiet = True
        self._quiet_hidden = 0
        # The chair side of blind votes opened from this client — the
        # watcher publishes them when the deadline hits or everyone voted.
        self.votes = VoteChair(self.client, self.me,
                               lambda text: self._print(self.style.dim(text)))

    # -- output ---------------------------------------------------------------

    def _print(self, text: str = "") -> None:
        print(text, flush=True)

    def show_envelope(self, env: Envelope) -> None:
        """Live traffic. The current room and DMs render as full blocks;
        other rooms as a one-line notice; file events and joins as one dim
        line; criticals always in full, loudly.

        QUIET MODE (default on — the operator-flood fix): bookkeeping
        traffic that carries no debt for YOU — resolved closes and replies
        not addressed to you — collapses to a counter instead of scrolling
        the room away. `/quiet` toggles; everything hidden stays readable
        (`/read`, history, digest — hiding is a render choice, never state)."""
        s = self.style
        is_dm = dm_peer(env.channel, self.me) is not None
        if (self.quiet and not env.critical and not is_dm
                and env.status.value in ("resolved", "reply")
                and not env.to_me and not env.reply_to_me):
            self._quiet_hidden += 1
            if self._quiet_hidden % 10 == 1:  # first, 11th, 21st... never per-line
                self._print(s.dim(f"  … {self._quiet_hidden} bookkeeping message(s)"
                                  " hidden (resolved/reply not for you) —"
                                  " /quiet to toggle"))
            return
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
            # Criticals from other rooms render here too — hint the
            # qualified ref, since a bare seq would resolve in the wrong
            # channel (seqs are only unique per channel). DM criticals
            # teach the short PEER:SEQ form.
            peer = dm_peer(env.channel, self.me)
            if env.channel == self.current:
                ref = env.seq
            else:
                ref = f"{peer}:{env.seq}" if peer else f"{env.seq}@{env.channel}"
            self._print(s.red("═" * term_width()))
            self._print(s.red(f" CRITICAL from {env.sender} in {env.channel}"
                              f" — pinned until you /read {ref}"))
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
            show_channel=env.channel != self.current,
            # data (and so the ask texts) is inlined per delivery policy;
            # pending_asks always travels, so state marks are exact here.
            asks=asks_from(env.data), pending_asks=env.pending_asks))

    def show_message_row(self, m: Any, *,
                         max_lines: int | None = BODY_MAX_LINES,
                         pending_asks: list[str] | None = None) -> None:
        """One stored message. Default is the capped preview; max_lines=None
        renders the full body (deliberate reads). Messages from outside the
        current room (a qualified /read) self-locate via the qualified ref.
        `pending_asks` marks ask state when the caller knows it (deliberate
        reads fetch it); history rows render asks with neutral marks."""
        self._print(message_block(
            self.style, sender=m.sender, seq=m.seq, status=m.status.value,
            created_at=m.created_at, title=m.title, body=m.body,
            me=self.me, channel=m.channel, max_lines=max_lines,
            show_channel=m.channel != self.current,
            asks=asks_from(m.data), pending_asks=pending_asks))

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

    async def _target_channel(self, token: str) -> str | None:
        """Resolve the '@' part of a qualified ref to a channel I belong to.
        An exact channel name wins — hints print that form, and it can never
        collide with peer sugar because plain channels may not start with the
        reserved 'dm:' prefix. Otherwise try the token as a DM peer id
        (typing sugar: '7@agency' for '7@dm:agency--laurent')."""
        mine = {c["name"] for c in await self.client.list_channels()
                if c["member"]}
        if token in mine:
            return token
        dm = dm_channel_name(self.me, token)
        return dm if dm in mine else None

    async def _locate(self, ref: str) -> tuple[str, str, list[str]] | None:
        """Resolve 'SEQ|ID[:ASK[,ASK...]][@CHANNEL|@PEER]' to
        (channel, message_id, ask_ids), printing the reason when it cannot.

        Seqs are per-channel, so blocks rendered away from their home room
        hint the qualified form — accepting it here is what keeps those hints
        honest. Parse order matters: split on the FIRST '@' (neither seqs,
        ULIDs nor ask ids may contain one), THEN on the first ':' of the
        local part only — channel names legitimately contain ':' (dm:a--b).
        ':ASK' names the ask(s) a /reply answers, e.g. '727:1' or '727:1,2'
        (unknown ids are rejected loudly by the hub, never mis-filed).

        DM sugar, leading form: 'artemis:3' == '3@artemis' — a DM has ONE
        peer, so PEER:SEQ is the natural handle (TARGET:SEQ also resolves
        plain channels). Only a NON-NUMERIC head that resolves to a channel
        or DM peer of mine is rewritten, so '727:1' (SEQ:ASK) and ULID:ASK
        fall through to the classic parse untouched."""
        if "@" not in ref and ":" in ref:
            head, _, rest = ref.partition(":")
            if head and rest and not head.isdigit():
                target = await self._target_channel(head)
                if target is not None:
                    ref = f"{rest}@{head}"
        local, _, at = ref.partition("@")
        local, _, ask_part = local.partition(":")
        ask_ids = [a.strip() for a in ask_part.split(",") if a.strip()]
        if not local:
            self._print(f"missing SEQ or ID in '{ref}'")
            return None
        if at:
            channel = await self._target_channel(at)
            if channel is None:
                self._print(f"unknown channel or DM peer '{at}' — "
                            "/channels lists yours")
                return None
        elif self.current:
            channel = self.current
        else:
            self._print("no current channel — use SEQ@CHANNEL, "
                        "or /switch NAME first")
            return None
        if not local.isdigit():
            return channel, local, ask_ids  # a ULID: already a unique id
        rows = await self.client.history(channel, since=int(local) - 1, limit=1)
        if rows and rows[0].seq == int(local):
            return channel, rows[0].id, ask_ids
        self._print(f"no message {local} in {channel}")
        return None

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
            # Teach the short forms: the peer's name is the whole address.
            switch_hint = self.style.dim(f"/dm {peer}")
            self._print(f"  {self.style.magenta('DM')} {self.style.sender(peer)}"
                        f"{' ' * max(1, 20 - len(peer))}"
                        f"{self.style.dim(f'last {age:<5}')} {n_s}   {switch_hint}")

    def _expand_dm(self, name: str) -> str:
        """`dm:agency` -> `dm:agency--laurent` (sorted). YOUR dms are the
        only ones you can reach, so naming the peer is enough — spelling
        your own handle into every ref was pure noise (operator, 2026-07-14).
        Full `dm:a--b` names pass through untouched."""
        if name.startswith("dm:") and "--" not in name:
            peer = name[3:].lstrip("@")
            if peer and peer != self.me:
                return f"dm:{min(peer, self.me)}--{max(peer, self.me)}"
        return name

    async def cmd_switch(self, arg: str) -> None:
        name, _, token = arg.partition(" ")
        name = self._expand_dm(name)
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

    async def cmd_vote(self, arg: str) -> None:
        """Open a BLIND vote: an ordinary open message + a machine-readable
        option list in data + the ballot contract in the body. Ballots are
        DMed to the author, never posted in the channel — a voter that sees
        earlier ballots anchors on them, so secrecy until the close is what
        keeps the poll informative. The result auto-publishes when the
        deadline hits ('/vote 2h TOPIC | …' overrides the default) or when
        every member has voted. Nothing hub-specific: any agent that can
        read, reply and DM can vote with its existing tools."""
        if not self.current:
            self._print("no current channel — /switch NAME first")
            return
        ttl, rest = split_ttl(arg)
        parsed = parse_vote_arg(rest)
        if parsed is None:
            self._print("usage: /vote [30m|2h|1d] TOPIC | OPTION | OPTION"
                        " [| OPTION…]  (two or more distinct options)")
            return
        topic, options = parsed
        ttl = ttl if ttl is not None else DEFAULT_VOTE_TTL
        payload = build_vote_post(self.me, topic, options, ttl)
        tag = payload["data"][VOTE_DATA_KEY]["tag"]
        try:
            msg = await self.client.post(
                self.current, payload["body"], title=payload["title"],
                status=Status.open, data=payload["data"])
        except Exception as exc:
            self._print(self.style.red(f"vote failed: {exc}"))
            return
        self.votes.register(msg, self.current)
        self._print(self.style.dim(
            f"(blind vote #{msg.seq} opened in {self.current} — ballots"
            f" arrive as DMs tagged '{tag}'; auto-publishes in"
            f" {fmt_age(ttl)} or when everyone voted; watch: /tally"
            f" {msg.seq}; close early: /tally {msg.seq} close)"))

    async def cmd_tally(self, arg: str) -> None:
        """The vote's state, honoring ballot secrecy. Before the close only
        the chair (the vote's author) sees counts — everyone else gets the
        blind notice. Publication happens automatically when the vote is
        finished (deadline reached, or every member voted — checked here
        too, so a chair's /tally never shows a stale 'still open' state);
        '/tally REF close' publishes early. From then on anyone's /tally
        renders the result straight from the transcript, and each voter
        can verify their listed ballot."""
        ref, _, sub = arg.partition(" ")
        close = sub.strip().lower() == "close"
        if not ref:
            self._print("usage: /tally SEQ|ID [close] — the vote message"
                        " (SEQ@CHANNEL from another room)")
            return
        located = await self._locate(ref)
        if located is None:
            return
        channel, mid, _ = located
        try:
            messages = await self.client.read(channel, mid)
        except Exception as exc:
            self._print(self.style.red(f"cannot read {ref}: {exc}"))
            return
        root = next((m for m in messages if m.id == mid), None)
        info = vote_info(root, channel) if root else None
        if info is None:
            self._print(f"message {ref} is not a vote (no options payload)"
                        f" — /read {ref} to see it")
            return
        options, topic = info["options"], info["topic"]
        ref_disp = str(root.seq) if channel == self.current \
            else f"{root.seq}@{channel}"
        gathered = await self.votes.collect(info)
        published, ballots = gathered["published"], gathered["ballots"]
        public, members = gathered["public"], gathered["members"]

        if published is not None:
            payload = published.data[VOTE_RESULT_KEY]
            shown = result_ballots(payload, options)
            total = payload.get("total_members")
            self._print(vote_block(
                self.style, ref=ref_disp, topic=topic, options=options,
                tally=tally_ballots(options, shown),
                total_members=total if isinstance(total, int) else len(shown),
                waiting=[], comments=[],
                footer=f"closed by {root.sender} — published as"
                       f" #{published.seq}"))
            return

        remaining = (info["closes_at"] - time.time()) \
            if info["closes_at"] is not None else None

        if self.me != root.sender:
            if close:
                self._print(f"only {root.sender} (the vote's author) can"
                            f" close vote #{ref_disp}")
                return
            note = (" · public ballots so far (visible to all): "
                    + ", ".join(sorted(public))) if public else ""
            when = f" (closes in {fmt_age(remaining)})" \
                if remaining is not None and remaining > 0 else ""
            self._print(self.style.dim(
                f"vote #{ref_disp} is blind: ballots go by DM to"
                f" {root.sender}; the result appears here when it"
                f" closes{when}{note}"))
            return

        # Chair view. If the vote is finished — or the chair asked — publish
        # now; blindness only ever protected the voting window.
        reason = "closed by the chair" if close \
            else self.votes.due(info, ballots, members)
        tally = tally_ballots(options, ballots)
        notes = ["public ballot (visible to everyone): "
                 + ", ".join(sorted(public))] if public else []
        if reason is not None:
            try:
                posted = await self.votes.publish(info, ballots, members,
                                                  reason)
            except Exception as exc:
                self._print(self.style.red(f"close failed: {exc}"))
                return
            self._print(self.style.dim(
                f"(vote #{ref_disp} closed — {reason}; result published as"
                f" #{posted.seq} in {channel})"))
            waiting: list[str] = []
            footer = f"closed — published as #{posted.seq}"
        else:
            presence: dict[str, str] = {}
            with contextlib.suppress(Exception):
                presence = {r["agent_id"]: r["state"]
                            for r in self.client._json(
                                await self.client._http.get("/presence"))}
            # The vote's author is not nagged in `waiting` — they asked.
            waiting = [m + (f" ({presence[m]})" if m in presence else "")
                       for m in sorted(members)
                       if m not in ballots and m != root.sender]
            # remaining <= 0 is unreachable here (due() would have published);
            # None = a vote posted without a deadline (pre-deadline clients).
            left = f"closes in {fmt_age(remaining)}" \
                if remaining is not None else "no deadline"
            footer = (f"chair view (only you see this) — {left} ·"
                      f" /tally {ref_disp} close publishes now")
        self._print(vote_block(
            self.style, ref=ref_disp, topic=topic, options=options,
            tally=tally, total_members=len(members) or len(ballots),
            waiting=waiting, comments=sorted(gathered["commenters"]),
            notes=notes, footer=footer))

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

    async def _pending_ask_ids(self, channel: str, target: Any) -> list[str] | None:
        """Which of `target`'s asks are still unanswered, from the channel
        digest — discharge is computed hub-side from the replies, which a
        single message fetch cannot see. None = unknown (digest unavailable):
        the renderer then shows neutral marks, it never guesses. A question
        absent from the digest's open list has been discharged or resolved,
        so 'absent' safely reads as 'nothing pending'."""
        if not asks_from(target.data):
            return None
        try:
            d = self.client._json(await self.client._http.get(
                f"/channels/{channel}/digest"))
        except Exception:
            return None
        for q in d.get("open_questions", []):
            if q.get("seq") == target.seq:
                return [str(a["id"]) for a in q.get("pending_asks", [])]
        return []

    async def cmd_delegate(self, arg: str) -> None:
        """Operator: `/delegate AGENT --power reporting,operational [--ttl 7d]`
        grants delegation from inside the chat (verifiable in every whoami);
        `/delegate AGENT --revoke` lifts it; `/delegate` lists. Uses the
        local admin key (config.json on the hub machine) — refuses elsewhere."""
        import httpx

        from . import config as _config
        s = self.style
        admin = _config.load_config().get("admin_key")
        if not admin:
            self._print(s.red("no admin key in ~/.agora/config.json — "
                              "delegation is the hub operator's verb"))
            return
        url = self.client.base_url
        headers = {"Authorization": f"Bearer {admin}"}
        words = arg.split()
        try:
            if not words:
                r = httpx.get(f"{url}/admin/delegations", headers=headers, timeout=10)
                rows = r.json() if r.status_code == 200 else []
                if not rows:
                    self._print(s.dim("no active delegations"))
                for d in rows:
                    until = time.strftime("%m-%d %H:%M", time.localtime(d["expires_at"]))
                    self._print(f"  {s.sender(d['agent_id'])} "
                                f"{'+'.join(d['powers'])} until {until}")
                return
            agent = words[0].lstrip("@")
            if "--revoke" in words:
                r = httpx.delete(f"{url}/admin/delegation/{agent}",
                                 headers=headers, timeout=10)
                self._print(s.dim(f"revoked {agent}" if r.status_code == 200
                                  else f"revoke failed: {r.status_code} {r.text}"))
                return
            powers: list[str] = []
            ttl = "7d"
            for i, w in enumerate(words):
                if w in ("--power", "--powers") and i + 1 < len(words):
                    powers = [p.strip() for p in words[i + 1].split(",") if p.strip()]
                if w == "--ttl" and i + 1 < len(words):
                    ttl = words[i + 1]
            if not powers:
                self._print("usage: /delegate AGENT --power reporting[,operational,"
                            "ruling,moderation] [--ttl 7d] | /delegate AGENT "
                            "--revoke | /delegate")
                return
            from .join import parse_ttl
            r = httpx.put(f"{url}/admin/delegation", headers=headers, timeout=10,
                          json={"agent_id": agent, "powers": powers,
                                "ttl_seconds": parse_ttl(ttl)})
            if r.status_code != 200:
                self._print(s.red(f"delegation failed: {r.status_code} {r.text}"))
                return
            d = r.json()
            until = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["expires_at"]))
            self._print(s.dim(f"delegated: {agent} holds "
                              f"{'+'.join(d['powers'])} until {until} — every "
                              "agent can verify via whoami.delegations"))
        except Exception as exc:
            self._print(s.red(f"delegate failed: {exc}"))

    def cmd_quiet(self) -> None:
        """Toggle quiet mode: collapse resolved/replies not addressed to you
        into a counter (hidden traffic stays in /read, /history, /digest)."""
        self.quiet = not self.quiet
        if self.quiet:
            self._quiet_hidden = 0
            self._print(self.style.dim(
                "quiet ON — bookkeeping traffic (resolved / replies not for "
                "you) collapses to a counter; /quiet to show everything"))
        else:
            self._print(self.style.dim(
                f"quiet OFF — showing all traffic "
                f"({self._quiet_hidden} were hidden this session)"))

    async def cmd_summary(self, arg: str) -> None:
        """`/summary` (whole hub from your view), `/summary CHANNEL` (one room),
        or `/summary @agent` (everything about one peer). Uses the endpoint set
        by `agora llm` — a calm, out-of-band read of a noisy hub."""
        from . import config as _config
        from .summarize import SummarizerError, summarize

        llm = _config.load_llm()
        if not llm.get("base_url") or not llm.get("model"):
            self._print(self.style.red(
                "no summarizer endpoint configured — run `agora llm --base-url "
                "URL --model NAME [--api-key KEY]` first"))
            return
        target = arg.strip()
        channel = agent = None
        if target.startswith("@"):
            agent = target[1:]
        elif target:
            channel = target
        self._print(self.style.dim("summarizing…"))
        try:
            # Its own client in a worker thread (a fresh event loop): the chat's
            # async client stays on the main loop, so the live pump keeps
            # rendering traffic while the model thinks.
            text = await asyncio.to_thread(
                _summarize_sync, self.client.base_url, self.client.api_key,
                self.me, llm, channel, agent)
        except SummarizerError as exc:
            self._print(self.style.red(f"summary failed: {exc}"))
            return
        except Exception as exc:
            self._print(self.style.red(f"summary failed: {exc}"))
            return
        self._print(text)

    async def cmd_read(self, ref: str) -> None:
        """Deliberate read: the full body, uncapped — this is the command the
        preview's '⋯ N more line(s)' hint points at, so it must never
        re-truncate (field bug: it rendered the identical capped block).
        Accepts the qualified ref cross-room hints print (SEQ@CHANNEL /
        SEQ@PEER) — a bare seq only means something in the current room.
        Any ':ASK' suffix is tolerated and ignored (reading is per message)."""
        if not ref:
            self._print("usage: /read SEQ|ID — or SEQ@CHANNEL from another room")
            return
        located = await self._locate(ref)
        if located is None:
            return
        channel, mid, _ = located
        try:
            messages = await self.client.read(channel, mid)
        except Exception as exc:
            self._print(self.style.red(f"cannot read {ref}: {exc}"))
            return
        for m in messages:
            pending = await self._pending_ask_ids(channel, m) \
                if m.id == mid else None
            self.show_message_row(m, max_lines=None, pending_asks=pending)

    async def cmd_reply(self, arg: str) -> None:
        """The reply posts into the referenced message's channel — answering
        a DM or a foreign-room critical must not require /switch-ing first
        (and must never land in the wrong room: seqs repeat across channels).
        'REF:N' (e.g. 727:1, or 727:1,2) formally answers those ask ids, the
        thing the 'asks N/M' badge counts — a plain reply on an ask-carrying
        message is visible but discharges nothing."""
        ref, _, text = arg.partition(" ")
        if not (ref and text.strip()):
            self._print("usage: /reply SEQ|ID TEXT — REF:N answers ask N "
                        "(727:1), SEQ@CHANNEL works from another room")
            return
        located = await self._locate(ref)
        if located is None:
            return
        channel, mid, ask_ids = located
        try:
            await self.client.post(channel, text.strip(), title=derive_title(text),
                                   status=Status.reply, reply_to=mid,
                                   answers=ask_ids or None)
        except Exception as exc:
            self._print(self.style.red(f"reply failed: {exc}"))
            return
        # Confirm what was formally discharged and where it landed — a reply
        # into another room produces no local echo, and an answered ask is
        # otherwise only visible in the badge/digest.
        note = ""
        if ask_ids:
            note = f" — answers ask [{', '.join(ask_ids)}]"
        if channel != self.current:
            self._print(self.style.dim(f"(reply sent to {channel}{note})"))
        elif note:
            self._print(self.style.dim(f"(reply sent{note})"))

    async def cmd_post(self, text: str, *, status: Status = Status.fyi,
                       critical: bool = False) -> None:
        if not self.current:
            self._print("no current channel — /switch NAME first")
            return
        if not text:
            return
        # `/ask @seat TEXT` — the direct "ask ONE agent" form the operator
        # asked for (2026-07-14): leading @mentions become the ask's per-ask
        # `to` (0077), so exactly the named seats are flagged, pinned, woken,
        # and shown the debt. Works in channels and DMs alike.
        named: list[str] = []
        asks = None
        if status == Status.open:
            words = text.split()
            while words and words[0].startswith("@") and len(words[0]) > 1:
                named.append(words.pop(0)[1:].rstrip(",:"))
                text = " ".join(words)
            if not text:
                self._print("usage: /ask [@seat ...] TEXT")
                return
            asks = [{"id": "1", "text": derive_title(text),
                     **({"to": named} if named else {})}]
        try:
            msg = await self.client.post(self.current, text, title=derive_title(text),
                                         status=status, critical=critical,
                                         to=named or None, asks=asks)
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
        elif status == Status.open and named:
            note += f" — owed by {', '.join(named)}: flagged, pinned, escalates"
        elif status == Status.open:
            note += " — open to the room; name a seat with /ask @seat TEXT"
        elif status == Status.fyi:
            note += " — no obligation; expecting answers? use /ask"
        self._print(self.style.dim(note + ")"))

    @staticmethod
    def _parse_moderation(arg: str) -> tuple[str, float | None, str, str] | str:
        """'AGENT [--time 15m|30mn|2h] [--target channel|hub] [reason...]' ->
        (agent, seconds|None, target, reason) — or an error string. 'mn' is
        accepted for minutes (the operator writes it); a missing --time means
        the CALLER's default (kick: 15m; ban: forever), so None here."""
        from .join import parse_ttl
        tokens = arg.split()
        if not tokens or tokens[0].startswith("--"):
            return "usage: AGENT [--time 15m] [--target channel|hub] [reason]"
        agent, seconds, target, reason_parts = tokens[0], None, "channel", []
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            key, _, inline = tok.partition("=")
            if key in ("--time", "--target"):
                value = inline
                if not value:
                    i += 1
                    if i >= len(tokens):
                        return f"{key} needs a value"
                    value = tokens[i]
                if key == "--time":
                    try:
                        seconds = parse_ttl(value.lower().replace("mn", "m"))
                    except ValueError as exc:
                        return str(exc)
                else:
                    if value not in ("channel", "hub"):
                        return "--target must be 'channel' or 'hub'"
                    target = value
            else:
                reason_parts.append(tok)
            i += 1
        return agent, seconds, target, " ".join(reason_parts)

    async def cmd_kick(self, arg: str, *, ban: bool) -> None:
        """/kick: timed block (default 15m) — removed now, rejoin refused
        until expiry. /ban: the same block with no expiry (or --time for a
        timed one). Default scope is THIS channel; --target hub locks the
        identity out of the whole hub (operator only, enforced server-side)."""
        parsed = self._parse_moderation(arg)
        if isinstance(parsed, str):
            self._print(f"usage: /{'ban' if ban else 'kick'} AGENT [--time 15m] "
                        f"[--target channel|hub] [reason] — {parsed}")
            return
        agent, seconds, target, reason = parsed
        if seconds is None and not ban:
            seconds = 900.0  # kick default: 15 minutes
        channel = None if target == "hub" else self.current
        if target == "channel" and channel is None:
            self._print("no current channel — /switch NAME first, or --target hub")
            return
        try:
            block = await self.client.impose_block(
                agent, channel=channel, seconds=seconds, reason=reason)
        except Exception as exc:
            self._print(self.style.red(f"cannot {'ban' if ban else 'kick'} "
                                       f"{agent}: {exc}"))
            return
        scope = block.get("scope", target)
        if block.get("expires_at"):
            until = time.strftime("%H:%M", time.localtime(block["expires_at"]))
            self._print(self.style.dim(
                f"({agent} kicked from {scope} until {until} — /unban {agent}"
                + (" --target hub" if target == "hub" else "") + " lifts it early)"))
        else:
            self._print(self.style.dim(
                f"({agent} BANNED from {scope} — /unban {agent}"
                + (" --target hub" if target == "hub" else "") + " lifts it)"))

    async def cmd_unban(self, arg: str) -> None:
        """Lift a kick or ban early (channel scope by default, --target hub)."""
        parsed = self._parse_moderation(arg)
        if isinstance(parsed, str):
            self._print(f"usage: /unban AGENT [--target channel|hub] — {parsed}")
            return
        agent, _, target, _ = parsed
        channel = None if target == "hub" else self.current
        if target == "channel" and channel is None:
            self._print("no current channel — /switch NAME first, or --target hub")
            return
        try:
            res = await self.client.lift_block(agent, channel=channel)
        except Exception as exc:
            self._print(self.style.red(f"cannot unban {agent}: {exc}"))
            return
        self._print(self.style.dim(
            f"({agent}: " + ("block lifted" if res.get("lifted")
                             else "no active block") + f" in {res.get('scope')})"))

    async def cmd_dm(self, arg: str) -> None:
        """The one DM verb, kept simple (operator request, 2026-07-14):
        `/dm PEER TEXT` sends; `/dm PEER` switches into the conversation;
        `/dm PEER:N` reads that DM message in full. A question sent as a
        plain DM carries NO obligation (fyi) — the hint teaches the owed
        path, because 'my delegate never answered' turned out to be an
        unowed fyi nobody's debt surface ever showed."""
        peer, _, text = arg.partition(" ")
        # `@flow` and `flow` are the same seat: /ask taught the @ convention
        # and hands type it everywhere (field 404, 2026-07-14).
        peer = peer.lstrip("@")
        text = text.strip()
        if peer and not text:
            head, _, seq = peer.partition(":")
            if seq.isdigit():
                await self.cmd_read(peer)          # /dm agency:5 == /read agency:5
                return
            await self.cmd_switch(f"dm:{min(head, self.me)}--{max(head, self.me)}")
            return
        if not (peer and text):
            self._print("usage: /dm PEER TEXT | /dm PEER (open the "
                        "conversation) | /dm PEER:N (read message N)")
            return
        try:
            await self.client.dm(peer, text, title=derive_title(text))
            self._print(self.style.dim(f"(dm sent to {peer})"))
            if "?" in text:
                self._print(self.style.dim(
                    "  a plain dm is fyi — no reply is owed and it never "
                    "shows in their debts. Need an answer? open the dm "
                    f"(/dm {peer}) and /ask TEXT — that pins and escalates."))
        except Exception as exc:
            self._print(self.style.red(f"dm failed: {exc}"))

    async def cmd_group(self, arg: str) -> None:
        """`/group Any topic text @seat1 more text @seat2 ...` — one gesture
        that spins up a FOCUSED room (operator dm 24): creates a private
        channel named after the topic, sets its purpose, invites exactly
        the @mentioned seats (invite token DM'd, joining stays their own
        auditable act), posts the topic as the room's first open message so
        arrivals see WHY the room exists, and switches you into it. Keeps
        the discussion constrained to the agents concerned — the hub-rules
        'deep work gets its OWN channel' norm, reduced to one line."""
        title, members = parse_group(arg)
        if not members:
            self._print("usage: /group TOPIC TEXT with @seat mentions — e.g. "
                        "/group fix the voice outage @gateway @core")
            return
        if not title:
            title = "focused work with " + ", ".join(members)
        s = self.style
        # Slug derivation stays client-side (presentation); the 4-call recipe
        # (create + purpose + invites + opening post) is now ONE hub call
        # (agora-0119), so chat and continuum can no longer drift on the
        # invite status. The name-collision suffix is computed from the
        # rooms this client can see; the hub still refuses a true collision.
        taken = {c["name"] for c in await self.client.list_channels()}
        name = group_slug(title, taken)
        try:
            out = await self.client.create_group(name, members, purpose=title,
                                                 opening_post=title)
        except Exception as exc:
            self._print(s.red(f"cannot create group '{name}': {exc}"))
            return
        invited = out.get("invited", [])
        for f in out.get("failed", []):
            self._print(s.red(f"  {f.get('agent')}: invite failed — {f.get('error')}"))
        self._print(f"group room {s.bold(name)} created — private, "
                    f"{len(invited)} invited: {', '.join(invited) or '-'}")
        await self.cmd_switch(name)

    async def cmd_owed(self) -> None:
        """YOUR debts and dues, straight from GET /owed: what awaits your
        answer, what answers to your own asks await consumption, and per
        addressee, whether the seats you are waiting on were served."""
        s = self.style
        try:
            owed = await self.client.owed()
        except Exception as exc:
            self._print(s.red(f"owed failed (hub too old?): {exc}"))
            return
        ta, tc, wo = owed["to_answer"], owed["to_consume"], owed.get("waiting_on", [])
        if not (ta or tc or wo):
            self._print(s.dim("nothing owed, nothing waiting — clean slate"))
            return
        if ta:
            self._print(s.bold("TO ANSWER (yours until you reply):"))
            for r in ta:
                esc = s.red(" ESCALATED") if r.get("escalated") else ""
                naming = (f" naming you: {','.join(r['asks_naming_you'])}"
                          if r.get("asks_naming_you") else "")
                self._print(f"  {safe(r['channel'])}#{r['seq']} from "
                            f"{s.sender(safe(r['from']))} — pending "
                            f"{r['pending_asks']}{naming} · {fmt_age(r['age_minutes'])}"
                            f"{esc} · /read {r['seq']}@{safe(r['channel'])}")
        if tc:
            self._print(s.bold("TO CONSUME (answers to YOUR asks — read and use):"))
            for r in tc:
                self._print(f"  {safe(r['channel'])}#{r['answer_seq']} "
                            f"{s.sender(safe(r['answered_by']))} answered your ask "
                            f"{r['your_asks']} · {fmt_age(r['age_minutes'])} · "
                            f"/read {r['answer_seq']}@{safe(r['channel'])}")
        if wo:
            self._print(s.bold("WAITING ON (your open asks, per seat):"))
            for r in wo:
                state = ("served, silent — nudge?" if r["state"] == "acked-past-no-reply"
                         else "not served yet (offline/behind)")
                self._print(f"  {safe(r['channel'])}#{r['seq']} ask {r['ask']} -> "
                            f"{s.sender(safe(r['seat']))}: {state}")

    async def cmd_board(self) -> None:
        """The operator's follow-the-work table (done / pending / ongoing /
        next), derived hub-side from the same settlement truth the inbox
        uses — the followability surface, no LLM required."""
        s = self.style
        try:
            b = await self.client.board()
        except Exception as exc:
            self._print(s.red(f"board failed: {exc}"))
            return
        def rows(name, items, fmt):
            if items:
                self._print(s.bold(name))
                for r in items[:12]:
                    self._print("  " + fmt(r))
                if len(items) > 12:
                    self._print(s.dim(f"  … {len(items) - 12} more"))
        rows("PENDING ON YOU:", b.get("pending_on_me", []),
             lambda r: (f"{safe(r['channel'])}#{r['seq']} {s.sender(safe(r['from']))}: "
                        f"{safe(r['q'])[:70]} · {fmt_age(r['age_minutes'])}"
                        + (s.red(" ESCALATED") if r.get("escalated") else "")))
        rows("QUEUE (operator-curated):", b.get("queue", []),
             lambda r: f"{safe(r.get('channel',''))} {safe(str(r.get('q') or r.get('key',''))[:70])}")
        rows("PROPOSALS (unowned open questions):", b.get("proposals", []),
             lambda r: (f"{safe(r['channel'])}#{r['seq']} {s.sender(safe(r['from']))}: "
                        f"{safe(r['q'])[:70]} · {fmt_age(r['age_minutes'])}"))
        rows("IN PROGRESS (claims):", b.get("in_progress", []),
             lambda r: f"{safe(r.get('channel',''))} {safe(str(r.get('key',''))[:70])}")
        rows("PENDING REVIEW:", b.get("pending_review", []),
             lambda r: f"{safe(r.get('channel',''))} {safe(str(r.get('key',''))[:70])}")
        done = b.get("done", [])
        if done:
            self._print(s.bold(f"DONE (decisions, latest {min(len(done), 8)}):"))
            for r in done[:8]:
                self._print(s.dim(f"  {safe(r.get('channel',''))} "
                                  f"{safe(str(r.get('key',''))[:70])} by "
                                  f"{safe(str(r.get('updated_by','')))}"))
        if not any(b.get(k) for k in ("pending_on_me", "queue", "proposals",
                                      "in_progress", "pending_review", "done")):
            self._print(s.dim("board is empty"))

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
            "group": lambda: self.cmd_group(arg),
            "fs": lambda: self.cmd_fs(arg), "files": lambda: self.cmd_fs(arg),
            "switch": lambda: self.cmd_switch(arg), "c": lambda: self.cmd_switch(arg),
            "join": lambda: self.cmd_switch(arg),
            "history": lambda: self.cmd_history(arg), "h": lambda: self.cmd_history(arg),
            "digest": self.cmd_digest,
            "vote": lambda: self.cmd_vote(arg),
            "tally": lambda: self.cmd_tally(arg),
            "who": self.cmd_who,
            "members": self.cmd_members,
            "summary": lambda: self.cmd_summary(arg),
            "read": lambda: self.cmd_read(arg),
            "kick": lambda: self.cmd_kick(arg, ban=False),
            "ban": lambda: self.cmd_kick(arg, ban=True),
            "unban": lambda: self.cmd_unban(arg),
            # Blanket by design: the human explicitly asked to mark
            # everything displayed as seen (0011: agents ack per handled
            # message; a human surface acking what it rendered is the
            # legitimate blanket case).
            "ack": lambda: self.client.ack_all_delivered(),
            "quiet": self.cmd_quiet,
            "owed": self.cmd_owed,
            "board": self.cmd_board,
            "delegate": lambda: self.cmd_delegate(arg),
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
                # Blanket ack is correct HERE: every envelope was just
                # rendered to the human's terminal — displayed IS handled
                # for a chat surface (0011).
                with contextlib.suppress(Exception):
                    await self.client.ack_all_delivered()

    async def _vote_watch(self) -> None:
        """Auto-publish chaired votes the moment their blindness protects
        nothing anymore (deadline reached / everyone voted) — the same
        chair-duty loop agents run in their MCP server / AgentRunner.
        Recovery adopts votes this identity opened from any surface."""
        await watch_votes(self.votes, closing=lambda: self._closing)

    # -- entry ---------------------------------------------------------------------

    async def run(self) -> None:
        s = self.style
        # The banner below flags a protocol mismatch inline, styled; silence
        # the client's module-level RuntimeWarning so the login screen is not
        # double-flagged (raw stderr + banner).
        self.client._protocol_warned = True
        me = await self.client.whoami()
        operator = bool(me.get("operator"))
        channels = await self._channels_with_stats()
        memberships = [c["name"] for c in channels if c["member"]]

        width = term_width()
        self._print(s.cyan("═" * width))
        role = s.yellow(" · operator") if operator else ""
        # Show the running hub's version + wire protocol at login, so it is
        # obvious what you are connected to (single source: agora.__version__).
        # A protocol mismatch is flagged inline instead of hidden in a warning.
        from . import PROTOCOL_VERSION
        ver = me.get("version")
        proto = me.get("protocol", "")
        if proto and proto != PROTOCOL_VERSION:
            # Mismatch renders even if a future hub omits `version` — the
            # protocol flag must never disappear behind a missing field.
            proto_s = s.yellow(f"{proto} ≠ client {PROTOCOL_VERSION} — upgrade one side")
            hub_label = f"  hub v{ver} (" if ver else "  hub ("
            ver_s = s.dim(hub_label) + proto_s + s.dim(")")
        else:
            ver_s = s.dim(f"  hub v{ver} ({proto})") if ver else ""
        self._print(f" {s.bold('agora chat')} — {s.sender(self.me)}{role}{ver_s}")
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
        vote_watch = asyncio.create_task(self._vote_watch())
        try:
            await self._input_loop()
        finally:
            self._closing = True
            for task in (pump, vote_watch):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self.client.close()
            self._print(s.dim("left the chat (memberships persist)"))

    def _prompt_text(self) -> str:
        s = self.style
        room = self.current or "-"
        peer = dm_peer(room, self.me) if self.current else None
        room_label = f"@{peer} (dm)" if peer else room
        return f"{s.sender(self.me)} {s.dim('@')} {s.cyan(room_label)} {s.dim('❯')} "

    def _second_interrupt(self, now: float | None = None) -> bool:
        """Ctrl-C quit policy: True when this interrupt follows another within
        CTRL_C_QUIT_WINDOW seconds (pure logic, injectable clock for tests)."""
        now = time.monotonic() if now is None else now
        second = (now - self._last_interrupt) < CTRL_C_QUIT_WINDOW
        self._last_interrupt = now
        return second

    async def _input_loop(self) -> None:
        prompt_async = self._make_prompt()
        while True:
            try:
                line = await prompt_async(self._prompt_text())
            except EOFError:
                return  # Ctrl-D: deliberate, quits at once
            except KeyboardInterrupt:
                # Ctrl-C aborts the prompt, discarding whatever was typed.
                # One press = just that (clear the line); a second within the
                # window = quit. Mirrors the ipython/psql convention.
                if self._second_interrupt():
                    return
                self._print(self.style.dim(
                    "(line cleared — Ctrl-C again within 2s to quit, or /quit)"))
                continue
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
            # Note: on this path Ctrl-C arrives as SIGINT to the event loop
            # (not as a KeyboardInterrupt from this await), so it still quits
            # immediately via run_chat's suppress — the clear-line-then-quit
            # gesture needs the prompt_toolkit path (raw mode reads Ctrl-C
            # as a key and aborts only the prompt).
            plain = re.sub(r"\x1b\[[0-9;]*m", "", prompt)
            return await asyncio.to_thread(input, plain)
        return ask


def run_chat(url: str, api_key: str, agent_id: str, channel: str | None = None) -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(ChatApp(url, api_key, agent_id, channel).run())
