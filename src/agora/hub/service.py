"""HubService: all hub behavior behind one object, transport-agnostic.

The HTTP API and the WebSocket endpoint are thin translations onto this
class, so behavior (membership enforcement, ordering, rate limits, wake-ups)
is defined exactly once and is directly unit-testable without a server.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections import deque
from typing import Any

from ..db import Database, JoinTokenRefused
from ..governance import CHARTER_PATH, HUB_RULES_DEFAULT, RESERVED_FS_PREFIX
from ..ids import new_token
from ..models import (
    DM_PREFIX,
    FS_PREFIX,
    MAX_ABOUT_CHARS,
    MAX_ASK_CHARS,
    MAX_ASKS,
    MAX_ASSIGNEE_CHARS,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_MESSAGE,
    MAX_BODY_BYTES,
    MAX_CHANNEL_ATTACHMENT_BYTES,
    MAX_CONTENT_TYPE_CHARS,
    MAX_FILENAME_CHARS,
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
    parse_work_id,
    sanitize_text,
    sanitize_title,
)
from .attention import DEFAULT_RESPONSE_SLA_MINUTES, AttentionPolicy, SlidingWindowBudget
from .notify import FanOut, LoopBinder, Notifier
from .obligations import (
    ask_addressees,
    asks_of,
    closed_authoritatively,
    discharge_state,
    pending_addressees,
)
from .presence import PresenceTracker
from .ratelimit import RateLimiter

RESERVED_STORE_PREFIX = "channel:"   # channel-level keys: owner-writable only
JOIN_TOKEN_PREFIX = "agora-join_"    # agora-join_<id:8hex>.<secret:48hex>
MAX_JOIN_TOKEN_TTL = 30 * 86400.0    # hard cap (kubeadm defaults 24h; we cap 30d)
MAX_JOIN_TOKEN_USES = 100            # fleet provisioning ceiling
CHANNEL_META_KEY = "channel:meta"
_META_FIELDS = {"purpose", "norms", "expected_traffic", "response_sla_minutes", "language",
                "authorship_required", "state", "norms_required"}
_CHANNEL_STATES = {"open", "closed"}
_META_LANGUAGES = {"plain", "terse", "structured"}
MAX_READ_ANCESTORS = 5
DARK_REALERT_SECONDS = 6 * 3600.0   # flap guard: max one alert per agent per window

# Attachment serve hardening (0091): content types a browser could execute
# as active content are stored verbatim but SERVED as octet-stream, so the
# hub can never become a script origin. Matched on the lowercased media
# type with parameters stripped; +xml/+html structured suffixes count too.
ACTIVE_CONTENT_TYPES = frozenset({
    "text/html", "application/xhtml+xml", "image/svg+xml",
    "text/xml", "application/xml",
    "application/javascript", "text/javascript", "application/ecmascript",
})
_CONTENT_TYPE_OK = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def safe_serve_content_type(declared: str) -> str:
    """The content type the hub SERVES for a stored blob. The declared type
    is client metadata and never verified against the bytes (settled with
    the consumer, dm continuum#10-11): serving must stay safe even when it
    lies, so anything active — or malformed — goes out as octet-stream.

    INVARIANT (review hardening nit): this is the ONLY function whose output
    may feed a real Content-Type header. The stored declared type is
    CR/LF-stripped but not charset-restricted, so routing it straight into a
    response Content-Type would reintroduce the risk this closes — keep it
    behind this gate."""
    media = declared.split(";", 1)[0].strip().lower()
    if not _CONTENT_TYPE_OK.fullmatch(media):
        return "application/octet-stream"
    if media in ACTIVE_CONTENT_TYPES or media.endswith(("+xml", "+html")):
        return "application/octet-stream"
    return media


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
                 notify_sink=None,
                 max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
                 max_channel_attachment_bytes: int = MAX_CHANNEL_ATTACHMENT_BYTES) -> None:
        self.db = db
        self.max_attachment_bytes = max_attachment_bytes
        self.max_channel_attachment_bytes = max_channel_attachment_bytes
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
        # Operator ids, cached (closure authority checks run per envelope);
        # busted on registration — the only path that mints operators. The
        # generation counter closes the read/bust race (review LOW-2).
        self._operators: frozenset[str] | None = None
        self._op_gen = 0
        # Dark-episode ledger for the 0067 watchdog: agent -> first dark ts,
        # plus a re-alert cooldown per agent (flap guard, review MED-4).
        # In-memory by design: a hub restart re-alerts once, which is honest.
        self._dark_since: dict[str, float] = {}
        self._dark_alerted_at: dict[str, float] = {}
        # Deaf-seat episodes (0098): present-looking but reception-stale.
        self._deaf_since: dict[str, float] = {}
        self._deaf_alerted_at: dict[str, float] = {}
        # Stewardship (0084/0093): stale-claim alert dedupe lives in the
        # standing alert's steward_sig — read from the channel, restart-safe
        # — not in process memory.
        # Pause state cache (0069) + last long-pause reminder timestamp.
        self._pause_cache: dict[str, Any] | None = None
        self._pause_cache_at = 0.0
        self._pause_reminded_at = 0.0
        self._intervals_cache: list[tuple[float, float | None]] = []
        self._intervals_cache_at = 0.0
        # Delegation grants cache (0068).
        self._delegations_cache: list[dict[str, Any]] = []
        self._delegations_cache_at = 0.0
        # Directive-debt epoch (0102 hardening, c3379): peer reply/fyi
        # debts exist only for messages posted AFTER the feature deployed
        # on this hub. Applying the new owed class to history turned weeks
        # of settled traffic into 15+ phantom debts per seat overnight —
        # semantics changes must not rewrite the past. Persisted in the DB
        # (set once, first boot on >=0.12.20) so every restart agrees.
        # Operator-addressed words stay UNBOUNDED: few, human, and the
        # buried-directive case is exactly what 0101/0102 exist for.
        self._directive_epoch = float(self.db.meta_set_default(
            "directive_debt_epoch", str(time.time())))
        # Operator-key burst tripwire (0104, the Jul-14 impersonation): on a
        # shared machine any local process can read the operator's cached
        # key and speak as the human — unpreventable hub-side (the key IS
        # the credential), but a 13-DM multicast in 10s is MACHINE cadence.
        # Track operator post timestamps; a burst raises one loud alert.
        self._operator_posts: dict[str, deque] = {}
        self._operator_burst_alerted_at: dict[str, float] = {}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the serving event loop. Called by every async entry point
        (WebSocket connect, long-poll wait) so cross-thread wakes are safe."""
        self._binder.bind(loop)

    # -- auth -----------------------------------------------------------------

    @staticmethod
    def _validate_agent_id(agent_id: str) -> None:
        # ASCII-only, no double-dash (would collide with the dm:<a>--<b>
        # separator), reserved ids blocked, bounded length. Prevents Unicode
        # homoglyph impersonation of the one signal the model trusts: identity.
        # Shared by plain registration AND the join-token paths (mint pins and
        # redeem-time ids face the same rules; there is no laxer side door).
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?", agent_id):
            raise HubError(400, "agent id must be lowercase ascii [a-z0-9_-], 1-64 chars, "
                                "no leading/trailing dash")
        if "--" in agent_id:
            raise HubError(400, "agent id may not contain '--' (reserved dm separator)")
        if agent_id in {"hub", "all"}:
            raise HubError(400, f"'{agent_id}' is a reserved id")

    def register_agent(self, agent_id: str, name: str, operator: bool = False,
                       about: str = "") -> tuple[AgentInfo, str]:
        self._validate_agent_id(agent_id)
        self._require_not_hub_blocked_id(agent_id)
        if self.db.agent_retirement(agent_id) is not None:
            # A retired id stays RESERVED forever: re-registering it would let
            # a new principal inherit an old id's message attribution (0089).
            raise HubError(409, f"agent id '{agent_id}' is retired and cannot "
                                "be reused — ids are never recycled so history "
                                "attribution holds. An operator can restore the "
                                "original identity with unretire.")
        if self.db.agent_exists(agent_id):
            raise HubError(409, f"agent '{agent_id}' already exists")
        api_key = new_token("agora")
        info = self.db.register_agent(agent_id, name, api_key, operator,
                                      sanitize_text(about, MAX_ABOUT_CHARS))
        self._op_gen += 1
        self._operators = None  # bust the closure-authority cache
        return info, api_key

    def operator_ids(self) -> frozenset[str]:
        """Operator agent ids (closure authority, ADR-0003). Cached: it is
        consulted per envelope; registration is the only mutation path. The
        generation check means a racing registration can never freeze a
        stale set into the cache."""
        cached = self._operators
        if cached is not None:
            return cached
        gen = self._op_gen
        ids = frozenset(self.db.list_operator_ids())
        if gen == self._op_gen:
            self._operators = ids
        return ids

    def set_about(self, agent: AgentInfo, about: str) -> AgentInfo:
        """Self-description: scope of ownership, what to ask this agent about.
        Self-editable only; sanitized like titles (every joiner reads it)."""
        self._require_unpaused(agent)
        cleaned = sanitize_text(about, MAX_ABOUT_CHARS)
        self.db.set_about(agent.id, cleaned)
        return agent.model_copy(update={"about": cleaned})

    def authenticate(self, api_key: str) -> AgentInfo:
        agent = self.db.agent_by_key(api_key)
        if agent is None:
            raise HubError(401, "invalid api key")
        # Hub-scope moderation is a full lockout ("can't sign in"): every
        # authenticated call refuses while the block stands. The teaching
        # text names the term and the lift path — the one thing the locked
        # agent can still learn.
        block = self.db.block_get(self.HUB_SCOPE, agent.id)
        if block is not None:
            raise HubError(403, f"you are {self._block_phrase(block)} from "
                                "this hub"
                                + (f" — {block['reason']}" if block["reason"] else "")
                                + ". Access resumes when the block expires "
                                  "or an operator lifts it.")
        # Retirement is a NEUTRAL end-of-life, distinct from a block: the key
        # stops working with wording that never implies wrongdoing (0089).
        # An operator un-retires; the id itself stays reserved forever.
        retirement = self.db.agent_retirement(agent.id)
        if retirement is not None:
            raise HubError(403, "this identity has been retired"
                                + (f" ({retirement['reason']})" if retirement["reason"] else "")
                                + " — a decommissioned seat, not a block. An "
                                  "operator can restore it; the id is never reused.")
        # Every authenticated call is a liveness signal: MCP/REST-only tabs
        # have no push connection, and without this they read "offline" while
        # visibly working.
        self.presence.touch(agent.id)
        return agent

    # -- join tokens (scoped registration credentials; admin key stays home) ----

    def create_join_token(self, agent_id: str | None = None, about: str = "",
                          channels: list[str] | None = None,
                          ttl_seconds: float = 86400.0, max_uses: int = 1,
                          created_by: str = "admin") -> dict[str, Any]:
        """Mint a join token: registers exactly ONE (or max_uses) non-operator
        agent(s) and is valid on no other endpoint. Plaintext is returned once
        here; the hub stores only the secret's hash. Format
        `agora-join_<token_id:8hex>.<secret:48hex>` — the public token_id
        supports list/revoke without ever re-handling the secret."""
        if agent_id is not None:
            self._validate_agent_id(agent_id)
            if self.db.agent_exists(agent_id):
                # A token pinned to a taken id could never be redeemed; fail
                # the mint, not the (possibly remote, later) redemption.
                raise HubError(409, f"agent '{agent_id}' already exists")
        if not ttl_seconds > 0:
            raise HubError(400, "ttl_seconds must be positive")
        if ttl_seconds > MAX_JOIN_TOKEN_TTL:
            raise HubError(400, f"ttl_seconds exceeds the cap "
                                f"({int(MAX_JOIN_TOKEN_TTL)}s = 30 days)")
        if not 1 <= max_uses <= MAX_JOIN_TOKEN_USES:
            raise HubError(400, f"max_uses must be 1..{MAX_JOIN_TOKEN_USES}")
        preset = [c.strip() for c in (channels or []) if isinstance(c, str) and c.strip()]
        token_id = os.urandom(4).hex()
        secret = os.urandom(24).hex()  # 192-bit secret, the api-key idiom
        row = self.db.create_join_token(
            token_id, secret, agent_id, sanitize_text(about, MAX_ABOUT_CHARS),
            preset, created_by, ttl_seconds, max_uses)
        return {**row, "token": f"{JOIN_TOKEN_PREFIX}{token_id}.{secret}"}

    @staticmethod
    def _parse_join_token(token: str) -> tuple[str, str] | None:
        """`agora-join_<token_id>.<secret>` -> (token_id, secret), or None if
        the shape is wrong (never raises: shape errors are a clean 403)."""
        if not token.startswith(JOIN_TOKEN_PREFIX):
            return None
        token_id, sep, secret = token.removeprefix(JOIN_TOKEN_PREFIX).partition(".")
        if not sep or not token_id or not secret:
            return None
        return token_id, secret

    def redeem_join_token(self, token: str, agent_id: str | None = None,
                          about: str = "") -> tuple[AgentInfo, str, list[str]]:
        """Redeem a join token: register the agent (operator=False FORCED —
        a join credential can never mint privilege) and auto-join the token's
        PUBLIC preset channels. Private channels still require owner-minted
        invites — a join token must not become a side door through the
        confused-deputy guard. Consumption is atomic with registration (see
        db.redeem_join_token): a 409 id collision does NOT burn the token."""
        if self.hub_paused() is not None:
            raise HubError(423, "hub paused by the operator — onboarding resumes with the hub")
        parsed = self._parse_join_token(token)
        if parsed is None:
            raise HubError(403, "invalid join token")
        if agent_id is not None:
            self._validate_agent_id(agent_id)
            # Hub kicks/bans survive key loss: the id cannot re-register via a
            # join token either. (Token-locked ids skip this pre-check but stay
            # dead regardless — authenticate() refuses every call they make.)
            self._require_not_hub_blocked_id(agent_id)
        api_key = new_token("agora")
        try:
            info, preset = self.db.redeem_join_token(
                *parsed, agent_id=agent_id, name="", api_key=api_key,
                about=sanitize_text(about, MAX_ABOUT_CHARS))
        except JoinTokenRefused as e:
            raise HubError(e.status_code, e.detail) from e
        joined: list[str] = []
        for channel in preset:
            try:
                self.join_channel(info, channel, None)
                joined.append(channel)
            except HubError:
                # Missing or private channel: skipped, never fatal — the
                # registration already succeeded and the token is consumed.
                continue
        return info, api_key, joined

    def list_join_tokens(self) -> list[dict[str, Any]]:
        return self.db.list_join_tokens()

    def revoke_join_token(self, token_id: str) -> None:
        if not self.db.revoke_join_token(token_id):
            raise HubError(404, f"join token '{token_id}' not found "
                                "(expired tokens are purged)")

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
        self._require_unpaused(agent)
        if name == self.DARK_ALERTS_CHANNEL:
            # Squat guard (review HIGH-1): an agent pre-creating the alerts
            # channel would own its meta and read/route operator alerts.
            raise HubError(400, f"'{name}' is reserved for hub operator alerts")
        if name == self.HUB_SCOPE:
            # Moderation blocks key on scope, where 'hub' means the whole hub:
            # a channel with that name would make its channel-scope blocks
            # indistinguishable from hub-wide lockouts in authenticate().
            raise HubError(400, f"'{name}' is reserved (moderation scope name)")
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
        self._require_unpaused(agent, dm_channel_name(agent.id, peer))
        if peer == agent.id:
            raise HubError(400, "cannot open a direct channel with yourself")
        if not self.db.agent_exists(peer):
            raise HubError(404, f"agent '{peer}' is not registered")
        if self.db.agent_retirement(peer) is not None:
            raise HubError(404, f"agent '{peer}' has been retired "
                                "(decommissioned) — no new direct channel")
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
        self._require_unpaused(agent)
        # Only owners may extend the trust boundary of a private channel.
        # This blunts the confused-deputy risk of an LLM member being talked
        # into inviting an attacker (red-team finding).
        if self.db.channel_archived(channel):
            raise HubError(409, f"channel '{channel}' is archived (ended) — "
                                "no new invites")
        role = self.db.member_role(channel, agent.id)
        if role != "owner":
            raise HubError(403, "only the channel owner can create invites")
        if invitee is not None and not self.db.agent_exists(invitee):
            raise HubError(404, f"agent '{invitee}' is not registered")
        token = new_token("invite")
        self.db.create_invite(token, channel, invitee, agent.id, ttl_seconds)
        return token

    def join_channel(self, agent: AgentInfo, channel: str, invite_token: str | None) -> dict[str, Any]:
        self._require_unpaused(agent)
        info = self.db.get_channel(channel)
        if info is None:
            raise HubError(404, f"channel '{channel}' not found")
        if self.db.channel_archived(channel):
            raise HubError(409, f"channel '{channel}' is archived (ended) — "
                                "an operator must reopen it before anyone joins")
        if channel.startswith(DM_PREFIX) and not self.db.is_member(channel, agent.id):
            raise HubError(403, "direct channels cannot be joined")
        # A kick/ban must hold against BOTH join paths (public join and
        # owner-minted invites): the block outranks any invite token. A
        # PRIVATE channel also needs a fresh invite after a kick (the old one
        # was consumed and membership was removed), so the teaching text must
        # not promise bare expiry re-admits (review F3).
        block = self.db.block_get(channel, agent.id)
        if block is not None:
            tail = (". Rejoin when the block expires or is lifted"
                    + ("; this channel is private, so you will also need a "
                       "fresh invite." if info.private else "."))
            raise HubError(403, f"you are {self._block_phrase(block)} from "
                                f"'{channel}'"
                                + (f" — {block['reason']}" if block["reason"] else "")
                                + tail)
        if not self.db.is_member(channel, agent.id):
            if info.private:
                if not invite_token or self.db.redeem_invite(invite_token, agent.id) != channel:
                    raise HubError(403, "a valid invite token is required for this private channel")
            else:
                self.db.add_member(channel, agent.id)
            # TOCTOU close (review F5): a kick landing between the block_get
            # above and add_member would otherwise leave the agent a member
            # WITH an active block (posting/delivery gate on membership only).
            # Re-check under the now-committed membership and roll back.
            racing = self.db.block_get(channel, agent.id)
            if racing is not None:
                self.db.remove_member(channel, agent.id)
                raise HubError(403, f"you are {self._block_phrase(racing)} "
                                    f"from '{channel}'")
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
        # Membership is shared state and the departure broadcasts: frozen
        # during a pause like every other shared-world mutation (review MED-2).
        self._require_unpaused(agent, channel)
        # Withdraw the leaver's own reputation votes (0094 F2): a rater must
        # not be able to drive-by downvote then leave, stranding the vote
        # where neither they (membership gate) nor the target can remove it.
        # Votes ABOUT the leaver stay — colleagues' judgment outlives a
        # target's exit, exactly as with retirement.
        self.db.reputation_clear_rater(channel, agent.id)
        self.db.remove_member(channel, agent.id)
        self._post_system(channel, f"{agent.id} left")

    # -- messages -----------------------------------------------------------------

    #: Per-ask addressing cap (0077): more than 3 named answerers on ONE ask
    #: is diffusion of responsibility — use message-level `to` for broadcast.
    MAX_ASK_TO = 3

    def _validate_asks(self, raw: Any, status: Status, *, sender: str = "",
                       channel: str = "") -> list[dict[str, Any]]:
        """Normalize + validate structured asks. Applied to whatever ends up in
        the message data — whether it arrived via the typed `asks` param or was
        hand-crafted into the raw `data` payload — so there is no bypass path."""
        if status not in (Status.open, Status.blocked):
            raise HubError(400, "asks[] are only allowed on open/blocked messages")
        if not isinstance(raw, list):
            raise HubError(400, "asks must be a list")
        if len(raw) > MAX_ASKS:
            raise HubError(400, f"too many asks (max {MAX_ASKS})")
        members: set[str] | None = None
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
            if a.get("to"):
                # Per-ask addressing (0077, anti-lurk): naming a seat INSIDE an
                # ask must flag that seat mechanically — the field incident was
                # 70 asks in 48h naming seats only in prose, which flags nobody
                # and buries canvass rows in headline scroll.
                if not isinstance(a["to"], list):
                    raise HubError(400, f"ask '{aid}': to must be a list of agent ids")
                named = [str(x) for x in a["to"]]
                if len(named) > self.MAX_ASK_TO:
                    raise HubError(400, f"ask '{aid}' addresses {len(named)} seats "
                                        f"(max {self.MAX_ASK_TO}) — use the message-"
                                        "level to for broadcast")
                if sender and sender in named:
                    raise HubError(400, f"ask '{aid}': you cannot address an ask "
                                        "to yourself")
                if channel:
                    if members is None:
                        members = {m.agent_id for m in self.db.list_members(channel)}
                    outsiders = [n for n in named if n not in members]
                    if outsiders:
                        raise HubError(400, f"ask '{aid}' addresses non-members: "
                                            f"{outsiders} — describe_channel lists "
                                            "who is here; drop the name or leave "
                                            "the ask broadcast")
                entry["to"] = named
            norm.append(entry)
        return norm

    def _validate_answers(self, raw: Any, status: Status, reply_to: str | None,
                          sender: str) -> list[str]:
        if status != Status.reply or not reply_to:
            raise HubError(400, "answers[] are only allowed on a reply with reply_to")
        if not isinstance(raw, list):
            raise HubError(400, "answers must be a list")
        if not raw:
            raise HubError(400, "answers=[] is empty — drop the field, or name "
                                "the ask ids you are discharging")
        answered = [str(x) for x in raw]
        parent = self.db.get_message(reply_to)
        # Teaching refusals (0062/ADR-0003): an answers[] that cannot discharge
        # anything is refused WITH the correct gesture, instead of being
        # accepted and silently voided (four field incidents in one day —
        # c817, c1090/c1095, c1106, c1113 — all of them this shape).
        if answered and parent is not None:
            if parent.sender == sender:
                raise HubError(400, "your reply can never discharge your own asks "
                                    "— to close your own thread post "
                                    "status=resolved with reply_to it (that closes "
                                    "it everywhere); to answer, wait for others")
            if not asks_of(parent):
                raise HubError(400, "the message you replied to carries no asks — "
                                    "answers=[] discharges nothing here; reply to "
                                    "the message that carries the asks, or drop "
                                    "answers")
        parent_ids = {str(a["id"]) for a in asks_of(parent)} if parent else set()
        if parent_ids:
            unknown = [a for a in answered if a not in parent_ids]
            if unknown:
                raise HubError(400, f"answers reference unknown ask ids: {unknown}")
        return answered

    def _prepare_structured(self, payload: PostMessage, sender: str = "",
                            channel: str = "") -> dict[str, Any] | None:
        """Validate and merge structured asks/answers into the message `data`.

        - `asks` are numbered questions; only meaningful on an open/blocked
          message (the thing that carries an obligation). Ids must be unique and
          non-empty; text/assignee are sanitized and bounded like any
          guaranteed-read field.
        - `answers` list the ask ids a reply discharges; only on a `reply` that
          names its `reply_to`, whose parent must carry those asks and must not
          be the poster's own message (teaching refusals, ADR-0003).
        - `settled_by` on a resolved reply is the supersession pointer that lets
          a non-asker close someone else's stale question: it must name a real
          message in THIS channel (audited closure, never a bare claim).

        Validation runs on the EFFECTIVE fields regardless of how they arrived —
        the typed params OR a hand-crafted `data` payload — so a raw-data write
        cannot smuggle in duplicate ids, unsanitized text, or a fake pointer.
        """
        data = dict(payload.data) if payload.data else {}
        if payload.asks is not None:
            data["asks"] = [a.model_dump(exclude_none=True) for a in payload.asks]
        if payload.answers is not None:
            data["answers"] = [str(x) for x in payload.answers]
        if payload.attachments is not None:
            data["attachments"] = [a.model_dump(exclude_none=True)
                                   for a in payload.attachments]
        if "asks" in data:
            data["asks"] = self._validate_asks(data["asks"], payload.status,
                                               sender=sender, channel=channel)
        if "answers" in data:
            data["answers"] = self._validate_answers(data["answers"], payload.status,
                                                     payload.reply_to, sender)
        if "attachments" in data:
            # Refs are validated against THIS channel's blob store and
            # normalized from server truth (0091) — whether they arrived via
            # the typed param or a hand-built data payload.
            data["attachments"] = self._validate_attachments(data["attachments"],
                                                             channel)
        if "item_ref" in data:
            # Work-id citation (0093): the STRUCTURED stitch between a hub
            # message and a backlog item. Validated when present so the
            # /work index never accumulates rotten refs; prose mentions
            # stay free-form (they index as 'mention', never refused).
            ref = str(data["item_ref"])
            if parse_work_id(ref) is None:
                raise HubError(400, f"item_ref '{sanitize_text(ref, 64)}' is "
                                    "not a work id — the ruled form is "
                                    "<package>-<NNNN> (e.g. agora-0093); "
                                    "citing in prose needs no field at all")
            data["item_ref"] = ref
        if "settled_by" in data:
            if payload.status != Status.resolved or not payload.reply_to:
                raise HubError(400, "settled_by is only allowed on a resolved "
                                    "reply (it closes the thread you reply to)")
            pointer = str(data["settled_by"])
            if pointer == payload.reply_to:
                # A pointer at the question itself is a bare claim wearing an
                # audit trail (review MED-2): supersession must name where the
                # question was SETTLED, not the question.
                raise HubError(400, "settled_by must name the message that "
                                    "settled the question — not the question "
                                    "itself")
            settled = self.db.get_message(pointer)
            if settled is None or settled.channel != channel:
                raise HubError(400, "settled_by must name a message id in this "
                                    "channel (the message that settled the "
                                    "question)")
            data["settled_by"] = pointer
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
        """A channel is `open` (default), `closed`, or `archived`. Closed =
        session ended, posts refused but the room stays on members' rails.
        Archived (0090) is the stronger end: members evicted, delisted,
        history kept. Archived (first-class column) outranks closed (meta)."""
        if self.db.channel_archived(channel):
            return "archived"
        meta = self.db.store_get(channel, CHANNEL_META_KEY)
        if meta and isinstance(meta.value, dict) and meta.value.get("state") == "closed":
            return "closed"
        return "open"

    def _require_not_archived(self, channel: str) -> None:
        """Defense in depth for every WRITE path (review P2): archive evicts
        members so membership normally blocks first, but a join/archive TOCTOU
        or a re-added operator (hub-alerts) can leave a live member on an
        archived channel — no write should mutate an ended room regardless."""
        if self.db.channel_archived(channel):
            raise HubError(409, f"channel '{channel}' is archived (ended); "
                                "history is preserved but it accepts no writes")

    def archive_channel(self, agent: AgentInfo, channel: str) -> dict[str, Any]:
        """End a channel (0090): evict every member (channel-scoped — hub
        membership and identities untouched), delist it for everyone, refuse
        further posts/joins/invites. Messages, store, fs and blobs are
        PRESERVED (append-only; operator-readable). Owner or operator only;
        idempotent. DMs are out of scope (ownerless; `leave` covers them, and
        a peer must never vaporize the other's view of the record)."""
        self._require_unpaused(agent, channel)
        info = self.db.get_channel(channel)
        if info is None:
            raise HubError(404, f"channel '{channel}' not found")
        if channel.startswith(DM_PREFIX):
            raise HubError(400, "direct channels cannot be archived — use "
                                "`leave` (a DM is ownerless; neither peer may "
                                "erase the other's record)")
        # Authority keys on the DURABLE creator id (channels.created_by), not
        # the members table: archive evicts everyone including the owner, so a
        # role lookup would make even a second archive fail. There is no
        # ownership transfer in this hub, so created_by IS the owner.
        if not agent.operator and info.created_by != agent.id:
            raise HubError(403, "only the channel owner or an operator can "
                                "archive a channel")
        already = self.db.channel_archived(channel)
        evicted = self.db.archive_channel(channel)
        if not already:
            # System note lands BEFORE eviction-as-seen: the record shows who
            # ended the room and when (the ledger keeps it forever).
            self._post_system(channel, f"channel archived by {agent.id} — "
                                       f"{len(evicted)} member(s) evicted; "
                                       "history preserved, room delisted")
        return {"channel": channel, "archived": True, "evicted": sorted(evicted),
                "already_archived": already}

    def unarchive_channel(self, agent: AgentInfo, channel: str) -> dict[str, Any]:
        """Reopen an archived channel (OPERATOR only — not the owner: an owner
        could otherwise flap a room on and off everyone's rails). Restores
        visibility and posting; members are NOT restored (rejoin/re-invite is
        explicit, same rule as unretire)."""
        self._require_unpaused(agent, channel)
        if not agent.operator:
            raise HubError(403, "only an operator can unarchive a channel "
                                "(reopening a room is not the owner's call)")
        info = self.db.get_channel(channel)
        if info is None:
            raise HubError(404, f"channel '{channel}' not found")
        if not self.db.channel_archived(channel):
            return {"channel": channel, "archived": False, "already_open": True}
        self.db.unarchive_channel(channel)
        # Restore the ORIGINAL owner's role, not a plain operator membership
        # (review P1): archive evicted everyone including the owner, and the
        # only owner-grant path is create_channel, so without this the room
        # reopens ownerless — invite minting and channel:meta writes (both
        # owner-gated) strand forever, sealing a private room shut. created_by
        # is immutable and never reused, so it is the durable owner id. This
        # mirrors the moderation rule that refuses to kick a channel owner
        # for exactly this strand.
        self.db.add_member(channel, info.created_by, role="owner")
        self._post_system(channel, f"channel reopened by {agent.id} — owner "
                                   f"{info.created_by} restored; prior members "
                                   "must rejoin")
        return {"channel": channel, "archived": False, "owner": info.created_by}

    def retire_agent(self, agent: AgentInfo, target_id: str,
                     reason: str = "") -> dict[str, Any]:
        """Retire an agent (0089): a NEUTRAL decommission, not a block. Its
        key stops authenticating (neutral 403), it is evicted from every
        channel and drops off rosters/presence, and its id stays reserved
        forever so message attribution can never be hijacked. Operator only
        (lifecycle is the operator's; an agent cannot retire a colleague or
        itself). Idempotent."""
        if not agent.operator:
            raise HubError(403, "retiring an identity is an operator act")
        if not self.db.agent_exists(target_id):
            raise HubError(404, f"agent '{target_id}' is not registered")
        if target_id in self.operator_ids():
            raise HubError(403, "operators cannot be retired (lifecycle safety)")
        reason = sanitize_text(str(reason or ""), 200)
        evicted = self.db.retire_agent(target_id, reason)
        return {"agent": target_id, "retired": True, "reason": reason,
                "evicted_from": sorted(evicted)}

    def unretire_agent(self, agent: AgentInfo, target_id: str) -> dict[str, Any]:
        """Restore a retired agent's auth (operator only). Memberships are NOT
        restored — the agent rejoins its rooms explicitly."""
        if not agent.operator:
            raise HubError(403, "restoring an identity is an operator act")
        if self.db.agent_retirement(target_id) is None:
            return {"agent": target_id, "retired": False, "already_active": True}
        self.db.unretire_agent(target_id)
        return {"agent": target_id, "retired": False}

    def post_message(self, agent: AgentInfo, channel: str, payload: PostMessage) -> Message:
        """Post with a refusal audit: a refused send previously left no trace
        anywhere, so "agent X never answers" was indistinguishable from
        "agent X is being blocked" (field finding). Every HubError is recorded
        per agent and surfaced in the operator status overview."""
        try:
            return self._post_message(agent, channel, payload)
        except HubError as e:
            # Pause 423s are EXPECTED refusals fleet-wide: logging them would
            # evict real refusals from the 50-slot audit ring and inflate the
            # operator's refused_sends count (review LOW-6).
            if e.status_code != 423:
                log = self.refusals.setdefault(agent.id, deque(maxlen=50))
                log.append({"ts": time.time(), "channel": channel,
                            "code": e.status_code, "detail": e.detail})
            raise

    def _post_message(self, agent: AgentInfo, channel: str, payload: PostMessage) -> Message:
        self.require_membership(channel, agent.id)
        self._require_unpaused(agent, channel)
        state = self.channel_state(channel)
        if state == "archived":
            # Archived rooms evict everyone, so membership normally blocks
            # first; this is the explicit, clear refusal (0090) in case a
            # member row ever survives, and names the stronger end-state.
            raise HubError(409, f"channel '{channel}' is archived (ended); "
                                "history is preserved but it accepts no posts")
        if state == "closed":
            # A room whose session died accepts no more turns — the bridge and
            # any subscriber get a clean 409 instead of writing into a dead room.
            raise HubError(409, f"channel '{channel}' is closed to new posts")
        # DM to a RETIRED peer: refuse uniformly here too, not just in the
        # open_dm/post_dm path (review P2 — retirement evicts only the retired
        # agent's own rows, so the surviving peer keeps DM membership and could
        # otherwise append to a decommissioned peer's DM via raw post_message).
        if channel.startswith(DM_PREFIX):
            peers = [i for i in channel[len(DM_PREFIX):].split("--") if i != agent.id]
            retired = [p for p in peers if self.db.agent_retirement(p) is not None]
            if retired:
                raise HubError(409, f"'{retired[0]}' has been retired "
                                    "(decommissioned) — this direct channel is "
                                    "closed to new messages")
            if not payload.to and peers:
                # A DM is a two-party room: every message in it is by
                # definition FOR the counterpart. The native /dms door
                # (post_dm) has always auto-addressed; posts arriving via
                # this generic channel route carried to=[] — they never
                # raised to-me, never woke --important-only listeners, and
                # read as ambient fyi (live incident: operator dm 84 /
                # c3073, three independent clients hit it). Address at the
                # hub so EVERY client inherits; an explicit `to` is kept
                # verbatim (it can only name the counterpart anyway —
                # there is nobody else in the room).
                payload = payload.model_copy(update={"to": peers})
        self._require_charter_read(channel, agent)
        if len(payload.body.encode()) > MAX_BODY_BYTES:
            raise HubError(413, f"body exceeds {MAX_BODY_BYTES} bytes")
        # A reply's whole meaning is "this answers something": a bare
        # status=reply pointing at nothing discharges nothing, so the sender
        # believes they answered while the asker's obligation rots and
        # escalates — a silent failure both sides misread (live incident
        # 2026-07-08; backlog 0050). Refuse with the fix in hand. Other
        # statuses legitimately stand alone (`resolved` without reply_to is
        # a valid free-standing close), and no parent is ever auto-inferred
        # (guessing would misattribute answers).
        if payload.status == Status.reply and payload.reply_to is None:
            raise HubError(400, "status=reply requires reply_to=<the message "
                                "id you are answering> — a bare reply "
                                "discharges nothing and the obligation you "
                                "answered stays open")
        # `reply_to` must reference a message in THIS channel. Without this a
        # sender could point reply_to at a message in a channel it cannot read
        # and later harvest it via read_message's ancestor walk (the v0.3 IDOR).
        # Checked BEFORE structured validation so the teaching 400s cannot act
        # as an existence oracle for foreign-channel ids (review LOW-1).
        parent: Message | None = None
        if payload.reply_to is not None:
            parent = self.db.get_message(payload.reply_to)
            if parent is None or parent.channel != channel:
                raise HubError(400, "reply_to must reference a message in this channel")
        data = self._prepare_structured(payload, sender=agent.id, channel=channel)
        if data is not None:
            try:
                # allow_nan=False doubles as the strict-JSON gate: NaN/Infinity
                # would hash and store fine but make the ledger response
                # unserializable (and unparseable outside Python) — refuse at
                # the boundary instead of poisoning the transcript.
                encoded = json.dumps(data, allow_nan=False).encode()
            except ValueError:
                raise HubError(400, "data must be strict JSON: NaN/Infinity "
                                    "are not representable — send null or a string")
            if len(encoded) > MAX_DATA_BYTES:
                raise HubError(413, f"data exceeds {MAX_DATA_BYTES} bytes")
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
        if agent.operator:
            self._operator_burst_check(agent.id, channel)
        if payload.reply_to and parent is not None and not parent.critical:
            # Replying IS attending: record the read receipt on the parent so
            # an addressee who answered straight from the inlined envelope
            # stops being re-pinned by a message it demonstrably handled
            # (0066; gateway's re-triaging-own-completed-work case, c1101).
            # CRITICALS are excluded: their contract is "pinned until
            # deliberately READ" (forced attention), and a scripted reply
            # must not become a side door around it (review MED-1).
            self.db.mark_read(payload.reply_to, agent.id)
        self._wake(message)
        return message

    #: Operator-key burst tripwire (0104): 6+ posts inside 15s is machine
    #: cadence — a human cannot compose six messages in fifteen seconds.
    #: The Jul-14 forgery was 13 DMs in 10s under the operator's cached key
    #: and NOTHING flagged it; six days later the fleet paid receipts to
    #: words the human never wrote. On one shared machine the hub cannot
    #: PREVENT a local process from using the cached key (the key IS the
    #: credential) — but it can make silent impersonation impossible.
    OPERATOR_BURST_N = 6
    OPERATOR_BURST_WINDOW = 15.0
    OPERATOR_BURST_COOLDOWN = 600.0

    def _operator_burst_check(self, operator_id: str, channel: str) -> None:
        now = time.time()
        q = self._operator_posts.setdefault(operator_id, deque(maxlen=64))
        q.append((now, channel))
        recent = [c for t, c in q if now - t <= self.OPERATOR_BURST_WINDOW]
        if len(recent) < self.OPERATOR_BURST_N:
            return
        if (now - self._operator_burst_alerted_at.get(operator_id, 0.0)
                < self.OPERATOR_BURST_COOLDOWN):
            return  # one alert per episode; a 13-post blast is one event
        self._operator_burst_alerted_at[operator_id] = now
        self._ensure_alerts_channel()
        self._post_system(
            self.DARK_ALERTS_CHANNEL,
            f"OPERATOR-KEY BURST: {len(recent)} posts under "
            f"'{operator_id}' within {self.OPERATOR_BURST_WINDOW:.0f}s "
            f"across {len(set(recent))} channel(s) — machine cadence on a "
            f"human key. If this was not {operator_id} at a keyboard, a "
            "local process is speaking with the operator's cached key "
            "(the Jul-14 forgery class): verify the posts, retract what "
            "is false, rotate the key. One alert per episode.")

    def _require_charter_read(self, channel: str, agent: AgentInfo) -> None:
        """The opt-in charter gate (channel:meta.norms_required): posting
        requires having READ the current channel/charter.md — the read IS the
        receipt, so the refusal is always self-healing in one call. The hub
        forces attention to the rules, never agreement with them ("understand
        and abide" is not machine-checkable; delivery is). Applies uniformly —
        owner and operator included: their writes/reads record receipts like
        anyone's, and a uniform rule beats special cases. fs audit and system
        messages insert directly into the db, so charter edits can never be
        blocked by the gate they refresh."""
        meta = self.db.store_get(channel, CHANNEL_META_KEY)
        if not (meta and isinstance(meta.value, dict)
                and meta.value.get("norms_required")):
            return
        row = self.db.fs_get(channel, FS_PREFIX + CHARTER_PATH)
        if row is None or row["deleted"]:
            return  # flag set but no charter written yet: nothing to require
        receipt = self.db.charter_receipt_get(agent.id, channel)
        if receipt is None or receipt < row["version"]:
            raise HubError(409, f"this channel requires reading its charter "
                                f"first: fs_read '{CHARTER_PATH}' in "
                                f"'{channel}' (v{row['version']}), then retry")

    def _post_system(self, channel: str, body: str,
                     to: list[str] | None = None,
                     status: str | None = None,
                     reply_to: str | None = None,
                     data: dict[str, Any] | None = None) -> Message:
        # `to` lets an alert ADDRESS its steward (0084): an addressed
        # message rides the to-me wake path and the owed ledger — a
        # broadcast alert would unpin on a bare read and decay.
        # `status`/`reply_to` let the hub CLOSE its own alerts (0093): an
        # open system message is an obligation, and obligations the hub
        # never discharges accumulate as permanent owed debt on the
        # addressees (measured: 8 undischargeable rows on one delegate).
        message = self.db.insert_message(
            channel, "hub", kind=Kind.system.value,
            status=status or ("open" if to else "fyi"), urgency="inbox",
            title="", body=body, data=data, reply_to=reply_to, to=to or [],
        )
        self._wake(message)
        return message

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
                    state = discharge_state(m, replies, self.operator_ids())
                    # Resolution-by-follow-up (now uniform across ALL surfaces,
                    # ADR-0003): an AUTHORITATIVE resolved reply — asker,
                    # operator, or settled_by pointer — closes the question.
                    # The digest previously accepted any member's resolved
                    # reply; that laxer rule was the digest/inbox split-brain
                    # behind the c713 incident and is deliberately narrowed.
                    # `self_resolved` labels only the asker's own closure
                    # (review LOW-3): operator/supersession closures land in
                    # `decided` unlabeled rather than mislabeled.
                    self_resolved = (not state.discharged and any(
                        r.status == Status.resolved and r.sender == m.sender
                        for r in replies))
                    if state.closed:
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
                                           if self_resolved else {})})
                    else:
                        asks = {str(a["id"]): a for a in asks_of(m)}
                        open_questions.append({
                            **brief, "status": m.status.value,
                            "pending_asks": [
                                {"id": i, "text": asks.get(i, {}).get("text", ""),
                                 # Per-ask addressing (0077): named seats ride
                                 # the digest so "scan for your name" is a
                                 # field lookup, not a prose search.
                                 **({"to": asks[i]["to"]}
                                    if asks.get(i, {}).get("to") else {})}
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
        # Obligation settlement (only meaningful for open/blocked): CLOSED —
        # every ask answered OR an authoritative resolved reply (asker,
        # operator, or pointer-carrying member; ADR-0003) — is what stops
        # escalation. A partial answer keeps it escalating with its pending
        # asks visible; has_resolved_reply travels so a reader is never cold.
        closed, pending, total, has_resolved = False, [], 0, False
        already_read = False
        owes_reply = False
        if message.status in (Status.open, Status.blocked):
            state = discharge_state(message, self.db.replies_to(message.id),
                                    self.operator_ids())
            closed = state.closed
            pending, total = state.pending, state.total
            has_resolved = state.has_resolved_reply
            # Only the pinned class can re-deliver; a read receipt turns its
            # re-surfaces headline-only (redelivery=true, body withheld).
            already_read = self.db.has_read(message.id, viewer_id)
        elif self._is_addressed_debt(viewer_id, message):
            # Directive debts (0102) age exactly like open/blocked: an
            # ignored addressed reply/fyi escalates past the channel SLA
            # and feeds the deaf/dark watchdogs — 'a reply is not
            # mandatory' stops being true mechanically, not hortatorily.
            replies = self.db.replies_to(message.id)
            owes_reply = not (
                closed_authoritatively(message, replies, self.operator_ids())
                or any(r.sender == viewer_id for r in replies))
            already_read = self.db.has_read(message.id, viewer_id)
        return self.attention.envelope_for(
            viewer_id, message,
            parent_sender=parent.sender if parent else None,
            has_reply=closed, pending_asks=pending, ask_total=total,
            has_resolved_reply=has_resolved, owes_reply=owes_reply,
            sla_minutes=sla_minutes if sla_minutes is not None
            else self.channel_sla(message.channel),
            # Escalation clock exclusion (0069): paused time never ages an
            # obligation toward its SLA, so a resume cannot open onto an
            # escalation storm the pause itself manufactured.
            paused_seconds=self.paused_seconds_since(message.created_at),
            already_read=already_read,
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

    def retract_message(self, agent: AgentInfo, channel: str,
                        message_id: str) -> Message:
        """Author-only (or operator) retraction (0097): redact the message
        on every agent-facing surface so no agent or entity can ever consume
        its words, and clear any obligation it carried (the stray-message
        phantom-debt case). Anytime — regret has no window. Idempotent.
        The original bytes stay in the row for operator audit and for the
        ledger hash (retraction is presentation, never a chain rewrite)."""
        self.require_membership(channel, agent.id)
        # Read RAW (redact=False) so authorship is checkable even after a
        # prior retraction redacted the agent-facing view.
        message = self.db.get_message(message_id, redact=False)
        if message is None or message.channel != channel:
            raise HubError(404, f"message '{message_id}' not found in '{channel}'")
        if message.sender != agent.id and not agent.operator:
            raise HubError(403, "only the author (or an operator) can retract "
                                "a message — you can retract what YOU said, "
                                "not what others said")
        if message.kind != Kind.message:
            raise HubError(400, "only chat messages can be retracted, not "
                                "system/fs events")
        self.db.retract_message(message_id, agent.id)
        redacted = self.db.get_message(message_id)  # redacted view for the wire
        # Broadcast the retraction so live subscribers redact in place (the
        # tombstone is the payload; the words never ride the wire again).
        self._wake(redacted)
        return redacted

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
        # Obligations stay pinned until CLOSED — every ask answered, or an
        # authoritative resolved reply (ADR-0003) — so a partially-answered
        # open message does not silently drop out of the inbox, while a
        # properly closed thread stops taxing anyone. Addressed obligations
        # (to=[...]) pin only their addressees (0066): the obligation lives
        # with them; bystanders see the message once via normal cursor flow
        # and can always find pending questions in the digest. Broadcast
        # obligations (no to=) keep pinning every member — someone must pick
        # them up.
        members_cache: dict[str, set[str]] = {}
        hub_blocked = {b["agent_id"] for b in self.db.blocks_active(self.HUB_SCOPE)}
        # Addressed reply/fyi debts pin like obligations (0101/0102): an
        # addressed directive must not drop below the cursor unheard.
        for message in (self.db.obligation_candidates(agent.id, channels)
                        + self._addressed_debts(agent.id, channels)):
            if message.status in (Status.reply, Status.fyi):
                # Directive debts (0102): PER-ADDRESSEE engagement — another
                # addressee's reply never unpins YOURS; only your own reply
                # or an authoritative closure does. (obligation_candidates
                # yields only open/blocked, so this branch is exactly the
                # _addressed_debts rows.)
                replies = self.db.replies_to(message.id)
                if closed_authoritatively(message, replies, self.operator_ids()):
                    continue
                if any(r.sender == agent.id for r in replies):
                    continue
                by_id[message.id] = message
                continue
            # Effective addressees = message-level `to` plus every seat named
            # by a per-ask `to` (0077): a canvass that names you in an ask IS
            # addressed to you — names living only in prose pinned nobody.
            named = ask_addressees(message)
            addressed = set(message.to) | named
            viewer_is_addressee = agent.id in addressed
            if addressed and not viewer_is_addressee:
                # Addressee-left fallback (review MED-3): if NO addressee is
                # still AVAILABLE, the obligation would become invisible to
                # everyone — revert to broadcast pinning so it cannot rot in
                # the dark. A hub-blocked addressee counts as unavailable
                # (review F3): it cannot sign in to discharge, so leaving the
                # obligation pinned only to it would orphan the work.
                if message.channel not in members_cache:
                    members_cache[message.channel] = {
                        m.agent_id for m in self.db.list_members(message.channel)}
                available = members_cache[message.channel] - hub_blocked
                if any(a in available for a in addressed):
                    continue
            if not viewer_is_addressee and self.db.has_read(message.id, agent.id):
                # Bystander economics (unchanged): for broadcast obligations —
                # and the fallback case above — a bare read IS the triage; a
                # bystander should not stay pinned to every open question.
                continue
            replies = self.db.replies_to(message.id)
            ds = discharge_state(message, replies, self.operator_ids())
            if ds.closed:
                continue
            if viewer_is_addressee:
                # The 0080 root fix: an ADDRESSEE's bare read does NOT unpin —
                # read+ack was exactly how lurking seats silenced the inbox,
                # status, the stop hook, and the dark watchdog in one motion.
                # Only engaging clears: any reply of theirs (answer, decline
                # on the record) or thread closure.
                if any(r.sender == agent.id for r in replies):
                    continue
                if (agent.id in named and agent.id not in message.to
                        and agent.id not in pending_addressees(message, ds.pending)):
                    # Ask-scoped pin (0077): a seat named ONLY by asks stops
                    # being pinned once every ask naming it is answered — its
                    # canvass row is done even while other rows stay open.
                    continue
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

    def _is_addressed_debt(self, viewer_id: str, m: Message) -> bool:
        """Is this reply/fyi message a DEBT the viewer owes an answer to?
        (0101, generalized 0102). Replies normally oblige nobody — obliging
        every reply would ping-pong — but 'a reply is not mandatory' was
        exactly the excuse behind silently dropped directives (operator,
        2026-07-19: 'it MUST be'). The rule, mechanical:

        - OPERATOR sender, status reply OR fyi: always obliges the named
          seats. Human words are few and never chatter; a DM auto-addresses
          (c3073), so every operator DM line lands here regardless of the
          status the composer picked. The operator ends a thread with
          status=resolved (closes), never by being ignored.
        - PEER sender, status reply: obliges the named seats UNLESS it is
          the sender's answer coming back to you — i.e. it replies to YOUR
          OWN message. Your debt for an answer is CONSUMPTION (0078's
          to_consume), not another reply; this exemption is also what
          terminates ack chains ('thanks' replying to their answer obliges
          them nothing) instead of ping-ponging forever.
        - PEER sender, status fyi: never obliges — fyi is the documented
          terminal gesture ('no reply owed'), and DMs auto-address, so
          without a non-obliging status no DM thread could ever end.
        - A reply carrying `answers` never obliges (both classes): it
          discharges an ask; the asker's debt is consumption.

        The viewer engaging (any reply of theirs to it) clears it, via the
        same per-addressee discharge as any addressed binary obligation."""
        if (m.kind != Kind.message or m.retracted
                or m.status not in (Status.reply, Status.fyi)):
            return False
        if m.sender == viewer_id or viewer_id not in m.to:
            return False
        if (m.data or {}).get("answers"):
            return False  # an answer, not a directive
        if m.sender in self.operator_ids():
            return True
        if m.status != Status.reply:
            return False  # peer fyi: terminal gesture, never a debt
        if m.created_at < self._directive_epoch:
            # Epoch bound (c3379): posted under the old 'replies oblige
            # nobody' semantics — it must not become a debt retroactively.
            return False
        parent = self.db.get_message(m.reply_to) if m.reply_to else None
        return not (parent is not None and parent.sender == viewer_id)

    def _addressed_debts(self, agent_id: str,
                         channels: list[str]) -> list[Message]:
        """Every reply/fyi debt the viewer owes across these channels — the
        candidate feed `owed` and the inbox pin merge with open/blocked
        obligations (0102). Engagement/closure filtering stays with the
        callers, identical to any other obligation."""
        return [m for m in self.db.addressed_directives(channels)
                if self._is_addressed_debt(agent_id, m)]

    def owed(self, agent: AgentInfo) -> dict[str, Any]:
        """The agent's outstanding debts (0079), read receipts deliberately
        IGNORED: read-but-unanswered is precisely the lurk the receipt filter
        would hide. Two ledgers:

        - `to_answer`: open/blocked messages addressed to the agent — via
          message `to`, an advisory assignee, or a still-pending per-ask `to`
          (0077) — that are not closed and that the agent has not replied to
          at all. Replying at all drops the row (the remaining debt is
          other seats'); closure drops it everywhere.
        - `to_consume` (0078): answers other seats posted to the agent's OWN
          open questions that the agent has neither read (receipt) nor
          followed in-thread (any later post of theirs) — the mechanical
          form of "someone answered you; use it or close it". Clears on
          read_message of the answer, on any later in-thread post by the
          asker, or on authoritative closure. Never escalates, never wakes
          by itself: it surfaces here, in check_inbox, and on the board.
        """
        channels = self.db.channels_of(agent.id)
        ops = self.operator_ids()
        now = time.time()
        sla_cache: dict[str, float] = {}
        to_answer: list[dict[str, Any]] = []
        # Candidates: open/blocked obligations PLUS addressed reply/fyi debts
        # (0101/0102) — a message that NAMES you owes your engagement, not
        # just messages that opened a thread. Both run the identical
        # discharge/engagement checks below; a directive carries no asks, so
        # it is a binary obligation that any reply from the addressee
        # discharges.
        candidates = self.db.open_obligations(channels) + self._addressed_debts(agent.id, channels)
        for m in candidates:
            if m.sender == agent.id:
                continue
            replies = self.db.replies_to(m.id)
            if m.status in (Status.reply, Status.fyi):
                # Directive debts (0102): PER-ADDRESSEE engagement — another
                # seat's reply never clears YOUR debt (the multi-addressee
                # free-rider hole); only your own reply or an authoritative
                # closure does.
                if closed_authoritatively(m, replies, ops):
                    continue
                if any(r.sender == agent.id for r in replies):
                    continue
                if m.channel not in sla_cache:
                    sla_cache[m.channel] = self.channel_sla(m.channel)
                age = now - m.created_at - self.paused_seconds_since(m.created_at)
                to_answer.append({
                    "channel": m.channel, "id": m.id, "seq": m.seq,
                    "from": m.sender, "title": m.title,
                    "pending_asks": [], "asks_naming_you": [],
                    "age_minutes": round(age / 60, 1),
                    "escalated": age > sla_cache[m.channel] * 60.0,
                })
                continue
            ds = discharge_state(m, replies, ops)
            if ds.closed:
                continue
            assignees = {a.get("assignee") for a in asks_of(m)} - {None}
            named_pending = pending_addressees(m, ds.pending)
            if not (agent.id in m.to or agent.id in assignees
                    or agent.id in named_pending):
                continue
            if any(r.sender == agent.id for r in replies):
                continue  # engaged: the remaining pending asks are other seats'
            if m.channel not in sla_cache:
                sla_cache[m.channel] = self.channel_sla(m.channel)
            age = now - m.created_at - self.paused_seconds_since(m.created_at)
            to_answer.append({
                "channel": m.channel, "id": m.id, "seq": m.seq,
                "from": m.sender, "title": m.title,
                "pending_asks": ds.pending,
                "asks_naming_you": sorted(
                    str(a["id"]) for a in asks_of(m)
                    if agent.id in (a.get("to") or []) and str(a["id"]) in ds.pending),
                "age_minutes": round(age / 60, 1),
                "escalated": age > sla_cache[m.channel] * 60.0,
            })
        to_consume: list[dict[str, Any]] = []
        waiting_on: list[dict[str, Any]] = []
        cursor_cache: dict[tuple[str, str], int] = {}
        for m in self.db.my_open_messages(agent.id, channels):
            replies = self.db.replies_to(m.id)
            if closed_authoritatively(m, replies, ops):
                continue
            structured = bool(asks_of(m))
            for r in replies:
                if r.sender == agent.id:
                    continue
                answers = (r.data or {}).get("answers") or []
                if structured and not answers:
                    continue  # commentary, not an answer to a numbered ask
                consumed = (self.db.has_read(r.id, agent.id)
                            or any(x.sender == agent.id and x.seq > r.seq
                                   for x in replies))
                if not consumed:
                    to_consume.append({
                        "channel": m.channel, "id": m.id, "seq": m.seq,
                        "title": m.title, "your_asks": [str(x) for x in answers],
                        "answered_by": r.sender, "answer_id": r.id,
                        "answer_seq": r.seq,
                        "age_minutes": round((now - r.created_at) / 60, 1),
                    })
            # waiting_on (asker side of the debrief): per still-pending ask
            # addressee, has the hub SERVED them past your question? "acked
            # past, no reply" and "not yet served" are different waits — one
            # is a nudge candidate, the other is an offline seat; seats spent
            # real turns inferring this from presence, which the hub knew.
            ds = discharge_state(m, replies, ops)
            if ds.closed:
                continue
            repliers = {r.sender for r in replies}
            for a in asks_of(m):
                if str(a["id"]) not in ds.pending:
                    continue
                for seat in (a.get("to") or []):
                    if seat in repliers:
                        continue
                    key = (seat, m.channel)
                    if key not in cursor_cache:
                        cursor_cache[key] = self.db.get_cursor(seat, m.channel)
                    waiting_on.append({
                        "channel": m.channel, "seq": m.seq, "ask": str(a["id"]),
                        "seat": seat,
                        "state": ("acked-past-no-reply"
                                  if cursor_cache[key] >= m.seq
                                  else "not-yet-acked"),
                    })
        return {"to_answer": to_answer, "to_consume": to_consume,
                "waiting_on": waiting_on,
                "counts": {"to_answer": len(to_answer),
                           "to_consume": len(to_consume)}}

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
        self._require_unpaused(agent, channel)
        self._require_not_archived(channel)
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
        if key.startswith(self._QUEUE_PREFIX):
            # Curation authority is now MECHANICAL (0068): queue rows are the
            # operator's/delegate's board surface. The refusal names the path
            # a requesting seat should use instead.
            if not (agent.operator or self.is_delegate(agent.id, "reporting")):
                raise HubError(403, "queue:* rows are curated by the operator "
                                    "or a delegate holding the 'reporting' "
                                    "power (whoami.delegations lists them) — "
                                    "to request a decision, post an open ask "
                                    "addressed to the decider instead")
            self._validate_queue_row(value)
        if key.startswith("claim:") and isinstance(value, dict):
            # Identity fields inside store values are validated against the
            # caller (0068/ADR-0004; live-test finding): you may claim FOR
            # yourself, take a claim over in your own name, or leave
            # ownership unchanged (e.g. marking someone's claim done) — you
            # may never write a claim in a colleague's name, and OMITTING the
            # owner field must not erase it either (review MED-1: erasure by
            # omission would misattribute the claim to the last writer).
            # Read-then-write happens under two lock acquisitions; two racing
            # no-CAS writers could both validate against the same stale owner
            # (review LOW-2) — the only "forgery" that admits is re-asserting
            # a microseconds-old owner, and CAS callers are fully protected,
            # so this stays a comment rather than a db-layer check.
            current = self.db.store_get(channel, key)
            current_owner = (current.value.get("owner")
                             if current is not None and isinstance(current.value, dict)
                             else None)
            if "owner" in value:
                if (not agent.operator and value["owner"] != agent.id
                        and value["owner"] != current_owner):
                    shown = sanitize_text(str(value["owner"]), 64)
                    raise HubError(400, f"claim owner '{shown}' is not you — "
                                        "claim in your own name, or leave the "
                                        "existing owner unchanged")
            elif current_owner is not None:
                value = {**value, "owner": current_owner}
            # Claim/key consistency (0093): when the claim key's task part
            # parses as a WORK ID and the value carries an `item` field,
            # they must agree — a pointer row that points two ways would
            # poison the /work index. Free-text claims (non-id task names)
            # and item-less values stay untouched forever.
            task = key[len("claim:"):]
            if (parse_work_id(task) is not None and "item" in value
                    and str(value["item"]) != task):
                raise HubError(400, f"claim key names work id '{task}' but "
                                    f"value.item says "
                                    f"'{sanitize_text(str(value['item']), 64)}'"
                                    " — a pointer claim must cite ONE id; "
                                    "drop value.item or make them agree")
        if key.startswith(self.WORK_ROW_PREFIX):
            self._validate_work_row(key, value)
        return self.db.store_set(channel, key, value, agent.id, expect_version)

    # -- unified backlog rows (0103, operator ruling c3328) ----------------------
    #
    # `work:<package>-<NNNN>` store rows are the hub-resident INDEX of the
    # room's backlog: the repo file stays the deep record; the row mirrors
    # its directory state so every seat (and the console) sees one
    # cross-agent picture without a gateway. claim:* stays the WHO/liveness
    # record; work:* is the WHAT/state record.

    WORK_ROW_PREFIX = "work:"
    #: The FILE's own directory words (continuum's S0 clause, c3343): rendered
    #: words like in_progress/in_review/done are DERIVATIONS over
    #: work-row + live claim, never stored — storing one would create a
    #: rendered word with no transition trigger and no disagreement owner.
    WORK_STATUSES = ("proposed", "planned", "completed", "deprecated")
    _WORK_DERIVED_STATUSES = frozenset(
        {"in_progress", "in-progress", "in_review", "in-review", "done"})

    def _validate_work_row(self, key: str, value: Any) -> None:
        item = key[len(self.WORK_ROW_PREFIX):]
        if parse_work_id(item) is None:
            raise HubError(400, f"'{sanitize_text(item, 64)}' is not a work id "
                                "— work:* rows are the backlog INDEX and key "
                                "on the ruled form <package>-<NNNN> "
                                "(e.g. agora-0093); free-text task names "
                                "belong on claim:* rows")
        if not isinstance(value, dict):
            raise HubError(400, "a work:* row must be an object: {title, "
                                "status, owner, card, priority?, receipt?}")
        status = str(value.get("status", "")).strip().lower()
        if status in self._WORK_DERIVED_STATUSES:
            raise HubError(400, f"status '{status}' is a DERIVED word, never "
                                "stored: in-progress = planned + a live "
                                "claim:* row; done = completed + receipt. "
                                "Store the file's directory word "
                                f"({'|'.join(self.WORK_STATUSES)}) and let "
                                "boards derive the rest")
        if status not in self.WORK_STATUSES:
            raise HubError(400, f"work:* status must be one of "
                                f"{'|'.join(self.WORK_STATUSES)} — the file's "
                                f"own directory word (got "
                                f"'{sanitize_text(status, 32)}')")

    def work_rows(self, agent: AgentInfo, channel: str) -> list[dict[str, Any]]:
        """All work:* rows of a channel, parsed — the one-call backlog list
        (0103) so consoles never page the raw store. Membership-gated like
        any store read."""
        self.require_membership(channel, agent.id)
        out: list[dict[str, Any]] = []
        for entry in self.db.store_keys(channel):
            if not entry["key"].startswith(self.WORK_ROW_PREFIX):
                continue
            stored = self.db.store_get(channel, entry["key"])
            if stored is None or not isinstance(stored.value, dict):
                continue
            v = stored.value
            out.append({
                "id": entry["key"][len(self.WORK_ROW_PREFIX):],
                "title": v.get("title", ""), "status": v.get("status", ""),
                "owner": v.get("owner", ""), "card": v.get("card", ""),
                "priority": v.get("priority"), "receipt": v.get("receipt"),
                "version": stored.version, "updated_by": stored.updated_by,
                "updated_at": stored.updated_at,
            })
        out.sort(key=lambda r: r["id"])
        return out

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
        # Opt-in charter gate: posting requires having READ the current
        # channel/charter.md (the receipt is recorded by the read itself).
        norms_required = value.get("norms_required")
        if norms_required is not None and not isinstance(norms_required, bool):
            raise HubError(400, "channel:meta.norms_required must be a boolean")
        # purpose/norms are free text delivered to every joiner: strip control
        # characters and cap them at write time like every other member-authored
        # headline (they were the one unvalidated path into join/describe).
        # expected_traffic stays free-form (existing rooms use lists).
        for field in ("purpose", "norms"):
            if field in value and value[field] is not None:
                if not isinstance(value[field], str):
                    raise HubError(400, f"channel:meta.{field} must be a string")
                value[field] = sanitize_text(value[field], 500)

    def channel_info(self, agent: AgentInfo, channel: str) -> dict[str, Any]:
        """Everything an agent needs before first post: channel, metadata, members."""
        self.require_membership(channel, agent.id)
        info = self.db.get_channel(channel)
        meta = self.db.store_get(channel, CHANNEL_META_KEY)
        meta_value = meta.value if meta else None
        language = "plain"
        if isinstance(meta_value, dict) and meta_value.get("language") in _META_LANGUAGES:
            language = meta_value["language"]
        # The charter pointer makes discovery mechanical: joiners are told
        # where the room's rules live and which version is current, without
        # guessing paths (design ruling: pointer in the join packet, not a
        # magic filename convention).
        charter_row = self.db.fs_get(channel, FS_PREFIX + CHARTER_PATH)
        charter = None
        if charter_row and not charter_row["deleted"]:
            charter = {"path": CHARTER_PATH, "version": charter_row["version"],
                       "updated_by": charter_row["updated_by"],
                       "updated_at": charter_row["updated_at"]}
        return {
            "channel": info.model_dump() if info else None,
            "meta": meta_value,
            "members": [m.model_dump() for m in self.db.list_members(channel)],
            "response_sla_minutes": self.channel_sla(channel),
            "language": language,
            "state": self.channel_state(channel),
            "is_dm": channel.startswith(DM_PREFIX),
            "charter": charter,
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

    # -- work-id activity index (0093) ---------------------------------------------

    def work_activity(self, agent: AgentInfo, item_id: str) -> dict[str, Any]:
        """One call for the whole stitch: every claim, decision, and message
        citing `item_id` across the channels THE CALLER can read. The
        membership gate is the caller's own channel list — private rooms a
        non-member cannot read simply do not contribute rows."""
        if parse_work_id(item_id) is None:
            raise HubError(400, f"'{sanitize_text(item_id, 64)}' is not a "
                                "work id — the ruled form is <package>-<NNNN> "
                                "(e.g. agora-0093)")
        channels = self.db.channels_of(agent.id)
        out = self.db.work_activity(item_id, channels)
        # The unified-backlog index rows (0103) ride the stitch surface too:
        # the work: row is the item's cross-agent state record, shown beside
        # who claimed it and where it was discussed.
        rows: list[dict[str, Any]] = []
        for ch in channels:
            stored = self.db.store_get(ch, f"{self.WORK_ROW_PREFIX}{item_id}")
            if stored is not None:
                rows.append({"channel": ch, "value": stored.value,
                             "version": stored.version,
                             "updated_by": stored.updated_by,
                             "updated_at": stored.updated_at})
        return {"item_id": item_id, "work_rows": rows, **out}

    # -- reputation (0094): peer ±1 on four fixed axes, per channel ---------------
    #
    # Design constraints from the operator's spec plus the anti-gaming pass:
    # identity-bound (the rater is the authenticated caller), ONE live vote
    # per (rater, target, axis, channel) with revision-in-place (the primary
    # key IS the ballot-stuffing guard), self-votes refused, membership
    # required on both sides (you rate colleagues you actually share a room
    # with), and full attribution kept (votes are public records like
    # messages — visible cost deters frivolous or retaliatory swings).

    REPUTATION_AXES = ("trust", "wisdom", "thorough", "helper")

    def rate_agent(self, agent: AgentInfo, channel: str, target: str,
                   axis: str, value: int, note: str = "") -> dict[str, Any]:
        self._require_unpaused(agent, channel)
        self.require_membership(channel, agent.id)
        self._require_not_archived(channel)
        if axis not in self.REPUTATION_AXES:
            raise HubError(400, f"axis must be one of "
                                f"{'|'.join(self.REPUTATION_AXES)}: "
                                "trust = does what it says; wisdom = often "
                                "right, leads by example; thorough = carries "
                                "work end-to-end with proofs; helper = "
                                "improves OTHERS' work")
        if value not in (1, -1):
            raise HubError(400, "value must be +1 or -1 (one increment per "
                                "vote; revise the same vote to change your "
                                "standing, it never stacks)")
        if target == agent.id:
            raise HubError(400, "self-votes are refused: reputation is what "
                                "COLLEAGUES observe about you")
        if not self.db.agent_exists(target):
            raise HubError(404, f"agent '{target}' is not registered")
        if not self.db.is_member(channel, target):
            raise HubError(400, f"'{target}' is not a member of '{channel}' "
                                "— rate colleagues where you actually work "
                                "with them")
        if len(note) > 280:
            raise HubError(413, "note exceeds 280 characters — the note is "
                                "a one-line WHY, not an essay")
        # Notes are read by terminal/CLI consumers, not only the React UI:
        # sanitize like every other cross-agent text field (strips control
        # chars/ANSI/newlines) so a note can't spoof a CLI leaderboard or
        # injection-poison a log (adversary V6).
        note = sanitize_text(note, 280)
        return self.db.reputation_cast(channel, target, agent.id, axis,
                                       value, note)

    def unrate_agent(self, agent: AgentInfo, channel: str, target: str,
                     axis: str | None = None) -> int:
        """Withdraw the caller's own live vote(s) on target. Pause-gated like
        casting (0094 F3: the board is shared state — a stand-down freezes
        withdrawals too); deliberately NOT archive-gated, since retracting a
        judgment should stay possible on a frozen channel."""
        self._require_unpaused(agent, channel)
        self.require_membership(channel, agent.id)
        if axis is not None and axis not in self.REPUTATION_AXES:
            raise HubError(400, f"axis must be one of "
                                f"{'|'.join(self.REPUTATION_AXES)}")
        return self.db.reputation_clear(channel, target, agent.id, axis)

    def reputation_leaderboard(self, agent: AgentInfo,
                               channel: str | None = None) -> dict[str, Any]:
        """Channel leaderboard (members only) or hub-wide (any registered
        agent: the hub score is the sum of channel scores, already an
        aggregate that leaks no private-channel specifics)."""
        if channel is not None:
            self.require_membership(channel, agent.id)
            return {"channel": channel, "axes": list(self.REPUTATION_AXES),
                    "leaderboard": self.db.reputation_channel(channel)}
        return {"channel": None, "axes": list(self.REPUTATION_AXES),
                "leaderboard": self.db.reputation_hub()}

    def reputation_votes(self, agent: AgentInfo, channel: str,
                         target: str) -> list[dict[str, Any]]:
        """The attributed votes behind one score (the WHY surface)."""
        self.require_membership(channel, agent.id)
        return self.db.reputation_votes_for(channel, target)

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

    # -- message attachments (0091): content-addressed channel blobs ------------

    def attachment_put(self, agent: AgentInfo, channel: str, data: bytes, *,
                       filename: str = "", content_type: str = "") -> dict[str, Any]:
        """Store an attachment blob in this channel; returns its metadata
        ({id, filename, content_type, size, ...}) where id = sha256(bytes).
        Idempotent for identical bytes. The declared content_type is stored
        VERBATIM as metadata and never verified against the bytes — serving
        is hardened independently (safe_serve_content_type), and consumers
        sniff before inline-rendering (contract, dm continuum#10-11).
        Upload is a post-class act: membership, pause, closed-state, and the
        sender rate limit all apply; the charter gate does not (the POST
        that references the blob is where the room's rules bind)."""
        self.require_membership(channel, agent.id)
        self._require_unpaused(agent, channel)
        self._require_not_archived(channel)
        if self.channel_state(channel) == "closed":
            raise HubError(409, f"channel '{channel}' is closed to new posts")
        if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
            raise HubError(400, "attachment is empty — upload the file bytes "
                                "as the request body")
        if len(data) > self.max_attachment_bytes:
            raise HubError(413, f"attachment exceeds {self.max_attachment_bytes} "
                                "bytes (operator-configurable cap)")
        # Per-channel aggregate quota (review P2): append-only blobs cannot be
        # deleted, so without a ceiling one member fills the disk one distinct
        # file at a time — the class that took the volume to 100% today.
        # Skip the walk when the new bytes already exist (dedup = no growth).
        if self.db.blob_meta(channel, hashlib.sha256(data).hexdigest()) is None:
            used = self.db.blob_channel_bytes(channel)
            if used + len(data) > self.max_channel_attachment_bytes:
                raise HubError(413,
                    f"channel attachment storage full: {used} + {len(data)} "
                    f"bytes exceeds the {self.max_channel_attachment_bytes}-byte "
                    "per-channel cap — an operator must raise the cap or archive "
                    "the channel")
        wait = self.ratelimiter.acquire(agent.id)
        if wait > 0.0:
            raise HubError(429, f"rate limit exceeded — retry in {wait:.1f}s "
                                "(steady pace; are you in an upload loop?)")
        filename = sanitize_text(str(filename or ""), MAX_FILENAME_CHARS) or "attachment"
        declared = sanitize_text(str(content_type or ""), MAX_CONTENT_TYPE_CHARS) \
            or "application/octet-stream"
        return self.db.blob_put(channel, bytes(data), filename=filename,
                                content_type=declared, created_by=agent.id)

    def attachment_get(self, agent: AgentInfo, channel: str,
                       blob_id: str) -> tuple[dict[str, Any], bytes]:
        """Fetch an attachment's metadata + bytes. Membership-gated like any
        read; the id is validated as a sha256 hex before touching the DB so
        the 404 cannot act as a shape oracle."""
        self.require_membership(channel, agent.id)
        if not _SHA256_HEX.fullmatch(str(blob_id or "")):
            raise HubError(400, "attachment id must be the blob's sha256 hex")
        found = self.db.blob_get(channel, blob_id)
        if found is None:
            raise HubError(404, f"no attachment {blob_id[:12]}… in '{channel}'")
        return found

    def _validate_attachments(self, raw: Any, channel: str) -> list[dict[str, Any]]:
        """Normalize a message's attachment refs against the channel's blob
        store. Runs on the EFFECTIVE field (typed param or raw `data`), like
        asks/answers — no bypass path. Size/content_type always come from
        the blob row (server truth): a message cannot misdescribe its file."""
        if not isinstance(raw, list):
            raise HubError(400, "attachments must be a list of {id, filename?} refs")
        if len(raw) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise HubError(400, f"a message carries at most "
                                f"{MAX_ATTACHMENTS_PER_MESSAGE} attachments")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict) or "id" not in item:
                raise HubError(400, "each attachment ref needs an id "
                                    "(the blob's sha256 from the upload)")
            blob_id = str(item["id"]).strip().lower()
            if not _SHA256_HEX.fullmatch(blob_id):
                raise HubError(400, "attachment id must be the sha256 hex the "
                                    "upload returned")
            if blob_id in seen:
                raise HubError(400, f"duplicate attachment ref {blob_id[:12]}…")
            seen.add(blob_id)
            meta = self.db.blob_meta(channel, blob_id)
            if meta is None:
                raise HubError(400, f"attachment {blob_id[:12]}… is not uploaded "
                                    f"to '{channel}' — POST the bytes to "
                                    "/channels/{channel}/attachments first")
            filename = sanitize_text(str(item.get("filename") or ""),
                                     MAX_FILENAME_CHARS) or meta["filename"]
            normalized.append({"id": blob_id, "filename": filename,
                               "content_type": meta["content_type"],
                               "size": meta["size"]})
        return normalized

    def _require_channel_authority(self, channel: str, agent: AgentInfo) -> None:
        """The reserved `channel/` fs prefix mirrors the store's `channel:` keys:
        channel-owned surfaces are writable by the owner alone — plus the
        operator, which is the unfreeze path when an owner session is gone
        (there is no ownership transfer). DMs have no owner, so the prefix is
        structurally unwritable there. One check, deliberately not a roles
        system (design ruling, backlog 0060)."""
        if agent.operator or self.db.member_role(channel, agent.id) == "owner":
            return
        raise HubError(403, f"'{RESERVED_FS_PREFIX}...' files are channel-owned: "
                            "writable by the channel owner and the operator only")

    def fs_write(self, agent: AgentInfo, channel: str, path: str, content: str,
                 mime: str = "text/markdown", expect_version: int | None = None,
                 description: str = "") -> FsFile:
        """Create or edit a file (compare-and-swap via `expect_version`; 0 means
        'must not exist yet'). `description` is the writer's one-line statement
        of what the file is, shown in listings; sanitized and capped like a
        title. Returns the new FsFile with its bumped version."""
        self.require_membership(channel, agent.id)
        self._require_unpaused(agent, channel)
        self._require_not_archived(channel)
        norm = self._normalize_fs_path(path)
        if norm.startswith(RESERVED_FS_PREFIX):
            self._require_channel_authority(channel, agent)
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
        if norm == CHARTER_PATH:
            # Writing the charter is reading it: the author holds the freshest
            # receipt by construction (otherwise the gate would block the owner
            # right after their own edit).
            self.db.charter_receipt_set(agent.id, channel, entry.version)
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
            if norm == CHARTER_PATH:
                # Reading the charter HEAD is the acceptance receipt (delivery
                # proof, nothing more). Archive reads are history-browsing and
                # deliberately record nothing.
                self.db.charter_receipt_set(agent.id, channel, row["version"])
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
        self._require_unpaused(agent, channel)
        self._require_not_archived(channel)
        norm = self._normalize_fs_path(path)
        if norm.startswith(RESERVED_FS_PREFIX):
            self._require_channel_authority(channel, agent)
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

    # -- delegation record (0068): authority as verifiable state --------------------

    # Separable powers (ADR-0004): a grant names exactly what it entrusts.
    # `moderation` (kick/ban to protect the collaboration) is deliberately
    # its own power, never a rider on `operational` — ejecting participants
    # is far more consequential than a restart and must be granted on purpose.
    DELEGATION_POWERS = frozenset({"ruling", "operational", "reporting",
                                   "moderation"})
    MAX_DELEGATION_TTL = 30 * 86400.0    # same cap discipline as join tokens
    DEFAULT_DELEGATION_TTL = 7 * 86400.0

    def active_delegations(self) -> list[dict[str, Any]]:
        """Active delegation grants, TTL-cached (consulted on queue writes and
        served in every whoami). Grant/revoke bust the cache."""
        now = time.time()
        if now - self._delegations_cache_at > 1.0:
            self._delegations_cache = self.db.delegations_active()
            self._delegations_cache_at = now
        return self._delegations_cache

    def is_delegate(self, agent_id: str, power: str) -> bool:
        return any(d["agent_id"] == agent_id and power in d["powers"]
                   for d in self.active_delegations())

    def _has_any_delegation(self, agent_id: str) -> bool:
        """True if the agent holds ANY active delegation (any power). Used to
        shield stewards from delegate-imposed kicks (a delegate may not eject
        another delegate; only an operator may)."""
        return any(d["agent_id"] == agent_id for d in self.active_delegations())

    def set_delegation(self, agent_id: str, powers: list[str],
                       ttl_seconds: float | None = None,
                       note: str = "") -> dict[str, Any]:
        """Operator grant (admin surface). The record is a verifiable LABEL
        plus a validation anchor (queue writes, tier fields) — it grants no
        other mechanical power (ADR-0004). Operators cannot be delegates:
        they already hold every power, and a dual role would blur audit."""
        if not self.db.agent_exists(agent_id):
            raise HubError(404, f"agent '{agent_id}' is not registered")
        if agent_id in self.operator_ids():
            raise HubError(400, f"'{agent_id}' is an operator — operators need "
                                "no delegation")
        wanted = [str(p) for p in powers]
        unknown = set(wanted) - self.DELEGATION_POWERS
        if not wanted or unknown:
            raise HubError(400, f"powers must be a non-empty subset of "
                                f"{sorted(self.DELEGATION_POWERS)}"
                                + (f" (unknown: {sorted(unknown)})" if unknown else ""))
        ttl = self.DEFAULT_DELEGATION_TTL if ttl_seconds is None else float(ttl_seconds)
        if not 0 < ttl <= self.MAX_DELEGATION_TTL:
            raise HubError(400, f"ttl must be within (0, {self.MAX_DELEGATION_TTL:.0f}s] "
                                "(expiry is deliberate: a forgotten delegation "
                                "is worse than a renewal)")
        grant = self.db.delegation_set(agent_id, wanted, time.time() + ttl,
                                       sanitize_text(note, 200))
        self._delegations_cache_at = 0.0
        self._ensure_alerts_channel()
        self._post_system(
            self.DARK_ALERTS_CHANNEL,
            f"DELEGATION GRANTED: {agent_id} holds {'+'.join(grant['powers'])} "
            f"until {time.strftime('%Y-%m-%d %H:%M', time.localtime(grant['expires_at']))}"
            f"{' — ' + grant['note'] if grant['note'] else ''}. Every agent can "
            f"verify via whoami.delegations; prose claims count for nothing.")
        return grant

    def revoke_delegation(self, agent_id: str) -> bool:
        revoked = self.db.delegation_revoke(agent_id)
        self._delegations_cache_at = 0.0
        if revoked:
            self._ensure_alerts_channel()
            self._post_system(self.DARK_ALERTS_CHANNEL,
                              f"DELEGATION REVOKED: {agent_id} holds no "
                              f"delegated powers as of now.")
        return revoked

    # -- moderation: kick (timed block) and ban (permanent block) -------------------
    #
    # A kick is a cooling-off signal, not punishment: membership is removed
    # NOW and rejoining refuses until the block expires. A ban is the same
    # block without an expiry. Scope 'hub' locks the identity out of the hub
    # entirely (every authenticated call refuses, teaching text names the
    # lift path). Deliberately NOT gated on pause: moderation is a safety
    # act and must work exactly when things are on fire.

    DEFAULT_KICK_SECONDS = 900.0           # 15 min: enough to type what must change
    MAX_TIMED_BLOCK_SECONDS = 7 * 86400.0  # longer than a week IS a ban — use one
    HUB_SCOPE = "hub"

    def _require_moderation_authority(self, actor: AgentInfo, scope: str) -> None:
        """Who may kick/ban. Operators always may (both scopes). A delegate
        holding `moderation` may too (both scopes) — the owner grants it
        solely to protect the collaboration from misalignment/misbehavior.
        A channel owner may kick within their own channel. Everyone else is
        refused. (Who may be TARGETED is a separate guard in impose_block:
        operators and delegates are shielded so this power can never become
        a coup against the trust chain.)"""
        if actor.operator or self.is_delegate(actor.id, "moderation"):
            return
        if scope == self.HUB_SCOPE:
            raise HubError(403, "hub-scope kicks/bans need an operator or a "
                                "delegate holding 'moderation'")
        if self.db.member_role(scope, actor.id) == "owner":
            return
        raise HubError(403, f"kicks/bans in '{scope}' need the channel owner, "
                            "an operator, or a 'moderation' delegate")

    @staticmethod
    def _block_phrase(block: dict[str, Any]) -> str:
        """One honest clause for refusals and audit lines: who, until when."""
        if block["expires_at"] is None:
            return f"banned by {block['imposed_by']}"
        until = time.strftime("%H:%M", time.localtime(block["expires_at"]))
        return f"kicked by {block['imposed_by']} until {until}"

    def impose_block(self, actor: AgentInfo, agent_id: str, *, scope: str,
                     seconds: float | None, reason: str = "") -> dict[str, Any]:
        """Kick (seconds set) or ban (seconds None) an agent from a channel
        or from the hub. The block is verifiable hub state (GET /blocks);
        enforcement reads the rows, never anyone's prose."""
        self._require_moderation_authority(actor, scope)
        if not self.db.agent_exists(agent_id):
            raise HubError(404, f"agent '{agent_id}' is not registered")
        if agent_id == actor.id:
            raise HubError(400, "you cannot kick or ban yourself")
        # The trust chain is shielded so this power can never become a coup.
        # Operators (which includes the human owner) are never kickable by
        # anyone. And a DELEGATE wielding `moderation` may not target another
        # steward — operator or delegate — so stewards cannot war on each
        # other; a misbehaving delegate is an operator's matter. Operators
        # themselves retain full authority over delegates.
        if agent_id in self.operator_ids():
            raise HubError(403, "operators cannot be kicked or banned — the "
                                "owner and operators are the root of trust")
        if not actor.operator and self._has_any_delegation(agent_id):
            raise HubError(403, f"'{agent_id}' is a delegate; a delegate cannot "
                                "kick another steward — raise it with an operator")
        if scope != self.HUB_SCOPE:
            if scope.startswith(DM_PREFIX):
                raise HubError(400, "DM channels have no owner and no kicks — "
                                    "hub-scope moderation is the operator's tool")
            if self.db.get_channel(scope) is None:
                raise HubError(404, f"channel '{scope}' not found")
            # A channel kick DELETES the member row — including role=owner,
            # with no transfer path — which would strand invite-minting and
            # channel:meta writes forever (review F2). Refuse it: an owner is
            # removed at hub scope, which keeps the membership row so authority
            # thaws on lift.
            if self.db.member_role(scope, agent_id) == "owner":
                raise HubError(403, f"'{agent_id}' owns '{scope}' — a channel "
                                    "kick would strand it (no ownership "
                                    "transfer). Use a hub-scope block instead, "
                                    "which preserves the channel.")
        if seconds is not None:
            if not 0 < seconds <= self.MAX_TIMED_BLOCK_SECONDS:
                raise HubError(400, f"kick duration must be within (0, "
                                    f"{self.MAX_TIMED_BLOCK_SECONDS:.0f}s] — "
                                    "for longer, ban (liftable any time)")
        expires = None if seconds is None else time.time() + seconds
        block = self.db.block_set(scope, agent_id, actor.id, expires,
                                  sanitize_text(reason, 200))
        phrase = self._block_phrase(block)
        if scope == self.HUB_SCOPE:
            # A permanent ban must not leave the fleet's whoami advertising a
            # locked-out identity as an authority (review F4). Revoke on BAN
            # (no expiry); a timed kick keeps the grant — a 15-min cooloff
            # should not destroy a 7-day delegation that will outlive it.
            if expires is None and any(d["agent_id"] == agent_id
                                       for d in self.active_delegations()):
                self.revoke_delegation(agent_id)
            self._ensure_alerts_channel()
            self._post_system(self.DARK_ALERTS_CHANNEL,
                              f"HUB BLOCK: {agent_id} {phrase}"
                              + (f" — {block['reason']}" if block["reason"] else "")
                              + ". Every call refuses while it stands.")
            # Sever live push too: authenticate() only gates NEW calls, so a
            # WebSocket opened before the block would keep delivering for the
            # life of the socket. The control frame makes the ws pump close
            # it (reconnects then refuse at the 4401 gate).
            self.fanout.publish(f"agent/{agent_id}",
                                {"type": "hub-blocked", "detail": phrase})
        else:
            if self.db.is_member(scope, agent_id):
                self.db.remove_member(scope, agent_id)
            self._post_system(scope, f"{agent_id} {phrase}"
                              + (f" — {block['reason']}" if block["reason"] else ""))
        return block

    def lift_block(self, actor: AgentInfo, agent_id: str, *, scope: str) -> bool:
        """Lift a kick or ban early. True only if a live block was lifted."""
        self._require_moderation_authority(actor, scope)
        lifted = self.db.block_lift(scope, agent_id)
        if lifted:
            if scope == self.HUB_SCOPE:
                self._ensure_alerts_channel()
                self._post_system(self.DARK_ALERTS_CHANNEL,
                                  f"HUB BLOCK LIFTED: {agent_id} may sign in again.")
            else:
                self._post_system(scope, f"{agent_id}'s block is lifted — "
                                         "they may rejoin.")
        return lifted

    def list_blocks(self, scope: str | None = None) -> list[dict[str, Any]]:
        """Active blocks, visible to any authenticated agent (verifiability:
        authority claims are checked against hub state, like delegations)."""
        return self.db.blocks_active(scope)

    def _require_not_hub_blocked_id(self, agent_id: str) -> None:
        """Registration-side gate: a hub ban survives key loss — the ID
        cannot re-register its way back in (kick likewise, until expiry)."""
        block = self.db.block_get(self.HUB_SCOPE, agent_id)
        if block is not None:
            raise HubError(403, f"'{agent_id}' is {self._block_phrase(block)} "
                                "from this hub — registration refused")

    # -- decision board (0070): derived pending + curated queue --------------------

    _QUEUE_PREFIX = "queue:"
    _QUEUE_FIELDS = {"q", "options", "evidence", "waiting", "since", "tier",
                     "default", "decided"}

    @staticmethod
    def _validate_queue_row(value: Any) -> None:
        """Schema caps for curated board rows (the anti-essay device): agents'
        prose stays in messages, referenced by seq — a row is a decision
        surface, not a document. WRITE AUTHORITY is mechanical since 0068
        (operator or reporting-delegate, checked in store_set); this
        validates shape and SANITIZES free text — rows reach the operator's
        terminal, so control characters are stripped at the source like
        every other member-authored headline (security M1)."""
        if not isinstance(value, dict):
            raise HubError(400, "queue rows must be objects (see docs: board)")
        unknown = set(value) - HubService._QUEUE_FIELDS
        if unknown:
            raise HubError(400, f"unknown queue-row fields: {sorted(unknown)} "
                                f"(allowed: {sorted(HubService._QUEUE_FIELDS)})")
        q = value.get("q")
        if not isinstance(q, str) or not q.strip() or len(q) > 120:
            raise HubError(400, "queue rows need q: the one-line question (<=120 chars)")
        value["q"] = sanitize_text(q, 120)
        for field, cap, item_cap in (("options", 5, 120), ("evidence", 8, 80),
                                     ("waiting", 10, 64)):
            items = value.get(field)
            if items is None:
                continue
            if (not isinstance(items, list) or len(items) > cap
                    or any(not isinstance(x, str) or len(x) > item_cap for x in items)):
                raise HubError(400, f"queue-row {field} must be <= {cap} strings "
                                    f"of <= {item_cap} chars")
            value[field] = [sanitize_text(x, item_cap) for x in items]
        tier = value.get("tier")
        if tier is not None and tier not in ("operator", "delegate"):
            raise HubError(400, "queue-row tier must be 'operator' or 'delegate'")
        default = value.get("default")
        if default is not None:
            if not isinstance(default, str) or len(default) > 160:
                raise HubError(400, "queue-row default must be a string <= 160 "
                                    "chars (what happens if nobody decides)")
            value["default"] = sanitize_text(default, 160)
        since = value.get("since")
        if since is not None and not isinstance(since, (int, float)):
            raise HubError(400, "queue-row since must be a unix timestamp")
        decided = value.get("decided")
        if decided is not None:
            if not isinstance(decided, str) or len(decided) > 200:
                raise HubError(400, "queue-row decided must be a string <= 200 "
                                    "chars (the decision:<slug> or message ref "
                                    "that settled it)")
            value["decided"] = sanitize_text(decided, 200)

    # Terminal claim-status spellings observed in the field beside the taught
    # {"done": true} (hub rule 2 / the skill): seats write status="done" or
    # "shipped" and mean the same thing. Matched on the status's FIRST word
    # (lowered, punctuation-stripped) since c3349 item 9: seats write
    # "DONE — shipped xyz, receipt c123" and the exact-whole-string match
    # kept re-alerting rows their owners had closed twice. A free-text
    # status like "designed ...; build next session" still stays live —
    # writers lead with the state word, prose follows it.
    _TERMINAL_CLAIM_STATUSES = frozenset(
        {"done", "shipped", "complete", "completed", "delivered", "closed"})
    # Parked spellings: deliberately-idle work. NOT terminal (the board keeps
    # showing it in progress) but the steward sweep must not nag it every SLA
    # window — parking IS the owner's answer to "is this stale?".
    _PARKED_CLAIM_STATUSES = frozenset({"parked", "paused", "on-hold", "onhold"})

    @staticmethod
    def _claim_status_word(value: dict[str, Any]) -> str:
        """First word of the claim's status, lowered, stripped of trailing
        punctuation — the state word the vocabulary keys on. `status` is
        the CANONICAL key (c3363 ruling); `state` is read as a legacy alias
        when no status exists, because a row closed under the wrong key
        must not nag its owner forever — but every taught surface says
        status, and only status is ever written by the hub's own examples."""
        raw = value.get("status")
        if raw is None:
            raw = value.get("state", "")
        status = str(raw).strip().lower()
        first = status.split()[0] if status.split() else ""
        return first.rstrip(".,;:!—-")

    @classmethod
    def _claim_done(cls, value: dict[str, Any]) -> bool:
        """ONE predicate for "this claim row is terminal", shared by the
        board and the steward sweep so the two surfaces can never disagree
        about what is in progress (field finding c2409: the sweep keyed on
        updated_at alone, so done rows re-escalated forever and every
        canvass round bumped timestamps nobody would ever touch again)."""
        if value.get("done"):
            return True
        return cls._claim_status_word(value) in cls._TERMINAL_CLAIM_STATUSES

    @classmethod
    def _claim_parked(cls, value: dict[str, Any]) -> bool:
        """Deliberately-idle claims (c3349): excluded from stale alerts —
        the owner already answered the staleness question — while staying
        live on the board (parked work is unfinished work)."""
        return cls._claim_status_word(value) in cls._PARKED_CLAIM_STATUSES

    def board(self, agent: AgentInfo) -> dict[str, Any]:
        """The viewer's decision board, derived from structure the messages
        and stores already carry (design 0070): pending-on-me (the inbox
        stickiness predicate served as a query), proposals (unaddressed open
        questions), in-progress (live claim:* keys), pending-review (done
        claims awaiting a review class), done (decision:* record), plus the
        curated queue:<viewer>:* rows. One derivation — UIs (the framework's
        Mission Control, `agora board`) render it; none re-derive."""
        ops = self.operator_ids()
        now = time.time()
        pending_on_me: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        in_progress: list[dict[str, Any]] = []
        pending_review: list[dict[str, Any]] = []
        done: list[dict[str, Any]] = []
        queue: list[dict[str, Any]] = []
        for channel in self.db.channels_of(agent.id):
            sla_s = self.channel_sla(channel) * 60.0
            cursor = 0
            while True:
                page = self.db.get_messages(channel, cursor)
                if not page:
                    break
                cursor = page[-1].seq
                for m in page:
                    if m.kind != Kind.message or m.status not in (Status.open, Status.blocked):
                        continue
                    state = discharge_state(m, self.db.replies_to(m.id), ops)
                    if state.closed:
                        continue
                    # Addressees = advisory assignees + per-ask `to` (0077):
                    # a seat named by a still-pending ask has this row
                    # pending ON IT, not floating as a proposal.
                    assignees = {a.get("assignee") for a in asks_of(m)} - {None}
                    assignees |= pending_addressees(m, state.pending)
                    age = now - m.created_at - self.paused_seconds_since(m.created_at)
                    row = {"channel": channel, "seq": m.seq, "id": m.id,
                           "from": m.sender, "q": m.title or m.body[:120],
                           "since": m.created_at, "age_minutes": round(age / 60, 1),
                           "pending_asks": state.pending,
                           "escalated": age > sla_s}
                    if agent.id in m.to or agent.id in assignees:
                        pending_on_me.append(row)
                    elif channel.startswith(DM_PREFIX) and m.sender != agent.id:
                        # A DM has an implicit audience of one: an open DM
                        # question is pending on the peer, never a "proposal"
                        # (review LOW-4).
                        pending_on_me.append(row)
                    elif not m.to and not assignees and m.sender != agent.id:
                        proposals.append(row)
            decision_slugs = set()
            claims: list[tuple[str, Any, Any]] = []
            for entry in self.db.store_keys(channel):
                key = entry["key"]
                if key.startswith("decision:"):
                    decision_slugs.add(key[len("decision:"):])
                    stored = self.db.store_get(channel, key)
                    if stored is not None:
                        done.append({"channel": channel, "key": key,
                                     "version": stored.version,
                                     "updated_by": stored.updated_by,
                                     "updated_at": stored.updated_at})
                elif key.startswith("claim:"):
                    claims.append((key, None, None))
                elif key.startswith(f"{self._QUEUE_PREFIX}{agent.id}:"):
                    stored = self.db.store_get(channel, key)
                    if stored is not None and isinstance(stored.value, dict) \
                            and not stored.value.get("decided"):
                        queue.append({"channel": channel, "key": key,
                                      **stored.value,
                                      "updated_by": stored.updated_by})
            for key, _, _ in claims:
                stored = self.db.store_get(channel, key)
                if stored is None:
                    continue
                v = stored.value if isinstance(stored.value, dict) else {}
                slug = key[len("claim:"):]
                item = {"channel": channel, "task": slug,
                        "owner": v.get("owner", stored.updated_by),
                        "updated_by": stored.updated_by,
                        "updated_at": stored.updated_at}
                if not self._claim_done(v):
                    in_progress.append(item)
                elif v.get("review", "none") in ("operator", "delegate") \
                        and slug not in decision_slugs:
                    pending_review.append({**item, "review": v["review"]})
        pending_on_me.sort(key=lambda r: (not r["escalated"], r["since"]))
        proposals.sort(key=lambda r: r["since"])
        done.sort(key=lambda d: d["updated_at"], reverse=True)
        return {
            "viewer": agent.id,
            "pending_on_me": pending_on_me,
            "queue": queue,
            "proposals": proposals,
            "in_progress": in_progress,
            "pending_review": pending_review,
            "done": done[:20],
            "counts": {"pending_on_me": len(pending_on_me), "queue": len(queue),
                       "proposals": len(proposals),
                       "in_progress": len(in_progress),
                       "pending_review": len(pending_review),
                       "done_shown": min(len(done), 20), "done_total": len(done)},
        }

    # -- operator pause / stand-down (0069) ----------------------------------------

    def hub_paused(self) -> dict[str, Any] | None:
        """The ongoing pause (since/reason/by) or None. Tiny TTL cache: this
        is consulted on every mutating call and per-envelope for the clock
        exclusion; pause transitions are rare."""
        now = time.time()
        if now - self._pause_cache_at > 1.0:
            self._pause_cache = self.db.pause_get()
            self._pause_cache_at = now
        return self._pause_cache

    def _bust_pause_cache(self) -> None:
        self._pause_cache_at = 0.0
        self._intervals_cache_at = 0.0

    def _require_unpaused(self, agent: AgentInfo, channel: str | None = None) -> None:
        """The stand-down gate: while paused, non-operators cannot mutate the
        SHARED world (posts, DMs between agents, store/fs writes, joins).
        Reads, acks, receipts and presence stay open — the operator pauses to
        catch up, and agents may catch up too. Operator exceptions: their own
        posts (incl. criticals) and any DM that involves an operator — "catch
        up including with the delegate" requires the delegate to answer."""
        pause = self.hub_paused()
        if pause is None or agent.operator:
            return
        if channel is not None and channel.startswith(DM_PREFIX):
            ids = channel[len(DM_PREFIX):].split("--")
            if any(i in self.operator_ids() for i in ids):
                return
        since = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(pause["since"]))
        reason = f" (reason: {pause['reason']})" if pause["reason"] else ""
        raise HubError(423, f"hub paused by the operator since {since}{reason} "
                            "— stand down: finish nothing new, do not retry "
                            "in a loop; reads, acks and DMs with the operator "
                            "stay open; whoami.hub_state shows the resume. "
                            "Nothing was posted or written.")

    def set_pause(self, reason: str = "", by: str = "operator") -> dict[str, Any]:
        """Pause the hub (admin surface; idempotent). Broadcasts one system
        message per non-DM channel — one wake to say 'stand down' beats idle
        seats discovering 423s piecemeal without context."""
        state, created = self.db.pause_start(sanitize_text(reason, 200), by)
        self._bust_pause_cache()
        if created:
            self._broadcast_system(
                f"HUB PAUSED by the operator{' — ' + state['reason'] if state['reason'] else ''}. "
                "Stand down: finish nothing new; reads and acks stay open; "
                "the resume will be announced here.",
                data={"hub_state": "paused"})
        return {"state": "paused", **state}

    def clear_pause(self, by: str = "operator") -> dict[str, Any]:
        """Resume (idempotent). Escalation clocks were frozen for the whole
        pause, so nothing bursts on resume."""
        ended = self.db.pause_end()
        self._bust_pause_cache()
        if ended:
            self._broadcast_system(
                "HUB RESUMED by the operator — normal collaboration resumes. "
                "Obligation clocks were frozen for the duration.",
                data={"hub_state": "open"})
        return {"state": "open"}

    def _broadcast_system(self, body: str, data: dict[str, Any] | None = None) -> None:
        for name in self.db.channel_names():
            if not name.startswith(DM_PREFIX):
                message = self.db.insert_message(
                    name, "hub", kind=Kind.system.value, status="fyi",
                    urgency="inbox", title="", body=body, data=data, reply_to=None)
                self._wake(message)

    def _pause_intervals_cached(self) -> list[tuple[float, float | None]]:
        """All pause intervals, TTL-cached: consulted per envelope, so a
        100-message inbox sweep must not mean 100 locked queries (review
        MED-3). Intervals change only on pause/resume, which busts this."""
        now = time.time()
        if now - self._intervals_cache_at > 1.0:
            self._intervals_cache = self.db.pause_intervals(0.0)
            self._intervals_cache_at = now
        return self._intervals_cache

    def paused_seconds_since(self, since_ts: float) -> float:
        """Total paused time overlapping [since_ts, now] — the escalation
        clock exclusion (a pause never ages an obligation toward its SLA)."""
        now = time.time()
        total = 0.0
        for started, ended in self._pause_intervals_cached():
            lo = max(started, since_ts)
            hi = min(ended if ended is not None else now, now)
            if hi > lo:
                total += hi - lo
        return total

    # -- hub rules (operator-authored general instructions) -----------------------

    def hub_rules(self) -> dict[str, Any]:
        """The general instructions every agent receives in /whoami. Version 0
        = the packaged default; the operator's live edits only grow the
        version, so 'am I on the current rules?' is one integer compare."""
        row = self.db.hub_rules_get()
        if row is None:
            return {"version": 0, "text": HUB_RULES_DEFAULT}
        return {"version": row["version"], "text": row["text"]}

    def set_hub_rules(self, text: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise HubError(400, "hub rules text must be a non-empty string")
        if len(text.encode()) > MAX_STORE_VALUE_BYTES:
            raise HubError(413, f"hub rules exceed {MAX_STORE_VALUE_BYTES} bytes")
        row = self.db.hub_rules_set(text)
        return {"version": row["version"], "text": row["text"]}

    # -- dark-episode operator alerts (0067) --------------------------------------

    DARK_ALERTS_CHANNEL = "hub-alerts"

    def _ensure_alerts_channel(self) -> None:
        """Lazy, idempotent: a PRIVATE, ownerless channel where the hub posts
        operator alerts as ordinary system messages — delivery (notify files,
        live push, listener wakes) rides the normal membership fan-out, so no
        new delivery machinery exists. Private + operator-membership because
        alerts name who is behind on what (review HIGH-2); ownerless + a
        reserved name (create_channel refuses it) because a squatter owning
        the room would read and control operator alerts (review HIGH-1).
        Operators are (re)added on every sweep so late-registered operators
        still receive alerts. Reporting delegates are enrolled too (0084):
        stewardship alerts must be able to ADDRESS the steward — an
        addressed message is the wake path proven to work — and alert texts
        already redact private-channel names (HIGH-2), so the wider
        audience leaks nothing new."""
        if self.db.get_channel(self.DARK_ALERTS_CHANNEL) is None:
            self.db.create_channel(self.DARK_ALERTS_CHANNEL, private=True,
                                   created_by="hub", add_owner=False)
        for op in self.operator_ids():
            self.db.add_member(self.DARK_ALERTS_CHANNEL, op, role="member")
        for d in self.active_delegations():
            if "reporting" in d.get("powers", ()):
                self.db.add_member(self.DARK_ALERTS_CHANNEL, d["agent_id"],
                                   role="member")

    def dark_sweep(self) -> list[str]:
        """One watchdog pass (0067): alert the operator ONCE per (agent,
        dark-episode) when a seat is offline while holding an obligation that
        has already escalated past its channel SLA — the state where hub-side
        escalation provably spins in place (the addressee cannot see it) and
        only the operator can start the seat. Episode state is in-memory: a
        hub restart re-alerts once, which is honest. Returns newly-alerted ids."""
        alerted: list[str] = []
        dark_now: set[str] = set()
        if self.db.get_channel(self.DARK_ALERTS_CHANNEL) is not None:
            self._ensure_alerts_channel()  # keep late-registered operators subscribed
        # Forgotten-pause reminder (0069): a pause has no TTL by design, so
        # the watchdog nudges the operator once per 24h while it stands.
        pause = self.hub_paused()
        if (pause is not None and time.time() - pause["since"] > 86400.0
                and time.time() - self._pause_reminded_at > 86400.0):
            self._pause_reminded_at = time.time()
            self._ensure_alerts_channel()
            self._post_system(
                self.DARK_ALERTS_CHANNEL,
                f"HUB STILL PAUSED (since {time.strftime('%Y-%m-%d %H:%M', time.localtime(pause['since']))}"
                f"{', reason: ' + pause['reason'] if pause['reason'] else ''}) — "
                f"resume with `agora resume` when ready; this reminder repeats daily.")
        hub_blocked = {b["agent_id"] for b in self.db.blocks_active(self.HUB_SCOPE)}
        for agent_id in self.db.list_agent_ids():
            if self.presence.get(agent_id).state != "offline":
                # NOT offline, but is it actually HEARING? A seat whose
                # reception loop was arming and then stopped is DEAF: it
                # looks present (stray session calls keep it "active") yet
                # wakes for nothing — the exact class that hid uic/camera/
                # framework for hours (0098). Alarm only when it has
                # SLA-breached addressed work it cannot hear (deafness with
                # consequence) and only if it WAS arming (reception 'stale',
                # never 'unknown' — absence of the heartbeat is not death).
                if agent_id not in hub_blocked:
                    self._deaf_sweep_one(agent_id, dark_now, alerted)
                continue
            # A hub-blocked seat is offline BY DESIGN — the operator locked it
            # out. Alerting "only the operator can start it" is a standing
            # misdiagnosis (review F5), and its obligations now revert to
            # broadcast (F3), so skip it.
            if agent_id in hub_blocked:
                continue
            envelopes = self.inbox(AgentInfo(id=agent_id, name=agent_id))
            # escalated is viewer-specific: open/blocked past SLA, or an
            # addressed directive debt (0102) the seat never engaged.
            overdue = [e for e in envelopes if e.escalated]
            # A dark DELEGATE is the reactive fleet one layer deeper (0084):
            # everything it stewards stalls silently. Alert on ANY pending
            # obligation it holds, not just escalated ones — the operator
            # should hear before the fleet's SLA does.
            holds_delegation = any(
                d["agent_id"] == agent_id for d in self.active_delegations())
            if not overdue and holds_delegation:
                overdue = [e for e in envelopes
                           if e.status in (Status.open, Status.blocked)]
            if not overdue:
                continue
            dark_now.add(agent_id)
            if agent_id in self._dark_since:
                continue  # already alerted this episode
            now = time.time()
            self._dark_since[agent_id] = now
            # Flap guard (review MED-4): an agent oscillating between active
            # and offline (one REST call per hour) must not re-alert on every
            # oscillation while the same overdue work stands.
            if now - self._dark_alerted_at.get(agent_id, 0.0) < DARK_REALERT_SECONDS:
                continue
            self._dark_alerted_at[agent_id] = now
            oldest = min(e.created_at for e in overdue)
            age_min = (now - oldest) / 60
            # Never leak private/DM channel names into the alert (HIGH-2 —
            # the alerts channel is operator-private, but redact anyway:
            # alert texts get quoted and forwarded).
            example = "a private thread"
            ch = self.db.get_channel(overdue[0].channel)
            if ch is not None and not ch.private:
                example = f"{overdue[0].channel}#{overdue[0].seq}"
            self._ensure_alerts_channel()
            self._post_system(
                self.DARK_ALERTS_CHANNEL,
                f"AGENT DARK: {agent_id} is offline holding {len(overdue)} "
                f"SLA-breached obligation(s), oldest ~{age_min:.0f} min "
                f"(e.g. {example}). Escalation cannot reach an offline seat "
                f"— only the operator can start it. One alert per dark "
                f"episode.")
            alerted.append(agent_id)
        # Episodes end when the seat returns or its overdue work clears.
        for agent_id in list(self._dark_since):
            if agent_id not in dark_now:
                del self._dark_since[agent_id]
        # Deaf episodes end the same way: reception recovered or work cleared.
        for agent_id in list(self._deaf_since):
            if agent_id not in dark_now:
                del self._deaf_since[agent_id]
        alerted.extend(self._steward_sweep())
        return alerted

    def _deaf_sweep_one(self, agent_id: str, dark_now: set[str],
                        alerted: list[str]) -> None:
        """DEAF leg of the watchdog (0098): a present-looking seat whose
        reception loop went stale while it holds SLA-breached addressed
        obligations. Same episode-dedupe + flap-guard as AGENT DARK, and
        it shares dark_now so the reception-recovered/work-cleared teardown
        above ends the episode."""
        state, age = self.presence.reception(agent_id)
        if state != "stale":  # 'armed' = hearing; 'unknown' = never announced
            return
        envelopes = self.inbox(AgentInfo(id=agent_id, name=agent_id))
        # Same widened predicate as AGENT DARK: any escalated row — an
        # SLA-breached question OR an ignored directive debt (0102).
        overdue = [e for e in envelopes if e.escalated]
        if not overdue:
            return
        dark_now.add(agent_id)
        if agent_id in self._deaf_since:
            return  # already alerted this deaf episode
        now = time.time()
        self._deaf_since[agent_id] = now
        if now - self._deaf_alerted_at.get(agent_id, 0.0) < DARK_REALERT_SECONDS:
            return
        self._deaf_alerted_at[agent_id] = now
        example = "a private thread"
        ch = self.db.get_channel(overdue[0].channel)
        if ch is not None and not ch.private:
            example = f"{overdue[0].channel}#{overdue[0].seq}"
        self._ensure_alerts_channel()
        self._post_system(
            self.DARK_ALERTS_CHANNEL,
            f"AGENT DEAF: {agent_id} looks present but its reception loop "
            f"went silent ~{age / 60:.0f} min ago while it holds "
            f"{len(overdue)} SLA-breached obligation(s) (e.g. {example}). "
            "Its listener is almost certainly dead — the seat wakes for "
            "nothing. Re-arm it (restart the reception loop / the session); "
            "escalation cannot reach a deaf seat. One alert per deaf episode.")
        alerted.append(agent_id)

    def _steward_sweep(self) -> list[str]:
        """Stewardship half of the watchdog (0084, hardened 0093): a claim
        whose row has not been touched past its channel SLA is work going
        quietly stale — exactly what the reporting delegate exists to
        chase, and exactly what it cannot see without a turn.

        BOUNDED-DEBT CONTRACT (0093, from the 2026-07-17 adversarial
        review): an open system alert is an OBLIGATION on its addressees,
        and v1 posted a new one per flap window without ever closing the
        old — delegates accumulated permanently undischargeable owed rows
        (measured: 8 on one seat, 10 posts in 24h). Now the hub closes its
        own thread like any well-behaved asker: at most ONE stale-claims
        alert stands at any time; a sweep whose live set matches the
        standing alert posts nothing; a changed set supersedes (resolved
        reply, then the new alert); an empty set closes the standing alert.
        Survives hub restarts because the standing alert is FOUND in the
        channel (sender=hub, open, unresolved), not remembered in memory."""
        stewards = sorted({d["agent_id"] for d in self.active_delegations()
                           if "reporting" in d.get("powers", ())})
        if not stewards:
            return []
        now = time.time()
        live: list[str] = []
        live_keys: list[str] = []
        for ch in self.db.channel_names():
            sla_s = self.channel_sla(ch) * 60.0
            for entry in self.db.store_keys(ch):
                key = entry["key"]
                if not key.startswith("claim:"):
                    continue
                stored = self.db.store_get(ch, key)
                if stored is None or not isinstance(stored.value, dict):
                    continue
                if self._claim_done(stored.value):
                    # Finished work is never stale work: a done/shipped row
                    # must not re-escalate on age (c2409).
                    continue
                if self._claim_parked(stored.value):
                    # Parked work is deliberately idle (c3349): the owner
                    # already answered the staleness question.
                    continue
                age = now - stored.updated_at
                if age <= sla_s:
                    continue
                live_keys.append(f"{ch}/{key}")
                owner = str(stored.value.get("owner", "?"))
                # Redact private channels like every alert (HIGH-2).
                info = self.db.get_channel(ch)
                shown = (f"{ch}/{key}" if info is not None and not info.private
                         else "a private-channel claim")
                live.append(f"{shown} (owner {owner}, idle {age / 60:.0f}m)")
        sig = hashlib.sha256("\n".join(sorted(live_keys)).encode()).hexdigest()[:16]
        standing = self._standing_steward_alerts()
        if not live:
            for old in standing:
                self._post_system(
                    self.DARK_ALERTS_CHANNEL,
                    "stale-claims episode closed: every flagged claim was "
                    "touched, finished, or aged back under its SLA.",
                    status="resolved", reply_to=old.id)
            return ["stale-claims:cleared"] if standing else []
        if standing and any(
                isinstance(m.data, dict)
                and m.data.get("steward_sig") == sig for m in standing):
            return []  # the standing alert already states exactly this debt
        for old in standing:
            self._post_system(
                self.DARK_ALERTS_CHANNEL,
                "superseded by the next stale-claims alert (the live set "
                "changed); this episode is closed.",
                status="resolved", reply_to=old.id)
        self._ensure_alerts_channel()
        self._post_system(
            self.DARK_ALERTS_CHANNEL,
            "STALE CLAIMS (stewardship): " + "; ".join(live[:8])
            + (f" (+{len(live) - 8} more)" if len(live) > 8 else "")
            + ". Canvass the owners per your charter: one bundled ask "
              "per seat, or reassign via the queue. Touching the claim "
              "row is the progress receipt that clears this; a row "
              "marked done/shipped never alerts. The hub closes this "
              "alert itself when the set changes or empties.",
            to=stewards, data={"steward_sig": sig})
        return [f"stale-claims:{len(live)}"]

    def _standing_steward_alerts(self) -> list[Message]:
        """Every hub-authored stale-claims alert still standing open (no
        authoritative close). Read from the channel, not memory, so a hub
        restart cannot orphan an open alert."""
        if self.db.get_channel(self.DARK_ALERTS_CHANNEL) is None:
            return []
        out: list[Message] = []
        ops = self.operator_ids()
        for m in self.db.open_obligations([self.DARK_ALERTS_CHANNEL]):
            if m.sender != "hub" or not m.body.startswith("STALE CLAIMS"):
                continue
            if closed_authoritatively(m, self.db.replies_to(m.id), ops):
                continue
            out.append(m)
        return out

    async def dark_watchdog(self, interval_seconds: float = 300.0) -> None:
        """Background loop for dark_sweep (started by the app lifespan;
        interval 0 disables). Failures are logged and swallowed: a watchdog
        must never take the hub down, but must never fail silently either."""
        import logging
        log = logging.getLogger("agora.hub.watchdog")
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await asyncio.to_thread(self.dark_sweep)
            except Exception:
                log.exception("dark sweep failed (will retry next interval)")

    def agent_status_overview(self) -> list[dict[str, Any]]:
        """Operator overview: per agent, presence + unread count + the oldest
        still-pending obligation's age. Reuses the exact inbox computation the
        agent itself would see, so the numbers cannot disagree with reality."""
        now = time.time()
        out = []
        for agent_id in self.db.list_agent_ids():
            info = AgentInfo(id=agent_id, name=agent_id)
            envelopes = self.inbox(info)
            pending = [e for e in envelopes
                       if e.status in (Status.open, Status.blocked) or e.critical]
            oldest = min((e.created_at for e in pending), default=None)
            presence = self.presence.get(agent_id)
            refusals = [r for r in self.refusals.get(agent_id, ())
                        if now - r["ts"] < 3600.0]
            # The lurk metric (0080): debts owed with the cursor already PAST
            # them — the seat served the message, acked it, and never engaged.
            # Computed from the same owed ledger the agent itself sees.
            debts = self.owed(info)
            acked_unanswered = 0
            cursor_cache: dict[str, int] = {}
            for row in debts["to_answer"]:
                ch = row["channel"]
                if ch not in cursor_cache:
                    cursor_cache[ch] = self.db.get_cursor(agent_id, ch)
                if cursor_cache[ch] >= row["seq"]:
                    acked_unanswered += 1
            reception_state, reception_age = self.presence.reception(agent_id)
            out.append({
                "agent_id": agent_id,
                "state": presence.state,
                # Reception truth (0098): armed = listener heard from within
                # the window; stale = it was arming and stopped (DEAF risk);
                # unknown = never announced (not alarmed). Distinct from
                # `state`, which any stray call keeps "active".
                "reception": reception_state,
                "reception_age_minutes": round(reception_age / 60, 1) if reception_age is not None else None,
                "deaf": reception_state == "stale" and len(pending) > 0,
                "unread": len(envelopes),
                "pending_obligations": len(pending),
                "oldest_pending_minutes": round((now - oldest) / 60, 1) if oldest else None,
                "owed_answers": debts["counts"]["to_answer"],
                "owed_consumption": debts["counts"]["to_consume"],
                "acked_unanswered": acked_unanswered,
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
