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
from collections import deque
from typing import Any

from ..db import Database
from ..ids import new_token
from ..models import (
    DM_PREFIX,
    FS_PREFIX,
    MAX_ABOUT_CHARS,
    MAX_ASK_CHARS,
    MAX_ASKS,
    MAX_ASSIGNEE_CHARS,
    MAX_BODY_BYTES,
    MAX_SIGNATURE_CHARS,
    MAX_DATA_BYTES,
    MAX_FS_PATH_CHARS,
    MAX_STORE_VALUE_BYTES,
    AgentInfo,
    ColleagueNote,
    Envelope,
    FsFile,
    Kind,
    Message,
    PostMessage,
    Status,
    StoreEntry,
    Urgency,
    dm_channel_name,
    sanitize_text,
    sanitize_title,
)
from .attention import DEFAULT_RESPONSE_SLA_MINUTES, AttentionPolicy, SlidingWindowBudget
from .notify import FanOut, LoopBinder, Notifier
from .obligations import asks_of, discharge_state
from .presence import PresenceTracker
from .ratelimit import RateLimiter

RESERVED_STORE_PREFIX = "channel:"   # channel-level keys: owner-writable only
CHANNEL_META_KEY = "channel:meta"
_META_FIELDS = {"purpose", "norms", "expected_traffic", "response_sla_minutes", "language",
                "authorship_required", "state"}
_CHANNEL_STATES = {"open", "closed"}
_META_LANGUAGES = {"plain", "terse", "structured"}
MAX_READ_ANCESTORS = 5


def _derived_description(head: str) -> str:
    """Fallback description for files whose writer set none: the first
    non-empty content line, de-markdowned, control-stripped and capped — so a
    listing is never a bare path dump, even for pre-description files."""
    for line in (head or "").splitlines():
        line = sanitize_text(" ".join(line.strip().lstrip("#*->|`").split()), 120)
        if line:
            return line
    return ""


class HubError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class HubService:
    def __init__(self, db: Database, *, rate_per_minute: float = 60.0,
                 interrupts_per_hour: int = 6, criticals_per_hour: int = 5,
                 notify_sink=None) -> None:
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
        # Hub-written notify files (see hub/notify_sink.py): liveness without
        # resident processes — the hub maintains each local agent's
        # <id>-inbox.log itself, the way the file mailbox's filesystem did.
        self.notify_sink = notify_sink
        # Refused sends, per agent (ring buffer): makes "can this agent send?"
        # verifiable by the operator instead of assumed.
        self.refusals: dict[str, deque] = {}

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
        # Every authenticated call is a liveness signal: MCP/REST-only tabs
        # have no push connection, and without this they read "offline" while
        # visibly working.
        self.presence.touch(agent.id)
        return agent

    # -- channels ---------------------------------------------------------------

    def create_channel(self, agent: AgentInfo, name: str, private: bool = True) -> dict[str, Any]:
        # A channel name is the one peer-chosen identifier that flows verbatim
        # into notify-file lines, `agora listen` wake sentinels and digests.
        # Control characters (newline/tab/CR/ESC…) are never a legitimate slug
        # and would let a crafted name smuggle a second line into any of those
        # single-line surfaces, so reject them at the source (same idiom as
        # _normalize_fs_path). Downstream sentinel neutralization stays as
        # defense in depth; this closes the hole where it starts.
        if (not name or "/" in name or " " in name
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise HubError(400, "channel name must be a simple slug "
                                "(no spaces, slashes or control characters)")
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
        # Idempotent get-or-create: concurrent first-contact from both peers must
        # not race into a 500, and membership is (re)asserted every call so a
        # peer that once left can always re-open the DM. add_member is
        # INSERT OR IGNORE, so re-asserting is a no-op for existing members.
        _, created = self.db.ensure_channel(name, private=True, created_by="hub",
                                            add_owner=False)
        self.db.add_member(name, agent.id, role="member")
        self.db.add_member(name, peer, role="member")
        if created:
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

    def _validate_asks(self, raw: Any, status: Status) -> list[dict[str, Any]]:
        """Normalize + validate structured asks. Applied to whatever ends up in
        the message data — whether it arrived via the typed `asks` param or was
        hand-crafted into the raw `data` payload — so there is no bypass path."""
        if status not in (Status.open, Status.blocked):
            raise HubError(400, "asks[] are only allowed on open/blocked messages")
        if not isinstance(raw, list):
            raise HubError(400, "asks must be a list")
        if len(raw) > MAX_ASKS:
            raise HubError(400, f"too many asks (max {MAX_ASKS})")
        seen: set[str] = set()
        norm: list[dict[str, Any]] = []
        for a in raw:
            if not isinstance(a, dict) or a.get("id") is None:
                raise HubError(400, "each ask must be an object with an id")
            aid = str(a["id"]).strip()
            if not aid or aid in seen:
                raise HubError(400, "ask ids must be unique and non-empty")
            seen.add(aid)
            entry = {"id": aid, "text": sanitize_text(str(a.get("text", "")), MAX_ASK_CHARS)}
            if a.get("assignee"):
                entry["assignee"] = sanitize_text(str(a["assignee"]), MAX_ASSIGNEE_CHARS)
            norm.append(entry)
        return norm

    def _validate_answers(self, raw: Any, status: Status, reply_to: str | None) -> list[str]:
        if status != Status.reply or not reply_to:
            raise HubError(400, "answers[] are only allowed on a reply with reply_to")
        if not isinstance(raw, list):
            raise HubError(400, "answers must be a list")
        answered = [str(x) for x in raw]
        parent = self.db.get_message(reply_to)
        parent_ids = {str(a["id"]) for a in asks_of(parent)} if parent else set()
        if parent_ids:
            unknown = [a for a in answered if a not in parent_ids]
            if unknown:
                raise HubError(400, f"answers reference unknown ask ids: {unknown}")
        return answered

    def _prepare_structured(self, payload: PostMessage) -> dict[str, Any] | None:
        """Validate and merge structured asks/answers into the message `data`.

        - `asks` are numbered questions; only meaningful on an open/blocked
          message (the thing that carries an obligation). Ids must be unique and
          non-empty; text/assignee are sanitized and bounded like any
          guaranteed-read field.
        - `answers` list the ask ids a reply discharges; only on a `reply` that
          names its `reply_to`. If the parent declares asks, the answered ids must
          exist there (fail loud, never silently mis-file); if the parent has no
          asks, answers are accepted as a harmless no-op.

        Validation runs on the EFFECTIVE fields regardless of how they arrived —
        the typed `asks`/`answers` params OR a hand-crafted `data` payload — so a
        raw-data write cannot smuggle in duplicate ids or unsanitized text.
        """
        data = dict(payload.data) if payload.data else {}
        if payload.asks is not None:
            data["asks"] = [a.model_dump(exclude_none=True) for a in payload.asks]
        if payload.answers is not None:
            data["answers"] = [str(x) for x in payload.answers]
        if "asks" in data:
            data["asks"] = self._validate_asks(data["asks"], payload.status)
        if "answers" in data:
            data["answers"] = self._validate_answers(data["answers"], payload.status,
                                                     payload.reply_to)
        if payload.signature is not None:
            # Reserved authorship token: opaque, stored verbatim (bounded), not
            # yet verified. Consumers may read it; the hub attaches no trust.
            data["signature"] = str(payload.signature)[:MAX_SIGNATURE_CHARS]
        return data or None

    def channel_ledger(self, agent: AgentInfo, channel: str, *, verify: bool = True) -> dict[str, Any]:
        """The channel's verbatim ledger: the complete, ordered, append-only
        transcript (every turn) plus the hash-chain `head` that commits to it —
        the durable common record of a room/session that any participant can
        read and verify, whatever system they run on. Membership-gated like any
        read. `verify` recomputes the chain to confirm it is intact."""
        self.require_membership(channel, agent.id)
        turns, head = self.db.channel_ledger(channel)
        result: dict[str, Any] = {"channel": channel, "count": len(turns),
                                  "head": head, "turns": turns}
        if verify:
            v = self.db.verify_channel(channel)
            result["verified"] = v["ok"]
            result["broken_at"] = v["broken_at"]
        return result

    def channel_state(self, channel: str) -> str:
        """A channel is `open` (default) or `closed`. Closed = its session/room
        ended; new member posts are refused. Owner-controlled via channel:meta."""
        meta = self.db.store_get(channel, CHANNEL_META_KEY)
        if meta and isinstance(meta.value, dict) and meta.value.get("state") == "closed":
            return "closed"
        return "open"

    def post_message(self, agent: AgentInfo, channel: str, payload: PostMessage) -> Message:
        """Post with a refusal audit: a refused send previously left no trace
        anywhere, so "agent X never answers" was indistinguishable from
        "agent X is being blocked" (field finding). Every HubError is recorded
        per agent and surfaced in the operator status overview."""
        try:
            return self._post_message(agent, channel, payload)
        except HubError as e:
            log = self.refusals.setdefault(agent.id, deque(maxlen=50))
            log.append({"ts": time.time(), "channel": channel,
                        "code": e.status_code, "detail": e.detail})
            raise

    def _post_message(self, agent: AgentInfo, channel: str, payload: PostMessage) -> Message:
        self.require_membership(channel, agent.id)
        if self.channel_state(channel) == "closed":
            # A room whose session died accepts no more turns — the bridge and
            # any subscriber get a clean 409 instead of writing into a dead room.
            raise HubError(409, f"channel '{channel}' is closed to new posts")
        if len(payload.body.encode()) > MAX_BODY_BYTES:
            raise HubError(413, f"body exceeds {MAX_BODY_BYTES} bytes")
        data = self._prepare_structured(payload)
        if data is not None and len(json.dumps(data).encode()) > MAX_DATA_BYTES:
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
        wait = self.ratelimiter.acquire(agent.id)
        if wait > 0.0:
            raise HubError(429, f"rate limit exceeded — retry in {wait:.1f}s "
                                "(steady pace; are you in a reply loop?)")

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
            data=data, reply_to=payload.reply_to,
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
        payload = {"type": "message", "message": message.model_dump()}
        self.fanout.publish(message.channel, payload)
        # Membership-keyed fan-out: reaches connected members whose channel
        # subscription predates this channel's existence (e.g. a DM opened
        # after their watcher connected — previously silently undeliverable
        # until the watcher restarted). The "agent/" prefix cannot collide
        # with channel names ("/" is rejected in channel slugs). Clients
        # dedup by per-channel seq, so double delivery is harmless.
        for member in self.db.list_members(message.channel):
            self.fanout.publish(f"agent/{member.agent_id}", payload)
            # Hub-written notify file: each member's <id>-inbox.log stays
            # fresh with zero agent-side processes (viewer-specific envelope,
            # skip the sender's own posts, best-effort).
            if self.notify_sink is not None and member.agent_id != message.sender:
                self.notify_sink.deliver(
                    member.agent_id, self.envelope_for(member.agent_id, message))
        self.notifier.notify()

    def get_messages(self, agent: AgentInfo, channel: str,
                     since_seq: int = 0, limit: int = 200) -> list[Message]:
        """Browse channel history. This is a bulk scan, NOT a deliberate read:
        it does NOT record read receipts, so paging history can no longer
        silently un-pin a critical or clear an obligation (v0.3 bug M2). Use
        read_message to actually attend to (and clear) a specific message."""
        self.require_membership(channel, agent.id)
        return self.db.get_messages(channel, since_seq, limit)

    def channel_digest(self, agent: AgentInfo, channel: str) -> dict[str, Any]:
        """Fold a channel's history into actionable knowledge — mechanically,
        from structure the messages already carry (no NLP, no embeddings):

        - open_questions: open/blocked messages not yet discharged, with their
          pending ask texts (asks/answers make Q->A pairs mechanical).
        - decided: discharged obligations (who answered) and `resolved` posts.
        - decisions: the channel store's `decision:*` keys — the room's
          distilled, versioned decision record (written by convention when a
          thread resolves).

        This is the 'cheap view' half of the knowledge norm; the distillation
        practice (writing decision keys) stays with the agents."""
        self.require_membership(channel, agent.id)
        open_questions: list[dict[str, Any]] = []
        decided: list[dict[str, Any]] = []
        cursor = 0
        while True:
            page = self.db.get_messages(channel, cursor)
            if not page:
                break
            cursor = page[-1].seq
            for m in page:
                if m.kind != Kind.message:
                    continue
                brief = {"seq": m.seq, "id": m.id, "from": m.sender,
                         "title": m.title, "created_at": m.created_at}
                if m.status in (Status.open, Status.blocked):
                    replies = self.db.replies_to(m.id)  # one query, reused
                    state = discharge_state(m, replies)
                    # Resolution-by-follow-up: a `resolved` reply in the thread
                    # closes the question regardless of sender — otherwise an
                    # asker who answered their own question would sit in
                    # open_questions forever while their resolved post sits in
                    # decided (review H2, self-contradiction).
                    self_resolved = any(r.status == Status.resolved for r in replies)
                    if state.discharged or self_resolved:
                        if asks_of(m):
                            # Credit only repliers who actually answered an ask
                            # (a "bump" reply must not be listed, review M2).
                            ask_ids = {str(a["id"]) for a in asks_of(m)}
                            answered_by = sorted({
                                r.sender for r in replies
                                if r.sender != m.sender and ask_ids
                                & {str(x) for x in (r.data or {}).get("answers", []) or []}
                            })
                        else:
                            answered_by = sorted({r.sender for r in replies
                                                  if r.sender != m.sender})
                        decided.append({**brief, "answered_by": answered_by,
                                        **({"self_resolved": True}
                                           if self_resolved and not state.discharged else {})})
                    else:
                        asks = {str(a["id"]): a.get("text", "") for a in asks_of(m)}
                        open_questions.append({
                            **brief, "status": m.status.value,
                            "pending_asks": [{"id": i, "text": asks.get(i, "")}
                                             for i in state.pending],
                        })
                elif m.status == Status.resolved:
                    decided.append({**brief, "resolved": True})
        decisions = []
        for entry in self.db.store_keys(channel):
            if not entry["key"].startswith("decision:"):
                continue
            stored = self.db.store_get(channel, entry["key"])
            if stored is not None:
                decisions.append({"key": entry["key"], "value": stored.value,
                                  "version": stored.version,
                                  "updated_by": stored.updated_by})
        # open_questions must be complete (an unanswered seq-5 question still
        # matters), but `decided` grows forever: cap it newest-first and keep
        # the total so truncation is visible (review M1).
        decided_total = len(decided)
        decided = sorted(decided, key=lambda d: d["seq"], reverse=True)[:50]
        return {
            "channel": channel,
            "open_questions": open_questions,
            "decided": decided,
            "decisions": decisions,
            "counts": {"open_questions": len(open_questions),
                       "decided_shown": len(decided), "decided_total": decided_total,
                       "decisions": len(decisions)},
        }

    # -- envelopes (viewer-specific delivery) ------------------------------------

    def envelope_for(self, viewer_id: str, message: Message,
                     sla_minutes: float | None = None) -> Envelope:
        parent = self.db.get_message(message.reply_to) if message.reply_to else None
        # Obligation discharge (only meaningful for open/blocked): a message with
        # structured asks is discharged only when every ask is answered, so a
        # partial answer keeps it escalating and its pending asks visible.
        discharged, pending, total = False, [], 0
        if message.status in (Status.open, Status.blocked):
            state = discharge_state(message, self.db.replies_to(message.id))
            discharged = state.discharged
            pending, total = state.pending, state.total
        return self.attention.envelope_for(
            viewer_id, message,
            parent_sender=parent.sender if parent else None,
            has_reply=discharged, pending_asks=pending, ask_total=total,
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
        # Obligations stay pinned until DISCHARGED (every ask answered), not just
        # until any reply exists — so a partially-answered open message does not
        # silently drop out of the inbox.
        for message in self.db.unread_obligation_candidates(agent.id, channels):
            if not discharge_state(message, self.db.replies_to(message.id)).discharged:
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
        Criticals are exempt — they stay pinned until read_message.

        The requested seq is clamped to the channel's current head: a buggy or
        hand-written client cannot leapfrog its cursor past messages that do not
        exist yet, which would otherwise permanently hide unread non-sticky
        traffic that arrives later below the inflated cursor.
        """
        for channel, seq in cursors.items():
            self.require_membership(channel, agent.id)
            self.db.set_cursor(agent.id, channel, min(seq, self.db.last_seq(channel)))

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
        if key.startswith(FS_PREFIX):
            # File keys are owned by the VFS API so every mutation is validated
            # and emits an audit event; a raw store_set would bypass both.
            raise HubError(403, f"'{key}' is a virtual-filesystem path: use the fs_* API")
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
        # Reserved: a channel may declare it will require authorship once the
        # gateway enforces it. Validated as a bool now; not enforced yet.
        authorship = value.get("authorship_required")
        if authorship is not None and not isinstance(authorship, bool):
            raise HubError(400, "channel:meta.authorship_required must be a boolean")
        # Channel lifecycle: a room/session channel is `open` while live and
        # `closed` once its session ends. Owner-set (meta is owner-writable);
        # a closed channel refuses new member posts (the 409 the room bus needs).
        state = value.get("state")
        if state is not None and state not in _CHANNEL_STATES:
            raise HubError(400, f"channel:meta.state must be one of {sorted(_CHANNEL_STATES)}")

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
            "state": self.channel_state(channel),
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

    # -- per-channel virtual filesystem ------------------------------------------
    #
    # A channel's files live as reserved `fs/<path>` keys in its store, so they
    # inherit membership gating, CAS versioning and durability for free. Every
    # mutation also appends a `Kind.fs` audit message to the channel log, making
    # the file history replayable and giving subscribed agents a change signal.
    # This is the shared, network-accessible "book" that lets agents on
    # different machines consult and edit a common workspace without a shared disk.

    @staticmethod
    def _normalize_fs_path(path: str) -> str:
        """Validate a relative POSIX-ish path and return it normalized. Rejects
        absolute paths, parent traversal, empty/whitespace segments, backslashes
        and control characters — so a path can never escape its channel or spoof
        the store-key namespace."""
        if not path or len(path) > MAX_FS_PATH_CHARS:
            raise HubError(400, f"fs path must be 1..{MAX_FS_PATH_CHARS} chars")
        if "\\" in path or "\x00" in path or any(ord(c) < 32 for c in path):
            raise HubError(400, "fs path contains illegal characters")
        if path.startswith("/"):
            raise HubError(400, "fs path must be relative (no leading '/')")
        segments = path.split("/")
        if any(seg in ("", ".", "..") or seg.strip() != seg for seg in segments):
            raise HubError(400, "fs path has empty, '.', '..' or whitespace-padded segments")
        return "/".join(segments)

    def _post_fs_audit(self, channel: str, actor: str, op: str, path: str,
                       version: int, size_bytes: int) -> None:
        """Append-only record of a file mutation (who/what/when), authored by the
        actor so `fs_history` and the mirror can replay the file's evolution."""
        message = self.db.insert_message(
            channel, actor, kind=Kind.fs.value, status="fyi", urgency="inbox",
            title=f"fs:{op} {path}", body="",
            data={"op": op, "path": path, "version": version, "size_bytes": size_bytes},
            reply_to=None,
        )
        self._wake(message)

    def fs_write(self, agent: AgentInfo, channel: str, path: str, content: str,
                 mime: str = "text/markdown", expect_version: int | None = None,
                 description: str = "") -> FsFile:
        """Create or edit a file (compare-and-swap via `expect_version`; 0 means
        'must not exist yet'). `description` is the writer's one-line statement
        of what the file is, shown in listings; sanitized and capped like a
        title. Returns the new FsFile with its bumped version."""
        self.require_membership(channel, agent.id)
        norm = self._normalize_fs_path(path)
        if not isinstance(content, str):
            raise HubError(400, "fs content must be text")
        size = len(content.encode())
        if size > MAX_STORE_VALUE_BYTES:
            raise HubError(413, f"fs file exceeds {MAX_STORE_VALUE_BYTES} bytes")
        # sanitize_text also strips control chars (ESC/BEL survive str.split —
        # they would otherwise reach the operator's terminal; security M1).
        description = sanitize_text(str(description or ""), 200)
        value = {"content": content, "mime": mime}
        if description:
            value["description"] = description
        entry = self.db.fs_put(channel, FS_PREFIX + norm, value, agent.id, expect_version)
        self._post_fs_audit(channel, agent.id, "put", norm, entry.version, size)
        return FsFile(path=norm, content=content, mime=mime, description=description,
                      size_bytes=size, version=entry.version,
                      updated_by=entry.updated_by, updated_at=entry.updated_at)

    def fs_read(self, agent: AgentInfo, channel: str, path: str,
                version: int | None = None) -> FsFile:
        """Read the head, or — with `version` — any archived version verbatim,
        with its original author and date. Every write archives its content
        (fs_versions), so history is recoverable, not just countable."""
        self.require_membership(channel, agent.id)
        norm = self._normalize_fs_path(path)
        if version is not None:
            if not 1 <= version <= 2**62:  # SQLite INTEGER bound -> clean 404, not a 500
                raise HubError(404, f"version {version} of '{norm}' is not in the archive")
            row = self.db.fs_version(channel, FS_PREFIX + norm, version)
            if row is None:
                raise HubError(404, f"version {version} of '{norm}' is not in the "
                                    "archive (it may predate version archiving, "
                                    "or be a delete)")
        else:
            row = self.db.fs_get(channel, FS_PREFIX + norm)
            if row is None or row["deleted"]:  # a tombstoned file reads as absent
                raise HubError(404, f"file '{norm}' not found in '{channel}'")
        value = row["value"] if isinstance(row["value"], dict) else {}
        content = value.get("content", "")
        return FsFile(path=norm, content=content, mime=value.get("mime", "text/markdown"),
                      description=value.get("description", ""),
                      size_bytes=len(content.encode()), version=row["version"],
                      updated_by=row["updated_by"], updated_at=row["updated_at"])

    def fs_list(self, agent: AgentInfo, channel: str, prefix: str = "") -> list[dict[str, Any]]:
        """List live files (metadata only, no content) under an optional prefix
        — the channel's table of contents. Every row carries a `description`:
        the writer's own when set, else derived from the file's first content
        line, so old files are never blank. Tombstoned files excluded."""
        self.require_membership(channel, agent.id)
        rows = self.db.fs_keys_live(channel, FS_PREFIX + prefix)
        return [
            {"path": r["key"][len(FS_PREFIX):], "version": r["version"],
             "updated_by": r["updated_by"], "updated_at": r["updated_at"],
             "size": r["size"],
             "description": r["description"] or _derived_description(r["head"]),
             "described": bool(r["description"])}
            for r in rows
        ]

    def fs_delete(self, agent: AgentInfo, channel: str, path: str,
                  expect_version: int | None = None) -> bool:
        """Delete a file (CAS via `expect_version`). Tombstones it so the path's
        version stays monotonic across delete+recreate (CAS remains a valid
        fence). Returns False if the file was absent or already deleted."""
        self.require_membership(channel, agent.id)
        norm = self._normalize_fs_path(path)
        new_version = self.db.fs_remove(channel, FS_PREFIX + norm, agent.id, expect_version)
        if new_version is None:
            return False
        self._post_fs_audit(channel, agent.id, "delete", norm, new_version, 0)
        return True

    def fs_history(self, agent: AgentInfo, channel: str, path: str,
                   since_seq: int = 0, limit: int = 200) -> list[Message]:
        """The append-only audit trail (put/delete events) for one file, oldest
        first — replayable history even though the store holds only current head."""
        self.require_membership(channel, agent.id)
        norm = self._normalize_fs_path(path)
        out = []
        for m in self.db.get_messages(channel, since_seq, limit=10_000):
            if m.kind == Kind.fs.value and (m.data or {}).get("path") == norm:
                out.append(m)
                if len(out) >= limit:
                    break
        return out

    def agent_status_overview(self) -> list[dict[str, Any]]:
        """Operator overview: per agent, presence + unread count + the oldest
        still-pending obligation's age. Reuses the exact inbox computation the
        agent itself would see, so the numbers cannot disagree with reality."""
        now = time.time()
        out = []
        for agent_id in self.db.list_agent_ids():
            envelopes = self.inbox(AgentInfo(id=agent_id, name=agent_id))
            pending = [e for e in envelopes
                       if e.status in (Status.open, Status.blocked) or e.critical]
            oldest = min((e.created_at for e in pending), default=None)
            presence = self.presence.get(agent_id)
            refusals = [r for r in self.refusals.get(agent_id, ())
                        if now - r["ts"] < 3600.0]
            out.append({
                "agent_id": agent_id,
                "state": presence.state,
                "unread": len(envelopes),
                "pending_obligations": len(pending),
                "oldest_pending_minutes": round((now - oldest) / 60, 1) if oldest else None,
                "refused_sends_1h": len(refusals),
                "last_refusal": refusals[-1] if refusals else None,
            })
        return out

    def list_presence(self, agent: AgentInfo) -> list:
        """Presence of every agent the caller shares a channel with (self
        included). Operators see everyone. Same visibility boundary as
        get_presence: no global who-exists oracle for ordinary agents."""
        if agent.operator:
            visible = set(self.db.list_agent_ids())
        else:
            visible = {agent.id}
            for channel in self.db.channels_of(agent.id):
                visible.update(m.agent_id for m in self.db.list_members(channel))
        return [self.presence.get(a) for a in sorted(visible)]

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
        """Register a live queue; return backlog for requested cursors (catch-up).

        The backlog is fully paginated: a client that reconnects after a long
        outage (a gap larger than one page) gets EVERY message it missed, not
        just the first page. Silently truncating catch-up would break the
        at-least-once contract for remote agents whose links flap.
        """
        backlog: list[Message] = []
        for channel in channels:
            self.require_membership(channel, agent.id)
            self.fanout.subscribe(channel, queue)
            if since and channel in since:
                # Fully paginate: a client reconnecting after a long outage
                # (a gap larger than one page) must receive EVERY message it
                # missed, not just the first page. Silent truncation would
                # break at-least-once catch-up for remote agents whose links
                # flap. (Cold start with no pinned cursor is handled by the
                # client-side inbox sweep on connect, not here.)
                cursor = since[channel]
                while True:
                    page = self.db.get_messages(channel, cursor)
                    if not page:
                        break
                    backlog.extend(page)
                    cursor = page[-1].seq
        backlog.sort(key=lambda m: (m.channel, m.seq))
        return backlog

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.fanout.unsubscribe_all(queue)
