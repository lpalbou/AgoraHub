"""HubService: all hub behavior behind one object, transport-agnostic.

The HTTP API and the WebSocket endpoint are thin translations onto this
class, so behavior (membership enforcement, ordering, rate limits, wake-ups)
is defined exactly once and is directly unit-testable without a server.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from ..db import Database
from ..ids import new_token
from ..models import (
    DM_PREFIX,
    MAX_ABOUT_CHARS,
    MAX_BODY_BYTES,
    MAX_DATA_BYTES,
    MAX_STORE_VALUE_BYTES,
    AgentInfo,
    ColleagueNote,
    Envelope,
    Kind,
    Message,
    PostMessage,
    StoreEntry,
    Urgency,
    dm_channel_name,
    sanitize_text,
    sanitize_title,
)
from .attention import DEFAULT_RESPONSE_SLA_MINUTES, AttentionPolicy, SlidingWindowBudget
from .notify import FanOut, LoopBinder, Notifier
from .presence import PresenceTracker
from .ratelimit import RateLimiter

RESERVED_STORE_PREFIX = "channel:"   # channel-level keys: owner-writable only
CHANNEL_META_KEY = "channel:meta"
_META_FIELDS = {"purpose", "norms", "expected_traffic", "response_sla_minutes", "language"}
_META_LANGUAGES = {"plain", "terse", "structured"}
MAX_READ_ANCESTORS = 5


class HubError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class HubService:
    def __init__(self, db: Database, *, rate_per_minute: float = 60.0,
                 interrupts_per_hour: int = 6, criticals_per_hour: int = 5) -> None:
        self.db = db
        # One shared binder so fan-out and long-poll wakes marshal onto the
        # same serving loop from synchronous (threadpool) request handlers.
        self._binder = LoopBinder()
        self.fanout = FanOut(self._binder)
        self.notifier = Notifier(self._binder)
        self.presence = PresenceTracker()
        self.ratelimiter = RateLimiter(rate_per_minute=rate_per_minute)
        self.attention = AttentionPolicy()
        self.interrupt_budget = SlidingWindowBudget(interrupts_per_hour)
        self.critical_budget = SlidingWindowBudget(criticals_per_hour)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the serving event loop. Called by every async entry point
        (WebSocket connect, long-poll wait) so cross-thread wakes are safe."""
        self._binder.bind(loop)

    # -- auth -----------------------------------------------------------------

    def register_agent(self, agent_id: str, name: str, operator: bool = False,
                       about: str = "") -> tuple[AgentInfo, str]:
        # ASCII-only, no double-dash (would collide with the dm:<a>--<b>
        # separator), reserved ids blocked, bounded length. Prevents Unicode
        # homoglyph impersonation of the one signal the model trusts: identity.
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?", agent_id):
            raise HubError(400, "agent id must be lowercase ascii [a-z0-9_-], 1-64 chars, "
                                "no leading/trailing dash")
        if "--" in agent_id:
            raise HubError(400, "agent id may not contain '--' (reserved dm separator)")
        if agent_id in {"hub", "all"}:
            raise HubError(400, f"'{agent_id}' is a reserved id")
        if self.db.agent_exists(agent_id):
            raise HubError(409, f"agent '{agent_id}' already exists")
        api_key = new_token("agora")
        info = self.db.register_agent(agent_id, name, api_key, operator,
                                      sanitize_text(about, MAX_ABOUT_CHARS))
        return info, api_key

    def set_about(self, agent: AgentInfo, about: str) -> AgentInfo:
        """Self-description: scope of ownership, what to ask this agent about.
        Self-editable only; sanitized like titles (every joiner reads it)."""
        cleaned = sanitize_text(about, MAX_ABOUT_CHARS)
        self.db.set_about(agent.id, cleaned)
        return agent.model_copy(update={"about": cleaned})

    def authenticate(self, api_key: str) -> AgentInfo:
        agent = self.db.agent_by_key(api_key)
        if agent is None:
            raise HubError(401, "invalid api key")
        return agent

    # -- channels ---------------------------------------------------------------

    def create_channel(self, agent: AgentInfo, name: str, private: bool = True) -> dict[str, Any]:
        if not name or "/" in name or " " in name:
            raise HubError(400, "channel name must be a simple slug (no spaces or slashes)")
        if name.startswith(DM_PREFIX):
            raise HubError(400, f"the '{DM_PREFIX}' prefix is reserved for direct channels")
        if self.db.get_channel(name) is not None:
            raise HubError(409, f"channel '{name}' already exists")
        channel = self.db.create_channel(name, private, agent.id)
        self._post_system(name, f"channel created by {agent.id}")
        return channel.model_dump()

    # -- direct (1:1) channels ---------------------------------------------------

    def open_dm(self, agent: AgentInfo, peer: str) -> dict[str, Any]:
        """Get-or-create the direct channel with `peer` (idempotent).

        DMs are ordinary channels with a reserved name and NO owner: with no
        owner, invite minting and channel-meta writes fail structurally, so a
        third party can never be added and the pair keeps hub defaults (SLA
        etc.). Everything else — envelopes, escalation, history, a pairwise
        store — is inherited.
        """
        if peer == agent.id:
            raise HubError(400, "cannot open a direct channel with yourself")
        if not self.db.agent_exists(peer):
            raise HubError(404, f"agent '{peer}' is not registered")
        name = dm_channel_name(agent.id, peer)
        if self.db.get_channel(name) is None:
            self.db.create_channel(name, private=True, created_by="hub", add_owner=False)
            self.db.add_member(name, agent.id, role="member")
            self.db.add_member(name, peer, role="member")
            self._post_system(name, f"direct channel between {agent.id} and {peer}")
        return self.channel_info(agent, name)

    def post_dm(self, agent: AgentInfo, peer: str, payload: PostMessage) -> Message:
        """Send a direct message (opens the DM channel on first use).
        Hub-addressed to the peer so bodies inline up to the addressed cap."""
        self.open_dm(agent, peer)
        payload = payload.model_copy(update={"to": [peer]})
        return self.post_message(agent, dm_channel_name(agent.id, peer), payload)

    def require_membership(self, channel: str, agent_id: str) -> None:
        if self.db.get_channel(channel) is None:
            raise HubError(404, f"channel '{channel}' not found")
        if not self.db.is_member(channel, agent_id):
            raise HubError(403, f"'{agent_id}' is not a member of '{channel}'")

    def create_invite(self, agent: AgentInfo, channel: str,
                      invitee: str | None, ttl_seconds: float = 86400.0) -> str:
        # Only owners may extend the trust boundary of a private channel.
        # This blunts the confused-deputy risk of an LLM member being talked
        # into inviting an attacker (red-team finding).
        role = self.db.member_role(channel, agent.id)
        if role != "owner":
            raise HubError(403, "only the channel owner can create invites")
        if invitee is not None and not self.db.agent_exists(invitee):
            raise HubError(404, f"agent '{invitee}' is not registered")
        token = new_token("invite")
        self.db.create_invite(token, channel, invitee, agent.id, ttl_seconds)
        return token

    def join_channel(self, agent: AgentInfo, channel: str, invite_token: str | None) -> dict[str, Any]:
        info = self.db.get_channel(channel)
        if info is None:
            raise HubError(404, f"channel '{channel}' not found")
        if channel.startswith(DM_PREFIX) and not self.db.is_member(channel, agent.id):
            raise HubError(403, "direct channels cannot be joined")
        if not self.db.is_member(channel, agent.id):
            if info.private:
                if not invite_token or self.db.redeem_invite(invite_token, agent.id) != channel:
                    raise HubError(403, "a valid invite token is required for this private channel")
            else:
                self.db.add_member(channel, agent.id)
            about = self.db.get_about(agent.id)
            self._post_system(channel, f"{agent.id} joined"
                                       + (f" — {about}" if about else ""))
            # History is deliberately readable (get_messages), but must not
            # flood the newcomer's inbox: start their triage cursor at head.
            self.db.set_cursor(agent.id, channel, self.db.last_seq(channel))
        # One-call onboarding: metadata + members with abouts, so the joiner
        # knows the channel's norms and who to ask what before posting.
        return {"joined": True, **self.channel_info(agent, channel)}

    def leave_channel(self, agent: AgentInfo, channel: str) -> None:
        self.require_membership(channel, agent.id)
        self.db.remove_member(channel, agent.id)
        self._post_system(channel, f"{agent.id} left")

    # -- messages -----------------------------------------------------------------

    def post_message(self, agent: AgentInfo, channel: str, payload: PostMessage) -> Message:
        self.require_membership(channel, agent.id)
        if len(payload.body.encode()) > MAX_BODY_BYTES:
            raise HubError(413, f"body exceeds {MAX_BODY_BYTES} bytes")
        if payload.data is not None and len(json.dumps(payload.data).encode()) > MAX_DATA_BYTES:
            raise HubError(413, f"data exceeds {MAX_DATA_BYTES} bytes")
        # `reply_to` must reference a message in THIS channel. Without this a
        # sender could point reply_to at a message in a channel it cannot read
        # and later harvest it via read_message's ancestor walk (the v0.3 IDOR).
        if payload.reply_to is not None:
            parent = self.db.get_message(payload.reply_to)
            if parent is None or parent.channel != channel:
                raise HubError(400, "reply_to must reference a message in this channel")
        # `to` may only address members of this channel (addressing is a
        # delivery/importance signal; it should not name outsiders).
        if payload.to:
            members = {m.agent_id for m in self.db.list_members(channel)}
            outsiders = [a for a in payload.to if a not in members]
            if outsiders:
                raise HubError(400, f"cannot address non-members: {outsiders}")
        if not self.ratelimiter.allow(agent.id):
            raise HubError(429, "rate limit exceeded — slow down (are you in a reply loop?)")

        if payload.critical:
            # Authority tier: operators only (owners self-mint channels, so
            # owner-critical would be self-granted forced attention), budgeted
            # even for them, and never envelope-elided.
            if not agent.operator:
                raise HubError(403, "critical messages require the operator flag")
            if not self.critical_budget.allow(agent.id):
                raise HubError(429, "critical budget exhausted (max per hour)")

        urgency, downgraded = payload.urgency, False
        if urgency == Urgency.interrupt and not payload.critical:
            # Crying wolf has a price: over-budget interrupts are delivered,
            # but demoted and visibly marked as such.
            if not self.interrupt_budget.allow(agent.id):
                urgency, downgraded = Urgency.next_turn, True

        message = self.db.insert_message(
            channel, agent.id, kind=Kind.message.value, status=payload.status.value,
            urgency=urgency.value, title=sanitize_title(payload.title), body=payload.body,
            data=payload.data, reply_to=payload.reply_to,
            critical=payload.critical, downgraded=downgraded, to=payload.to,
        )
        self._wake(message)
        return message

    def _post_system(self, channel: str, body: str) -> None:
        message = self.db.insert_message(
            channel, "hub", kind=Kind.system.value, status="fyi", urgency="inbox",
            title="", body=body, data=None, reply_to=None,
        )
        self._wake(message)

    def _wake(self, message: Message) -> None:
        self.fanout.publish(message.channel, {"type": "message", "message": message.model_dump()})
        self.notifier.notify()

    def get_messages(self, agent: AgentInfo, channel: str,
                     since_seq: int = 0, limit: int = 200) -> list[Message]:
        """Browse channel history. This is a bulk scan, NOT a deliberate read:
        it does NOT record read receipts, so paging history can no longer
        silently un-pin a critical or clear an obligation (v0.3 bug M2). Use
        read_message to actually attend to (and clear) a specific message."""
        self.require_membership(channel, agent.id)
        return self.db.get_messages(channel, since_seq, limit)

    # -- envelopes (viewer-specific delivery) ------------------------------------

    def envelope_for(self, viewer_id: str, message: Message,
                     sla_minutes: float | None = None) -> Envelope:
        parent = self.db.get_message(message.reply_to) if message.reply_to else None
        return self.attention.envelope_for(
            viewer_id, message,
            parent_sender=parent.sender if parent else None,
            # An asker's own follow-up must not silence its obligation.
            has_reply=self.db.has_reply(message.id, exclude_sender=message.sender),
            sla_minutes=sla_minutes if sla_minutes is not None
            else self.channel_sla(message.channel),
        )

    def channel_sla(self, channel: str) -> float:
        meta = self.db.store_get(channel, CHANNEL_META_KEY)
        if meta and isinstance(meta.value, dict):
            sla = meta.value.get("response_sla_minutes")
            if isinstance(sla, (int, float)) and sla > 0:
                return float(sla)
        return DEFAULT_RESPONSE_SLA_MINUTES

    def read_message(self, agent: AgentInfo, channel: str, message_id: str) -> list[Message]:
        """Fetch a body deliberately. Returns the message PLUS its unread
        reply-chain ancestors (bounded) — read decisions are only coherent
        per conversation burst, not per isolated message. Records read
        receipts (which is also what un-pins criticals)."""
        self.require_membership(channel, agent.id)
        message = self.db.get_message(message_id)
        if message is None or message.channel != channel:
            raise HubError(404, f"message '{message_id}' not found in '{channel}'")
        chain: list[Message] = [message]
        cursor = message
        for _ in range(MAX_READ_ANCESTORS):
            if not cursor.reply_to:
                break
            parent = self.db.get_message(cursor.reply_to)
            # Defense in depth against cross-channel disclosure: never follow a
            # reply_to that leaves this channel, even if post-time validation
            # were somehow bypassed. Membership was already checked above.
            if parent is None or parent.channel != channel:
                break
            if parent.sender == agent.id or self.db.has_read(parent.id, agent.id):
                break
            chain.append(parent)
            cursor = parent
        chain.reverse()  # oldest first: read the conversation in order
        for item in chain:
            self.db.mark_read(item.id, agent.id)
        return chain

    # -- inbox (cursor-based unread across all my channels) --------------------------

    def inbox(self, agent: AgentInfo, *, limit_per_channel: int = 100) -> list[Envelope]:
        """Unread envelopes, plus two sticky classes that survive cursor acks:
        unread criticals and outstanding obligations (open/blocked owed a
        reply, unread). Stickiness is what makes 'obligations can't rot' true
        even after an agent acks its triage. Order: critical, then escalated
        obligation, then arrival."""
        channels = self.db.channels_of(agent.id)
        by_id: dict[str, Message] = {}
        for channel in channels:
            cursor = self.db.get_cursor(agent.id, channel)
            for message in self.db.get_messages(channel, cursor, limit_per_channel):
                if message.sender != agent.id:
                    by_id[message.id] = message
        for message in self.db.unread_criticals(agent.id, channels):
            by_id[message.id] = message
        for message in self.db.unread_obligations(agent.id, channels):
            by_id[message.id] = message
        # channel_sla is one store read per channel; cache it across the sweep
        # instead of per message (v0.3 perf finding H3).
        sla_cache: dict[str, float] = {}
        envelopes = []
        for m in by_id.values():
            if m.channel not in sla_cache:
                sla_cache[m.channel] = self.channel_sla(m.channel)
            envelopes.append(self.envelope_for(agent.id, m, sla_minutes=sla_cache[m.channel]))
        envelopes.sort(key=lambda e: (not e.critical, not e.escalated, e.created_at))
        return envelopes

    async def wait_inbox(self, agent: AgentInfo, timeout: float) -> list[Envelope]:
        """Long-poll: return unread envelopes, waiting up to `timeout` for one."""
        self.bind_loop(asyncio.get_running_loop())  # producers wake us thread-safely
        deadline = time.time() + timeout
        while True:
            event = self.notifier.snapshot()  # grab BEFORE checking (no lost wake-ups)
            items = self.inbox(agent)
            remaining = deadline - time.time()
            if items or remaining <= 0:
                return items
            await Notifier.wait(event, min(remaining, 5.0))

    def ack_inbox(self, agent: AgentInfo, cursors: dict[str, int]) -> None:
        """Advance triage cursors: 'I have SEEN these envelopes' (not read bodies).
        Criticals are exempt — they stay pinned until read_message."""
        for channel, seq in cursors.items():
            self.require_membership(channel, agent.id)
            self.db.set_cursor(agent.id, channel, seq)

    # -- store -------------------------------------------------------------------

    def store_get(self, agent: AgentInfo, channel: str, key: str) -> StoreEntry:
        self.require_membership(channel, agent.id)
        entry = self.db.store_get(channel, key)
        if entry is None:
            raise HubError(404, f"key '{key}' not found in '{channel}' store")
        return entry

    def store_set(self, agent: AgentInfo, channel: str, key: str, value: Any,
                  expect_version: int | None = None) -> StoreEntry:
        self.require_membership(channel, agent.id)
        if len(json.dumps(value).encode()) > MAX_STORE_VALUE_BYTES:
            raise HubError(413, f"store value exceeds {MAX_STORE_VALUE_BYTES} bytes")
        if key.startswith(RESERVED_STORE_PREFIX):
            if self.db.member_role(channel, agent.id) != "owner":
                raise HubError(403, f"'{key}' is channel-level metadata: owner-writable only")
            if key == CHANNEL_META_KEY:
                self._validate_channel_meta(value)
        return self.db.store_set(channel, key, value, agent.id, expect_version)

    @staticmethod
    def _validate_channel_meta(value: Any) -> None:
        if not isinstance(value, dict):
            raise HubError(400, "channel:meta must be an object")
        unknown = set(value) - _META_FIELDS
        if unknown:
            raise HubError(400, f"unknown channel:meta fields: {sorted(unknown)} "
                                f"(allowed: {sorted(_META_FIELDS)})")
        language = value.get("language")
        if language is not None and language not in _META_LANGUAGES:
            raise HubError(400, f"channel:meta.language must be one of "
                                f"{sorted(_META_LANGUAGES)} (got {language!r})")

    def channel_info(self, agent: AgentInfo, channel: str) -> dict[str, Any]:
        """Everything an agent needs before first post: channel, metadata, members."""
        self.require_membership(channel, agent.id)
        info = self.db.get_channel(channel)
        meta = self.db.store_get(channel, CHANNEL_META_KEY)
        meta_value = meta.value if meta else None
        language = "plain"
        if isinstance(meta_value, dict) and meta_value.get("language") in _META_LANGUAGES:
            language = meta_value["language"]
        return {
            "channel": info.model_dump() if info else None,
            "meta": meta_value,
            "members": [m.model_dump() for m in self.db.list_members(channel)],
            "response_sla_minutes": self.channel_sla(channel),
            "language": language,
            "is_dm": channel.startswith(DM_PREFIX),
        }

    # -- colleague notes (private, subjective, advisory) ---------------------------

    def set_note(self, agent: AgentInfo, subject: str, note: str) -> ColleagueNote:
        if not self.db.agent_exists(subject):
            raise HubError(404, f"agent '{subject}' is not registered")
        if len(note) > 2000:
            raise HubError(413, "note exceeds 2000 characters")
        self.db.set_note(agent.id, subject, note)
        return ColleagueNote(observer=agent.id, subject=subject, note=note,
                             updated_at=time.time())

    def get_notes(self, agent: AgentInfo, subject: str | None = None) -> list[dict[str, Any]]:
        """Only the observer can read their own notes — subjectivity by design."""
        if subject is not None:
            note = self.db.get_note(agent.id, subject)
            return [note] if note else []
        return self.db.get_notes(agent.id)

    def store_keys(self, agent: AgentInfo, channel: str) -> list[dict[str, Any]]:
        self.require_membership(channel, agent.id)
        return self.db.store_keys(channel)

    def get_presence(self, agent: AgentInfo, target_id: str):
        """Presence is visible to yourself, to operators, and to agents you
        share a channel with — not to arbitrary registrants (avoids a global
        who's-online / who-exists oracle)."""
        if agent.id != target_id and not agent.operator:
            shared = set(self.db.channels_of(agent.id)) & set(self.db.channels_of(target_id))
            if not shared:
                raise HubError(404, f"no visible presence for '{target_id}'")
        return self.presence.get(target_id)

    # -- live subscription (used by the WebSocket endpoint) -------------------------

    def subscribe(self, agent: AgentInfo, channels: list[str],
                  queue: asyncio.Queue, since: dict[str, int] | None = None) -> list[Message]:
        """Register a live queue; return backlog for requested cursors (catch-up)."""
        backlog: list[Message] = []
        for channel in channels:
            self.require_membership(channel, agent.id)
            self.fanout.subscribe(channel, queue)
            if since and channel in since:
                backlog.extend(self.db.get_messages(channel, since[channel]))
        backlog.sort(key=lambda m: (m.channel, m.seq))
        return backlog

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.fanout.unsubscribe_all(queue)
