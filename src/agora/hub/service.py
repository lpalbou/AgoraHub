"""HubService: all hub behavior behind one object, transport-agnostic.

The HTTP API and the WebSocket endpoint are thin translations onto this
class, so behavior (membership enforcement, ordering, rate limits, wake-ups)
is defined exactly once and is directly unit-testable without a server.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .orchestration import OrchestrationMixin
from .fs_triggers import FsTriggersMixin
from .proxy_authority import ProxyAuthorityMixin
from ..agent_id import validate_agent_id
from ..db import (
    Database,
    DuplicateMessage,
    JoinTokenRefused,
    SpawnTransitionRefused,
)
from ..governance import (
    CHANNEL_CHARTER_SEED,
    CHARTER_PATH,
    COMMONS_CHARTER_SEED,
    DELEGATE_POWERS,
    GROUP_CHARTER_TEMPLATE,
    HUB_CHARTER_SCOPE,
    HUB_RULES_DEFAULT,
    PROXY_POWER,
    RESERVED_FS_PREFIX,
    ROLE_CHARTER,
    CharterViewResult,
    charter_view,
    charter_view_covers,
    charter_view_key,
    split_charter,
)
from ..ids import new_token
from ..mentions import resolve_mentions, leading_addressee
from ..models import (
    TextTooLong,
    attributed_quote,
    elide,
    DM_PREFIX,
    FS_PREFIX,
    MAX_ABOUT_CHARS,
    MAX_MISSION_CHARS,
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
    MAX_FS_BINARY_BYTES,
    MAX_FS_PATH_CHARS,
    MAX_SPAWN_DETAIL_CHARS,
    MAX_SPAWN_HARNESS_CHARS,
    MAX_SPAWN_MODEL_CHARS,
    MAX_STORE_VALUE_BYTES,
    NOTICE_KINDS,
    AgentInfo,
    CharterDebt,
    CloseRow,
    ColleagueNote,
    ConsumeRow,
    Envelope,
    FsFile,
    Kind,
    Message,
    MessageRow,
    ObligationRow,
    PickupRung,
    OwedCounts,
    OwedReport,
    PhaseRow,
    PostMessage,
    RatingTally,
    SearchHit,
    SearchReport,
    SearchSection,
    SpawnFolderRefused,
    SpawnRequest,
    SpawnState,
    Status,
    StoreEntry,
    Urgency,
    WaitingRow,
    dm_channel_name,
    parse_work_id,
    sanitize_block,
    sanitize_text,
    sanitize_title,
    validate_spawn_folder,
)
from ..vote import (
    VOTE_RESULT_KEY,
    BallotScan,
    VoteChair,
    fold_ballot_thread,
    published_result,
    receipt_marker,
    result_body,
    result_payload,
    tally_ballots,
    vote_info,
)
from .attention import DEFAULT_RESPONSE_SLA_MINUTES, AttentionPolicy, SlidingWindowBudget
from .findings import DISPOSITIONS, KIND as FINDING_KIND, PREFIX as FINDING_PREFIX, is_typed as is_typed_finding, key_for as finding_key_for, task_ref as finding_task_ref
from .notify import FanOut, LoopBinder, Notifier
from .obligations import (
    DischargeState,
    _cites_evidence,
    _closes,
    ask_addressees,
    asks_of,
    closed_authoritatively,
    declines_of,
    discharge_state,
    pending_addressees,
    resolved_settles_nothing,
    substantive_answers_of,
)
from .presence import PresenceTracker
from .ratelimit import RateLimiter

RESERVED_STORE_PREFIX = "channel:"   # channel-level keys: owner-writable only
JOIN_TOKEN_PREFIX = "agora-join_"    # agora-join_<id:8hex>.<secret:48hex>
MAX_JOIN_TOKEN_TTL = 30 * 86400.0    # hard cap (kubeadm defaults 24h; we cap 30d)
# 0116: grace before surfacing a discharged-but-unclosed own thread.
TO_CLOSE_MIN_AGE_SECONDS = 5 * 60.0
# 0114/0107: routing hints appended to silence-class-tagged watchdog alerts.
# ADVISORY ONLY (operator ruling 2026-07-28): delivery is never refused for
# recipient state — the old saturation/dark 403 gates muted the fleet toward
# the operator and were removed.
_SILENCE_CLASS_ROUTE: dict[str, str] = {
    "dead": "ACTION: start/relaunch the offline seat — escalation cannot reach it.",
    "deaf": "ACTION: re-arm reception loop / restart the session.",
    "unseen": "ACTION: reprompt or relaunch — listener may be armed but debts are unread.",
    # The seat is demonstrably RUNNING (it authored work after these rows
    # arrived), so the relaunch remedy above is not merely useless here, it
    # kills a productive session. Same unread count, opposite action.
    "unseen-but-working": "ACTION: do NOT relaunch — the seat is live and producing; "
                          "these rows are losing its triage race. Drain, re-route or narrow its queue.",
    "seen-and-ignored": "ACTION: compliance (0114) — prefer draining before adding asks.",
}
MAX_JOIN_TOKEN_USES = 100            # fleet provisioning ceiling
CHANNEL_META_KEY = "channel:meta"
_META_FIELDS = {"purpose", "norms", "expected_traffic", "response_sla_minutes", "language",
                "authorship_required", "state", "norms_required", "rulings_required",
                "traffic_policy", "gated_acts"}
#: Act classes a room's OWNER may put behind an owner gate. Closed vocabulary
#: on purpose: each names an act the hub already mediates, so the check is
#: (who are you) x (which API did you call), never a reading of your prose.
#: Default absent — no room changes behaviour until its owner opts in, the
#: same contract `norms_required` has.
#: Deliberately only the three acts that actually BIND on a non-owner seat.
#: `archive` and `vote_close` were considered and dropped: archiving is
#: already owner-or-operator only, and a vote closes by a chaired message
#: rather than a gated call — so naming them here would have shipped
#: vocabulary that can never fire, which is its own documented failure mode
#: on this hub (a fork nudge floored at 10 members in rooms of 6).
GATED_ACT_CLASSES = {"fs_remove", "decision", "phase_complete"}
_CHANNEL_STATES = {"open", "closed"}
_META_LANGUAGES = {"plain", "terse", "structured"}
_TRAFFIC_POLICIES = {"collaboration", "noticeboard"}
MAX_READ_ANCESTORS = 5
DARK_REALERT_SECONDS = 6 * 3600.0   # flap guard: max one alert per agent per window
# 0107: propose retirement after a seat stays dark this long with breached debt.
DARK_RETIRE_PROPOSAL_SECONDS = 7 * 86400.0
LURK_SLA_MULTIPLE = 2.0             # lurk = escalated unread PAST 2x the channel SLA
LURK_CONFIRM_SECONDS = 600.0        # candidate must persist a full listener cycle+turn
#                                     before the alert: a seat that JUST re-armed (or
#                                     is mid-recovery) gets its chance to catch up
# 0106: hub re-emits notify lines at SLA breach steps so file listeners see
# the `escalated` flag (the arm-time /owed signature flip handles the other
# path; this closes the notify-tail gap for --important-only).
ESCALATION_REWAKE_BANDS = (1.0, 2.0, 4.0)  # multiples of channel SLA
# Pre-SLA emit≠process (0106): re-ring unread debts on armed seats when the
# client likely recorded the owed signature without the woken turn reading.
DROPPED_WAKE_REEMIT_SECONDS = 30.0
# 0110: aggregate fleet-liveness — per-seat DARK/DEAF miss "everyone gone".
FLEET_MIN_ELIGIBLE = 3
FLEET_LIVE_FRACTION = 0.5
FLEET_DARK_CONFIRM_SECONDS = 300.0
# How long a seat stays in the FLEET DARK denominator after the hub last
# observed it live (armed reception or recent authenticated activity).
FLEET_SIGNAL_WINDOW = 6 * 3600.0
# 0140 field test 2: the HUB owns the vote deadline. The chair's watcher is
# the fast path, never the guarantee — a driven seat only owns a process
# during a turn, and a 5-minute window sat unpublished through 15 minutes of
# fleet silence. This sweep runs on its OWN cadence (the dark watchdog's 300s
# would make "everyone voted" feel broken), and 30s matches the chair
# watcher's own tick so both publishers react at the same speed.
VOTE_SWEEP_SECONDS = 30.0
# `agora stats` coarse resolution: six of these cover the trailing hour.
ACTIVITY_BUCKET_SECONDS = 600.0
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


def _topic_slug(title: str, max_words: int = 4) -> str:
    """A lowercase-kebab topic slug from a thread title, for the fork nudge's
    pre-filled `agora group` command (0135). Group naming rule: subject nouns,
    2-4 words — future agents search by topic, never by date or seat name."""
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return "-".join(words[:max_words])


def _derived_description(head: str) -> str:
    """Fallback description for files whose writer set none: the first
    non-empty content line, de-markdowned, control-stripped and capped — so a
    listing is never a bare path dump, even for pre-description files."""
    for line in (head or "").splitlines():
        line = elide(" ".join(line.strip().lstrip("#*->|`").split()), 120)
        if line:
            return line
    return ""


def _b64_decoded_size(b64: str) -> int:
    """DECODED byte count of a strict standard-base64 string, computed from
    its length (no decode): reads and listings report a binary file's size in
    real bytes, exactly as text entries report theirs."""
    pad = 2 if b64.endswith("==") else 1 if b64.endswith("=") else 0
    return len(b64) * 3 // 4 - pad


def _fs_sha256(*, content: str | None = None, content_b64: str | None = None) -> str:
    """Digest exact VFS payload bytes, never its JSON/base64 transport."""
    if content_b64 is not None:
        return hashlib.sha256(base64.b64decode(content_b64, validate=True)).hexdigest()
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


class HubError(Exception):
    def __init__(self, status_code: int, detail: Any) -> None:
        text = detail if isinstance(detail, str) else json.dumps(detail, sort_keys=True)
        super().__init__(text)
        self.status_code = status_code
        self.detail = detail


from .task_reviews import TaskReviewsMixin
from .consultations import ConsultationsMixin, is_consultation


class HubService(FsTriggersMixin, ConsultationsMixin, TaskReviewsMixin, OrchestrationMixin, ProxyAuthorityMixin):
    def __init__(self, db: Database, *, rate_per_minute: float = 60.0,
                 interrupts_per_hour: int = 6, criticals_per_hour: int = 5,
                 notify_sink=None,
                 max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
                 max_channel_attachment_bytes: int = MAX_CHANNEL_ATTACHMENT_BYTES,
                 db_path: str = "",
                 embedding: dict[str, str] | None = None,
                 event_log: Callable[[str], None] | None = None) -> None:
        self.db = db
        self._event_log = event_log
        self._ensure_builtin_channels()
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
        # Rating-write budget (agora-0122): reputation writes were the one
        # unmetered class. 30/min per rater — humans never notice, loops do.
        self._rating_budget = SlidingWindowBudget(
            self.RATING_BURST, window_seconds=self.RATING_WINDOW_SECONDS)
        # Search budget (agora-0132): the hub's first expensive READ gets
        # its OWN bucket — a search storm must never eat posting capacity
        # (H9). 30/min, burst 10; the 429's computable wait stays
        # corpus-independent.
        self._search_limiter = RateLimiter(rate_per_minute=30.0, burst=10.0)
        # Hub-written notify files (see hub/notify_sink.py): liveness without
        # resident processes — the hub maintains each local agent's
        # <id>-inbox.log itself, the way the file mailbox's filesystem did.
        self.notify_sink = notify_sink
        # Semantic search lifecycle (agora-0137): the manager owns the
        # vector store, the embedder thread, and the model-change state
        # machine. Constructed always (it reads durable meta and reports
        # `disabled` honestly when nothing is configured); the embedder
        # thread only starts when a model is actually set.
        from .embedding_service import EmbeddingManager
        emb = embedding or {}
        self.embedding = EmbeddingManager(
            db, db_path=db_path, url=emb.get("url", ""),
            model=emb.get("model", ""), api_key=emb.get("api_key", ""))
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
        # Deaf-seat episodes (0098): present-looking but reception-stale.
        self._deaf_since: dict[str, float] = {}
        # Lurking-seat episodes (RC-3, 2026-07-23 forensics): reception ARMED
        # and heartbeating, yet the model behind it never triages — debts rot
        # far past SLA unread while the watchdog sees a healthy pulse. The
        # exact state that hid a two-day fleet blackout from every sweep.
        # _lurk_since = when the candidate state was first observed (alert
        # only after it persists LURK_CONFIRM_SECONDS); _lurk_alerted =
        # episode dedupe, both torn down when the seat reads/answers.
        self._lurk_since: dict[str, float] = {}
        self._lurk_alerted: set[str] = set()
        # 0106: per (agent, message) highest SLA band re-emitted to notify files.
        self._rewake_band: dict[tuple[str, str], int] = {}
        # 0106 emit≠process: last pre-SLA re-emit for unread armed-seat debts.
        self._dropped_wake_at: dict[tuple[str, str], float] = {}
        # 0110: fleet-wide dark episode (aggregate reception collapse).
        self._fleet_dark_since: float | None = None
        self._fleet_dark_alerted = False
        # 0110 denominator repair (2026-08-04): agent -> last time THIS hub
        # observed it live. Persisted as one meta row so a restart keeps
        # continuity; seeded from meta at boot. The old denominator was
        # "every seat ever registered", so a graveyard roster (7 live / 50
        # registered) held FLEET DARK permanently and distorted every
        # liveness-derived surface.
        self._fleet_last_signal: dict[str, float] = {}
        self._fleet_signal_persisted_at = 0.0
        try:
            raw = self.db.meta_get("fleet:last_signal")
            if raw:
                self._fleet_last_signal = {
                    str(k): float(v) for k, v in json.loads(raw).items()}
        except Exception:
            self._fleet_last_signal = {}
        # Sweep telemetry for `agora doctor`: name -> {last_run, seconds, n}.
        # "When did the watchdog last actually run?" was unanswerable from
        # outside the process, so a wedged sweep looked exactly like a quiet
        # fleet. In-memory and honest about it: unknown after a restart.
        self.sweep_runs: dict[str, dict[str, Any]] = {}
        # DARK/DEAF re-alert cooldown (c3436, HOLE 3): PERSISTED, not
        # in-memory. The old in-memory flap guard reset on every restart,
        # so each hub bounce re-fired the whole DARK/DEAF wave off the same
        # standing debts (21 alerts across three restarts one morning). The
        # cache is read-through from the `meta` table so the cooldown
        # survives a bounce; keyed (kind, agent_id).
        self._alerted_cache: dict[tuple[str, str], float] = {}
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
        # Same discipline for the 2026-08-04 operator-ask tightening (an
        # addressed operator message is no longer discharged by any reply,
        # and a delegate's `resolved` must cite evidence). Set once, first
        # boot on a build that carries the rule; every restart agrees.
        self._operator_rule_epoch = float(self.db.meta_set_default(
            "operator_ask_rule_epoch", str(time.time())))
        # ...and again for the 2026-08-06 tightening: answering an operator
        # commission's structured ASKS no longer discharges its BODY. Its
        # OWN epoch — reusing the one above would re-judge every ask-carrying
        # operator message settled in the two days since that rule shipped.
        self._operator_asks_rule_epoch = float(self.db.meta_set_default(
            "operator_asks_rule_epoch", str(time.time())))
        # ...and for the 2026-08-06 canvass rule: an ask naming several seats
        # is answered when EVERY named seat has answered it, not when the
        # first one has. Its own epoch — 28 historical multi-addressee asks
        # would otherwise re-open at once, instantly SLA-breached.
        self._canvass_rule_epoch = float(self.db.meta_set_default(
            "canvass_rule_epoch", str(time.time())))
        # ...and again for the 2026-08-11 peer-addressed tightening: a bare
        # reply to another seat's addressed work ask no longer closes the ask.
        # Its own epoch: older rows that were already settled under the cheap
        # binary rule must stay settled.
        self._peer_addressed_rule_epoch = float(self.db.meta_set_default(
            "peer_addressed_rule_epoch", str(time.time())))
        # ...and for the 2026-08-22 closure ruling: only the asker, an
        # operator, or a reporting delegate citing evidence on an operator's
        # request may close a thread — a bystander's `settled_by` pointer no
        # longer carries authority. Its own epoch, for the same reason as
        # every sibling above: threads already closed under the old rule stay
        # closed. Without it, tightening reopens each one AT ONCE, instantly
        # SLA-breached, and the fleet wakes on work that finished weeks ago.
        self._closure_rule_epoch = float(self.db.meta_set_default(
            "closure_rule_epoch", str(time.time())))
        # Operator-key burst tripwire (0104, the Jul-14 impersonation): on a
        # shared machine any local process can read the operator's cached
        # key and speak as the human — unpreventable hub-side (the key IS
        # the credential), but a 13-DM multicast in 10s is MACHINE cadence.
        # Track operator post timestamps; a burst raises one loud alert.
        self._operator_posts: dict[str, deque] = {}
        self._operator_burst_alerted_at: dict[str, float] = {}

    def _ensure_builtin_channels(self) -> None:
        """Create the conventional built-in rooms a fresh hub should expose.

        `commons` is the operator-wide noticeboard the rules and setup flows
        already teach. Fresh-hub onboarding should not require an operator to
        hand-create that conventional room before `agora setup --channels
        commons` works.

        AND IT IS BORN WITH ITS CHARTER (operator order, agora-and-wui#30).
        Every other room gets one at creation (0146) because `create_channel`
        seeds it; this path calls the db directly, so #commons — the room
        every seat is auto-joined to, and therefore the first charter anyone
        reads — was the single room on every hub that arrived charterless.
        Written through the db rather than `fs_write` because bootstrap has
        no agent to write as and no membership to check: `hub` is the author,
        as it is for the channel itself.

        Create-only (`expect_version=0`), so a hub whose operator has already
        written a charter here keeps every word of it — this seeds hubs that
        do not exist yet, it does not overwrite hubs that do. Best-effort for
        the same reason `_seed_charter` is: a charter write must never be the
        reason a hub fails to start.
        """
        self.db.ensure_channel("commons", private=False, created_by="hub",
                               add_owner=False)
        try:
            self.db.fs_put("commons", FS_PREFIX + CHARTER_PATH,
                           {"content": COMMONS_CHARTER_SEED,
                            "mime": "text/markdown",
                            "description": "channel charter: what the open "
                                           "floor is for, and what belongs "
                                           "elsewhere"},
                           "hub", expect_version=0)
        except Exception:
            logging.getLogger("agora.hub.charter").debug(
                "commons charter already present or unwritable", exc_info=True)
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the serving event loop. Called by every async entry point
        (WebSocket connect, long-poll wait) so cross-thread wakes are safe."""
        self._binder.bind(loop)

    def _log_event(self, event: str, **fields: Any) -> None:
        """Best-effort operator log; lifecycle recording must never fail work."""
        if self._event_log is None:
            return
        parts = [f"AGORA_HUB event={event}"]
        for key, value in fields.items():
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                value = str(value).lower()
            elif isinstance(value, str) and any(
                    char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159
                    for char in value):
                value = json.dumps(value, ensure_ascii=True)
            parts.append(f"{key}={value}")
        try:
            self._event_log(" ".join(parts))
        except Exception:
            logging.getLogger("agora.hub.events").debug(
                "operator event log failed", exc_info=True)

    # -- auth -----------------------------------------------------------------

    @staticmethod
    def _validate_agent_id(agent_id: str) -> None:
        # ASCII-only, no double-dash (would collide with the dm:<a>--<b>
        # separator), reserved ids blocked, bounded length. Prevents Unicode
        # homoglyph impersonation of the one signal the model trusts: identity.
        # Shared by plain registration AND the join-token paths (mint pins and
        # redeem-time ids face the same rules; there is no laxer side door).
        try:
            validate_agent_id(agent_id)
        except ValueError as exc:
            raise HubError(400, str(exc)) from exc

    def register_agent(self, agent_id: str, name: str, operator: bool = False,
                       about: str = "", mission: str = "") -> tuple[AgentInfo, str]:
        """Registration is already an operator act (admin key), so the
        operator's standing charge for the seat may be written here — the one
        other place besides `set_mission` that may touch that column."""
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
                                      sanitize_text(about, MAX_ABOUT_CHARS, field="about"))
        if mission.strip():
            self.db.set_mission(agent_id, sanitize_block(mission, MAX_MISSION_CHARS))
        self._log_event("seat-registered", seat=agent_id,
                        role="operator" if operator else "member")
        self._auto_join_commons(info)
        self._op_gen += 1
        self._operators = None  # bust the closure-authority cache
        return info, api_key

    def _auto_join_commons(self, agent: AgentInfo) -> None:
        """New seats belong to the built-in open floor by default."""
        if self.db.is_member("commons", agent.id):
            return
        self.db.add_member("commons", agent.id)
        about = self.db.get_about(agent.id)
        self._post_system("commons", f"{agent.id} joined"
                                     + (f" — {about}" if about else ""))
        self.db.set_cursor(agent.id, "commons", self.db.last_seq("commons"))
        self._log_event("channel-joined", channel="commons", seat=agent.id,
                        source="registration")

    def operator_ids(self) -> frozenset[str]:
        """Operator agent ids (closure authority, ADR-0003). Cached: it is
        consulted per envelope. TWO writers bust it — `register_agent` and
        `set_agent_role` — and the generation check means a racing write can
        never freeze a stale set into the cache. (It said "registration is
        the only mutation path" until roles became changeable; a promotion
        the cache never saw looks exactly like the ruling not working.)"""
        cached = self._operators
        if cached is not None:
            return cached
        gen = self._op_gen
        ids = frozenset(self.db.list_operator_ids())
        if gen == self._op_gen:
            self._operators = ids
        return ids

    def set_agent_role(self, agent: AgentInfo, target_id: str,
                       operator: bool) -> dict[str, Any]:
        """Promote a seat to operator, or demote it back to member.

        Registration was the only writer of this column, so a fleet wired by
        `agora setup` had no operator and no way to mint one: the human's own
        seat could not set a mission, and the `from-operator` carve-outs in
        `qualifies`, the stop hook and the SDK were unreachable code on every
        CLI-provisioned hub.

        WHO MAY CALL IT: an operator, or the admin key (`operator_or_admin`) —
        the same gate as every other lifecycle verb. Never a seat on itself:
        self-promotion from a member key would make the whole role meaningless.

        THE LAST OPERATOR MAY NOT DEMOTE THEMSELVES. Operator-hood is the only
        identity-bearing authority; with zero operators the hub keeps working
        but every operator verb is reachable only from the machine holding the
        admin key. That is a recoverable state, not a nice one, and it should
        be entered on purpose (demote via the admin key) rather than by a seat
        tidying up its own roster."""
        # `operator_or_admin` AUTHENTICATES; it does not authorize. It hands
        # back whichever seat's key was presented (the admin key arrives as a
        # synthetic `operator` principal), so every lifecycle verb enforces
        # operator-hood itself — `retire_agent` does the same three lines
        # above. Without this a plain member could demote the operator, which
        # is exactly what happened the first time this verb was tested.
        if not agent.operator:
            raise HubError(403, "changing a seat's role is an operator act")
        if not self.db.agent_exists(target_id):
            raise HubError(404, f"no such agent '{target_id}'")
        if self.db.agent_retirement(target_id) is not None:
            raise HubError(409, f"agent '{target_id}' is retired")
        was = self.db.agent_is_operator(target_id)
        if operator and agent.id == target_id and not was:
            # Unreachable while the gate above stands (a non-operator never
            # gets here), and stated anyway: the day someone loosens that
            # gate, self-promotion is the hole it opens.
            raise HubError(403, "a seat cannot promote itself")
        if not operator:
            remaining = set(self.db.list_operator_ids()) - {target_id}
            if was and not remaining and agent.id == target_id:
                raise HubError(
                    409,
                    "you are the last operator: demoting yourself would leave "
                    "the hub with none, and only the admin key could appoint "
                    "another. Promote a successor first, or do it with the "
                    "admin key if that is really what you want.")
        self.db.set_operator(target_id, operator)
        # BUST THE CLOSURE-AUTHORITY CACHE. `operator_ids()` memoizes on the
        # assumption that registration is the only mutation; without this a
        # promoted seat would keep being read as a member for the life of the
        # process — and the bug would look like "the ruling did not work".
        self._op_gen += 1
        self._operators = None
        if was == operator:
            return {"agent": target_id, "operator": operator, "changed": False,
                    "by": agent.id}
        try:
            self._ensure_alerts_channel()
            if operator:
                # An operator reads hub-alerts by construction (the room's
                # whole membership is operators + reporting delegates), so a
                # promotion that does not seat them there is a promotion in
                # name only.
                if not self.db.is_member(self.DARK_ALERTS_CHANNEL, target_id):
                    self.db.add_member(self.DARK_ALERTS_CHANNEL, target_id)
            elif target_id not in self.reporting_delegate_ids():
                # ...and a demotion that leaves them there is a disclosure
                # leak, not an oversight: that room is private precisely
                # because its alerts name which seats are behind on what. A
                # reporting delegate keeps its seat on its own authority.
                self.db.remove_member(self.DARK_ALERTS_CHANNEL, target_id)
        except HubError:
            pass
        # ANNOUNCE ON BOTH EDGES, like a delegation. Operator-hood is a
        # strictly larger grant than any delegation, and `grant_delegation`
        # has told both the room and the holder since 2026-08-06 ("a power
        # the holder cannot discover is not a power"). A role that changes
        # in silence is worse: the seat keeps acting under authority it no
        # longer has, and the room cannot tell who may rule on what.
        role = "an operator" if operator else "a member"
        self._post_system(
            self.DARK_ALERTS_CHANNEL,
            f"ROLE CHANGED: {target_id} is now {role} — by {agent.id}.",
            status=Status.fyi)
        self._post_system(
            self.DARK_ALERTS_CHANNEL,
            (f"YOU ARE NOW AN OPERATOR ({target_id}), granted by {agent.id}. "
             "You may set missions, delegate, rule on charters, pause and "
             "resume the hub, and close any thread. Your open/blocked in a "
             "channel wakes every member of it, addressed or not — the room "
             "is expected to evaluate what you ask. Use it deliberately."
             if operator else
             f"YOUR ROLE CHANGED ({target_id}): you are now a member, by "
             f"{agent.id}. Operator verbs will refuse from here. Anything "
             "you were carrying on that authority stops — say so where you "
             "were carrying it rather than letting it look done."),
            to=[target_id], status=Status.fyi)
        self._log_event("role-changed", seat=target_id,
                        role="operator" if operator else "member",
                        by=agent.id)
        return {"agent": target_id, "operator": operator, "changed": True,
                "by": agent.id}

    def set_about(self, agent: AgentInfo, about: str) -> AgentInfo:
        """Self-description: scope of ownership, what to ask this agent about.
        Self-editable only; sanitized like titles (every joiner reads it).

        Structurally CANNOT reach the seat's mission: they are separate
        columns with separate writers. When the two shared storage, the first
        driven turn of `rt2-critic` called this method and replaced its
        operator charge — "disagreement is your job ... if you end a phase
        having agreed with everyone, you did not do your job" — with a tidy
        summary of itself (2026-08-06)."""
        self._require_unpaused(agent)
        cleaned = sanitize_text(about, MAX_ABOUT_CHARS, field="about")
        self.db.set_about(agent.id, cleaned)
        return agent.model_copy(update={"about": cleaned})

    def set_mission(self, agent_id: str, mission: str) -> dict[str, Any]:
        """The OPERATOR writes a seat's standing mission (admin surface).

        Its own column, and it rides every `whoami` — the one place a fresh
        harness session is guaranteed to read before it does anything. The
        AUTHOR is what makes it different from `about`: a seat may describe
        itself, but it may not write its own charge. A critic that can
        soften its own mandate is not adversarial by construction.

        Measured, 2026-08-06: `rt2-lead` was the only working seat on the
        hub with an empty mission, and it declared a build finished at
        message 4 of 62, naming two files that did not exist yet. The two
        seats whose text encoded a PROCESS ("owns decomposing work into
        addressed asks", "pressure-tests decisions") held their lanes across
        five phases. The one durable per-seat sentence predicted behaviour
        better than a 400-word commission message did."""
        if not self.db.agent_exists(agent_id):
            raise HubError(404, f"agent '{agent_id}' is not registered")
        cleaned = sanitize_block(mission, MAX_MISSION_CHARS, field="mission")
        if not cleaned.strip():
            raise HubError(400, "a mission must say what the seat is FOR — "
                                "an empty one is how a delegate arrives with "
                                "no idea that its job is to orchestrate")
        with self.db.orchestration_lock:
            self.db.set_mission(agent_id, cleaned, notify=True)
            self._mission_change_sweep()
        return {"agent_id": agent_id, "mission": cleaned}

    def _mission_change_sweep(self) -> list[str]:
        """Deliver a changed charge through existing reception, without its text.

        Setup before channel membership and crashes after a write retain the
        marker. A publication retry is deduplicated by the ledger.
        """
        if self.hub_paused() is not None:
            return []
        fired = []
        with self.db.orchestration_lock:
            for seat, revision in self.db.meta_list_prefix("mission-change:").items():
                channels = [c for c in sorted(self.db.channels_of(seat))
                            if self.channel_state(c) not in ("closed", "archived")]
                if not channels:
                    continue
                channel = "commons" if "commons" in channels else channels[0]
                try:
                    self._post_system(channel,
                        "Your operator changed your mission. Call whoami and reconsider affected work before continuing.",
                        to=[seat], status="fyi", urgency="next_turn",
                        data={"mission_changed": {"seat": seat, "revision": revision}},
                        dedupe_key=f"mission:{seat}:{revision}")
                except DuplicateMessage as exc:
                    self._wake(self.db.get_message(exc.message_id))
                self.db.meta_delete("mission-change:" + seat)
                fired.append(seat)
        return fired

    def list_missions(self) -> list[dict[str, Any]]:
        """Every live seat and its charge, blanks included. The blanks are
        the point: an unmissioned seat is invisible until you ask for the
        list, and by then it has already improvised a role."""
        return [{"agent_id": aid,
                 "mission": (self.db.get_mission(aid) or "").strip()}
                for aid in self.db.list_agent_ids()]

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
                          created_by: str = "admin",
                          mission: str = "") -> dict[str, Any]:
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
            token_id, secret, agent_id, sanitize_text(about, MAX_ABOUT_CHARS, field="about"),
            preset, created_by, ttl_seconds, max_uses,
            mission=sanitize_block(mission, MAX_MISSION_CHARS, field="mission"))
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
                about=sanitize_text(about, MAX_ABOUT_CHARS, field="about"))
        except JoinTokenRefused as e:
            raise HubError(e.status_code, e.detail) from e
        self._log_event("seat-registered", seat=info.id, role="member",
                        source="join-token")
        self._auto_join_commons(info)
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

    # -- spawn requests (a seat is WANTED; a runner pulls it) --------------------
    #
    # The hub records the intent. A human-started `agora runner` on the target
    # machine claims the row and runs the same join a human runs today. Design:
    # `plan/spawn-a-seat-from-the-chat.md` (agora-and-wui vfs), from dm#24.

    #: meta-key prefix for the machine -> runner-seat registry. This is
    #: deliberately NOT the `machines` table: that is v2, and the design says
    #: so. It is the smallest thing that can answer "who may claim work for
    #: this machine" — and it answers NOBODY until an admin says otherwise.
    MACHINE_RUNNER_PREFIX = "spawn_runner:"

    #: meta-key prefix for a runner's ANNOUNCED harness set — the only source
    #: any client's dropdown or completion may read.
    MACHINE_HARNESS_PREFIX = "spawn_harnesses:"

    #: A spawned seat's join token is redeemed by a runner that is already
    #: holding the row: minutes, not the 24h a human onboarding gets.
    SPAWN_TOKEN_TTL = 900.0

    @staticmethod
    def _validate_machine(machine: str) -> str:
        name = (machine or "local").strip()
        if not name or len(name) > 64 or any(
                ord(c) < 33 or ord(c) == 127 for c in name):
            raise HubError(400, "machine must be a short printable name with "
                                "no spaces or control characters")
        return name

    def set_machine_runner(self, machine: str, agent_id: str) -> dict[str, str]:
        """Name the ONE seat allowed to claim spawn work for `machine`.

        This gate is not in the reviewed design and it closes a hole that was:
        `claim` hands out a join token, so without it ANY authenticated member
        could claim a pending request and register the wanted seat under its
        own key — the hub's own "identity creation is admin-key-only" line,
        walked around. Writing it is therefore an ADMIN act, like every other
        identity verb, and the registry starts EMPTY: with no runner named,
        nothing can be claimed at all. That default is also exactly the
        "no runner available on <machine>" empty state both clients agreed to
        render, true by construction rather than by a client's guess.
        """
        machine = self._validate_machine(machine)
        self._validate_agent_id(agent_id)
        if not self.db.agent_exists(agent_id):
            raise HubError(404, f"no such agent '{agent_id}' — a runner is a "
                                "seat with its own key, registered like any "
                                "other")
        self.db.meta_set(self.MACHINE_RUNNER_PREFIX + machine, agent_id)
        self._log_event("runner-assigned", machine=machine, runner=agent_id)
        return {"machine": machine, "runner": agent_id}

    def clear_machine_runner(self, machine: str) -> bool:
        machine = self._validate_machine(machine)
        changed = self.db.meta_delete(self.MACHINE_RUNNER_PREFIX + machine)
        if changed:
            self._log_event("runner-cleared", machine=machine)
        return changed

    def machine_runner(self, machine: str) -> str:
        return self.db.meta_get(self.MACHINE_RUNNER_PREFIX + machine) or ""

    def list_machine_runners(self) -> dict[str, str]:
        return self.db.meta_list_prefix(self.MACHINE_RUNNER_PREFIX)

    #: A runner is called SILENT after this many of its own poll intervals.
    #: Twelve is deliberately generous: a missed poll is a loaded machine, and
    #: calling a live runner dead is the more expensive error — an operator who
    #: restarts a healthy runner loses whatever it was hosting. It multiplies
    #: the runner's ANNOUNCED interval rather than a constant, so a runner
    #: polling every 60s is not called dead at 60s.
    STALE_POLL_MULTIPLE = 12

    def announce_harnesses(self, agent: AgentInfo, machine: str,
                           harnesses: list[str],
                           capabilities: dict[str, Any] | None = None,
                           poll_seconds: float | None = None
                           ) -> dict[str, Any]:
        """The runner says what it can actually run, and with which KNOBS.

        There is deliberately no hub-side harness enum to validate against:
        every new harness would then need a hub release, and the hub cannot
        know what is installed on someone else's machine anyway. Names are
        stored as sent and every client renders an unrecognised one VERBATIM.

        `capabilities` extends that rule one level down, to the values each
        harness ACCEPTS (2026-08-23). The harness list stopped clients
        inventing harnesses; nothing stopped them inventing reasoning
        vocabularies, so `agora` hand-transcribed four adapters into a message
        as the source for a client dropdown and it was wrong by two before it
        was sent. The hub validates the SHAPE and never the values — an
        unknown reasoning level is the runner's truth about its own machine,
        exactly like an unknown harness name.

        `models` (2026-08-24, laurent's per-harness model list, `commons#296`)
        is the same rule for the model menu, with one deliberate asymmetry:
        `reasoning` is a GATE the hub enforces at the spawn door, `models` is
        only a MENU. See `_clean_model_list`.
        """
        machine = self._require_runner(agent, machine)
        names = sorted({h.strip() for h in harnesses
                        if isinstance(h, str) and h.strip()})
        if len(names) > 32 or any(len(n) > MAX_SPAWN_HARNESS_CHARS
                                  for n in names):
            raise HubError(400, "harness names must be at most "
                                f"{MAX_SPAWN_HARNESS_CHARS} characters, at "
                                "most 32 of them")
        caps = self._clean_capabilities(capabilities, names)
        now = time.time()
        # A poll interval the runner did not announce is NOT defaulted here.
        # The hub has no way to know it — it is a runner-side flag — and a
        # guessed cutoff served as fact is the invented constant this field
        # exists to remove from the clients.
        try:
            poll = float(poll_seconds) if poll_seconds is not None else None
        except (TypeError, ValueError):
            poll = None
        if poll is not None and not (0 < poll < 86400):
            raise HubError(400, "poll_seconds must be a positive number of "
                                "seconds under a day")
        # `at` is the startup stamp and never moves again; `last_seen` is the
        # liveness one and moves on every poll. Both set here so a runner that
        # has announced but not yet polled is not reported as unheard-from.
        self.db.meta_set(self.MACHINE_HARNESS_PREFIX + machine,
                         json.dumps({"harnesses": names, "capabilities": caps,
                                     "at": now, "last_seen": now,
                                     "poll_seconds": poll}))
        self._log_event("runner-announced", machine=machine, runner=agent.id,
                        harnesses=len(names))
        return {"machine": machine, "harnesses": names, "capabilities": caps}

    def _clean_capabilities(self, capabilities: dict[str, Any] | None,
                            names: list[str]) -> dict[str, Any]:
        """Shape-check the announced knobs; never judge their values.

        Refused rather than trimmed when a harness is described that was not
        announced: a capability row for a harness nobody can spawn is a client
        rendering a control that cannot be used, which is the silent-wrong
        class this whole path exists to end.

        The same rule applies to the KNOB NAMES (2026-08-24). Until now this
        function rebuilt each row from three keys and dropped every other one,
        so a runner announcing `models` was answered `200` with the field
        silently absent from the echo — the confirmation-that-lies shape the
        `model` field's own docstring was written against. An unknown knob is
        now refused BY NAME: the runner and the hub ship from one package, the
        refusal is printed by the runner's own startup log, and a machine that
        announces nothing is visible where a dropped knob is not."""
        if not capabilities:
            return {}
        if not isinstance(capabilities, dict):
            raise HubError(400, "capabilities must be a map of harness -> "
                                f"{{{', '.join(sorted(self.CAPABILITY_KEYS))}}}")
        unknown = sorted(set(capabilities) - set(names))
        if unknown:
            raise HubError(
                400, f"capabilities describe {', '.join(unknown)}, which "
                     f"{'is' if len(unknown) == 1 else 'are'} not in the "
                     "announced harness list — announce the harness or drop "
                     "the row; a knob for a harness nobody can spawn renders "
                     "as a control that cannot be used")
        out: dict[str, Any] = {}
        for name, row in capabilities.items():
            if not isinstance(row, dict):
                raise HubError(400, f"capabilities['{name}'] must be a map")
            stray = sorted(set(row) - self.CAPABILITY_KEYS)
            if stray:
                raise HubError(
                    400, f"capabilities['{name}'] names knobs this hub does "
                         f"not carry: {', '.join(stray)}. It serves "
                         f"{', '.join(sorted(self.CAPABILITY_KEYS))} — drop "
                         "the key or upgrade the hub. Refused rather than "
                         "dropped: a knob accepted and not served is a "
                         "runner that believes it announced something")
            vocab = row.get("reasoning") or []
            if not isinstance(vocab, list) or len(vocab) > 32:
                raise HubError(400, f"capabilities['{name}'].reasoning must "
                                    "be a list of at most 32 values")
            levels = [str(v).strip() for v in vocab if str(v).strip()]
            if any(len(v) > MAX_SPAWN_HARNESS_CHARS for v in levels):
                raise HubError(400, "a reasoning value must be at most "
                                    f"{MAX_SPAWN_HARNESS_CHARS} characters")
            default_model = row.get("default_model")
            if default_model is not None:
                # REFUSE, never slice: a truncated model id is a string that
                # looks like a model and is not one, and the client would
                # render it as this machine's default. The repo's own
                # no-silent-truncation guard caught this line as a slice.
                default_model = str(default_model).strip()
                if len(default_model) > MAX_SPAWN_MODEL_CHARS:
                    raise TextTooLong(f"capabilities['{name}'].default_model",
                                      len(default_model),
                                      MAX_SPAWN_MODEL_CHARS)
            out[name] = {
                "reasoning": levels,
                "reasoning_advisory": bool(row.get("reasoning_advisory")),
                "default_model": default_model or None,
            }
            models = row.get("models")
            if models is not None:
                out[name]["models"] = self._clean_model_list(name, models)
        return out

    #: Every knob name this hub serves. A runner announcing one that is not
    #: here is refused by name rather than trimmed (see `_clean_capabilities`).
    CAPABILITY_KEYS = frozenset({"reasoning", "reasoning_advisory",
                                 "default_model", "models"})

    #: A machine may offer a long menu; this only stops a runner writing an
    #: unbounded list into a row every client renders.
    MAX_ANNOUNCED_MODELS = 64

    def _clean_model_list(self, harness: str, models: Any) -> list[str]:
        """Shape-check an announced model menu. THREE STATES, all distinct.

        Absent (the key is not sent) means *this runner has not said* — the
        client leaves the model a free-text field. `[]` means the runner said
        it constrains nothing, which reads differently to an operator and is
        why an empty list is stored rather than folded into absent. A list is
        the menu.

        It is a MENU, never a gate. The hub refuses a `reasoning` level the
        machine did not announce, because the machine said it cannot express
        it; it does NOT refuse an unlisted `model`, because no adapter
        enumerates models and this list is whatever a human at that machine
        typed. Enforcing a hand-typed list would refuse working models with a
        hub-authored 'not allowed', which is the invented vocabulary this
        whole path exists to keep out of the hub."""
        if not isinstance(models, list):
            raise HubError(400, f"capabilities['{harness}'].models must be a "
                                "list of model ids — omit the key for 'this "
                                "runner has not said', [] for 'it constrains "
                                "nothing'")
        if len(models) > self.MAX_ANNOUNCED_MODELS:
            raise HubError(400, f"capabilities['{harness}'].models must hold "
                                f"at most {self.MAX_ANNOUNCED_MODELS} ids")
        out: list[str] = []
        for entry in models:
            ident = str(entry).strip()
            if not ident:
                continue
            # REFUSE, never slice — same reason as `default_model` above: a
            # truncated model id is a string that looks like a model and is
            # not one, and it would land in a dropdown as a choosable option.
            if len(ident) > MAX_SPAWN_MODEL_CHARS:
                raise TextTooLong(f"capabilities['{harness}'].models entry",
                                  len(ident), MAX_SPAWN_MODEL_CHARS)
            if ident not in out:
                out.append(ident)
        return out

    _EMPTY_MACHINE = {"harnesses": [], "capabilities": {},
                      "announced_at": None, "last_seen_at": None,
                      "stale_after_seconds": None}

    def machine_harnesses(self, machine: str) -> dict[str, Any]:
        raw = self.db.meta_get(self.MACHINE_HARNESS_PREFIX + machine)
        if not raw:
            return dict(self._EMPTY_MACHINE)
        try:
            row = json.loads(raw)
        except ValueError:
            return dict(self._EMPTY_MACHINE)
        poll = row.get("poll_seconds")
        return {"harnesses": list(row.get("harnesses") or []),
                "capabilities": dict(row.get("capabilities") or {}),
                "announced_at": row.get("at"),
                "last_seen_at": row.get("last_seen"),
                # Served so no client holds a constant. Null means the runner
                # did not announce its interval — render the age with NO
                # verdict rather than picking a cutoff, because two clients
                # picking their own is how they come to disagree about one
                # machine in front of one operator.
                "stale_after_seconds": (poll * self.STALE_POLL_MULTIPLE
                                        if isinstance(poll, (int, float))
                                        and poll > 0 else None)}

    def touch_machine_runner(self, machine: str) -> None:
        """Record that this machine's runner spoke to us just now.

        The runner already proves it is alive on a cadence: its claim poll is
        an authenticated call every `poll_seconds` (5s by default) whether or
        not there is anything to spawn. Until 2026-08-24 the hub threw that
        away and served only `announced_at` — a one-time startup stamp — so a
        runner that announced and then DIED presented identically to one alive
        and idle, and no client or seat could answer "is a runner up?". This
        is that evidence, kept. No new endpoint and no runner change: an
        un-upgraded runner heartbeats correctly because the poll it already
        makes IS the heartbeat.

        A row is NEVER created here when none exists. `announced_at: null`
        means no runner ever announced successfully, and minting a row would
        turn that into "announced with no harnesses installed" — a different
        documented fact the clients render differently. An absent announce
        with a live `last_seen_at` stays visible as exactly that.
        """
        raw = self.db.meta_get(self.MACHINE_HARNESS_PREFIX + machine)
        if not raw:
            return
        try:
            row = json.loads(raw)
        except ValueError:
            return
        row["last_seen"] = time.time()
        self.db.meta_set(self.MACHINE_HARNESS_PREFIX + machine, json.dumps(row))

    def list_machines(self) -> list[dict[str, Any]]:
        """Every machine an operator can route to, with what it can run.

        `announced_at: null` means no runner has ever started there — a
        different fact from "a runner is up and can run nothing", and the
        clients render them differently. `last_seen_at` is the LIVENESS one:
        when the machine's runner last spoke to the hub. Null means it has not
        since this hub started keeping the fact — never "it is down"."""
        out = []
        for machine, runner in sorted(self.list_machine_runners().items()):
            out.append({"machine": machine, "runner": runner,
                        **self.machine_harnesses(machine)})
        return out

    def _require_runner(self, agent: AgentInfo, machine: str) -> str:
        machine = self._validate_machine(machine)
        named = self.machine_runner(machine)
        if not named:
            raise HubError(
                403, f"no runner is registered for '{machine}' — an admin "
                     f"names one with PUT /admin/machines/{machine}/runner "
                     "before any seat can be spawned there")
        if agent.id != named:
            raise HubError(403, f"'{named}' is the runner for '{machine}'")
        return machine

    def create_spawn_request(self, actor: AgentInfo, *, seat_id: str,
                             mission: str = "", harness: str = "",
                             machine: str = "local", folder: str = "",
                             channels: list[str] | None = None,
                             options: dict[str, Any] | None = None,
                             model: str = "", reasoning: str = ""
                             ) -> SpawnRequest:
        """Record that a seat is wanted. Every refusal here happens BEFORE the
        row exists, so a request that cannot possibly succeed never becomes a
        pending row a runner has to reject minutes later on another machine."""
        machine = self._validate_machine(machine)
        self._validate_agent_id(seat_id)
        self._require_not_hub_blocked_id(seat_id)
        if self.db.agent_exists(seat_id):
            raise HubError(409, f"agent '{seat_id}' already exists")
        if self.db.agent_retirement(seat_id) is not None:
            raise HubError(409, f"agent id '{seat_id}' is retired and is never "
                                "reused")
        harness = (harness or "").strip()
        if not harness:
            raise HubError(400, "harness is required — the runner refuses one "
                                "it does not have installed, and a request "
                                "without one cannot be checked at all")
        if len(harness) > MAX_SPAWN_HARNESS_CHARS:
            raise HubError(400, f"harness name exceeds "
                                f"{MAX_SPAWN_HARNESS_CHARS} characters")
        try:
            folder = validate_spawn_folder(folder)
        except SpawnFolderRefused as e:
            raise HubError(400, str(e)) from e
        mission = sanitize_block(mission, MAX_MISSION_CHARS, field="mission")
        preset = [c.strip() for c in (channels or [])
                  if isinstance(c, str) and c.strip()]
        opts = options if isinstance(options, dict) else {}
        model = (model or "").strip()
        if len(model) > MAX_SPAWN_MODEL_CHARS:
            raise TextTooLong("model", len(model), MAX_SPAWN_MODEL_CHARS)
        reasoning = self._validate_spawn_reasoning(machine, harness, reasoning)
        row = self.db.create_spawn_request(
            machine=machine, seat_id=seat_id, mission=mission, harness=harness,
            folder=folder, channels=preset, options=opts,
            model=model, reasoning=reasoning, requested_by=actor.id)
        self._log_event("spawn-requested", spawn=row.id, seat=seat_id,
                        machine=machine, harness=harness, by=actor.id)
        return row

    def _validate_spawn_reasoning(self, machine: str, harness: str,
                                  reasoning: str) -> str:
        """Refuse a reasoning level THIS MACHINE said it cannot express.

        The hub holds no vocabulary of its own: it checks against what the
        runner announced for this harness (`capabilities` on /machines), so a
        refusal is the machine's own statement rather than a hub-side enum
        that would need a release every time a vendor adds a level.

        The mirror of `drive.py`'s arm-time check, which exists because
        `--reasoning-effort max` on a harness stopping at `xhigh` used to arm
        GREEN and then fail every single wake with rc=1. The spawn row is a
        SECOND door to the same knob and must refuse at the same standard —
        and it matters more here, because a driver that fails every wake does
        NOT exit, so `reap` never fires and the row asserts `running` forever
        (measured, agora-and-wui#280).
        """
        reasoning = (reasoning or "").strip()
        if not reasoning:
            return ""
        caps = (self.machine_harnesses(machine).get("capabilities")
                or {}).get(harness)
        if caps is None:
            # This runner predates the capabilities announce, or has not
            # started. Silence is not permission, but neither is it a reason
            # to refuse a value that may well be right — the operator is told
            # the check could not run rather than given a false green.
            return reasoning
        vocab = list(caps.get("reasoning") or [])
        if not vocab:
            raise HubError(
                400, f"'{harness}' takes no reasoning setting at all on "
                     f"'{machine}' — the runner announced an empty vocabulary, "
                     "which is a fact about the harness rather than a missing "
                     "list. Drop `reasoning` for this harness.")
        if reasoning not in vocab:
            raise HubError(
                400, f"'{harness}' on '{machine}' cannot express reasoning "
                     f"'{reasoning}' — it announced {'|'.join(vocab)}. A level "
                     "it cannot express would launch the seat and then fail "
                     "every wake, with the spawn row still reading `running`.")
        return reasoning

    def list_spawn_requests(self, *, machine: str = "",
                            active_only: bool = False,
                            limit: int = 100) -> list[SpawnRequest]:
        return self.db.list_spawn_requests(
            machine=self._validate_machine(machine) if machine else "",
            active_only=active_only, limit=max(1, min(int(limit), 500)))

    def get_spawn_request(self, spawn_id: str) -> SpawnRequest:
        row = self.db.get_spawn_request(spawn_id)
        if row is None:
            raise HubError(404, f"no spawn request '{spawn_id}'")
        return row

    def claim_spawn_request(self, agent: AgentInfo, machine: str = "local"
                            ) -> dict[str, Any] | None:
        """The runner's pull. Returns the claimed row plus a single-use join
        token whose PLAINTEXT is served exactly once, or None when there is
        nothing to do.

        The token is minted here rather than by the runner because minting is
        an identity act: the hub pins it to the row's `seat_id`, gives it
        minutes of life, and carries the operator's mission on it. The runner
        never gains the power to mint a token for any other id.
        """
        machine = self._require_runner(agent, machine)
        # Before the early return: an idle poll is the one that proves life.
        self.touch_machine_runner(machine)
        row = self.db.claim_spawn_request(machine=machine, runner_id=agent.id)
        if row is None:
            return None
        # The id may have been taken between the request and the claim (a
        # human ran `agora setup` with the same name). Reject the row with the
        # reason rather than handing the runner a token that cannot redeem.
        try:
            token = self.create_join_token(
                agent_id=row.seat_id, channels=row.channels,
                ttl_seconds=self.SPAWN_TOKEN_TTL, max_uses=1,
                created_by=agent.id, mission=row.mission)
        except HubError as e:
            self.db.set_spawn_state(row.id, SpawnState.rejected,
                                    detail=str(e.detail), by=agent.id)
            raise
        row = self.db.attach_spawn_token(row.id, token["token_id"])
        self._log_event("spawn-claimed", spawn=row.id, seat=row.seat_id,
                        machine=machine, runner=agent.id)
        return {"request": row.model_dump(mode="json"),
                # WHO asked, as a ROLE rather than an id the runner would have
                # to interpret. The runner's tty prompt exists to collect a
                # human's consent; when the requester IS the human operator,
                # that consent has already been given and asking again at the
                # shell stalls the spawn behind a second yes from the same
                # person (laurent, commons#290). The runner must not derive
                # this from the id — role is the hub's to state.
                "requested_by_operator": row.requested_by in self.operator_ids(),
                "join_token": token["token"]}

    def set_spawn_state(self, agent: AgentInfo, spawn_id: str, state: str,
                        detail: str = "") -> SpawnRequest:
        """The runner reports where it has got to. The transition table lives
        in the db so this and any other writer are held to the same rules."""
        row = self.get_spawn_request(spawn_id)
        self._require_runner(agent, row.machine)
        try:
            target = SpawnState(state)
        except ValueError as e:
            raise HubError(400, f"unknown spawn state '{state}' — one of "
                                f"{sorted(s.value for s in SpawnState)}") from e
        detail = sanitize_text(detail, MAX_SPAWN_DETAIL_CHARS, field="detail")
        try:
            changed = self.db.set_spawn_state(spawn_id, target, detail=detail,
                                              by=agent.id)
        except SpawnTransitionRefused as e:
            raise HubError(e.status_code, e.detail) from e
        self._log_event("spawn-state", spawn=spawn_id, seat=row.seat_id,
                        state=target.value, runner=agent.id)
        return changed

    def request_spawn_stop(self, agent: AgentInfo, spawn_id: str) -> SpawnRequest:
        """Record that the operator wants this seat stopped. The hub does not
        move the state: only the runner can end a process."""
        self.get_spawn_request(spawn_id)
        try:
            return self.db.request_spawn_stop(spawn_id)
        except SpawnTransitionRefused as e:
            raise HubError(e.status_code, e.detail) from e

    # -- channels ---------------------------------------------------------------

    def create_channel(self, agent: AgentInfo, name: str, private: bool = True,
                       *, charter: str | None = None) -> dict[str, Any]:
        """Create a channel; the caller becomes its owner. `charter` is an
        internal seam (create_group stamps the group lifecycle text instead
        of the generic seed) — not a wire parameter: the HTTP route never
        passes it, so a member cannot inject charter prose at creation."""
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
        self._seed_charter(agent, name, charter)
        self._log_event("channel-created", channel=name, owner=agent.id,
                        private=private)
        # No channel — `commons` included — is born with a `traffic_policy`.
        # A board is an operator's deliberate opt-in via channel:meta, not a
        # name the hub recognises and restricts.
        return channel.model_dump()

    def _seed_charter(self, agent: AgentInfo, channel: str,
                      text: str | None = None) -> None:
        """Every room is born with a charter (0146). Deliberately the SEED
        text, not the placeholder template: an unedited seed is what most
        rooms will actually serve, so every line of it is true before anyone
        touches it — it names the owner, states the inheritance, and points
        at the hub charter for the role model. Best-effort: a charter write
        must never be the reason a channel fails to exist."""
        if text is None:
            text = CHANNEL_CHARTER_SEED.format(
                channel=channel, owner=agent.id,
                purpose="Not declared yet — the owner sets it here and in "
                        "channel:meta.purpose.")
        try:
            self.fs_write(agent, channel, CHARTER_PATH, text,
                          description="channel charter: purpose and room rules")
        except HubError:
            pass

    def create_group(self, agent: AgentInfo, name: str, members: list[str],
                     *, purpose: str = "", opening_post: str = "",
                     private: bool = True) -> dict[str, Any]:
        """Spin up a focused room in ONE hub call (agora-0119, operator go
        2026-07-21): create the channel, set its purpose, invite each named
        member (token DM'd — joining stays their auditable act), and post
        the topic as the room's opening OPEN obligation. Every client used
        to re-script these 4 calls and they DRIFTED — chat sent the invite
        DM `fyi`, continuum forced `open`, so invitees were treated
        differently. The hub now owns the recipe with ONE uniform shape:
        the invite DM is `fyi` (a nudge; joining is the act, not a reply
        owed) and the opening post is the in-room `open` obligation once
        they join. Not DB-atomic (each step commits), but ONE
        implementation and one status — partial failures are reported per
        member, never silently dropped."""
        self._require_unpaused(agent)
        # Auto-charter (0135): the room arrives with its lifecycle contract
        # already written — receipt-to-commons, close-when-done — so routing
        # discipline costs the creator zero extra calls. It rides the SAME
        # creation-time seam every channel now uses (0146), so a group room
        # is born at charter v1 like everything else rather than immediately
        # superseding a generic seed.
        channel = self.create_channel(  # validates slug/collision
            agent, name, private,
            charter=GROUP_CHARTER_TEMPLATE.format(
                channel=name, owner=agent.id,
                purpose=sanitize_text(purpose, MAX_ABOUT_CHARS, field="purpose") or "<set by owner>"))
        # A DELEGATE MAY NOT WORK WHERE ITS OPERATOR CANNOT LOOK
        # (2026-08-06). `rt2-lead` split the real work into a private room
        # and invited only the six workers. For three hours the operator's
        # board showed a dead channel while a milestone shipped next door;
        # the room's own creation notice was posted INSIDE the room nobody
        # could read. Delegation is verifiable state (ADR-0004), and a
        # delegate's workroom is part of that state — so every operator is
        # invited, exactly as any other member, joining still their own
        # auditable act. Membership is not readership: they are invited,
        # not force-joined.
        if not agent.operator and any(
                self.is_delegate(agent.id, p) for p in DELEGATE_POWERS):
            members = list(members) + [op for op in sorted(self.operator_ids())
                                       if op not in members]
        if purpose:
            self.store_set(agent, name, CHANNEL_META_KEY,
                           {"purpose": sanitize_text(purpose, MAX_ABOUT_CHARS, field="purpose")})
        invited: list[str] = []
        failed: list[dict[str, str]] = []
        for peer in members:
            try:
                token = self.create_invite(agent, name, peer)
                # The token is INLINE in the body, exactly as the CLI path
                # writes it. Agents read bodies; `data` is not rendered on any
                # reading surface, so "invite_token below" pointed at nothing
                # and blocked five driven seats at once (0140 field test 2).
                # It stays in `data` too, for machine consumers.
                self.post_dm(agent, peer, PostMessage(
                    body=(f"You are invited to '{name}' — focused room"
                          + (f": {purpose}" if purpose else "")
                          + f". Join with join_channel(channel={name!r}, "
                            f"invite_token={token!r}), read the opening post, "
                            "and work the topic THERE (not in commons)."),
                    title=f"invite to {name}",
                    status=Status.fyi,
                    data={"kind": "channel_invite",
                          "invite_token": token, "channel": name}))
                invited.append(peer)
            except HubError as e:
                failed.append({"agent": peer, "error": e.detail})
        opening = None
        if opening_post:
            opening = self.post_message(agent, name, PostMessage(
                body=opening_post, title=sanitize_title(elide(opening_post, 80)),
                status=Status.open))
        return {"channel": name, "created": channel, "invited": invited,
                "failed": failed,
                "opening_seq": opening.seq if opening else None}

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
        # A RE-ADD IS RECORDED, because the departure was. Leaving posts
        # `<seat> left`; re-opening the DM by writing into it used to post
        # nothing, so the transcript showed a departure and no return. The
        # operator read their own record and asked why it showed them leaving
        # and rejoining — the leave was there, the return was not, and the
        # gap between them looked like the hub losing track of a member.
        # Symmetry is the whole fix: both transitions, or neither.
        rejoining = [seat for seat in (agent.id, peer)
                     if not created and not self.db.is_member(name, seat)]
        self.db.add_member(name, agent.id, role="member")
        self.db.add_member(name, peer, role="member")
        if created:
            self._post_system(name, f"direct channel between {agent.id} and {peer}")
        for seat in rejoining:
            self._post_system(
                name, f"{seat} rejoined — writing to a direct channel re-opens "
                      "it for both peers")
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
        # Widening the trust boundary of a private room is an OWNER's act —
        # the confused-deputy risk of an LLM member being talked into
        # inviting an attacker (red-team finding) is why it is not every
        # member's. It is not the owner's ALONE, though: an operator and a
        # delegate running the room need it too, and an owner-only rule left
        # a delegate able to declare a room's phases but not invite the seat
        # required to work them.
        if self.db.channel_archived(channel):
            raise HubError(409, f"channel '{channel}' is archived (ended) — "
                                "no new invites")
        self._require_channel_admin(agent, channel, "creating an invite")
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
            self._log_event("channel-joined", channel=channel, seat=agent.id)
        # One-call onboarding: metadata + members with abouts, so the joiner
        # knows the channel's norms and who to ask what before posting.
        return {"joined": True, **self.channel_info(agent, channel)}

    def leave_channel(self, agent: AgentInfo, channel: str) -> None:
        self.require_membership(channel, agent.id)
        # A DM HAS NO EXIT, SO THE HUB OWNS THE REFUSAL (operator order,
        # agora-and-wui#18). Membership in a two-party room is structural —
        # it IS the channel name — and every other door already says so:
        # `join_channel` refuses ("direct channels cannot be joined"),
        # `open_dm` re-asserts membership on both sides, and the reserved
        # `channel/` prefix is unwritable there because a DM is ownerless.
        # Only this door disagreed, and leaving through it is destructive to
        # the OTHER peer: their room keeps accepting posts nobody is
        # delivered.
        #
        # The order it implements is the general one, not this instance: a
        # client must not be able to drop a seat — least of all the human —
        # out of a DM as a side effect of another action. Three clients
        # independently reported having no leave control (WUI unreachable by
        # luck, TUI absent entirely), which is exactly why the guarantee does
        # not belong in any of them: `leave` is self-only, so the request
        # that removed the operator carried the operator's own key, and no
        # amount of client discipline constrains the next caller. Enforced
        # here, every client inherits it.
        if channel.startswith(DM_PREFIX):
            raise HubError(403, f"'{channel}' is a direct channel — its two "
                                "members are the room, so it cannot be left "
                                "(there is no rejoin door, and the peer would "
                                "keep posting to nobody). Stop posting, or ask "
                                "an operator to retire the seat.")
        # Membership is shared state and the departure broadcasts: frozen
        # during a pause like every other shared-world mutation (review MED-2).
        self._require_unpaused(agent, channel)
        # Withdraw the leaver's own reputation votes (0094 F2): a rater must
        # not be able to drive-by downvote then leave, stranding the vote
        # where neither they (membership gate) nor the target can remove it.
        # Votes ABOUT the leaver stay — colleagues' judgment outlives a
        # target's exit, exactly as with retirement. Message ratings clear
        # under the same rule (agora-0122): both tables or the hole reopens
        # through whichever door forgot.
        self.db.reputation_clear_rater(channel, agent.id)
        self.db.rating_clear_rater(channel, agent.id)
        self.db.remove_member(channel, agent.id)
        self._post_system(channel, f"{agent.id} left")
        self._log_event("channel-left", channel=channel, seat=agent.id)

    # -- messages -----------------------------------------------------------------

    #: Per-ask addressing cap (0077): more than 3 named answerers on ONE ask
    #: is diffusion of responsibility — use message-level `to` for broadcast.
    MAX_ASK_TO = 8   # explicit per-ask `to`; an INHERITED list is uncapped (ADR-0006)

    def _validate_asks(self, raw: Any, status: Status, *, sender: str = "",
                       channel: str = "",
                       message_to: list[str] | None = None) -> list[dict[str, Any]]:
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
            if "@" in aid and not a.get("to"):
                # `ID@SEAT,SEAT` is the terse form of "an ask names its seats"
                # (ADR-0006). It is parsed HERE, once, so no client — the CLI,
                # an MCP call, a hand-written payload — can ever store an id
                # nobody can answer (a 0.17.8 CLI stored '1@companion' raw and
                # the reply answers=['1'] was refused as unknown).
                aid, _, seats = aid.partition("@")
                aid = aid.strip()
                a = {**a, "to": [x.strip() for x in seats.split(",") if x.strip()]}
            if not aid or aid in seen:
                raise HubError(400, "ask ids must be unique and non-empty")
            seen.add(aid)
            entry = {"id": aid, "text": sanitize_text(str(a.get("text", "")), MAX_ASK_CHARS, field="ask text")}
            if a.get("assignee"):
                assignee = sanitize_text(str(a["assignee"]), MAX_ASSIGNEE_CHARS, field="ask assignee")
                # An assignee is an addressee (storm review, 2026-07-28): it
                # creates owed debt, so it gets the same gates as ask `to` —
                # a ghost name must not satisfy addressing rules while the
                # message obliges nobody real, and self-assignment is as
                # meaningless as asking yourself.
                if sender and assignee == sender:
                    raise HubError(400, f"ask '{aid}': you cannot assign an "
                                        "ask to yourself")
                if channel:
                    if members is None:
                        members = {m.agent_id for m in self.db.list_members(channel)}
                    if assignee not in members:
                        raise HubError(400, f"ask '{aid}' assigns a non-member: "
                                            f"'{assignee}' — describe_channel "
                                            "lists who is here; drop the "
                                            "assignee or invite them first")
                entry["assignee"] = assignee
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
            # Always present, always a list — never omitted when empty. A
            # client that must first ask whether the key exists will get it
            # wrong exactly once, in the direction of reading every addressee
            # as deliberate, which is the confusion the field exists to end.
            # INHERITANCE, materialised (ADR-0006, 2026-09-09). An ask with no
            # `to` on a message that HAS a `to` is addressed to exactly those
            # seats — written once here so every consumer (owed, discharge,
            # the driver's narrowing, every client) reads one explicit list.
            # Not capped: the author already addressed the message to them.
            # A room-wide message's unaddressed ask keeps `to: []` and, since
            # this change, obliges NOBODY: 22 of 22 owed-only wakes in the
            # lab runs were one such ask, due for every seat, undischargeable
            # by any of them (`asks_naming_you=[]` beside `pending_asks=[1]`).
            if not entry.get("to") and not entry.get("assignee") and message_to:
                inherited = [str(x) for x in message_to if str(x) != sender]
                if inherited:
                    entry["to"] = inherited
                    entry["to_inherited"] = True
            derived = [s for s in (a.get("to_from_text") or [])
                       if s in entry.get("to", [])]
            entry["to_from_text"] = derived
            # Ask.phase (cycle 2): carried verbatim, validated as a phase
            # slug. It gates PENDING in discharge_state — an ask scoped to
            # a phase mints no debt until that phase is declared open.
            if a.get("phase"):
                phase = sanitize_text(str(a["phase"]), 64, field="ask phase").strip()
                if phase and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", phase):
                    raise HubError(400, f"ask '{aid}' phase '{phase}' is not a "
                                        "phase slug (letters, digits, - and _)")
                if phase:
                    entry["phase"] = phase
            norm.append(entry)
        return norm

    def _validate_discharge(self, answers: Any, declines: Any, status: Status,
                            reply_to: str | None,
                            sender: str) -> tuple[list[str], list[str]]:
        """Validate the ask ids a reply DISCHARGES, and which of them it refuses.

        `answers` and `declines` are the same act with different substance, so
        they are validated as ONE set and stored as one: `answers` keeps its
        documented meaning — the ask ids this reply discharges — and carries
        the union, while `declines` records the refused subset (0153).

        A refusal was previously indistinguishable from an answer on the wire:
        the only carrier was English in the body, which no mechanical surface
        reads, so the digest credited a refuser under `decided` and the asker
        was pointed at a non-answer to consume. Folding declines INTO answers
        (rather than making discharge read a second field) is what keeps this
        additive: discharge, unpin, `/owed` and every already-persisted row
        behave exactly as before, and the surfaces that care subtract.
        """
        declined = self._id_list(declines, "declines") if declines is not None else []
        answered = self._id_list(answers, "answers") if answers is not None else []
        # Order-preserving union: what the sender listed as answers first,
        # then the refusals they did not also list.
        union = answered + [d for d in declined if d not in answered]
        # A field is named in the refusal only when it is the one at fault:
        # a seat that typed `declines=["9"]` must not be taught about
        # `answers`.
        label = "declines" if declined and not answered else "answers"
        validated = self._validate_answers(union, status, reply_to, sender,
                                           label=label, declined=set(declined))
        return validated, [d for d in validated if d in set(declined)]

    @staticmethod
    def _id_list(raw: Any, field: str) -> list[str]:
        """A discharge field is a non-empty list of ask ids, deduped in the
        order the sender wrote them."""
        if not isinstance(raw, list):
            raise HubError(400, f"{field} must be a list")
        if not raw:
            raise HubError(400, f"{field}=[] is empty — drop the field, or name "
                                "the ask ids you are discharging")
        out: list[str] = []
        for x in raw:
            if str(x) not in out:
                out.append(str(x))
        return out

    @staticmethod
    def _name_fields(ids: list[str], declined: set[str], label: str) -> str:
        """Name the field the sender actually TYPED each bad id in. A teaching
        refusal that points at `answers` when the id came from `declines`
        teaches the wrong gesture — which is the failure this validator
        exists to prevent."""
        from_declines = [i for i in ids if i in declined]
        from_answers = [i for i in ids if i not in declined]
        parts = []
        if from_answers:
            parts.append(f"{label}: {elide(str(from_answers), 200)}")
        if from_declines:
            parts.append(f"declines: {elide(str(from_declines), 200)}")
        return ", ".join(parts)

    def _validate_answers(self, raw: Any, status: Status, reply_to: str | None,
                          sender: str, label: str = "answers",
                          declined: set[str] | None = None) -> list[str]:
        if status not in (Status.reply, Status.resolved) or not reply_to:
            # `resolved` IS the natural shape of a completion report, and it
            # was the one shape that could not discharge the ask it
            # completed (0152): the delegate's acceptance report landed as
            # `resolved`, its ask stayed open, and it had to post a SECOND
            # message as `reply` purely to close it — "my error in shape,
            # not in substance", when it was this rule. The asymmetry that
            # marks it an oversight rather than a stance: `consumes`, the
            # sibling discharge field, has always been accepted on a
            # `resolved`. Discharge itself never looked at status — it
            # reads `data.answers` off any non-sender reply — so this
            # refusal was the only thing between the two.
            raise HubError(400, f"{label}[] are only allowed on a reply that "
                                "names its reply_to (status=reply, or "
                                "status=resolved when the completion report "
                                "is itself the answer)")
        if not isinstance(raw, list):
            raise HubError(400, f"{label} must be a list")
        if not raw:
            raise HubError(400, f"{label}=[] is empty — drop the field, or name "
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
                                    f"{label}=[] discharges nothing here; reply "
                                    "to the message that carries the asks, or "
                                    f"drop {label}")
        parent_ids = {str(a["id"]) for a in asks_of(parent)} if parent else set()
        if parent_ids:
            unknown = [a for a in answered if a not in parent_ids]
            if unknown:
                raise HubError(400, "unknown ask ids — the message you "
                                    "replied to carries no such ask ("
                                    + self._name_fields(unknown, declined or set(),
                                                        label) + ")")
            by_id = {str(a["id"]): a for a in asks_of(parent)}
            foreign = []
            for aid in answered:
                ask = by_id.get(aid)
                if ask is None:
                    continue
                # Per-ask `to` is the only HARD addressing. `assignee` is
                # advisory everywhere else — discharge and /owed both treat
                # an assignee-only ask as answerable by anyone — so gating
                # the answer on it here silently voided legitimate
                # third-party answers (the poster got a 400, the ask stayed
                # pending, and the assignee stayed pinned for work already
                # done).
                named = {str(x) for x in (ask.get("to") or [])}
                if named and sender not in named:
                    foreign.append(aid)
            if foreign:
                raise HubError(
                    400,
                    "you may not discharge ask ids not addressed to you ("
                    + self._name_fields(foreign, declined or set(), label)
                    + ") — let the named seat answer or decline it, or reply "
                      "without those ids if you are only adding context",
                )
        return answered

    def _resolved_settles_nothing_refusal(self, parent: Message, sender: str,
                                          replies: list[Message]) -> str:
        """The teaching half of the no-op `resolved` refusal: who may close
        this row, and the ONE gesture that would work for THIS sender.

        Three shapes, and they are mutually exclusive by construction:

        1. the sender has a pending ask they may discharge → `answers[]` (the
           gate never sees a message that already carries one);
        2. the sender is a seat the asker NAMED on an ask-less request → a
           cited `resolved` is their door (`d6b63d4`), so the only thing
           missing is `data.evidence`;
        3. neither → a bystander, whose correct move is an ordinary reply.

        Case 2 is why this is computed rather than written once: an
        evidence recipe handed to case 1 or case 3 would be refused a second
        time, and a reader who follows a refusal into another refusal stops
        believing the next one."""
        row = f"{parent.channel}#{parent.seq}"
        who = (f"only {parent.sender} (who asked), an operator, or a delegate "
               "holding ruling/operational scoped to this channel may close "
               f"{row}")
        mine = [str(a["id"]) for a in asks_of(parent)
                if str(a["id"]) in set(self._discharge(parent, replies).pending)
                and (not (a.get("to"))
                     or sender in {str(x) for x in (a.get("to") or [])})]
        if mine:
            ids = ", ".join(f'"{a}"' for a in mine)
            return (f"your `resolved` on {row} would settle nothing as it "
                    f"stands — {who}. But {parent.sender} left "
                    f"{'an ask' if len(mine) == 1 else 'asks'} open for you: "
                    f"add answers=[{ids}] (or declines=[{ids}] to refuse "
                    "them on the record) and the same `resolved` is accepted "
                    "and discharges them.")
        if sender in (set(parent.to) | ask_addressees(parent)):
            return (f"your `resolved` on {row} would settle nothing as it "
                    f"stands — {who}. But {parent.sender} NAMED you, so a "
                    "`resolved` from you CAN settle it — as a completion "
                    "report that points at what it delivered. Add "
                    "data.evidence=[{kind, ref}]: a store row (kind 'store', "
                    "e.g. your decision:<slug>), a channel file ('fs', "
                    "'path@version'), an uploaded blob ('blob', sha256), or "
                    "an outside artifact ('external', with "
                    "sha256+size_bytes). If you are ANSWERING rather than "
                    "reporting a delivery, post an ordinary reply instead — "
                    "that is a complete and correct turn.")
        return (f"your `resolved` on {row} would settle nothing — {who}, and "
                "you are none of those. Post an ordinary reply instead: it is "
                f"just as visible, and it obliges {parent.sender} to read it "
                "and close their own question. If they have gone quiet, ask "
                "an operator. What does not work is this — a bystander's "
                "`resolved` closes nothing and discharges nothing, so the row "
                f"keeps escalating against whoever {parent.sender} is waiting "
                "on.")

    #: Batched consumption (0140/3). The at-test fleet paid an O(n²) ceremony
    #: tax: the obligation model demands an on-the-record consumption per
    #: thread, so with 8 seats one seat posted TEN identical "adopted and
    #: consumed" messages inside one second, and 26% of all 253 messages
    #: carried zero information. `consumes=[...]` settles N debts with ONE
    #: message, through the exact discharge path a reply uses (a read receipt
    #: on the answer) — no new debt semantics, nothing to keep in sync.
    MAX_CONSUMES = 32

    def _resolve_consume_ref(self, ref: str, channel: str) -> Message | None:
        """A consumption ref: a message id, `channel#seq`, `#seq`, or a bare
        seq in THIS channel. Debts span rooms (you can owe consumption in a
        channel you are not posting in today), so the qualified form is
        accepted everywhere."""
        ref = ref.strip()
        if not ref:
            return None
        if "#" in ref:
            room, _, seq = ref.rpartition("#")
            room = room.strip() or channel
            return (self.db.get_message_by_seq(room, int(seq))
                    if seq.isdigit() else None)
        if ref.isdigit():
            return self.db.get_message_by_seq(channel, int(ref))
        return self.db.get_message(ref)

    def _replies_directly_to(self, m: Message, agent_id: str) -> bool:
        """Is `m` a DIRECT reply to a message `agent_id` wrote? (0157.)

        Direct, not a walk to the thread root — and the tightness is the
        safety. The hub already enforces that an answer replies to the
        message carrying the ask (a deeper `answers` is refused: "your reply
        can never discharge your own asks"), so every real consumption debt
        is depth one and a walk would buy nothing. What it WOULD buy is a
        false receipt: in `root(mine) -> r1(theirs) -> r2(theirs)`, `r2` is
        no debt at all, and an ancestor walk would let a read of it be
        recorded as a debt settled. A receipt for a debt that never existed
        is the same lie as a debt that looks settled and is not — the two
        failures this whole area keeps producing, one in each direction."""
        if not m.reply_to:
            return False
        parent = self.db.get_message(m.reply_to)
        return (parent is not None and parent.channel == m.channel
                and parent.sender == agent_id)

    def _validate_consumes(self, agent: AgentInfo, channel: str,
                           raw: Any) -> tuple[list[str], list[str]]:
        """Resolve `consumes` to (answer message ids to receipt, stored refs).

        Every ref must name a debt the SENDER actually owes — an unconsumed
        answer to their own open thread, or the thread ROOT (which settles
        every unconsumed answer in it at once, since 'I read the thread' is
        the honest unit of what a seat actually did). Unknown or un-owed
        refs are refused LOUDLY, naming each one: silently accepting a ref
        that discharges nothing would recreate the exact failure the field
        test found — a seat believing it settled a debt that stays open."""
        if not isinstance(raw, list) or not raw:
            raise HubError(400, "consumes must be a non-empty list of message "
                                "ids or channel#seq refs — drop the field if "
                                "this message settles nothing")
        if len(raw) > self.MAX_CONSUMES:
            raise HubError(400, f"consumes lists {len(raw)} refs; the cap is "
                                f"{self.MAX_CONSUMES} per message (a batch is "
                                "a receipt, not a migration — split it)")
        owed = self.owed(agent)
        by_answer: dict[str, str] = {r.answer_id: r.answer_id
                                     for r in owed.to_consume}
        by_root: dict[str, list[str]] = {}
        for row in owed.to_consume:
            by_root.setdefault(row.id, []).append(row.answer_id)
        targets: list[str] = []
        stored: list[str] = []
        unknown: list[str] = []
        for item in raw:
            ref = str(item)
            found = self._resolve_consume_ref(ref, channel)
            hits = ([found.id] if found is not None and found.id in by_answer
                    else by_root.get(found.id, []) if found is not None
                    else [])
            if not hits:
                # YOUR OWN THREAD WITH NOTHING OUTSTANDING IS A NO-OP, NOT AN
                # ERROR (0153). A thread whose every reply DECLINED owes you
                # no consumption — a refusal is terminal — so citing its root
                # resolved to zero rows and hit the loud refusal below, which
                # would teach "you owe no consumption for your own thread"
                # for the one gesture the docs recommend ("I read the
                # thread"). No existence oracle: the ref resolves to a
                # message this sender wrote, and only their OWN open thread
                # qualifies — someone else's debt stays un-settleable, and so
                # does a reply of theirs that never carried consumption.
                if (found is not None and found.sender == agent.id
                        and found.status in (Status.open, Status.blocked)):
                    if found.id not in stored:
                        stored.append(found.id)
                    continue
                # AN ANSWER YOU ALREADY DISCHARGED BY READING IT IS ALSO A
                # NO-OP, NOT AN ERROR — the same ruling as the clause above,
                # for the case three seats hit in one afternoon.
                #
                # THE TRAP, IN THE ORDER THE HUB ITSELF PRESCRIBES IT:
                # `check_inbox` prints "CONSUME <ref> — read_message id=…
                # and use it". The seat reads it, which IS a discharge
                # (to_consume clears on the read receipt). Then the seat does
                # the honest, documented thing and cites the ref on its next
                # message — and got "you owe no consumption for", which reads
                # as "you never owed this" when the truth is "you already
                # settled it, a moment ago, by following the instruction".
                #
                # agora-wui#114 named the bias exactly: "the surface that
                # quietly succeeds is the one that discharges without a
                # record, and the surface that refuses is the one that would
                # have created the record. That is the wrong way round for an
                # instrument the operator reads." Refusing here taught three
                # seats to stop using `consumes` — so the hub kept the silent
                # discharge and lost the receipt.
                #
                # Accepted narrowly: a reply, in a thread whose ROOT this
                # sender wrote, that this sender has actually READ. No
                # existence oracle (someone else's thread stays
                # un-settleable), and no free pass for a message that was
                # never a debt — the read receipt is the evidence the
                # discharge really happened.
                if (found is not None and found.sender != agent.id
                        and self._replies_directly_to(found, agent.id)
                        and self.db.has_read(found.id, agent.id)):
                    if found.id not in stored:
                        stored.append(found.id)
                    continue
                unknown.append(elide(ref, 80))
                continue
            # Deduped: the same debt cited twice (as its seq and as its id,
            # or twice over) is ONE settlement, and the stored record should
            # read as the set of debts settled, not as the sender's typing.
            if found.id not in stored:
                stored.append(found.id)
            targets.extend(h for h in hits if h not in targets)
        if unknown:
            # ONE refusal for both "no such message" and "not yours to
            # settle": a split message would turn consumes into an existence
            # oracle for channels the sender cannot read (the v0.3 IDOR
            # class, same reasoning as reply_to's ordering).
            #
            # NAME THE DEBT CLASS (agora-tui, thread-shape-and-panels#103).
            # `to_answer` and `to_consume` are two collections with different
            # reasons, and this refusal never said so: a seat that reached
            # for `consumes` on rows it OWES ANSWERS for read "you owe no
            # consumption for" and had to infer the split from an error
            # string. They inferred it correctly and then someone had to read
            # `_validate_consumes` to confirm the guess — a finding resting on
            # a reading of a sentence rather than on the code.
            #
            # So when every refused ref is in fact a row on the sender's
            # to_answer list, say which collection they are looking at and
            # what settles it. No new oracle: this reads only rows already on
            # THIS sender's own /owed, which they can list themselves.
            answer_rows = {f"{r.channel}#{r.seq}" for r in owed.to_answer}
            refused = {u.strip() for u in unknown}
            wrong_class = sorted(refused & answer_rows)
            if wrong_class and len(wrong_class) == len(refused):
                raise HubError(
                    400, f"consumes cannot settle {wrong_class}: those are "
                         "to_answer rows and `consumes` settles to_consume — "
                         "two different debts. A to_answer row is cleared by "
                         "replying in its own thread (with answers=[...] if "
                         "the asks name you); to_consume is an answer to YOUR "
                         "ask that you have read and not yet used. /owed "
                         "lists both, separately. Nothing was posted")
            raise HubError(400, f"consumes names {len(unknown)} ref(s) you owe "
                                f"no consumption for: {unknown} — /owed lists "
                                "your to_consume rows (cite the answer as "
                                "channel#seq or the thread root's id); nothing "
                                "was posted")
        return targets, stored

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
        - `declines` name the discharged asks the reply REFUSES rather than
          answers (0153). They are folded into `answers` — a decline
          discharges exactly like an answer — and kept as the refused subset,
          so the digest, `/owed` and the asker's envelope can tell the two
          apart. The body is the why; it is never required.
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
        if payload.declines is not None:
            data["declines"] = [str(x) for x in payload.declines]
        if payload.consumes is not None:
            data["consumes"] = [str(x) for x in payload.consumes]
        if payload.attachments is not None:
            data["attachments"] = [a.model_dump(exclude_none=True)
                                   for a in payload.attachments]
        if payload.notice is not None:
            data["notice"] = payload.notice.model_dump()
        if "asks" in data:
            data["asks"] = self._validate_asks(data["asks"], payload.status,
                                               sender=sender, channel=channel, message_to=list(payload.to or []))
        if "answers" in data or "declines" in data:
            answers, declines = self._validate_discharge(
                data.get("answers"), data.get("declines"), payload.status,
                payload.reply_to, sender)
            data["answers"] = answers
            # Only a real refusal leaves a `declines` key: an empty one would
            # make every plain answer carry a field saying nothing.
            if declines:
                data["declines"] = declines
            else:
                data.pop("declines", None)
        if "attachments" in data:
            # Refs are validated against THIS channel's blob store and
            # normalized from server truth (0091) — whether they arrived via
            # the typed param or a hand-built data payload.
            data["attachments"] = self._validate_attachments(data["attachments"],
                                                             channel)
        if "notice" in data:
            raw = data["notice"]
            if not isinstance(raw, dict) or set(raw) != {"kind", "key"}:
                raise HubError(400, "notice must be exactly {kind, key}")
            kind, key = str(raw.get("kind", "")), str(raw.get("key", ""))
            if kind not in NOTICE_KINDS:
                raise HubError(400, "notice.kind must be one of: "
                                    + ", ".join(NOTICE_KINDS))
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}", key):
                raise HubError(400, "notice.key must be a stable 1-160 character "
                                    "event id using letters, digits, . _ : / or -")
            data["notice"] = {"kind": kind, "key": key}
        if "item_ref" in data:
            # Work-id citation (0093): the STRUCTURED stitch between a hub
            # message and a backlog item. Validated when present so the
            # /work index never accumulates rotten refs; prose mentions
            # stay free-form (they index as 'mention', never refused).
            ref = str(data["item_ref"])
            if parse_work_id(ref) is None:
                raise HubError(400, f"item_ref '{elide(ref, 64)}' is "
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
            # AN OPERATOR'S REQUEST IS NOT CLOSABLE BY A BYSTANDER
            # (2026-08-06). `settled_by` was an unconditional master key:
            # ANY member could close ANY obligation — an operator commission
            # included — by pointing a `resolved` reply at any other message
            # in the room, including one they had posted a second earlier.
            # That bypassed the 2026-08-01 operator-broadcast ruling, the
            # 2026-08-04 addressed-commission tightening, and the evidence
            # gate, in a single post.
            #
            # The reporting delegate KEEPS this door, because something must
            # be able to settle a commission whose operator has gone quiet —
            # but it pays the same price as any other completion report: it
            # must cite what it delivered.
            # ...AND THE SAME IS NOW TRUE OF A PEER'S THREAD (operator ruling,
            # 2026-08-22). The 2026-08-06 fix closed this door only in front
            # of operator-authored requests; everywhere else `settled_by`
            # stayed the master key it had always been, and a bystander could
            # retire any question by pointing at any message. A question
            # belongs to whoever asked it: they know whether it was answered
            # and a stranger does not.
            parent = self.db.get_message(payload.reply_to)
            if parent is not None:
                parent_is_operator = parent.sender in self.operator_ids()
                # `_prepare_structured` is called before the Message exists;
                # the authenticated poster reaches this helper as `sender`.
                if (sender in self.operator_ids() or sender == parent.sender
                        or sender in self.ruling_delegate_ids(channel)):
                    pass
                elif sender in self.reporting_delegate_ids() and parent_is_operator:
                    if not (data.get("evidence")
                            or payload.data and payload.data.get("evidence")):
                        raise HubError(400,
                                       "settling an operator's request needs "
                                       "`evidence` naming what you delivered "
                                       "— a pointer at another message is a "
                                       "bare claim wearing an audit trail")
                elif parent_is_operator:
                    raise HubError(403,
                                   "only the operator, or a reporting "
                                   "delegate citing evidence, may settle an "
                                   "operator's request. Answer it, or ask the "
                                   "delegate to report on it.")
                else:
                    raise HubError(
                        403,
                        f"only {parent.sender} (who asked) or an operator may "
                        "close this thread. Post your pointer as an ordinary "
                        "reply instead — it is just as visible, and it obliges "
                        f"{parent.sender} to read it and close their own "
                        "question. If they have gone quiet, ask an operator.")
            data["settled_by"] = pointer
        if "evidence" in data:
            data["evidence"] = self._validate_evidence(channel, data["evidence"],
                                                       agent_id=sender or None)
        if (payload.status == Status.resolved and payload.reply_to
                and "settled_by" not in data):
            # A reporting delegate's `resolved` on an operator's request IS
            # the completion report (2026-08-01 ruling) — and an uncited one
            # settles NOTHING: discharge requires `_cites_evidence`, so the
            # thread stays open and every pinned seat keeps waking on it.
            # Live (fund1, 2026-08-11): the delegate posted three evidence-less
            # "delivery complete" resolveds in a row, each spawned by the very
            # debt the previous one failed to clear. Nothing had told it the
            # contract. Refuse at post time, with the recipe — the one moment
            # the model is guaranteed to be listening.
            parent = self.db.get_message(payload.reply_to)
            if (parent is not None and parent.channel == channel
                    and parent.status in (Status.open, Status.blocked)
                    and parent.sender in self.operator_ids()
                    and sender not in self.operator_ids()):
                is_delegate = sender in self.reporting_delegate_ids()
                # THE ADDRESSEE PAYS THE SAME PRICE, SO IT MUST BE TOLD THE
                # SAME THING (2026-08-22). `_operator_settled` accepts a
                # cited `resolved` from a seat the operator NAMED as well as
                # from the reporting delegate — that addressee door is the
                # 2026-08-04 fix. This gate did not know about it: it fired
                # only for delegates, so a NAMED seat's evidence-less
                # `resolved` was accepted in silence and discharged nothing.
                # The hub refused the delegate with a recipe and let the
                # addressee walk into the identical void.
                #
                # Measured live (agora-wui, six rows): three `resolved`
                # replies from the addressed seat on an operator's ask-less
                # DM opens, none carrying evidence, all accepted, all still
                # ANSWER-owed and escalating hours later — while the seat ran
                # a controlled experiment to find out why. Every exit was
                # shut and none of them was named, which is the exact
                # complaint the 2026-08-04 comment above records.
                #
                # Scope: the CITATION requirement follows `_operator_settled`
                # and so covers both seats. The adversarial-review and plan
                # gates below stay delegate-only — they are about how a
                # delegate delivers a commission, not about a named seat
                # reporting its own slice.
                named = set(parent.to) | ask_addressees(parent)
                refs = data.get("evidence") or []
                citable = any(not isinstance(r, dict)
                              or r.get("verified") is not False
                              for r in refs)
                if is_delegate or sender in named:
                    if not citable and is_delegate:
                        raise HubError(400,
                                       "your `resolved` here is the completion "
                                       "report on an operator's request, and it "
                                       "must point at what it delivered — add "
                                       "data.evidence=[{kind, ref}] citing the "
                                       "delivery: a store row (kind 'store', e.g. "
                                       "your decision:<slug>), a channel file "
                                       "('fs', 'path@version'), an uploaded blob "
                                       "('blob', sha256), or an outside artifact "
                                       "('external', with sha256+size_bytes). "
                                       "Without a citation the request stays "
                                       "open and every addressed seat keeps "
                                       "waking on it.")
                    if not citable:
                        raise HubError(
                            400,
                            f"{parent.sender} named you on "
                            f"{parent.channel}#{parent.seq}, so a `resolved` "
                            "reply from you CAN settle it — but only as a "
                            "completion report that points at what it "
                            "delivered. Add data.evidence=[{kind, ref}]: a "
                            "store row (kind 'store', e.g. your "
                            "decision:<slug>), a channel file ('fs', "
                            "'path@version'), an uploaded blob ('blob', "
                            "sha256), or an outside artifact ('external', "
                            "with sha256+size_bytes). If you are ANSWERING "
                            "rather than reporting a delivery, post an "
                            "ordinary reply instead: that is a complete and "
                            "correct turn, and the request then stays open "
                            f"until {parent.sender} closes it. What does not "
                            "work is this — an uncited `resolved` is accepted "
                            "and discharges nothing, so the row keeps "
                            "escalating against you.")
                if is_delegate:
                    # Delegate-only: how a DELEGATE delivers a
                    # commission. A named seat reporting its own
                    # slice pays the citation above and no more.
                    # ADVERSARIAL REVIEW IS PART OF DELIVERY (operator ruling,
                    # 2026-08-12): "no agent should single-handedly try to solve
                    # the full task ... we HAVE to encourage ADVERSARIAL
                    # discussions to ensure quality delivery." A completion
                    # report every citation of which the delegate authored
                    # itself is an uncontested delivery — live (fund3), a
                    # working game shipped with zero peer review and two of
                    # three specialists' plans silently reduced to inputs. In a
                    # room that HAS peers, at least one cited artifact must be
                    # authored by someone other than the delegate — a review
                    # verdict, a peer's slice delivery, anything a reader can
                    # attribute to a second mind. Hub-authored rows and the
                    # operator's own seats don't count as peers; a delegate
                    # working alone keeps the old single-author rule (no
                    # deadlock).
                    peers = {m.agent_id for m in self.db.list_members(channel)
                             if m.agent_id not in (sender, "hub")
                             and m.agent_id not in self.operator_ids()}
                    if peers:
                        # THE PLAN IS NOT ITS OWN REVIEW (2026-08-13). Both
                        # checks in this block read the same `refs`, so ONE
                        # peer-authored `plan:` row satisfied both: the plan
                        # citation found its row, and this check found a
                        # non-sender author. A delivery with zero review passed
                        # the review gate. The reviewed artifact must be a
                        # second, non-plan citation.
                        authors = {r.get("updated_by") or r.get("created_by")
                                   for r in refs if isinstance(r, dict)
                                   and not (r.get("kind") == "store"
                                            and str(r.get("ref", "")).startswith("plan:"))
                                   and not (r.get("kind") == "message" and
                                            (r.get("sender") in (sender, "hub") or
                                             r.get("sender") in self.operator_ids()))}
                        authors.discard(None)
                        if not (authors - {sender}):
                            raise HubError(400,
                                           "an uncontested delivery is not a "
                                           "delivery: every evidence citation "
                                           "here is authored by you, and this "
                                           "room has peers. Have a contributor "
                                           "adversarially review a slice they "
                                           "did NOT write — cold-read the "
                                           "artifact against the operator's "
                                           "original words, hunt defects, put "
                                           "the verdict on the record (a "
                                           "message cited with kind='message', "
                                           "a `review:<slug>` store row or a "
                                           "reviewed file on the channel fs) — "
                                           "then cite that peer-authored "
                                           "artifact in data.evidence alongside "
                                           "your own. The agreed plan does "
                                           "not count as its own review.")
                    # PLAN ELABORATION IS A MANDATORY STEP (operator ruling,
                    # 2026-08-12): "no agent should ever start working
                    # before a plan has been created and agreed upon." The
                    # plan phase is where seats argue, own and defend their
                    # perspectives — especially seats rooted in existing
                    # packages whose constraints rule paths in and out. The
                    # hub cannot see a premature build in a filesystem
                    # workspace, but it CAN refuse to answer the user
                    # without the plan on the record: the completion report
                    # must cite the agreed plan row it delivered under.
                    #
                    # OUTSIDE `if peers:` (2026-08-13): this requirement is
                    # about the delegate's own conduct, not about having
                    # colleagues, and nested inside the peer gate it
                    # evaporated in exactly the two rooms where nobody else
                    # is watching — a peerless channel, and the operator DM,
                    # whose only other member IS the operator, so `peers` is
                    # empty there by construction. Reporting into the DM was
                    # a one-line bypass. Kept AFTER the review check so the
                    # refusal a delegate meets first in a peopled room is
                    # still the review one.
                    # ...AND IT PRESUPPOSES PEERS, exactly as the review check
                    # above does (2026-08-25, agora-and-wui#730).
                    #
                    # The review check is already skipped when `peers` is empty
                    # — "a delegate working alone keeps the old single-author
                    # rule (no deadlock)". The plan check sat OUTSIDE that
                    # guard, and the asymmetry has no defence: a plan row
                    # records "each seat's slice, the SEAMS between those
                    # slices, and how the contested points were settled in the
                    # room". In a two-party DM with the operator there is
                    # exactly one seat, no seam and no room. The requirement is
                    # not inconvenient there, it is INCOHERENT, and the only
                    # way to satisfy it is to invent an agreement between seats
                    # who never discussed anything.
                    #
                    # Measured by @delegate: three of laurent's own messages
                    # pinned open and escalating on HIS desk — dm#104 ("be
                    # more concise"), dm#116, and commons#604 — because the
                    # gate refused every honest close. Two are DMs and this
                    # frees them. `commons#604` is a peopled room where a plan
                    # genuinely exists, so it stays gated, correctly.
                    #
                    # AND IT ENDS A CONTRADICTION BETWEEN TWO SURFACES I OWN.
                    # `check_inbox` prints, as its own recipe: "Only an opinion
                    # to give? Record it as a decision:/finding: row IN THIS
                    # CHANNEL and cite it with kind=store". @delegate did
                    # exactly that and this line returned 400. One of the two
                    # had to be wrong; the advice was right and the gate was
                    # over-broad.
                    #
                    # WHAT THIS DOES NOT DO: it does not let a delegate close a
                    # commission without a plan. In any room with peers the
                    # requirement stands untouched, which is every room where a
                    # plan could have been agreed in the first place.
                    if peers and not any(
                            isinstance(r, dict) and r.get("kind") == "store"
                            and str(r.get("ref", "")).startswith("plan:")
                            for r in refs):
                        raise HubError(400,
                                       "no delivery without the plan it was "
                                       "built under: cite the agreed plan — "
                                       "a `plan:<slug>` store row recording "
                                       "each seat's slice, the SEAMS between "
                                       "those slices (each place one seat's "
                                       "output is another's input, with the "
                                       "observation that proves it landed), "
                                       "and how the contested points were "
                                       "settled in the room — in data.evidence "
                                       "alongside the artifact and the peer "
                                       "review. Plan elaboration is a "
                                       "mandatory step: seats argue and "
                                       "align BEFORE implementation, and "
                                       "the report points at what was "
                                       "agreed.")
        if (payload.status == Status.resolved and payload.reply_to
                and "settled_by" not in data and not data.get("answers")
                and not data.get("consumes")):
            # A `resolved` THAT CAN SETTLE NOTHING IS REFUSED, WITH THE
            # GESTURE THAT WORKS (2026-08-23). The hub refuses an `answers[]`
            # that discharges nothing and teaches the correct move; it
            # ACCEPTED IN SILENCE the same shape one field over — a
            # bystander's bare `resolved`, which closes nothing, discharges
            # nothing, and leaves the thread open and escalating. Same shape,
            # opposite treatment, chosen by nobody.
            #
            # The cost is not the wasted post: it is a FALSE ASSERTION in the
            # shared record. agora-wui's console printed "Marked #N
            # resolved." on exactly this no-op, and both clients derived a
            # `resolve` affordance from the status word alone because the
            # hub's silence read as permission. Two clients that share no
            # code shipped the identical defect (agora-tui `4a575a3`,
            # agora-wui `claim:resolve-verb-derived-from-tree-position`) —
            # what "derive the affordance from the message" produces when the
            # precondition lives somewhere else. A client gate over a hub
            # that accepts is advisory; this is what makes it load-bearing.
            #
            # THE REFUSAL NAMES WHO MAY CLOSE, AND THE GESTURE THAT WORKS ON
            # THIS ROW — never a bare no, and never a gesture that would be
            # refused too. A bare refusal on a control the reader believes in
            # produces a bug report, not a correction (agora-wui,
            # thread-shape-and-panels#29), and a refusal naming the WRONG exit
            # is worse: it sends a correct reader round a second loop. So the
            # recipe is computed per row — `answers[]` where the sender has an
            # ask to discharge, `evidence` where they are the named seat on an
            # ask-less request, and neither where they are a bystander.
            #
            # `settled_by` is deliberately NOT offered. Since the 2026-08-22
            # ruling it is refused for everyone this gate can reach: the
            # asker, an operator and a scoped ruling delegate are exempt
            # above, and a bystander's pointer is a 403. Naming it here would
            # be exactly the wrong-exit loop.
            #
            # Fields that exempt: `settled_by` is authorized above;
            # `answers`/`consumes` are policed by their own validators and are
            # by construction not no-ops.
            parent = self.db.get_message(payload.reply_to)
            if (parent is not None and parent.channel == channel
                    and parent.status in (Status.open, Status.blocked)
                    and parent.sender != sender
                    and sender not in self.operator_ids()
                    and sender not in self.ruling_delegate_ids(channel)):
                replies = self.db.replies_to(parent.id)
                prospective = Message(id="", channel=channel, seq=0,
                                      sender=sender, status=Status.resolved,
                                      to=list(payload.to or []),
                                      data=dict(data),
                                      reply_to=payload.reply_to)
                if resolved_settles_nothing(parent, replies, prospective,
                                            self._discharge):
                    raise HubError(400, self._resolved_settles_nothing_refusal(
                        parent, sender, replies))
        if payload.signature is not None:
            # Reserved authorship token: opaque, stored VERBATIM, not yet
            # verified. Consumers may read it; the hub attaches no trust.
            # Never shortened: half a signature is not a shorter signature,
            # it is a corrupt one, and it would verify as forged the day
            # someone wires verification up.
            sig = str(payload.signature)
            if len(sig) > MAX_SIGNATURE_CHARS:
                raise TextTooLong("signature", len(sig), MAX_SIGNATURE_CHARS)
            data["signature"] = sig
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
            self._log_event("channel-archived", channel=channel, by=agent.id,
                            evicted=len(evicted))
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
        self._log_event("channel-reopened", channel=channel, by=agent.id,
                        owner=info.created_by)
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
        reason = sanitize_text(str(reason or ""), 200, field="reason")
        evicted = self.db.retire_agent(target_id, reason)
        self._log_event("seat-retired", seat=target_id, by=agent.id,
                        evicted=len(evicted))
        return {"agent": target_id, "retired": True, "reason": reason,
                "evicted_from": sorted(evicted)}

    def unretire_agent(self, agent: AgentInfo, target_id: str) -> dict[str, Any]:
        """Restore a retired agent's auth (operator only). Memberships are NOT
        restored — the agent rejoins its rooms explicitly."""
        if not agent.operator:
            raise HubError(403, "restoring an identity is an operator act")
        if self.db.agent_deleted(target_id):
            raise HubError(410, f"'{target_id}' was deleted — deletion is "
                                "final (the id stays reserved; register a "
                                "new identity instead)")
        if self.db.agent_retirement(target_id) is None:
            return {"agent": target_id, "retired": False, "already_active": True}
        self.db.unretire_agent(target_id)
        self._log_event("seat-restored", seat=target_id, by=agent.id)
        return {"agent": target_id, "retired": False}

    def delete_agent(self, agent: AgentInfo, target_id: str) -> dict[str, Any]:
        """Hard-delete a RETIRED identity (0131, operator ask dm#164: 'no
        more mention of it anymore, not listed anywhere; just cleaning').
        The deliberate two-step: retire first (reversible, id listed under
        /agents/retired), then delete (irreversible, off every surface).
        Requiring the retire step means one click can never vaporize a live
        seat. History and the ledger are untouched — delete cleans rosters,
        not archives; the id stays reserved forever (anti-hijack tombstone).
        Operator only. Idempotent."""
        if not agent.operator:
            raise HubError(403, "deleting an identity is an operator act")
        if not self.db.agent_exists(target_id):
            raise HubError(404, f"agent '{target_id}' is not registered")
        if self.db.agent_deleted(target_id):
            return {"agent": target_id, "deleted": True, "already": True}
        if self.db.agent_retirement(target_id) is None:
            raise HubError(409, f"'{target_id}' is still active — retire it "
                                "first; delete is the second, irreversible "
                                "step (safety: one call can never vaporize "
                                "a live seat)")
        self.db.delete_agent(target_id)
        return {"agent": target_id, "deleted": True}

    def _obligation_addressees(self, payload: PostMessage,
                               data: dict[str, Any] | None) -> set[str]:
        """Seats a post would newly oblige (message to, ask assignee, ask to)."""
        if payload.status not in (Status.open, Status.blocked, Status.reply):
            return set()
        out: set[str] = set()
        if payload.to:
            out.update(payload.to)
        for a in (data or {}).get("asks") or []:
            if a.get("assignee"):
                out.add(str(a["assignee"]))
            out.update(str(x) for x in (a.get("to") or []))
        return out

    def _channel_traffic_policy(self, channel: str) -> str:
        row = self.db.store_get(channel, CHANNEL_META_KEY)
        if row and isinstance(row.value, dict):
            value = row.value.get("traffic_policy")
            if value in _TRAFFIC_POLICIES:
                return str(value)
        return "collaboration"

    def _message_contract(self, agent: AgentInfo, channel: str,
                          payload: PostMessage,
                          data: dict[str, Any] | None,
                          addressees: set[str]) -> str | None:
        """Validate obligation and noticeboard invariants before commit.

        Returns the typed root's durable idempotency key when one applies.
        """
        # `blocked` is the single most important escalation gesture in the
        # system: "I am stuck." It is never refused. The structured-ask form is
        # BETTER (it names who can unblock you and creates tracked debt), so
        # the hub teaches it with a non-waking sender doorbell — but a seat
        # that says it plainly is heard. Refusing this (0.12.55) meant
        # "boss, I'm blocked on the schema ruling" in a two-party DM was a 400,
        # where the addressee is structurally the only other party.
        del addressees  # informational only; no delivery decision rides on it

        if (self._channel_traffic_policy(channel) != "noticeboard"
                or agent.operator or agent.id == "hub"):
            return None

        if payload.reply_to is not None:
            # Every member may answer and update a noticeboard thread. Vote
            # results keep their stronger machine-readable contract and
            # idempotency key, but ordinary replies are never censored by the
            # hub; routing/noise guidance remains etiquette plus fork nudges.
            result = (data or {}).get("vote_result")
            if result is None:
                return None
            root = self.db.get_message(payload.reply_to)
            root_vote = (root.data or {}).get("vote") if root and root.data else None
            if (payload.status == Status.resolved
                    and isinstance(result, dict)
                    and root is not None and root.channel == channel
                    and root.sender == agent.id
                    and isinstance(root_vote, dict)):
                return f"vote-result:{root.id}"
            raise HubError(
                400,
                "malformed noticeboard vote result: only the vote chair may "
                "resolve their canonical vote root; use close_vote",
            )

        vote = (data or {}).get("vote")
        if isinstance(vote, dict):
            # The vote exception exists because a vote is the one
            # legitimately OBLIGING root on a board — but only a real vote.
            # Require the canonical shape every chairing surface builds via
            # vote.build_vote_post (storm review: a bare {"tag": ...} dict
            # minted unlimited unaddressed open roots, dedupe evaded by
            # fresh tags). A hand-crafted payload passing this shape IS a
            # vote — any member may chair one — so nothing legitimate is
            # lost; only the degenerate spam shape dies. Ballots arrive by
            # DM, never as asks: co-resident asks would sticky-pin every
            # member room-wide — the exact incident class this gate closes.
            if (data or {}).get("asks"):
                raise HubError(400, "noticeboard votes carry no asks — "
                                    "ballots arrive by DM (open_vote); run "
                                    "roll-call votes in a collaboration "
                                    "channel or group")
            tag = str(vote.get("tag") or "")
            topic = str(vote.get("topic") or "").strip()
            options = vote.get("options")
            distinct = ({o.strip().casefold() for o in options
                         if isinstance(o, str) and o.strip()}
                        if isinstance(options, list) else set())
            valid_options = (isinstance(options, list)
                             and len(distinct) >= 2
                             and all(isinstance(o, str) and o.strip()
                                     for o in options))
            closes_at = vote.get("closes_at")
            valid_deadline = (isinstance(closes_at, (int, float))
                              and not isinstance(closes_at, bool)
                              and math.isfinite(closes_at) and closes_at > 0)
            valid_tag = (0 < len(tag) <= 64
                         and not any(c.isspace() or ord(c) < 32 for c in tag))
            if not (valid_tag and topic and valid_options and valid_deadline
                    and payload.status == Status.open):
                raise HubError(400, "noticeboard votes must be canonical "
                                    "blind votes — use open_vote (status="
                                    "open with data.vote carrying tag, "
                                    "topic, >=2 distinct options, a finite "
                                    "closes_at)")
            return f"vote:{tag}"

        # A typed notice is OPTIONAL metadata that buys idempotency (a stable
        # event key the hub dedupes on) — never a licence to speak. The hub
        # does NOT gate root posts on carrying one, and does NOT restrict which
        # status a root may use.
        #
        # It used to do both (0.12.55, the wake-storm fix), and that inverted
        # the operator's standing principle — light safeguards, never silent,
        # never blocking — on the one room that exists for open dialogue: on a
        # noticeboard channel a member could not open a question, report a
        # problem that needs collaborative planning, or say `blocked` at all,
        # while the operator could. Only a formal blind vote woke the room.
        # A board's signal-to-noise is etiquette plus the sender-facing
        # convention doorbell in `_routing_nudges`, never a 400.
        notice = (data or {}).get("notice")
        if isinstance(notice, dict):
            return f"notice:{notice['kind']}:{notice['key']}"
        return None

    def _is_dark_seat(self, agent_id: str) -> tuple[bool, float | None]:
        """0107: offline / dark-episode — escalation cannot reach the seat."""
        since = self._dark_since.get(agent_id)
        if since is not None:
            return True, time.time() - since
        if self.silence_class_for_seat(agent_id) == "dead":
            return True, None
        return False, None

    def _address_dark_override(self, payload: PostMessage) -> bool:
        if payload.address_dark:
            return True
        data = payload.data or {}
        return bool(data.get("address_dark"))

    def _dark_addressee_nudge(self, poster: AgentInfo, message: Message,
                              addressees: set[str],
                              override_dark: bool) -> None:
        """0107, reshaped by operator ruling (2026-07-28): DELIVERY IS NEVER
        REFUSED for recipient state — humans always receive their messages,
        and so do agents. The hub's job here is information, not censorship:
        the message is already committed and delivered; the SENDER gets one
        ephemeral, non-waking doorbell saying their addressee is offline, so
        they can also route to a live seat instead of waiting on a corpse.
        `address_dark=true` (a deliberate canvass) suppresses even that.
        This replaced two 403 gates (0114 saturation, 0107 dark) that had
        muted the whole fleet toward the operator — see CHANGELOG 0.12.56/57."""
        if poster.operator or override_dark or not addressees:
            return
        if message.status not in (Status.open, Status.blocked):
            return
        dark_seats: list[str] = []
        for seat in sorted(addressees):
            if seat == poster.id or self.db.agent_is_operator(seat):
                continue
            dark, _age = self._is_dark_seat(seat)
            if dark:
                dark_seats.append(seat)
        if not dark_seats:
            return
        names = ", ".join(f"@{s}" for s in dark_seats)
        plural = "is" if len(dark_seats) == 1 else "are"
        self._deliver_doorbell(
            poster.id, message,
            f"HUB NOTICE — delivered, but {names} {plural} currently DARK "
            "(offline; escalation cannot reach them until they return). "
            "Expect delay; consider also routing to a live seat or a "
            "reporting delegate.")

    def post_message(self, agent: AgentInfo, channel: str, payload: PostMessage,
                     *, _task_review: dict | None = None,
                     _consultation_conclusion: dict | None = None) -> Message:
        """Post with a refusal audit: a refused send previously left no trace
        anywhere, so "agent X never answers" was indistinguishable from
        "agent X is being blocked" (field finding). Every HubError is recorded
        per agent and surfaced in the operator status overview."""
        try:
            if _consultation_conclusion is not None:
                return self._post_message(agent, channel, payload,
                                          _consultation_conclusion=_consultation_conclusion)
            if _task_review is not None:
                return self._post_message(agent, channel, payload, _task_review=_task_review)
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

    def _post_message(self, agent: AgentInfo, channel: str, payload: PostMessage,
                      *, _task_review: dict | None = None,
                      _consultation_conclusion: dict | None = None) -> Message:
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
        self._require_rulings_ack(channel, agent)
        payload, mention_ctx = self._apply_mention_addressing(agent, channel, payload)
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
        data = self._validate_consultation(agent, channel, payload, data)
        if data and {"consultation_conclusion", "consultation_event", "mission_changed"} & data.keys():
            raise HubError(400, "consultation_conclusion, consultation_event and mission_changed are reserved; use conclude_consultation for a decision")
        if (is_consultation(parent)
                and payload.status == Status.resolved and _consultation_conclusion is None):
            if agent.id != parent.sender and not agent.operator and agent.id not in self.ruling_delegate_ids(channel):
                raise HubError(409, "answer a consultation with status=reply and answers=[ask_id]; its requester uses conclude_consultation after collection")
            raise HubError(409, "use conclude_consultation to reconcile or explicitly cancel a collective question")
        if _consultation_conclusion is not None:
            data = {**(data or {}), "consultation_conclusion": _consultation_conclusion}
        if data and "task_review" in data:
            raise HubError(400, "task_review is reserved; use review_task")
        if _task_review is not None:
            data = {**(data or {}), "task_review": _task_review}
        # Batched consumption (0140/3): validated BEFORE the insert so a bad
        # ref refuses with nothing posted, and normalized to server-truth
        # message ids so the transcript records WHICH debts this settled.
        consume_targets: list[str] = []
        if data is not None and "consumes" in data:
            consume_targets, data["consumes"] = self._validate_consumes(
                agent, channel, data["consumes"])
        # This is deliberately before insert: a finding-integration failure
        # must not append a report that looks delivered but cannot stamp its
        # task.  The post-commit task projection remains a projection only.
        self._validate_task_delivery(agent, channel, payload, data, parent)
        addressees = self._obligation_addressees(payload, data)
        dedupe_key = self._message_contract(
            agent, channel, payload, data, addressees
        )
        # Operator ruling (2026-07-28): no recipient-state refusals — humans
        # and agents ALWAYS receive their messages. Dark/saturation state is
        # information (status rows, watchdog alerts, the post-commit sender
        # advisory below), never a delivery gate.
        override_dark = self._address_dark_override(payload)
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

        try:
            # VFS mutations and task/finding store writes take this lock too.
            # Rechecking immediately before insert makes the current-artifact
            # predicate and appended delivery one serialized operation.
            with self.db.orchestration_lock:
                data = self._validate_consultation(agent, channel, payload, data)
                self._validate_task_delivery(agent, channel, payload, data, parent)
                message = self.db.insert_message(
                    channel, agent.id, kind=Kind.message.value,
                    status=payload.status.value, urgency=urgency.value,
                    title=sanitize_title(payload.title), body=payload.body,
                    data=data, reply_to=payload.reply_to,
                    critical=payload.critical, downgraded=downgraded,
                    to=payload.to, dedupe_key=dedupe_key,
                )
                if message.reply_to is not None and message.status == Status.resolved:
                    # Keep the delivery decision and task projection in the
                    # same lock span as review/VFS writers. Existing durable
                    # task projection is still a separate database commit.
                    try:
                        self._task_on_resolved(message, parent)
                    except Exception:
                        logging.getLogger("agora.hub.tasks").exception("task projection failed (post succeeded)")
        except DuplicateMessage as exc:
            raise HubError(
                409,
                "duplicate notice refused: this channel already contains "
                f"the same typed event ({exc.message_id})",
            ) from exc
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
        for target in consume_targets:
            # The SAME discharge a reply performs, N times from one message:
            # a read receipt on the answer is what `owed.to_consume` clears
            # on. One message, N debts settled, one line in the transcript.
            self.db.mark_read(target, agent.id)
        # Task rows ride post-commit and never fail the post (0.18.0): an
        # operator's root in a shared room mints one; a `resolved` on a root
        # that has one may deliver or accept it.
        try:
            if message.reply_to is None:
                self._task_mint(message)
        except Exception:
            pass
        self._wake(message)
        # Routing nudges (0133/0135) ride post-commit and NEVER fail the
        # post: a teaching gesture that could 500 a message would be worse
        # than the noise it prevents.
        try:
            self._routing_nudges(agent, channel, message)
            # RE-ENABLED 2026-08-25, INVERTED. It was disabled saying "that
            # reply obliged nobody", which the (c) revert made false. The
            # true sentence is the opposite and needs no ruling: it reports
            # what the reply did and names `fyi` + `reply_to`, the terminal
            # gesture 0102 already blessed. agora-and-wui#724 measured why —
            # 8 of agora-wui's 12 reply-debts carry the sender's own
            # "nothing owed back" in PROSE the hub cannot read.
            self._reply_obliges_someone_nudge(agent, message)
            self._mention_nudges(agent, channel, message, mention_ctx)
            self._dark_addressee_nudge(agent, message, addressees,
                                       override_dark)
            self._undelegated_operator_warning(agent, message)
            # `replied` / `declined` just moved on the PARENT's ladder, so
            # the push goes to the parent's sender, not to this one (0156).
            # A reply is the rung this seat's asker is actually waiting on.
            if parent is not None:
                self._publish_pickup(parent)
        except Exception:
            logging.getLogger("agora.hub.routing").exception(
                "routing nudge failed (post succeeded)")
        return message

    #: Routing nudges (agora-0133/0135). Measured on 3.5 days of commons:
    #: 76% of envelope deliveries landed on seats that never spoke in the
    #: thread, and the operator ordered routing discipline (dm#177). Three
    #: mechanical, budgeted teaching gestures — never blocks, never a 500:
    #: - broadcast notice: an open/blocked with NO addressee obliges NOBODY
    #:   (zero /owed rows) and therefore buys no turn from an idle seat;
    #:   tell the SENDER what they just did (doorbell only — nothing stored,
    #:   no channel traffic, nobody shamed). No member threshold: the
    #:   arithmetic is the same in a room of two, and the measured failure
    #:   was a six-seat working group. See _routing_nudges.
    #: - task-room nudge: once a commons/open-floor thread already has the
    #:   real contributors, tell the thread to move the day-to-day work into
    #:   a focused room NOW rather than waiting for the noticeboard cleanup
    #:   nudge after the thread has already sprawled.
    #: - fork nudge: a thread in a noticeboard-scale room where 3+ seats are
    #:   building gets ONE in-thread pointer to `agora group` (stored fyi:
    #:   visible to the participants it addresses, wakes nobody).
    TASK_ROOM_NUDGE_MIN_PARTICIPANTS = 3
    FORK_NUDGE_MIN_SENDERS = 3
    FORK_NUDGE_MIN_MSGS = 6
    #: Rooms of 6-7 seats are the working unit now, and a floor of 10 meant
    #: this nudge had NEVER fired outside `commons` (27 members) and
    #: `at-test` (10) — never in a purpose-built working group, which is
    #: exactly where a thread outgrowing the room costs the most. The
    #: broadcast notice at `_deliver_doorbell` already made this move for the
    #: same reason (its floor went 6 -> >1 because "the measured failure was
    #: a 6-seat room"). 5 keeps a DM-sized huddle out of scope.
    FORK_NUDGE_MIN_MEMBERS = 5
    #: Share of a thread that must be ADDRESSED fan-out/fan-in around one
    #: seat before the fork nudge stands down (0140 field test 2).
    FORK_NUDGE_ORCHESTRATED_SHARE = 2 / 3

    @staticmethod
    def _orchestrated_thread(root: Message, replies: list[Message]) -> bool:
        """Is this thread one seat's ADDRESSED fan-out and the answers coming
        back in? That shape is orchestration WORKING, not sprawl: seven seats
        answering seven addressed asks in one root read as 'outgrown the
        room' and the nudge's fork cost five blocked seats and put the
        artifact owner outside the room (0140 field test 2).

        The nudge exists for UNADDRESSED many-to-many pile-on, where nobody
        owes anything and the thread is a room-wide broadcast in disguise.
        So: take the root's sender as the hub of the star, collect everyone
        it NAMES anywhere in the thread (message `to` or per-ask addressee),
        and require most of the thread to be either its addressed asks or
        those named seats answering. A root that names nobody can never
        qualify."""
        hub_seat = root.sender
        named: set[str] = set()
        for m in [root, *replies]:
            if m.sender == hub_seat and m.kind == Kind.message:
                named.update(m.to or [])
                named.update(ask_addressees(m))
        named.discard(hub_seat)
        if not named:
            return False
        thread = [root, *[r for r in replies if r.kind == Kind.message]]
        shaped = sum(
            1 for m in thread
            if (m.sender == hub_seat and (m.to or ask_addressees(m)))
            or m.sender in named)
        return shaped >= len(thread) * HubService.FORK_NUDGE_ORCHESTRATED_SHARE

    @staticmethod
    def _task_room_participants(root: Message,
                                replies: list[Message]) -> list[str]:
        """Seats that must SPEAK on a thread, as far as the hub can see.

        This is intentionally broader than the late fork nudge's "sprawl"
        test: if a task on the commons/open floor already has the real
        contributors, the right move is to open the focused room early —
        even when the thread is perfectly well-orchestrated so far."""
        participants: set[str] = {root.sender}
        for m in [root, *replies]:
            if m.kind != Kind.message:
                continue
            participants.add(m.sender)
            participants.update(m.to or [])
            participants.update(ask_addressees(m))
        participants.discard("hub")
        return sorted(p for p in participants if p)

    def _teaching_is_for_a_machine(self, agent_id: str) -> bool:
        """Would this hub teaching land on a HUMAN?

        Operator ruling, 2026-08-22: *"the hub should never say to a human he
        was wrong and ask him to run MCP commands — a human is not an AI
        agent and can't run MCP like that. When a human asks several seats a
        question on any channel, the hub doesn't have to tell him to do it
        somewhere else."*

        The hub's routing and convention teachings are written for agents:
        they prescribe `create_group(...)`, `notice={kind,key}`, store rows.
        A human reading them is told they posted wrongly and handed an API
        they cannot call. So these teachings are suppressed for an operator
        seat — the human's message is delivered exactly as sent, and the
        seats decide who answers, which is the whole point of a room.

        Alerts a human can ACT on are untouched; this only silences the
        "you should have done it another way" class."""
        return agent_id not in self.operator_ids()

    def _routing_nudges(self, agent: AgentInfo, channel: str,
                        message: Message) -> None:
        if channel.startswith(DM_PREFIX) or agent.id == "hub":
            return
        info = self.db.get_channel(channel)
        if info is None:
            return
        members = self.db.list_members(channel)
        # -- broadcast notice (0133; corrected 2026-08-01) -------------------
        # WHAT THIS USED TO SAY WAS FALSE, and the lie cost a fleet an
        # operator's deliverable. It told the sender an unaddressed open
        # "obliges ALL N other members until the thread closes"; the hub's
        # own ledger disagrees — a room-wide open with no `to` and no
        # per-ask addressee produces ZERO /owed rows for every member
        # (verified against a live 0.14.0 hub). Since 0140 the driver sides
        # with the ledger: a seat woken by room traffic it does not owe
        # spends no turn on it (wake-carries-work). So the steward fanning
        # the operator's task out as room-wide opens got the worst of both
        # readings — the hub said "this obliges everyone", every idle seat
        # said "this obliges nobody", and the work simply did not happen.
        # The notice now states the ledger's arithmetic instead.
        #
        # It also fires where the old one could not. PRIVATE purpose-built
        # groups are exempted from the ROUTING nudges below (those teach
        # "go make a group like this one" — self-defeating inside one), but
        # obligation arithmetic is not a routing opinion, and the fan-out
        # this corrects happens precisely inside freshly-created working
        # groups. The member floor drops to "anyone but the poster" for the
        # same reason: the old floor of 6 was a volume price tag, and the
        # measured failure was a 6-seat room whose brief was posted while
        # the steward was still its only member.
        if (message.status in (Status.open, Status.blocked)
                and not message.to and not ask_addressees(message)
                and len(members) > 1):
            n = len(members) - 1
            body = (f"HUB NOTICE — your {message.status.value} message "
                    f"'{(message.title or message.id)}' names nobody, so it "
                    f"creates NO obligation for any of the {n} other "
                    f"members of #{channel}: /owed stays empty for all of "
                    "them. They are woken and may answer if it touches what "
                    "they own, but nothing tracks, escalates or re-rings it. "
                    "If you need someone to ACT, name them — "
                    'per-ask to=["seat"] or message-level to — which is the '
                    "only form the hub tracks, escalates and re-rings. "
                    "Background for whoever is already reading? Fine — this "
                    "is the price tag, not a block.")
            self._deliver_doorbell(agent.id, message, body)
            # ...and ring the DELEGATE, not just the sender (2026-08-04).
            # The sender-facing notice teaches the human to name seats, but
            # the human is usually gone by the time it lands: scifi-novel#211
            # was an operator revision brief naming nobody, and the room did
            # the work of exactly one seat while five sat with empty /owed.
            # The reporting delegate ALREADY owes every operator message
            # (`_operator_delegate_debt`); this tells it the one thing its
            # own ledger cannot: that nobody else owes anything, so the
            # dispatch is its move. Doorbell only — nothing stored, no
            # channel traffic, no obligation minted.
            if agent.operator:
                for delegate in self._reporting_stewards(exclude=agent.id):
                    if delegate in members:
                        self._deliver_doorbell(
                            delegate, message,
                            f"HUB NOTICE — operator message "
                            f"'{(message.title or message.id)}' in #{channel} "
                            f"names nobody, so none of the {n} other members "
                            "owes anything on it. You own it as reporting "
                            "delegate. If it needs more hands than yours, "
                            'DECOMPOSE it into asks carrying to=["seat"] — '
                            "that is the only form that obliges anyone, and "
                            "an unaddressed ask buys no turn from anybody.")
        if info.private:
            # Purpose-built (private) groups ARE the destination the routing
            # nudges below teach; nudging inside them would fight their own
            # design.
            return
        # -- task-room nudge: move multi-seat work out of the open floor ----
        # The late fork nudge below is cleanup: by the time it fires, the
        # room already paid the context/noise cost. The earlier contract is
        # simpler: once a task on #commons or a noticeboard already has the
        # real contributors, create the focused room NOW and keep the floor
        # for the pointer, outsider-facing decisions, milestones and final
        # delivery. This deliberately ignores `_orchestrated_thread`: even a
        # well-addressed fan-out in commons belongs in its own room once the
        # contributor set is known.
        if (channel == "commons"
                or self._channel_traffic_policy(channel) == "noticeboard"):
            root = message if message.reply_to is None else self.db.get_message(
                message.reply_to)
            # commons/open-floor threads reply to the root; one hop covers the
            # measured shape and keeps the check cheap.
            if root is not None and root.reply_to:
                root = self.db.get_message(root.reply_to) or root
            if (root is not None and root.kind == Kind.message
                    and root.status in (Status.open, Status.blocked)
                    and self._teaching_is_for_a_machine(root.sender)
                    and self.db.meta_get(f"taskroomnudge:{root.id}") is None):
                replies = self.db.replies_to(root.id)
                if not any(r.status == Status.resolved for r in replies):
                    participants = self._task_room_participants(root, replies)
                    if len(participants) >= self.TASK_ROOM_NUDGE_MIN_PARTICIPANTS:
                        self.db.meta_set(f"taskroomnudge:{root.id}",
                                         str(time.time()))
                        slug = _topic_slug(root.title) or "this-topic"
                        names = " ".join(f"@{s}" for s in participants)
                        self._post_system(
                            channel,
                            f"ROUTING — this task already has "
                            f"{len(participants)} seats who must speak. "
                            f"Open a focused room NOW — MCP: create_group("
                            f'name="{slug}", members={list(participants)}); '
                            f"CLI: agora group {slug} "
                            f"{names}  — keep #{channel} for the pointer, "
                            "cross-room decisions, milestones, and final "
                            "delivery.",
                            # The `@names` in the body are create_group
                            # ARGUMENTS, not addressees — naming three seats
                            # to make one act is the diffusion `MAX_ASK_TO`
                            # exists to prevent. The thread's author is the
                            # seat who opens the room, so the nudge is
                            # theirs. Addressed `fyi`: attribution without a
                            # debt the hub cannot close.
                            to=[root.sender], status="fyi", reply_to=root.id)
        # -- board convention (replaces the 0.12.55 refusal): sender-facing ---
        # On an opt-in noticeboard channel a root post without a typed notice
        # is DELIVERED; the sender simply learns the convention that buys them
        # dedupe. Teaching, once per post, non-waking — never a block.
        if (message.reply_to is None
                and self._channel_traffic_policy(channel) == "noticeboard"
                and not (message.data or {}).get("notice")
                and not (message.data or {}).get("vote")
                and self._teaching_is_for_a_machine(agent.id)):
            self._deliver_doorbell(
                agent.id, message,
                f"HUB NOTICE — #{channel} is a noticeboard: posts here carry "
                "best with notice={kind,key} (job, announcement, problem, "
                "resolution, consensus, milestone, delivery), which gives the "
                "event a stable identity the hub dedupes on so a repost cannot "
                "double-announce it. Your message was delivered as-is; this is "
                "a convention, not a gate. Long back-and-forth is better in a "
                "focused group (`agora group`).")
        # -- fork nudge (0135): thread-facing, once per root -----------------
        # This is a NOTICEBOARD teaching, not a public-room teaching. A
        # collaboration room may be large and still be the right place for
        # the work; firing the noticeboard wording there is both misleading
        # and a false routing push.
        if (message.reply_to
                and self._channel_traffic_policy(channel) == "noticeboard"
                and len(members) >= self.FORK_NUDGE_MIN_MEMBERS):
            root = self.db.get_message(message.reply_to)
            # Flat-thread walk: commons threads reply to the root; one hop
            # covers the measured shape without a recursive scan.
            if root is not None and root.reply_to:
                root = self.db.get_message(root.reply_to) or root
            if root is None or root.kind != Kind.message:
                return
            if self.db.meta_get(f"forknudge:{root.id}") is not None:
                return
            if not self._teaching_is_for_a_machine(root.sender):
                return          # the nudge would tell a human to run MCP
            replies = self.db.replies_to(root.id)
            if any(r.status == Status.resolved for r in replies):
                return  # thread already closing — too late to redirect
            speakers = {m.sender for m in replies if m.kind == Kind.message}
            speakers.add(root.sender)
            speakers.discard("hub")
            total = 1 + sum(1 for m in replies if m.kind == Kind.message)
            if self._orchestrated_thread(root, replies):
                return          # addressed fan-out/fan-in: the shape works
            if (len(speakers) >= self.FORK_NUDGE_MIN_SENDERS
                    and total >= self.FORK_NUDGE_MIN_MSGS):
                self.db.meta_set(f"forknudge:{root.id}", str(time.time()))
                slug = _topic_slug(root.title) or "this-topic"
                names = " ".join(f"@{s}" for s in sorted(speakers))
                self._post_system(
                    channel,
                    f"ROUTING — {len(speakers)} seats, {total} messages: this "
                    f"thread has outgrown the noticeboard (hub rules, "
                    f"Routing). Fork it:  agora group {slug} "
                    f"{names}  — then resolve this thread with one pointer "
                    "reply. (One-time nudge; the hub never blocks.)",
                    # "resolve this thread" is an act only the root author
                    # holds authority for (`obligations._closes`), so the
                    # nudge names them and nobody else.
                    to=[root.sender], status="fyi", reply_to=root.id)

    @dataclass
    class _MentionContext:
        outsiders: list[str] = field(default_factory=list)
        unobliged: list[str] = field(default_factory=list)

    def _apply_mention_addressing(self, agent: AgentInfo, channel: str,
                                  payload: PostMessage) -> tuple[PostMessage, _MentionContext]:
        """0105 widened: in-room @mentions become real addressing for every
        sender. Quoted fence spans are ignored; explicit `to` is preserved
        and merged."""
        ctx = self._MentionContext()
        if channel.startswith(DM_PREFIX):
            return payload, ctx
        members = {m.agent_id for m in self.db.list_members(channel)}
        # Seat-identity precedence (operator ruling): a token that exactly
        # matches a registered seat id is a mention even written @seat/... or
        # @seat:...; a path-like token matching no registered seat is a vfs
        # reference — no mention, no obligation, no outsider warning. The
        # registry (not room membership) decides, so outsider warnings keep
        # firing for real seats named from the wrong room.
        registered = set(self.db.list_agent_ids())
        in_room, ctx.outsiders = resolve_mentions(payload.body, members,
                                                  registered)
        if in_room:
            merged = list(payload.to or [])
            for seat in in_room:
                if seat != agent.id and seat not in merged:
                    merged.append(seat)
            if merged != list(payload.to or []):
                payload = payload.model_copy(update={"to": merged})
        if payload.asks:
            new_asks = []
            asks_changed = False
            for ask in payload.asks:
                in_ask, out_ask = resolve_mentions(ask.text, members,
                                                   registered)
                # `seat: …` — the fan-out form a human writes (0077 field
                # incident, 2026-09-07 fleet run). A leading MEMBER name is
                # an addressee exactly like `@seat`; a non-member leading
                # token (`note:`, `claim:`) is prose.
                # Only when the author gave NO explicit `to`: an explicit list
                # is the author's intent, and a leading token that happens to
                # be a member name (`note: …`) must not add a respondent to it
                # (companion review, 2026-09-09).
                lead_seat = leading_addressee(ask.text) if not ask.to else None
                if lead_seat and lead_seat in members and lead_seat not in in_ask:
                    in_ask = [lead_seat] + list(in_ask)
                for o in out_ask:
                    if o not in ctx.outsiders:
                        ctx.outsiders.append(o)
                seats = list(ask.to or [])
                # WHICH names the hub added, recorded at the source (agora-tui
                # #142, agora-wui #131). An addressee merged from the ask's
                # TEXT gates the ask's discharge exactly like a passed one, but
                # means something opposite: "@x wants this" is a subject,
                # "@x, which?" is an addressee, and the merge cannot tell them
                # apart. It cost agora-wui 25 minutes and an escalation on a
                # row they had answered completely.
                #
                # Recorded here rather than left to renderers because the
                # alternative is every client re-deriving `resolve_mentions`
                # in its own language: deterministic today, drifting the first
                # @-form this function learns that the copies do not, with
                # every suite green while the clients disagree about which
                # names on a row are advisory.
                derived: list[str] = []
                for seat in in_ask:
                    if seat == agent.id or seat in seats:
                        continue
                    if len(seats) < self.MAX_ASK_TO:
                        seats.append(seat)
                        derived.append(seat)
                    elif seat not in ctx.unobliged:
                        ctx.unobliged.append(seat)
                if seats != list(ask.to or []):
                    ask = ask.model_copy(update={"to": seats,
                                                 "to_from_text": derived})
                    asks_changed = True
                new_asks.append(ask)
            if asks_changed:
                payload = payload.model_copy(update={"asks": new_asks})
        return payload, ctx

    def _mention_nudges(self, agent: AgentInfo, channel: str,
                        message: Message,
                        ctx: _MentionContext) -> None:
        """0105 teaching gestures — ephemeral doorbells, never stored."""
        if ctx.outsiders:
            names = ", ".join(f"@{s}" for s in ctx.outsiders)
            self._deliver_doorbell(
                agent.id, message,
                f"HUB NOTICE — you wrote {names} but "
                f"{'that seat is' if len(ctx.outsiders) == 1 else 'those seats are'} "
                "not a member of this channel. Invite them first if you "
                "meant to oblige them here.")
        if ctx.unobliged:
            names = ", ".join(f"@{s}" for s in ctx.unobliged)
            self._deliver_doorbell(
                agent.id, message,
                f"HUB NOTICE — you named {names} in one ask, but a single "
                f"ask may obligate at most {self.MAX_ASK_TO} seats. Split "
                "the ask, or keep the extras at message-level `to`.")

    def noise_report(self, hours: float = 24.0) -> dict[str, Any]:
        """The routing reform's proof instrument (0135): per-channel wake and
        participation numbers over a bounded window, derived live — nothing
        hand-kept. `wakes_old` prices every open/blocked at room size (the
        pre-0135 listener rule); `wakes_new` prices addressed ones at their
        named-seat count (the narrowed rule). The delta is the reform's
        measurable claim; participation shows how many members a channel's
        threads actually involve — the taxonomy's 4-of-24 finding, kept
        current."""
        hours = max(1.0, min(hours, 24.0 * 14))
        msgs = self.db.messages_since(time.time() - hours * 3600.0)
        by_channel: dict[str, list[Message]] = {}
        for m in msgs:
            if m.kind == Kind.message and not m.channel.startswith(DM_PREFIX):
                by_channel.setdefault(m.channel, []).append(m)
        report: list[dict[str, Any]] = []
        for channel, rows in sorted(by_channel.items()):
            members = len(self.db.list_members(channel))
            audience = max(members - 1, 0)
            broadcast_opens = addressed_opens = 0
            wakes_old = wakes_new = 0
            speakers_by_root: dict[str, set[str]] = {}
            for m in rows:
                named = set(m.to) | ask_addressees(m)
                if m.status in (Status.open, Status.blocked):
                    wakes_old += audience
                    if named:
                        addressed_opens += 1
                        wakes_new += len(named - {m.sender})
                    else:
                        broadcast_opens += 1
                        wakes_new += audience
                root = m.reply_to or m.id
                speakers_by_root.setdefault(root, set()).add(m.sender)
            threads = [s for s in speakers_by_root.values() if len(s) > 1]
            report.append({
                "channel": channel, "members": members, "messages": len(rows),
                "broadcast_opens": broadcast_opens,
                "addressed_opens": addressed_opens,
                "wakes_old_rule": wakes_old, "wakes_new_rule": wakes_new,
                "multi_speaker_threads": len(threads),
                "avg_speakers_per_thread": (
                    round(sum(len(s) for s in threads) / len(threads), 1)
                    if threads else 0.0),
            })
        return {"hours": hours, "channels": report,
                "computed_at": time.time()}

    def _reply_obliges_someone_nudge(self, agent: AgentInfo,
                                     message: Message) -> None:
        """Tell a PEER who addressed a `reply` that it OBLIGED the seats it
        named, and name the terminal gesture that does not.

        INVERTED 2026-08-25, and the inversion is the whole point. This
        doorbell used to say *"that reply obliged nobody"* — the sender-facing
        half of (c), which was reverted because it deleted the class an
        operator ruling created (claim:reply-cannot-renounce-its-debt). A
        doorbell that contradicts the predicate is worse than none, so it was
        disabled rather than left lying. The true sentence after the revert is
        the opposite one, and it turns out to be the more useful of the two.

        WHAT IT IS FOR, measured rather than supposed. agora-wui counted their
        own owed list at agora-and-wui#724: of 12 open reply-debts, **at least
        8 end with their sender explicitly disclaiming a reply** — "Nothing
        owed back", "@agora-wui — nothing for you". Their reading: *"a
        sender's 'nothing owed back' is prose the hub cannot see."*

        True, and the conclusion runs the other way from the one being asked
        for. Those senders were reaching for a gesture THAT ALREADY EXISTS and
        used the wrong status word: `fyi` + `reply_to` is the terminal reply,
        blessed by design 0102 as "THE TERMINAL GESTURE" and confirmed at
        decision:fyi-plus-reply-to-is-the-exit-and-my-content-free-premise-was-wrong.
        Eight rows were minted because nobody told the author, at the moment
        they could still choose, that `reply` is not it.

        So this needs no ruling and changes no meaning: it does not touch what
        a reply obliges, it reports what this one just did and names the door.
        The ruling on laurent's desk is about whether to DELETE the obligation;
        this is about whether senders can find the exit they already have. If
        it works, the case for deleting the class gets materially weaker — and
        that is worth knowing before he rules rather than after.

        A DOORBELL AND NOT A REFUSAL, deliberately: an addressed reply is
        legal and often exactly right. Refusing it would tax the common case
        to teach the rare one. Ephemeral, wakes nobody, stores nothing.

        Operators are exempt: an operator's reply obliges under the 2026-07-19
        ruling whatever they intend, so there is no choice to offer them."""
        if message.kind != Kind.message or message.status != Status.reply:
            return
        if not message.to or agent.operator:
            return
        if (message.data or {}).get("answers") or (message.data or {}).get("declines"):
            return          # a discharge, not a directive: nothing to teach
        named = ", ".join(sorted(message.to))
        # FRONT-LOADED ON PURPOSE, and I got this wrong on the first pass.
        # A notify line carries `body[:200]` (notify_sink.py), which is the
        # whole of what a tailing or `--preview` listener ever shows. My first
        # wording spent its opening sentence explaining the obligation and put
        # the EXIT after a paragraph break — so the only actionable half was
        # the half that got cut, and a teaching notice whose teaching is
        # truncated teaches nothing. The fact, the door and the trap all live
        # in the first 200 characters now; the paragraph after is for readers
        # who get the full body. test_the_actionable_half_survives_the_preview
        # is the guard, because this is invisible to every other test.
        self._deliver_doorbell(
            agent.id, message,
            f"HUB NOTICE — that reply OBLIGES {named}: a row that rots, "
            "escalates, wakes them. Meant \"nothing owed back\"? Post `fyi` "
            "with the same `reply_to`; writing it in the BODY does not do it."
            "\n\nAddressing a `reply` mints an obligation row on each seat you "
            "named, and it stands until they answer in this thread. `fyi` + "
            "`reply_to` is the terminal gesture (design 0102): same thread, "
            "same visibility to the same seats, no row. The hub cannot read "
            "prose, so a disclaimer in the body excuses nobody — the seat you "
            "meant to let off still pays it.",
            title="that reply obliged the seats you named")

    def _deliver_doorbell(self, agent_id: str, mirror: Message, body: str,
                          title: str = "hub notice") -> None:
        """An EPHEMERAL, non-waking sender notice: one notify-file line,
        nothing stored — read_message on its id 404s and the body stands alone. The
        channel/seq MIRROR the real message so acking can never move a
        cursor past real traffic (same construction as the stale-client
        notice, http_api._stale_client_notice).

        `title` is what a `--preview` listener and any notify-file tailer
        actually SEE (the body reaches no model on the driven lane: the
        --once digest is redacted to identifiers and the notice is never
        stored, so check_inbox cannot show it). Every notice used to render
        under the single hardcoded headline "broadcast obligation", so a
        charter publication was indistinguishable from a mention nudge on
        every tailer; the default is now neutral and a caller whose notice
        is actionable names itself."""
        if self.notify_sink is None:
            return
        self.notify_sink.deliver(agent_id, Envelope(
            id=f"notice:{mirror.id}", channel=mirror.channel, seq=mirror.seq,
            sender="hub", kind=Kind.system, status=Status.fyi,
            urgency=Urgency.inbox, effective_urgency=Urgency.inbox,
            # Delivery is targeted by the sink call itself. Do not set to_me:
            # teaching feedback must be visible without recursively spawning
            # another driven reception turn.
            to_me=False, addressed=False,
            title=title,
            body=body, body_bytes=len(body.encode())))

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
            "is false, rotate the key. One alert per episode.",
            # The body issues instructions to ONE named human, who holds the
            # only lever that resolves it (the key). Addressed `fyi`: it is
            # theirs to read, but the hub cannot discharge a key rotation, so
            # it must not mint a debt that outlives the episode.
            to=[operator_id], status=Status.fyi)
        # ...and reach them where a human actually reads. A suspected key
        # compromise is the last alert that should sit unseen in a room the
        # operator may not be watching — `_fleet_liveness_sweep` already
        # mirrors its alerts to the operator's DM; this one did not.
        self._post_operator_dm(
            operator_id,
            f"HUB WARNING — {len(recent)} posts under your key "
            f"'{operator_id}' within {self.OPERATOR_BURST_WINDOW:.0f}s "
            "(machine cadence on a human key). If that was not you: verify "
            "the posts, retract what is false, rotate the key.",
            dedupe_key=f"operator-burst:{operator_id}:{int(now)}")

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
        if not self._norms_required(channel):
            return
        row = self.db.fs_get(channel, FS_PREFIX + CHARTER_PATH)
        if row is None or row["deleted"]:
            return  # flag set but no charter written yet: nothing to require
        receipt = self.db.charter_receipt_get(agent.id, channel)
        if receipt is None or receipt < row["version"]:
            raise HubError(409, f"this channel requires reading its charter "
                                f"first: read_charter(channel={channel!r}) "
                                f"(v{row['version']}, at '{CHARTER_PATH}'), "
                                "then retry")
    def _post_system(self, channel: str, body: str,
                     to: list[str] | None = None,
                     status: str | None = None,
                     reply_to: str | None = None,
                     data: dict[str, Any] | None = None,
                     dedupe_key: str | None = None,
                     urgency: str = "inbox") -> Message:
        # `to` lets an alert ADDRESS its steward (0084): an addressed
        # message rides the to-me wake path and the owed ledger — a
        # broadcast alert would unpin on a bare read and decay.
        # `status`/`reply_to` let the hub CLOSE its own alerts (0093): an
        # open system message is an obligation, and obligations the hub
        # never discharges accumulate as permanent owed debt on the
        # addressees (measured: 8 undischargeable rows on one delegate).
        # `dedupe_key` makes a hub event idempotent under (channel, hub, key):
        # a repeated sweep tick cannot double-announce the same event.
        #
        # `to` AND `status` ARE DECIDED SEPARATELY, ALWAYS. This used to read
        # `status or ("open" if to else "fyi")`, so naming an addressee
        # silently promoted a notice to an OBLIGATION — the 0093 class this
        # function's own comment warns about, re-opened by anyone who "just
        # adds `to`" for correct attribution. Addressing says WHO a message
        # is for; status says whether it is a debt. A room announcement whose
        # subject is a seat wants the first and not the second.
        if to and status is None:
            raise ValueError(
                "a hub message with `to` must state its `status` explicitly: "
                "'fyi' addresses the seat without minting a debt the hub "
                "can never discharge; 'open' mints one and needs a closer.")
        message = self.db.insert_message(
            channel, "hub", kind=Kind.system.value,
            status=status or "fyi", urgency=urgency,
            title="", body=body, data=data, reply_to=reply_to, to=to or [],
            dedupe_key=dedupe_key,
        )
        self._wake(message)
        return message

    def _wake(self, message: Message, *, require_notify: str | None = None) -> bool:
        payload = {"type": "message", "message": message.model_dump()}
        # New corpus content: shorten the embedder's idle sleep (the work
        # set is DERIVED, this is purely a latency nudge — fs/store writes
        # ride the ≤20s standing sweep, message traffic is the hot class).
        self.embedding.nudge()
        self.fanout.publish(message.channel, payload)
        # Membership-keyed fan-out: reaches connected members whose channel
        # subscription predates this channel's existence (e.g. a DM opened
        # after their watcher connected — previously silently undeliverable
        # until the watcher restarted). The "agent/" prefix cannot collide
        # with channel names ("/" is rejected in channel slugs). Clients
        # dedup by per-channel seq, so double delivery is harmless.
        notified = True
        for member in self.db.list_members(message.channel):
            self.fanout.publish(f"agent/{member.agent_id}", payload)
            # Hub-written notify file: each member's <id>-inbox.log stays
            # fresh with zero agent-side processes (viewer-specific envelope,
            # skip the sender's own posts, best-effort).
            if self.notify_sink is not None and member.agent_id != message.sender:
                delivered = self.notify_sink.deliver(
                    member.agent_id, self.envelope_for(member.agent_id, message))
                if member.agent_id == require_notify and delivered is False:
                    notified = False
        self.notifier.notify()
        return notified

    def get_messages(self, agent: AgentInfo, channel: str,
                     since_seq: int = 0, limit: int = 200) -> list[MessageRow]:
        """Browse channel history. This is a bulk scan, NOT a deliberate read:
        it does NOT record read receipts, so paging history can no longer
        silently un-pin a critical or clear an obligation (v0.3 bug M2). Use
        read_message to actually attend to (and clear) a specific message.

        Rows are DECORATED with the two thread-derived facts every client
        was re-deriving from its own reply scans (parity move 2, agora-0118):
        `pending_asks` and `has_resolved_reply`. One batched reply query per
        page, the same discharge_state the obligation surfaces use — so a
        history page and /owed can never tell a different story about the
        same thread."""
        self.require_membership(channel, agent.id)
        messages = self.db.get_messages(channel, since_seq, limit)
        return self._decorate_rows(messages, agent.id)

    def top_rated_messages(self, agent: AgentInfo, channel: str,
                           limit: int = 50) -> list[MessageRow]:
        """Whole-channel top-N messages by net rating (agora-0125): the
        'sort by votes' surface, alongside recency. Decorated like every
        history row (the ratings tally IS the sort key, so clients render
        served order and never re-rank). A browse, not a deliberate read —
        no receipts, same as get_messages."""
        self.require_membership(channel, agent.id)
        limit = max(1, min(limit, 200))
        return self._decorate_rows(
            self.db.top_rated_messages(channel, limit), agent.id)

    def get_message_by_seq(self, agent: AgentInfo, channel: str,
                           seq: int) -> MessageRow:
        """Positional lookup (parity move 2): '#N' is how humans and UIs cite
        messages, and every client used to page history to resolve one. Like
        get_messages this is a browse, NOT a deliberate read — no receipt."""
        self.require_membership(channel, agent.id)
        m = self.db.get_message_by_seq(channel, seq)
        if m is None:
            raise HubError(404, f"no message #{seq} in '{channel}'")
        return self._decorate_rows([m], agent.id)[0]

    def _decorate_rows(self, messages: list[Message],
                       viewer_id: str = "") -> list[MessageRow]:
        """Message -> MessageRow: attach pending_asks / has_resolved_reply
        from ONE batched reply scan, the rating tally (+ the viewer's own
        standing rating) from ONE batched rating scan (agora-0122), and the
        viewer's own read receipts from ONE batched reads scan (agora-0130:
        with the channel cursor, `cursor >= seq AND NOT read` is the
        acked-but-never-read badge — the burst-skip fact clients could not
        compute). Retracted rows keep null decorations — their obligations
        are already cleared everywhere else, and a tombstone carries no
        rateable content."""
        ops = self.operator_ids()
        ids = [m.id for m in messages]
        by_parent = self.db.replies_map(ids)
        by_rated = self.db.ratings_for_messages(ids)
        read_ids = (self.db.reads_for_messages(ids, viewer_id)
                    if viewer_id else set())
        # Parent coordinates (0154), ONE batched query for the whole page —
        # the alternative every client was reduced to is a per-row fetch or a
        # private id->seq map that only knows what it has already scrolled.
        parents = self.db.messages_by_ids(
            [m.reply_to for m in messages if m.reply_to])
        # Lazily scanned once per page, and only for a viewer reading their
        # own messages — see the pickup branch below.
        claim_links: dict[str, dict[str, tuple[str, str]]] | None = None
        out: list[MessageRow] = []
        for m in messages:
            row = MessageRow(**m.model_dump())
            # Set OUTSIDE the `not retracted` guard, unlike every other
            # decoration here: those are thread state, which a tombstone
            # genuinely no longer has. A retracted message's own `reply_to`
            # is still served, so blanking its coordinates would serve an
            # unprintable id and call it a day — the exact defect this field
            # exists to end, reintroduced on the one row class that cannot
            # complain about it.
            if m.reply_to:
                parent = parents.get(m.reply_to)
                if parent is not None:
                    row.reply_to_seq = parent.seq
                    row.reply_to_sender = parent.sender
                    row.reply_to_retracted = parent.retracted
            if not m.retracted:
                replies = by_parent.get(m.id, [])
                ds = self._discharge(m, replies)
                row.pending_asks = [] if ds.closed else list(ds.pending)
                row.has_resolved_reply = ds.has_resolved_reply
                # The VERDICT, not just the shape of the thread — one field
                # so a client never has to infer settlement from an absence
                # or from `has_resolved_reply`, which a bystander can set.
                row.closed = ds.closed
                row.closed_by = next(
                    (r.sender for r in replies
                     if self._closes_one(m, r)), None) if ds.closed else None
                # ...and the OTHER closure question: may THIS reader close it
                # (thread-shape-and-panels#189). Only on a live question — a
                # closed row has nothing to offer a verb for, and `null`
                # there says "no statement" rather than "you may not".
                row.may_close = (
                    self._may_close(m, viewer_id)
                    if viewer_id and not ds.closed
                    and m.status in (Status.open, Status.blocked)
                    else None)
                ratings = by_rated.get(m.id, [])
                row.ratings = RatingTally(
                    up=sum(1 for r in ratings if r["value"] > 0),
                    down=sum(1 for r in ratings if r["value"] < 0),
                    mine=next((r["value"] for r in ratings
                               if r["rater"] == viewer_id), 0))
                if viewer_id and m.sender != viewer_id:
                    # Own messages stay null: authorship needs no reading,
                    # and a false "unread" on your own post would badge it.
                    row.read = m.id in read_ids
                elif viewer_id and m.sender == viewer_id:
                    # WHO HAS THIS (0156) — the asker's own instrument, and
                    # only theirs. It exposes other seats' read receipts to
                    # the sender, which both client seats consented to on the
                    # record; nobody consented to it being public, so it
                    # never rides a row served to a third party.
                    #
                    # The claim index is scanned ONCE for the page and only
                    # if this viewer has a message on it: it is O(store rows)
                    # and a 200-row history page of your own channel would
                    # otherwise scan the whole store 200 times.
                    if claim_links is None:
                        claim_links = self._declared_claim_links(m.channel)
                    rungs = self._pickup_rungs(m, replies, links=claim_links)
                    if rungs:
                        row.pickup = rungs
            out.append(row)
        return out

    def channel_digest(self, agent: AgentInfo, channel: str) -> dict[str, Any]:
        """Fold a channel's history into actionable knowledge — mechanically,
        from structure the messages already carry (no NLP, no embeddings):

        - open_questions: open/blocked messages not yet discharged, with their
          pending ask texts (asks/answers make Q->A pairs mechanical).
        - decided: discharged obligations (who answered — and, separately, who
          DECLINED: a refusal discharges but is not an answer) and `resolved`
          posts.
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
                # `sender` everywhere (agora/0.4): the digest used to call
                # the author `from` while /owed called it `sender`, and one
                # fact with two names is what every client special-cased.
                brief = {"seq": m.seq, "id": m.id, "sender": m.sender,
                         "title": m.title, "created_at": m.created_at}
                if m.status in (Status.open, Status.blocked):
                    replies = self.db.replies_to(m.id)  # one query, reused
                    state = self._discharge(m, replies)
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
                        declined_by: list[str] = []
                        if asks_of(m):
                            # Credit only repliers who actually answered an ask
                            # (a "bump" reply must not be listed, review M2) —
                            # and a REFUSAL IS NOT AN ANSWER (0153). A seat
                            # that declines every ask aimed at it used to
                            # accrue the same `decided` credit as one that
                            # answered them, which is the digest recording
                            # the opposite of what happened.
                            ask_ids = {str(a["id"]) for a in asks_of(m)}
                            answered_by = sorted({
                                r.sender for r in replies
                                if r.sender != m.sender
                                and ask_ids & set(substantive_answers_of(r))
                            })
                            declined_by = sorted({
                                r.sender for r in replies
                                if r.sender != m.sender
                                and ask_ids & set(declines_of(r))
                            })
                        else:
                            answered_by = sorted({r.sender for r in replies
                                                  if r.sender != m.sender})
                        decided.append({**brief, "answered_by": answered_by,
                                        # Present only when something WAS
                                        # declined: a row of empty fields
                                        # teaches a reader nothing. Keyed on
                                        # EITHER signal — on a canvass where
                                        # one named seat answers and another
                                        # refuses, the ask reads answered
                                        # (`state.declined` is empty) and
                                        # gating on that alone erased the
                                        # refusal from the room's memory.
                                        **({"declined_by": declined_by,
                                            "declined_asks": state.declined}
                                           if (declined_by or state.declined)
                                           else {}),
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
        rulings = []
        for entry in self.db.store_keys(channel):
            if entry["key"].startswith("decision:"):
                stored = self.db.store_get(channel, entry["key"])
                if stored is not None:
                    decisions.append({"key": entry["key"], "value": stored.value,
                                      "version": stored.version,
                                      "updated_by": stored.updated_by})
            elif entry["key"].startswith(self._RULING_PREFIX):
                stored = self.db.store_get(channel, entry["key"])
                if stored is not None and isinstance(stored.value, dict):
                    if stored.value.get("active", True):
                        rulings.append({"key": entry["key"], "value": stored.value,
                                        "version": stored.version,
                                        "updated_by": stored.updated_by})
        # open_questions must be complete (an unanswered seq-5 question still
        # matters), but `decided` grows forever: cap it newest-first and keep
        # the total so truncation is visible (review M1).
        decided_total = len(decided)
        # Counted over ALL decided rows, before the cap: a decline that falls
        # off the shown page is still a decline the room made.
        declined_total = sum(len(d.get("declined_asks") or []) for d in decided)
        decided = sorted(decided, key=lambda d: d["seq"], reverse=True)[:50]
        # Phases lead the digest by construction (0140/2): the digest is the
        # "returning after a gap" surface, and the question a returning seat
        # gets wrong is WHICH VERSION is live — the at-test v3/v4 collision.
        unacked = self._unacknowledged_rulings(agent, channel)
        phases = self.phase_rows(channel)
        return {
            "channel": channel,
            "phases": phases,
            "rulings": rulings,
            "unacknowledged_rulings": unacked,
            "phase_lines": [self.phase_line(p) for p in phases],
            "open_questions": open_questions,
            "decided": decided,
            "decisions": decisions,
            "counts": {"open_questions": len(open_questions),
                       "decided_shown": len(decided), "decided_total": decided_total,
                       # Visible rather than folded into credit (0153).
                       "declined_asks": declined_total,
                       "decisions": len(decisions), "rulings": len(rulings),
                       "phases": len(phases),
                       "unacknowledged_rulings": len(unacked)},
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
        declined: list[str] = []
        # Per-seat discharge, so the pin scope can ask "has THIS viewer
        # answered?" rather than inferring it from the ask's own state —
        # the two come apart on a multi-addressee ask (agora-and-wui#190).
        answered_by: dict[str, list[str]] = {}
        already_read = False
        owes_reply = False
        if message.status in (Status.open, Status.blocked):
            state = self._discharge(message, self.db.replies_to(message.id))
            closed = state.closed
            # A CLOSED thread has no pending asks on ANY surface (impl
            # adversary P2-2): the history row, /owed and the digest already
            # blank them on closure; the envelope reporting raw discharge
            # state was the one dissenting voice — an authoritatively
            # resolved question must not keep waving its unanswered ask ids.
            pending = [] if closed else state.pending
            total = state.total
            declined = state.declined
            answered_by = state.answered_by
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
                self._closed_authoritatively(message, replies)
                or any(r.sender == viewer_id for r in replies))
            already_read = self.db.has_read(message.id, viewer_id)
        envelope = self.attention.envelope_for(
            viewer_id, message,
            parent=parent,
            has_reply=closed, pending_asks=pending, ask_total=total,
            declined_asks=declined, answered_by=answered_by,
            has_resolved_reply=has_resolved, owes_reply=owes_reply,
            sla_minutes=sla_minutes if sla_minutes is not None
            else self.channel_sla(message.channel),
            # Escalation clock exclusion (0069): paused time never ages an
            # obligation toward its SLA, so a resume cannot open onto an
            # escalation storm the pause itself manufactured.
            paused_seconds=self.paused_seconds_since(message.created_at),
            already_read=already_read,
            # Debt-age floor (c3436): a directive debt ages from the later
            # of its post time and the epoch that created the debt class —
            # a semantics change can never make a message born escalated.
            debt_epoch=self._directive_epoch if owes_reply else 0.0,
        )
        # The reporting delegate is the hub-routed owner of operator traffic
        # even when the sender did not type them into `to`. Surfacing that as
        # viewer-specific `to_me` lets listener/hook/runner narrow operator
        # work to the seats that actually owe it, instead of waking bystanders.
        if (message.status in (Status.open, Status.blocked)
                and self._operator_delegate_debt(viewer_id, message)):
            envelope.to_me = True
        # Computed at the one choke point every envelope surface goes through,
        # so notify lines, /inbox and the WS agree.
        envelope.from_operator = message.sender in self.operator_ids()
        asks = asks_of(message)
        if asks:
            envelope.asks_yours = [str(a.get("id")) for a in asks
                                   if viewer_id in (a.get("to") or [])]
            envelope.asks_others = not envelope.asks_yours
        return envelope

    def _linked_claim_sources(self, owner: str, channels: list[str]) -> set[str]:
        """Every live claim source id this seat owns across the given channels.

        Used by `/owed` to distinguish 'I replied' from 'I took the work':
        a bare peer ack does not settle a work ask, but a linked claim row
        does materialize ownership and moves the pressure onto the claim."""
        out: set[str] = set()
        for channel in channels:
            for entry in self.db.store_keys(channel):
                key = entry["key"]
                if not key.startswith("claim:"):
                    continue
                stored = self.db.store_get(channel, key)
                if stored is None or not isinstance(stored.value, dict):
                    continue
                value = self._normalize_claim_source(channel, stored.value)
                row_owner = str(value.get("owner") or stored.updated_by or "")
                if row_owner != owner or self._claim_done(value):
                    continue
                source = str(value.get("source_message_id") or "")
                if source:
                    out.add(source)
        return out

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
            # The `read` rung just moved for whoever asked (0156). Weak on
            # purpose — a driven seat reads its whole inbox by construction
            # — but it is the difference between "never delivered" and
            # "opened it and did not pick it up", and the second is the
            # state the operator has no instrument for today.
            self._publish_pickup_quietly(item)
        return chain

    def retract_message(self, agent: AgentInfo, channel: str,
                        message_id: str) -> Message:
        """Author-only (or operator) retraction (0097): redact the message
        on every agent-facing surface so no agent or entity can ever consume
        its words, and clear any obligation it carried (the stray-message
        phantom-debt case). Anytime — regret has no window. Idempotent.
        The original bytes stay in the row for operator audit and for the
        ledger hash (retraction is presentation, never a chain rewrite)."""
        # Delivery validation/insertion and VFS writes use this same outer
        # lock.  A typed-review retraction changes the live delivery gate, so
        # it must not slip between that validation and the report append.
        # Keep the established lock order (orchestration, then Database).
        with self.db.orchestration_lock:
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
            if message.kind != Kind.message and not agent.operator:
                # A system row is authored by `hub`, so author-retraction can
                # never reach it: without an operator door, a hub notice posted
                # into someone's room is unremovable by anyone, forever. The
                # operator is the one principal who can be trusted to unsay the
                # hub's own words — and had to be, the first time the hub put a
                # notice where a human was holding a conversation.
                raise HubError(403, "only an operator can retract a system/fs "
                                    "event — they are the hub's own words, not "
                                    "yours")
            self.db.retract_message(message_id, agent.id)
            redacted = self.db.get_message(message_id)  # redacted view for the wire
        # Broadcast the retraction so live subscribers redact in place (the
        # tombstone is the payload; the words never ride the wire again).
        self._wake(redacted)
        return redacted

    RESOLVE_THREAD_LIMIT = 200

    def resolve_thread(self, agent: AgentInfo, channel: str,
                       message_id: str, body: str = "") -> dict[str, Any]:
        """Close a WHOLE TRAIL: the named message and every open obligation
        beneath it, in one call.

        WHY IT EXISTS. Resolution was per-MESSAGE — `db.replies_to` walks
        direct children only — so an operator who resolved the root of a task
        thread closed exactly one row. Measured on a live hub (2026-08-22):
        the operator resolved the WebOS commission at 04:58 and three seats
        went on working it for hours, because every obligation between them
        had been minted by REPLIES the resolve never touched, and the work
        itself had moved to a room spawned from that thread.

        AUTHORITY is the single-message closure rule (`_closes`) applied to
        every node, never a weaker one: an operator may close anyone's, the
        asker their own. A non-operator whose trail contains someone else's
        open message is REFUSED OUTRIGHT and nothing is closed — a partial
        close is worse than none, because it reports success over a thread
        that is still alive.

        WHAT IT DOES NOT DO: touch `claim:` rows. It REPORTS the ones whose
        `source_message_id` points into this trail and leaves them to their
        owners — the hub cannot know whether work already done should be
        thrown away, and `store_set` refuses to write another seat's row
        anyway. A verb that returned success while the claims still read
        `active` would be the same lie from the other direction."""
        self.require_membership(channel, agent.id)
        trail = self.db.thread_messages(channel, message_id,
                                        limit=self.RESOLVE_THREAD_LIMIT + 1)
        if not trail:
            raise HubError(404, f"message '{message_id}' not found in '{channel}'")
        overflow = len(trail) > self.RESOLVE_THREAD_LIMIT
        trail = trail[:self.RESOLVE_THREAD_LIMIT]
        live: list[Message] = []
        for m in trail:
            if m.kind != Kind.message or m.retracted:
                continue
            if m.status not in (Status.open, Status.blocked):
                continue
            if self._closed_authoritatively(m, self.db.replies_to(m.id)):
                continue
            live.append(m)
        others = sorted({m.sender for m in live if m.sender != agent.id})
        may_close_anyones = (agent.operator
                             or agent.id in self.ruling_delegate_ids(channel))
        if others and not may_close_anyones:
            raise HubError(
                403,
                "closing a thread is the asker's, an operator's, or a "
                "ruling delegate's act — this "
                f"trail has {len(others)} open message(s) asked by other "
                f"seat(s) ({', '.join(others)}), so NOTHING was closed. Close "
                "your own, or ask an operator or the room's delegate.")
        closed: list[str] = []
        for m in live:
            note = body or ("thread closed: this work is stood down. Stop "
                            "here and post no further replies on it.")
            if is_consultation(m):
                state = self.consultation_state(agent, channel, m.id)
                closed.append(self.conclude_consultation(
                    agent, channel, m.id, "cancelled", state["version"], note).id)
                continue
            reply = PostMessage(body=note, status=Status.resolved,
                                reply_to=m.id, title="thread closed")
            closed.append(self.post_message(agent, channel, reply).id)
        # Every closer is a reply, so each thread participant gets exactly one
        # `reply-to-me` wake — the stand-down notification falls out of the
        # closure and needs no separate broadcast (a `resolved` naming nobody
        # wakes nobody, which is what made the operator's stop order silent).
        return {"channel": channel, "root": message_id,
                "closed": closed, "claims": self._claims_touching(channel, trail),
                "truncated": overflow}

    def _claims_touching(self, channel: str, trail: list[Message]) -> list[dict[str, Any]]:
        """Live claim rows whose `source_message_id` points into this trail.

        REPORTED, never mutated. Matching is a substring test because the
        field is free text in practice: real rows carry bare ids, `webos#85`,
        and prose like "webos#157 / 01M0…, corrected by webos#160". An exact
        match would silently miss most of them, and silence is the failure
        this whole verb exists to fix."""
        needles = {m.id for m in trail} | {f"{channel}#{m.seq}" for m in trail}
        out: list[dict[str, Any]] = []
        for entry in self.db.store_keys(channel):
            key = entry["key"]
            if not key.startswith("claim:"):
                continue
            stored = self.db.store_get(channel, key)
            if stored is None or not isinstance(stored.value, dict):
                continue
            src = str(stored.value.get("source_message_id") or "")
            if not src or not any(n in src for n in needles):
                continue
            status = str(stored.value.get("status") or "")
            if status.split() and status.split()[0].strip(":.,").lower() in (
                    "done", "shipped", "cancelled", "canceled", "abandoned"):
                continue
            out.append({"key": key, "owner": str(stored.value.get("owner") or ""),
                        "status": status[:120]})
        return out

    def _declared_claim_links(
            self, channel: str) -> dict[str, dict[str, tuple[str, str]]]:
        """claim rows in `channel` whose `source_message_id` DECLARES its
        source exactly: {message id: {owner: (claim key, state word)}} (0156).

        Deliberately stricter than `_claims_touching`, and the two are not
        redundant. That one matches by SUBSTRING because the field is free
        text in practice (`webos#85`, bare ids, prose like "webos#157 /
        01M0…, corrected by webos#160") and it feeds a REPORT a human reads
        — a loose match there costs a glance. This feeds a live per-message
        instrument the operator will trust, and both client seats refused
        the substring join for exactly that reason: it will eventually
        attribute one seat's claim to another seat's message, in front of
        them, and do it silently (agora-tui#81, agora-wui#90).

        So a link is DECLARED only when the whole field, stripped, is one
        resolvable ref — a message id or `channel#seq`. Prose does not link
        here. That is a real cost (a row whose source is a sentence shows as
        unclaimed) and it is the honest side of it: the alternative is a
        confident wrong name.

        ONE pass yields both the key and the state word. The first cut of
        this had a second scanner beside it (`_claim_key_for`) repeating the
        same ref-matching to fetch the key — two implementations of one
        match, which is exactly the drift `has_resolved_reply` cost three
        clients this morning, and O(store) per message on top.
        """
        out: dict[str, dict[str, tuple[str, str]]] = {}
        for entry in self.db.store_keys(channel):
            key = entry["key"]
            if not key.startswith("claim:"):
                continue
            stored = self.db.store_get(channel, key)
            if stored is None or not isinstance(stored.value, dict):
                continue
            value = self._normalize_claim_source(channel, stored.value)
            ref = str(value.get("source_message_id") or "").strip()
            if not ref or any(c.isspace() for c in ref):
                continue        # prose, or several refs: not a declaration
            target = self._resolve_consume_ref(ref, channel)
            if target is None or target.channel != channel:
                continue
            owner = str(value.get("owner") or stored.updated_by or "")
            if not owner:
                continue
            state = str(value.get("status") or "").split()
            # The row's OWN first word, re-read every time (agora-tui#81):
            # never "claimed plus an age" for a client to interpret, because
            # `parked` is a declared state that a count-up rail would render
            # as neglect.
            out.setdefault(target.id, {})[owner] = (
                key, state[0].strip(":.,").lower() if state else "")
        return out

    def _pickup_rungs(
            self, m: Message, replies: list[Message],
            links: dict[str, dict[str, tuple[str, str]]] | None = None,
    ) -> list[PickupRung]:
        """How far each addressee of `m` has got (0156). Sender-only; see
        PickupRung for what each rung claims and refuses to claim.

        agora-wui#90 asked that an offline, never-reachable seat show
        NOTHING rather than `delivered` ("`delivered` beside `offline` reads
        as progress when it is the absence of it"). There is no branch for
        that here because there is no `delivered` rung to suppress: delivery
        is true the instant the message is posted, for every member, so it
        carries no information. The constraint is satisfied by the rung set,
        which is the only place a constraint like that stays satisfied.

        `links` is the channel's declared claim index, passed in when a
        CALLER is doing a whole page (one store scan for the page instead of
        one per row) and computed here when nobody has one."""
        seats = [s for s in dict.fromkeys(list(m.to) + sorted(ask_addressees(m)))
                 if s != m.sender]
        if not seats:
            return []
        ds = self._discharge(m, replies)
        if links is None:
            links = self._declared_claim_links(m.channel)
        by_seat = {r.sender: r for r in replies if r.sender != m.sender}
        declined_by = {
            r.sender for r in replies
            if isinstance((r.data or {}).get("declines"), list)
            and (r.data or {}).get("declines")}
        out: list[PickupRung] = []
        for seat in seats:
            presence = self.presence.get(seat).state
            reply = by_seat.get(seat)
            claim = links.get(m.id, {}).get(seat)
            read_at = self.db.read_at(m.id, seat)
            acked = self.db.get_cursor(seat, m.channel) >= m.seq
            rung, since, claim_key, claim_state = "none", 0.0, None, None
            if acked:
                rung, since = "acked", 0.0
            if read_at is not None:
                rung, since = "read", read_at
            if claim is not None:
                rung, (claim_key, claim_state) = "claimed", claim
            if reply is not None:
                rung, since = "replied", reply.created_at
            if seat in declined_by:
                rung = "declined"
            out.append(PickupRung(
                seat=seat, rung=rung, since=since, presence=presence,
                claim_key=claim_key, claim_state=claim_state,
                # From _discharge, never from "a reply arrived": the two come
                # apart on a multi-addressee ask (agora-tui#86).
                still_owes=not self._addressee_released(m, ds, seat),
            ))
        return out

    def _publish_pickup(self, message: Message | None,
                        links: dict[str, dict[str, tuple[str, str]]] | None = None,
                        ) -> None:
        """Push the CURRENT ladder of `message` to the seat that sent it
        (0156) — the half of this feature the operator actually asked for.

        The row alone would leave him where he started: *"we just type a
        message and wait to see if anyone is gonna answer"* describes a
        surface you have to go back and look at. A push is what turns "did
        anyone pick this up" from a question into something that arrives.

        EVERY EVENT CARRIES THE WHOLE LADDER, and no delta form exists —
        agora-tui#81, adopted as C3 and deliberately absolute rather than
        "for now": a client that missed one delta of a count-up stream is
        not stale, it is confidently wrong with no way to find out. The
        cheap optimisation is exactly how that property gets removed later
        by someone who reads only the code.

        Sender-only, like the row: this carries other seats' read receipts.
        Fire-and-forget — a push that could 500 the act that triggered it
        would be worse than no push at all, so every call site wraps this."""
        if message is None or message.retracted:
            return
        rungs = self._pickup_rungs(message, self.db.replies_to(message.id),
                                   links=links)
        if not rungs:
            return
        self.fanout.publish(f"agent/{message.sender}", {
            "type": "pickup",
            "channel": message.channel,
            "message_id": message.id,
            "seq": message.seq,
            "pickup": [r.model_dump() for r in rungs],
        })

    def _publish_pickup_quietly(self, message: Message | None) -> None:
        """`_publish_pickup`, never raising. The ladder is an instrument
        bolted onto acts that must succeed without it (a post, a read, a
        claim write); a fault here is a lost frame the next row read
        repairs, and it must never become a failed post."""
        try:
            self._publish_pickup(message)
        except Exception:
            logging.getLogger("agora.hub.pickup").exception(
                "pickup push failed (the act it followed succeeded)")

    def retract_thread(self, agent: AgentInfo, channel: str,
                       message_id: str) -> dict[str, Any]:
        """Retract a WHOLE TRAIL (0097): the named message and every reply
        beneath it, in one transaction. The single-message verb could already
        do this one call at a time; a thread is the unit a human actually
        regrets, and N calls is N chances to stop halfway — leaving some of
        the words readable, some obligations alive, and no record that the
        caller meant the whole thing to go.

        Authority is the SINGLE-message rule applied to every member of the
        trail, not a new weaker one: an operator may retract anyone's, an
        author only their own. A non-operator whose trail contains someone
        else's message is REFUSED OUTRIGHT (403) and nothing is retracted —
        partial application would leave exactly the noise they asked to be
        rid of while telling them it was handled. Their own solo threads
        still go in one call.

        Scope is the named message and its DESCENDANTS, never its ancestors:
        the blast radius can only be what the caller pointed at (from a root
        row that is the whole thread; from a mid-thread reply, that branch).
        System/fs rows inside the trail are SKIPPED, not fatal — the single
        verb refuses them, and one join notice must not veto a retraction.
        Already-retracted members are counted, not re-stamped. Idempotent;
        the ledger is untouched (presentation, never a chain rewrite)."""
        # A thread can contain a typed-review message.  Serialize the whole
        # target selection and retraction with delivery validation, exactly as
        # the single-message form does.
        with self.db.orchestration_lock:
            self.require_membership(channel, agent.id)
            trail = self.db.thread_messages(channel, message_id)
            if not trail:
                raise HubError(404, f"message '{message_id}' not found in '{channel}'")
            others = sorted({m.sender for m in trail
                             if m.sender != agent.id and m.kind == Kind.message})
            if others and not agent.operator:
                raise HubError(403,
                               "only the author (or an operator) can retract a "
                               f"message — this trail has {len(others)} other "
                               f"author(s) ({', '.join(others)}), so NOTHING was "
                               "retracted. Retract your own messages one by one, "
                               "or ask an operator to retract the thread.")
            targets = [m for m in trail if m.kind == Kind.message]
            skipped = [m.id for m in trail if m.kind != Kind.message]
            already = [m.id for m in targets if m.retracted]
            self.db.retract_messages([m.id for m in targets], agent.id)
            # One transaction above, then one tombstone per message on the wire:
            # every existing consumer (CLI, MCP, web client) already knows how to
            # redact a message in place from its id, and inventing a thread frame
            # would leave each of them silently ignoring it until it shipped.
            redacted = [self.db.get_message(message.id) for message in targets]
        for row in redacted:
            self._wake(row)
        return {"channel": channel, "root": message_id,
                "count": len(redacted),
                "already_retracted": already,
                "skipped_non_messages": skipped,
                "messages": [r.model_dump() for r in redacted]}

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
                if self._closed_authoritatively(message, replies):
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
            ds = self._discharge(message, replies)
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
                        and agent.id not in pending_addressees(message, ds.pending, ds.answered_by)):
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

        - OPERATOR sender, status reply: obliges the named seats and scoped
          reporting delegate. FYI is optional; critical requires reading.
        - PEER sender, status reply: obliges the named seats UNLESS it is
          the sender's answer coming back to you — i.e. it replies to YOUR
          OWN message. Your debt for an answer is CONSUMPTION (0078's
          to_consume), not another reply; this exemption is also what
          terminates ack chains ('thanks' replying to their answer obliges
          them nothing) instead of ping-ponging forever.
        - PEER status fyi: never obliges by itself. Addressing or tagging a
          peer fyi affects visibility, not debt; if the sender wants a
          guaranteed answer, it must not be fyi.
        - A reply carrying `answers` never obliges (both classes): it
          discharges an ask; the asker's debt is consumption.

        The viewer engaging (any reply of theirs to it) clears it, via the
        same per-addressee discharge as any addressed binary obligation."""
        if (m.kind != Kind.message or m.retracted
                or m.status not in (Status.reply, Status.fyi)):
            return False
        if m.sender == viewer_id or (m.status == Status.fyi and (not m.critical or self.db.has_read(m.id, viewer_id))):
            return False
        if (viewer_id not in m.to and m.critical
                and not self._operator_delegate_debt(viewer_id, m)
                and self.db.has_read(m.id, viewer_id)):
            # A critical BROADCAST is everyone's debt only until it is READ
            # (ADR-0006 §4): the row is the second delivery path for a seat
            # that missed the notify line mid-turn, not a reply obligation.
            # `unread_criticals` keeps the inbox pin's own read-based contract.
            return False
        if (viewer_id not in m.to and not m.critical
                and not self._operator_delegate_debt(viewer_id, m)):
            # Unaddressed reply/fyi obliges nobody — EXCEPT the reporting
            # delegate on an operator line (ruling 2026-08-01): see
            # _operator_delegate_debt for the to=[] hole this closes.
            return False
        if (m.data or {}).get("answers"):
            return False  # an answer, not a directive
        # Epoch bound (c3379, generalized c3436 by operator ruling dm#42):
        # a debt can never be OLDER THAN THE RULE THAT CREATED IT. A message
        # posted before this hub learned the directive-debt semantics
        # predates the class and must not become a debt retroactively —
        # for EVERY sender, operator included. The unbounded-operator
        # carve-out (0.12.20) was exactly what resurfaced weeks-old and
        # forged operator DMs the morning after the feature shipped
        # ("no more surfacing old requests already emitted and treated").
        # A pre-epoch directive that still matters is RE-EMITTED (the
        # operator's own verb) — it lands post-epoch and obliges cleanly.
        if m.created_at < self._directive_epoch:
            return False
        if m.status == Status.fyi and isinstance(m.data, dict) and (
                m.data.get("invite_token") or m.data.get("channel_invite")):
            # AN INVITE IS NOT A DIRECTIVE (2026-09-05). Room invites ride
            # as addressed fyi DMs carrying the token; when the inviter is an
            # operator every invitee owed a reply for it, and the record
            # shows what that bought: replies whose whole content was
            # "joined" (B round 2: 64 ceremony replies on 339 operator
            # addressed fyi/reply rows, nearly all invite notes). Joining
            # the room is the act; the DM obliges nothing.
            return False
        if m.sender in self.operator_ids():
            return True
        if m.status == Status.fyi:
            return False
        # A PEER'S ADDRESSED REPLY STILL OBLIGES. This clause was extended to
        # return False for peers on 2026-08-25 (claim:reply-cannot-renounce-
        # its-debt (c)) on the rationale that `reply` should "agree with `fyi`
        # three lines above it — an inconsistency removed". REVERTED the same
        # day, because the premise is false and the docs say so:
        #
        # 0102 did not leave an inconsistency here, it DREW this exact line.
        # "Peer sender, `reply`: obliges the named seats" and "peer `fyi`:
        # never obliges — THE TERMINAL GESTURE" are a designed pair; `fyi` is
        # the exit precisely BECAUSE `reply` is not. Extending fyi's rule to
        # reply does not harmonise them, it deletes the class: after it, no
        # peer message of any status can oblige anyone. That is the state the
        # operator overturned on 2026-07-19 — a seat ignored addressed
        # replies "because a reply is not mandatory", and the ruling was
        # "it MUST be". A delegate decision cannot reverse an operator
        # ruling, and this one did not survive reading the item it undid.
        #
        # THE MEASUREMENT BEHIND IT IS REAL AND IS NOT LOST: ~20 rows in one
        # afternoon whose senders wanted no answer, and clearing that does
        # not batch. It is a good case for laurent to change the ruling. It
        # is not authority to change it here. Kept on the claim row.
        #
        # THE DOORBELL CAME BACK INVERTED (2026-08-25) and is now the reason
        # the measurement may not need a ruling at all. It used to say "that
        # reply obliged nobody" and was disabled with (c). What it says now is
        # what this function does — the reply OBLIGED the named seats — plus
        # the door: `fyi` + `reply_to`, which 0102 blessed on the very line
        # above as the terminal gesture. agora-and-wui#724: 8 of agora-wui's
        # 12 reply-debts end with the SENDER disclaiming a reply in prose.
        # Those senders wanted `fyi` and typed `reply`. If telling them at
        # post time drains that pool, the case for deleting the class is a
        # case about discoverability instead — see
        # `_reply_obliges_someone_nudge`.
        if m.status != Status.reply:
            return False
        parent = self.db.get_message(m.reply_to) if m.reply_to else None
        return not (parent is not None and parent.sender == viewer_id)

    def ruling_delegate_ids(self, channel: str) -> frozenset[str]:
        """Seats whose ruling/operational grant REACHES this channel.

        Closure authority for a delegate is scoped exactly like every other
        act it may perform here: an unscoped grant reaches nothing, and a
        grant scoped elsewhere does not leak in."""
        out = set()
        for d in self.active_delegations():
            scope = str(d.get("scope") or "").strip()
            if scope not in ("*", channel):
                continue
            if set(str(p) for p in (d.get("powers") or ())) & self.CHANNEL_AUTHORITY_POWERS:
                out.add(str(d["agent_id"]))
        return frozenset(out)

    def _discharge(self, m: Message, replies: list[Message]) -> DischargeState:
        """THE discharge call. Every surface goes through here so operator
        and delegate authority can never be computed one way for /owed and
        another for the envelope — the class of drift that let at-test#382
        read as closed on one surface while its work was still undone."""
        return discharge_state(m, replies, self.operator_ids(),
                               self.reporting_delegate_ids(),
                               self._operator_rule_epoch,
                               self._operator_asks_rule_epoch,
                               self._canvass_rule_epoch,
                               self._peer_addressed_rule_epoch,
                               self._closure_rule_epoch,
                               self.ruling_delegate_ids(m.channel),
                               open_phases=self._open_phases_cached(m.channel))

    @property
    def _phase_memo(self) -> dict[str, tuple[float, frozenset[str]]]:
        # Per INSTANCE (lazily): a class-level dict leaked one app's "no open
        # phases" into the next app's `work` channel within the same second.
        return self.__dict__.setdefault("_phase_memo_inst", {})

    def _open_phases_cached(self, channel: str) -> frozenset[str]:
        """Every phase this channel has EVER opened (each track's hub-kept
        `opened` list, plus the current open one), memoised for one second
        and dropped on any `phase:` write: `_discharge` runs per message per
        reader in `owed()`, and a phase changes by declaration, not per
        request."""
        now = time.time()
        hit = self._phase_memo.get(channel)
        if hit and now - hit[0] < 1.0:
            return hit[1]
        opened = frozenset(
            ph for row in self.phase_rows(channel)
            for ph in ([row["current"]] if row["status"] == "open" and row["current"] else [])
                      + list(row.get("opened") or []))
        self._phase_memo[channel] = (now, opened)
        return opened

    def _addressee_released(self, m: Message, ds: DischargeState,
                            agent_id: str) -> bool:
        """Has an addressee ENGAGED and been left with no pending ask of
        their own? `/owed` has applied this since 2026-08-11 ("keeping the
        row told one seat two things at once"); `board` did not, so the two
        surfaces answered the same question differently.

        Not a new rule and deliberately not a looser one: an ask-less
        commission still pins every addressee (the 75-second-discharge
        protection), an UNADDRESSED pending ask still pins everyone it could
        name, and an operator's request still needs its cited completion
        report."""
        if not asks_of(m):
            return False              # ask-less: only a real settlement clears
        if agent_id in pending_addressees(m, ds.pending, ds.answered_by):
            return False              # a pending ask still names them
        pending = set(ds.pending)
        if any(not (a.get("to") or a.get("assignee"))
               for a in asks_of(m) if str(a.get("id")) in pending):
            return False              # an ask naming nobody is everyone's
        if self._operator_delegate_debt(agent_id, m):
            return False
        return any(r.sender == agent_id for r in self.db.replies_to(m.id))

    def _answered_operator_request(self, m: Message, replies: list[Message],
                                   agent_id: str) -> bool:
        """Has this seat REPLIED to an operator's ask-less request without
        closing it? A reason, never a release.

        WHAT THIS IS NOT, because it was that for about forty minutes
        (agora-and-wui#700, ruled at #703). It was built as an escalation
        VALVE, on the premise that the row's only exit is the operator's own
        word — which the answering seat cannot speak — so the alarm rang at
        the one party who could not silence it. THE PREMISE WAS FALSE.
        `obligations._operator_settled` also closes on a `resolved` reply
        citing evidence from `r.sender in delegates or r.sender in named`,
        and `named = set(parent.to) | ask_addressees(parent)`. The named seat
        has a door. It was added deliberately for this exact defect and it is
        tested twice in test_obligation_reason.py. I built the valve out of
        this file alone and never read the one that decides when the row
        CLOSES.

        The surviving case was going to be "an answer with nothing to cite —
        an opinion, not a deliverable". Delegate refuted that too: AN OPINION
        BECOMES CITABLE BY BEING RECORDED. `kind: "store"` takes a
        `decision:`/`finding:` row written for the purpose, and they produced
        two live receipts (commons#491, hub-alerts#210) that cite a row and
        nothing else, both `verified: true`. One ordering trap worth knowing
        and invisible until the post is refused: `_validate_evidence`
        resolves against THE CHANNEL YOU ARE POSTING IN, so the row must live
        there.

        So: no suppression, in any scope. The row keeps escalating, because
        the pressure has somewhere to go. What this predicate buys is that
        `/owed` can say WHICH exit is left — you replied, and a second reply
        will not help; cite what you delivered — instead of printing the
        same sentence at a seat that has already spoken.

        `asks_of` guard: a structured request is not this case. An ask has
        its own exit, and answering one ask never speaks for the rest."""
        if m.status not in (Status.open, Status.blocked):
            return False
        if m.sender not in self.operator_ids():
            return False
        if asks_of(m):
            return False
        if not (agent_id in m.to or self._operator_delegate_debt(agent_id, m)):
            return False
        # THIS seat's own reply, never any reply: another addressee engaging
        # is exactly the free-rider hole the per-addressee discharge exists
        # to close, and it would let one seat silence five seats' alarms.
        return any(r.sender == agent_id for r in replies)

    def _closes_one(self, m: Message, reply: Message) -> bool:
        """Did THIS reply carry the closure authority? `_closed_authoritatively`
        answers "did anyone"; naming who is what lets a reader see whether a
        thread was ruled shut or simply answered in full."""
        return _closes(m, reply, self.operator_ids(),
                       self.reporting_delegate_ids(),
                       self._closure_rule_epoch,
                       self.ruling_delegate_ids(m.channel))

    def _may_close(self, m: Message, viewer_id: str) -> str:
        """May `viewer_id` close `m`, and on what terms? "yes" / "no" /
        "with_evidence" (thread-shape-and-panels#189, agora-tui).

        `closed` answers *is this thread settled*; this answers *may I settle
        it*, which no surface served. Both clients were about to derive it
        from delegation scopes read once at load — a client-side authority
        verdict, from a grant the hub can narrow or revoke afterwards, with
        no oracle to catch the drift until an operator hits a refusal. The
        quiet direction is the dangerous one: a resolve verb HIDDEN from a
        seat the hub would have allowed suppresses a real act, and nobody
        ever sees the error.

        IT RUNS THE REAL LADDER rather than restating today's exits —
        `resolved_settles_nothing`'s discipline, for the same reason. The
        authority rules in `_closes` move (they moved twice this month), and
        a spelled-out copy here would answer for last week's hub while
        `post_message` enforced this week's. A prospective `resolved` from
        the viewer is fed to the same predicate the write path uses, so this
        field cannot disagree with the refusal it is meant to predict.
        """
        def _asks(data: dict[str, Any] | None) -> Message:
            return m.model_copy(update={
                "id": "", "sender": viewer_id, "status": Status.resolved,
                "reply_to": m.id, "created_at": time.time(), "data": data})

        if self._closes_one(m, _asks(None)):
            return "yes"
        # The reporting delegate's door on an OPERATOR's request is real but
        # PRICED: `settled_by` plus cited evidence. Answering "no" would hide
        # the one exit that seat has; answering "yes" would offer a bare verb
        # the hub then refuses as settling nothing. So it is its own word.
        # The probe carries a VERIFIED-shaped ref on purpose: that is what
        # `_validate_evidence` stamps for a citation it resolved, and an
        # all-`external` report (stamped `verified: false`) does NOT satisfy
        # the gate. Probing with the weaker shape would answer for a report
        # the hub would refuse.
        if self._closes_one(m, _asks({"settled_by": m.id, "evidence": [
                {"kind": "store", "ref": "probe", "verified": True}]})):
            return "with_evidence"
        return "no"

    def _closed_authoritatively(self, m: Message,
                                replies: list[Message]) -> bool:
        """THE closure call, for the same reason `_discharge` is THE discharge
        call: closure authority computed one way for `/owed` and another for a
        sweep is how a thread reads settled on one surface while its work is
        still undone. Fourteen call sites used to pass `operator_ids()` alone,
        which silently meant "no delegates, no epoch"."""
        return closed_authoritatively(m, replies, self.operator_ids(),
                                      self.reporting_delegate_ids(),
                                      self._closure_rule_epoch,
                                      self.ruling_delegate_ids(m.channel))

    def reporting_delegate_ids(self) -> frozenset[str]:
        """Seats holding an active `reporting` delegation — the fleet's
        routing point for operator traffic (operator ruling, 2026-08-01:
        "reader IS the delegate ... he is the one with the responsibility
        making sure a request is done end to end")."""
        return frozenset(d["agent_id"] for d in self.active_delegations()
                         if "reporting" in (d.get("powers") or ()))

    def _operator_delegate_debt(self, viewer_id: str, m: Message) -> bool:
        """Does this OPERATOR message oblige `viewer_id` as the reporting
        delegate? (operator ruling, 2026-08-01.)

        THE HOLE THIS CLOSES. On 2026-08-01 the operator posted the task as
        `status=reply, to=[]`. Every obligation surface let it through:
        open_obligations covers only open/blocked, `_is_addressed_debt`
        requires the viewer in `m.to`, and the sender-facing doorbell is
        gated on open/blocked. The message therefore created ZERO
        obligations fleet-wide — nobody owed it, nothing escalated, and the
        deliverable was never built. A human's request to their fleet must
        land on someone by construction.

        DELIBERATELY NOT oblige-all-members: that is the wake-storm shape
        0.12.55 killed, and re-creating it here would trade a silent failure
        for a loud one. The delegate is the single routing point, which is
        precisely what the ruling makes them responsible for. Addressed
        operator messages keep obliging their named seats as well — this
        predicate only ADDS the delegate, it never removes an addressee.

        Every other guard the directive class already earned still applies:
        the pre-epoch bound (a debt is never older than the rule that made
        it), retractions, answers-carrying replies, and the delegate's own
        posts."""
        if m.kind != Kind.message or m.retracted:
            return False
        if m.sender == viewer_id:
            return False
        if m.created_at < self._directive_epoch:
            return False
        if (m.data or {}).get("answers"):
            return False  # an answer, not a request
        if m.sender not in self.operator_ids():
            return False
        return m.status != Status.fyi and self._delegation_reaches(viewer_id, m.channel, ("reporting",))

    def _addressed_debts(self, agent_id: str,
                         channels: list[str]) -> list[Message]:
        """Every reply/fyi debt the viewer owes across these channels — the
        candidate feed `owed` and the inbox pin merge with open/blocked
        obligations (0102). Engagement/closure filtering stays with the
        callers, identical to any other obligation."""
        candidates = self.db.addressed_directives(channels)
        # The reporting delegate additionally owes the operator's UNaddressed
        # reply/fyi lines (ruling 2026-08-01). Queried only for delegates and
        # only for operator senders, so the ping-pong the addressed rule
        # prevents stays prevented for everyone else.
        ops = sorted(self.operator_ids())
        if agent_id in self.reporting_delegate_ids():
            candidates = candidates + self.db.unaddressed_directives(channels, ops)
        else:
            # A CRITICAL broadcast is everyone's debt: it needs a second
            # delivery path (the owed signature) beside the notify line,
            # which a seat mid-turn can miss (cycle-1 A4, 2026-09-09).
            candidates = candidates + [
                m for m in self.db.unaddressed_directives(channels, ops)
                if m.critical]
        return [m for m in candidates if self._is_addressed_debt(agent_id, m)]

    #: THE CAPABILITY BESIDE THE NAME (reason-enum-and-unknown-values#7 ask
    #: 2). One mapping, keyed by the same value the ladder below assigns, so
    #: the two cannot drift: adding a reason without a row here raises on the
    #: first request rather than serving a silent `[]`. Members are acts the
    #: OWED SEAT performs — see ObligationRow.clears_on for the vocabulary
    #: and the null-vs-empty contract.
    #:
    #: Both consumers ran a census against this table before it was built:
    #: agora-wui across four call sites (#15) and agora-tui across every verb
    #: their card header can offer (#14). Sufficient in both, and the two
    #: gaps agora-tui found were in their own rail.
    _REASON_CLEARS_ON: dict[str, tuple[str, ...]] = {
        # Numbered asks that are yours: answer them, or refuse them on the
        # record. Both discharge; only these rows have ids to name, which is
        # why `decline` appears here and nowhere else.
        "asks_pending": ("answer", "decline"),
        # A directive debt. The hub's own owed block says it: any reply.
        "names_you": ("reply",),
        # A PEER's ask-less work ask. A bare reply settles nothing — the
        # 2026-08-11 ruling — and what moves it is ownership: a claim row
        # citing the message. (An authoritative close also ends it and is
        # deliberately absent: not an act this seat performs.)
        "peer_request_no_asks": ("claim",),
        # Only the operator's own word, or a cited report, clears these two.
        # They are separate VALUES for their sentence, not their exit: after
        # you have replied, "reply again" is the one move that cannot help —
        # nuance the NAME carries, behaviour the acts carry.
        "operator_request_awaiting_your_report": ("evidence",),
        "operator_request_awaiting_your_citation": ("evidence",),
        # A machine-routed alert. The real exit is the CONDITION, and the
        # hub closes its own alert on the next sweep once it is gone — but
        # the act that clears YOUR ledger row is a reply (discharge_state's
        # system branch, `any(r.sender in parent.to)`). This field answers
        # "what clears this row", so it says `reply`; that the reply is
        # bookkeeping rather than delivery is what the reason NAME is for.
        "hub_alert_fix_the_condition": ("reply",),
    }

    def _seat_is_blocked(self, agent_id: str, channels: list[str]) -> bool:
        """True while one of the seat's own live `claim:` rows says `blocked`.
        The seat said it cannot proceed; an answer to its ask is then due,
        not ambient mail (reviewer Round 2, Q1a). Bounded: rows are read
        once per /owed, and the state clears when the claim moves on."""
        for ch in channels:
            try:
                keys = self.db.store_keys(ch)
            except Exception:
                continue
            for row in keys:
                key = str(row.get("key") or "")
                if not key.startswith("claim:"):
                    continue
                entry = self.db.store_get(ch, key)
                value = entry.value if entry is not None else None
                if not isinstance(value, dict) or value.get("owner") != agent_id:
                    continue
                if str(value.get("status") or "").strip().lower().startswith("blocked"):
                    return True
        return False

    def owed(self, agent: AgentInfo) -> OwedReport:
        """The agent's outstanding debts (0079), read receipts deliberately
        IGNORED: read-but-unanswered is precisely the lurk the receipt filter
        would hide. Returns the TYPED OwedReport (parity move 1, agora-0118):
        the served OpenAPI states this exact shape, so generated clients
        replace hand-kept ones. Two ledgers:

        - `to_answer`: open/blocked messages addressed to the agent — via
          message `to`, an advisory assignee, or a still-pending per-ask `to`
          (0077) — that are not closed and that the agent has not yet
          discharged. A peer's bare reply does NOT drop the row by itself:
          quick answers clear it, authoritative closure clears it everywhere,
          and a non-final work ack clears it only once a linked claim row
          exists.
        - `to_consume` (0078): answers other seats posted to the agent's OWN
          open questions that the agent has neither read (receipt) nor
          followed in-thread (any later post of theirs) — the mechanical
          form of "someone answered you; use it or close it". Clears on
          read_message of the answer, on any later in-thread post by the
          asker, or on authoritative closure. Never escalates, never wakes
          by itself: it surfaces here, in check_inbox, and on the board.
        - `to_close` (0116): the agent's OWN open/blocked threads that are
          fully discharged (every ask answered or DECLINED, or a binary reply
          received) but not authoritatively closed — advisory hygiene only;
          never wakes or escalates. Surfaces after to_answer/to_consume.
          A row naming `declined_asks` is the asker's durable record that
          nobody answered: repost it or close it, but do not read it as done.
        """
        channels = self.db.channels_of(agent.id)
        ops = self.operator_ids()
        now = time.time()
        sla_cache: dict[str, float] = {}
        claim_source_cache: dict[str, set[str]] = {}
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
                if self._closed_authoritatively(m, replies):
                    continue
                if any(r.sender == agent.id for r in replies):
                    continue
                if m.channel not in sla_cache:
                    sla_cache[m.channel] = self.channel_sla(m.channel)
                # Age from max(created_at, epoch) (c3436): a debt cannot be
                # older than the rule that created it, so a message newly
                # classified as a directive by a semantics change is not
                # born SLA-breached. No-op today (all directive debts are
                # post-epoch since c3436) — the durable invariant that
                # stops the NEXT semantics change repeating the storm.
                born = max(m.created_at, self._directive_epoch)
                age = now - born - self.paused_seconds_since(born)
                to_answer.append(ObligationRow(
                    channel=m.channel, id=m.id, seq=m.seq,
                    sender=m.sender, title=m.title,
                    # This whole branch IS case 2 (0155): an addressed
                    # reply/fyi carrying no asks. Any reply from this seat
                    # clears it — which is exactly what the client could not
                    # say, because the row looked identical to the operator
                    # case that a reply cannot clear.
                    reason="names_you",
                    clears_on=list(self._REASON_CLEARS_ON["names_you"]),
                    owed=True,
                    # (d): an operator's addressed fyi is OWED — and not
                    # DUE. It reaches the seat in its next check_inbox; it
                    # does not justify spawning that turn. This narrows the
                    # 2026-07-19 ruling ("humans are sloppy about status")
                    # by the operator's own 2026-09-08 criterion: an fyi is
                    # the author saying "can wait". `critical` still rings.
                    due=(m.status != Status.fyi
                         or bool(m.critical)),
                    created_at=m.created_at,
                    escalated=age > sla_cache[m.channel] * 60.0,
                ))
                continue
            ds = self._discharge(m, replies)
            if ds.closed:
                continue
            # `named_pending` covers per-ask `to` AND `assignee`, scoped to
            # asks STILL PENDING. An unscoped assignee term used to sit
            # beside it and kept the row after the assigned ask was answered
            # — while the SAME message's envelope said `to_me=false` and
            # `asks_naming_you=[]`. The hub told one seat two things at once,
            # and the louder one (an escalating /owed row that the watchdogs
            # read) named it in AGENT DARK for a discharged ask. Deleted:
            # per-ask scoping is the rule everywhere else on this path.
            named_pending = pending_addressees(m, ds.pending, ds.answered_by)
            if not (agent.id in m.to
                    or agent.id in named_pending
                    # An operator's open/blocked that names nobody still
                    # lands on the reporting delegate (ruling 2026-08-01):
                    # at-test#382 was a broadcast open carrying five
                    # requirements and it obliged no single seat.
                    or self._operator_delegate_debt(agent.id, m)):
                continue
            deferred_named = {seat for a in asks_of(m)
                              if str(a.get("id")) in set(ds.deferred)
                              for seat in (a.get("to") or [])}
            if (asks_of(m) and agent.id not in named_pending
                    and agent.id not in deferred_named
                    and not self._operator_delegate_debt(agent.id, m)):
                # ADR-0006, whether or not this seat has replied (20-seat run,
                # 2026-09-09): a STRUCTURED message's asks name who ANSWERS;
                # its `to` names who READS. A manager's open to nine seats
                # with one ask addressed to `gateway` pinned the other eight
                # with `names_you` and a manager spent a turn telling a seat
                # it owed nothing. The reporting delegate alone carries an
                # operator's commission to its evidence-cited report.
                continue
            if any(r.sender == agent.id for r in replies):
                # Engaged: the remaining pending asks are other seats' —
                # EXCEPT an ask-less operator message that lands on you (by
                # `to` or as reporting delegate). There is no "other seat"
                # to carry the remainder of a binary commission, so your
                # planning ack must not silence it: only operator engagement
                # or, for the reporting delegate, an evidence-cited
                # `resolved` completion report clears it
                # (2026-08-04; the delegate's 62-second ack erased the novel
                # commission from its own ledger and nothing chased the
                # 17.5h stall that followed).
                # ...and the SAME is true when the commission carried asks
                # (2026-08-06). `not asks_of(m)` here was the companion of
                # the discharge hole above: even once answering the
                # questions stopped settling the commission, the delegate's
                # first "on it" would have re-erased the row from its own
                # ledger. Measured: rtype-open#10 had three kickoff asks and
                # its delegate never replied to the thread at all — but one
                # ack was all it would have taken.
                # A STRUCTURED message releases an addressee who has engaged
                # and has no ask left naming them (2026-08-11, fund1): their
                # per-ask row was the whole of their debt, and the envelope
                # already tells them `asks_naming_you=[]` — keeping the /owed
                # row told one seat two things at once, and the louder one
                # re-woke it forever. The reporting delegate alone carries the
                # commission itself to its evidence-cited completion report;
                # an ask-less commission still pins every addressee exactly as
                # before (the 75-second-discharge protection).
                #
                # WHOEVER SENT IT (0149). This release was nested inside the
                # operator test below, so it never fired for the shape a
                # driven fleet actually runs: a DELEGATE fans work out as ONE
                # message carrying one addressed ask per seat, and every seat
                # that answered its own ask kept this row until the SLOWEST
                # seat answered theirs.
                #
                # An UNADDRESSED pending ask still pins EVERY addressee.
                # `pending_addressees` reads an ask with an empty `to` as
                # naming nobody, so without this clause a bare "noted" on a
                # `to=[...]` open whose single ask is unanswered would clear
                # the row for all of them — the partial-answer rot the asks
                # feature exists to prevent. The driver's
                # `_message_pending_asks` takes the same reading ("an ask
                # addressed to nobody is everyone's"), so hub and driver now
                # agree instead of contradicting each other.
                # ADR-0006: an ask names its seats (given, derived from a
                # leading `seat:`, or inherited from the message `to`); an
                # ask that names nobody obliges nobody. The former
                # "addressed to nobody is everyone's" clause is gone.
                if (asks_of(m)
                        and agent.id not in named_pending
                        and not self._operator_delegate_debt(agent.id, m)):
                    continue
                if not (m.sender in ops
                        and (agent.id in m.to
                             or self._operator_delegate_debt(agent.id, m))):
                    if agent.id not in claim_source_cache:
                        claim_source_cache[agent.id] = self._linked_claim_sources(
                            agent.id, channels)
                    # A claim cites its source in either form — the message
                    # id or the human-readable "channel#seq" every doc and
                    # digest uses. Matching only the id left the excusal
                    # dead for the form models actually write (fund4).
                    if (m.id in claim_source_cache[agent.id]
                            or f"{m.channel}#{m.seq}"
                            in claim_source_cache[agent.id]):
                        continue
            if m.channel not in sla_cache:
                sla_cache[m.channel] = self.channel_sla(m.channel)
            age = now - m.created_at - self.paused_seconds_since(m.created_at)
            # WHY this row is here (0155). Everything below is read off the
            # same facts the branches above already established — the point
            # is that the CLIENT could not read them: `pending_asks` and
            # `asks_naming_you` both empty left it printing "needs reply"
            # with no question, which the operator reasonably called a bug.
            # YOURS by name, or unaddressed and therefore everyone's. The
            # first cut read `if ds.pending` — ANY pending ask in the
            # thread — and that is a different question. Caught live within
            # the hour, on this hub, by two seats in the same afternoon:
            # agora-and-wui#117 carried one ask addressed to `laurent` and
            # named `agora` in `to`, so agora's row said `asks_pending`
            # with `['1']` beside it, and the hub then REFUSED agora's
            # decline of ask 1 — "you may not discharge ask ids not
            # addressed to you". The row named an exit the same hub forbids.
            #
            # agora-tui hit it from the other side at #122 and hedged it as
            # one unverified instance; it is not a hedge, it is this line.
            # The seat's real exit here is `names_you` — any reply from
            # them — and the pending ids belong to someone else.
            # Reason only. It deliberately does NOT feed the escalated term
            # below: ruled at agora-and-wui#703 — the exit is reachable
            # (a `resolved` citing evidence), so the pressure stays on.
            answered_op = self._answered_operator_request(m, replies, agent.id)
            mine_pending = [
                a for a in asks_of(m)
                if str(a.get("id")) in set(ds.pending)
                and agent.id in (a.get("to") or [])]          # ADR-0006
            if mine_pending:
                reason = "asks_pending"
            elif (asks_of(m) and m.status in (Status.open, Status.blocked)
                  and not self._operator_delegate_debt(agent.id, m)):
                # STRUCTURED, and none of it is yours. The value tracks the
                # EXIT, not the message class — that is the whole reason the
                # two ask-less cases were split — and the exit here is a
                # reply: the structured release above drops this row for an
                # addressee who engages and has no ask left naming them.
                # Calling it `peer_request_no_asks` would print "claim it or
                # decline it" on a row a reply genuinely clears, which is
                # the wrong-affordance cost inverted. The reporting delegate
                # is excluded because the release excludes them: they carry
                # an operator's commission to its evidence-cited report.
                reason = "names_you"
            elif m.sender in ops:
                # Only the operator's word, or a resolved reply citing
                # evidence, clears this one. The value says so because a
                # client offering "reply" here offers a verb that cannot
                # discharge the row (agora-tui#60).
                #
                # ...and once the seat HAS replied, saying "reply" again is
                # the one thing that cannot help. Same exit, sharper
                # sentence: see _answered_operator_request.
                reason = ("operator_request_awaiting_your_citation"
                          if answered_op
                          else "operator_request_awaiting_your_report")
            elif m.kind == Kind.system:
                # NO `and m.to` HERE, and it was written then removed. It
                # killed no mutant, and unlike the `asks_of` guard on
                # `_answered_operator_request` I could find no live path for
                # it: reaching this ladder already requires `agent.id in
                # m.to` (or an ask naming them, which lands on
                # `asks_pending` above, or operator-delegate debt, which a
                # `hub` sender never creates). So the addressing is an
                # invariant established ~180 lines up, not a condition — and
                # it is pinned by test_an_UNADDRESSED_hub_alert_is_not_this_case,
                # which asserts the upstream filter drops the row entirely.
                # Guard the invariant where it lives, once.
                #
                # A MACHINE-ROUTED ALERT IS NOT A PEER'S WORK ASK, and until
                # now it was served as one. `hub` is not an operator, so a
                # CLAIMS DUE / YOU ARE THE BLOCKER / AGENT DARK / STALE
                # CLAIMS row fell through to `peer_request_no_asks` — whose
                # documented and rendered exit is "a bare reply does NOT
                # clear it; materialize a claim row citing this message, or
                # post `resolved` citing evidence".
                #
                # Every clause of that is wrong here. `discharge_state`'s
                # system branch (obligations.py, "THE HUB CANNOT SPEAK FOR
                # ITSELF") clears these on `any(r.sender in parent.to)` — a
                # bare reply from the addressee is exactly what works. And
                # the advice it displaced is absurd on its face: a claim row
                # citing the ping that asked you to touch your claim rows.
                #
                # Measured on this hub 2026-08-25: commons#531 told its
                # addressee to materialize a claim row; a bare reply cleared
                # the row in the harness on the first try. The reason and
                # the discharge rule disagreed about the same message.
                #
                # What the value says instead is what the alert is FOR: fix
                # the condition. The hub closes its own alerts when that
                # condition ends (`_standing_claim_pings`,
                # `_standing_hub_alerts`), so replying is optional
                # bookkeeping — it clears your ledger row and nothing reads
                # it. That is the opposite of a peer request, where the
                # reply is the point.
                reason = "hub_alert_fix_the_condition"
            elif m.status in (Status.open, Status.blocked):
                reason = "peer_request_no_asks"
            else:
                reason = "names_you"
            # OWED vs DUE (ObligationRow.due). Every row on this path names
            # this seat — `peer_request_no_asks` is minted ONLY for a seat in
            # message-level `to` (the gate above), and a peer's fyi never
            # reaches here — so every row is due EXCEPT a watchdog alert in
            # the operator's own room: AGENT DARK/DEAF/LURKING are status
            # for a human, not a task for the seat they name. A hub alert
            # addressed into a work channel (YOU ARE THE BLOCKER, CLAIMS
            # DUE) IS the seat's own work and stays due. First cut (same
            # day) also excluded `peer_request_no_asks`, misreading it as
            # "names nobody"; the independent review caught it.
            due = not (reason == "hub_alert_fix_the_condition"
                       and m.channel == self.DARK_ALERTS_CHANNEL)
            # READER-SCOPED: a pending ask that names OTHER seats is not this
            # reader's business. The message-global list told every seat it
            # owed 16-18 asks in the fleet run (most another seat's) and told
            # the companion its own asks were "another seat's". An ask that
            # names nobody stays everyone's (the anti-partial-rot reading).
            _to_of = {str(a.get("id")): [str(x) for x in (a.get("to") or [])]
                      for a in asks_of(m)}
            mine_or_open = [aid for aid in ds.pending
                            if agent.id in _to_of.get(aid, [])]     # ADR-0006
            to_answer.append(ObligationRow(
                channel=m.channel, id=m.id, seq=m.seq,
                sender=m.sender, title=m.title,
                pending_asks=mine_or_open,
                asks_naming_you=sorted(
                    str(a["id"]) for a in asks_of(m)
                    if agent.id in (a.get("to") or []) and str(a["id"]) in ds.pending),
                reason=reason,
                due=due,
                # Indexed, never `.get(reason, [])`: a reason with no
                # mapping must blow up in a test, not serve `[]` — which
                # is the strongest claim on the wire ("nothing you can do")
                # and would be a lie told by an omission.
                clears_on=list(self._REASON_CLEARS_ON[reason]),
                owed=True,
                created_at=m.created_at,
                escalated=age > sla_cache[m.channel] * 60.0,
            ))
        to_consume: list[ConsumeRow] = []
        waiting_on: list[WaitingRow] = []
        cursor_cache: dict[tuple[str, str], int] = {}
        blocked = self._seat_is_blocked(agent.id, channels)
        for m in self.db.my_open_messages(agent.id, channels):
            replies = self.db.replies_to(m.id)
            if self._closed_authoritatively(m, replies):
                continue
            structured = bool(asks_of(m))
            for r in replies:
                if r.sender == agent.id:
                    continue
                # A REFUSAL IS TERMINAL (0153): there is nothing in it to
                # adopt or reject, so a decline owes the asker no consumption
                # — pointing them at it would be asking them to consume a
                # non-answer. A reply that answers one ask and declines
                # another still owes consumption for the half it answered.
                answers = substantive_answers_of(r)
                if structured and not answers:
                    continue  # commentary or a refusal, not an answer
                consumed = (self.db.has_read(r.id, agent.id)
                            or any(x.sender == agent.id and x.seq > r.seq
                                   for x in replies))
                if not consumed:
                    to_consume.append(ConsumeRow(
                        channel=m.channel, id=m.id, seq=m.seq,
                        title=m.title, your_asks=[str(x) for x in answers],
                        answered_by=r.sender, answer_id=r.id,
                        answer_seq=r.seq,
                        answer_created_at=r.created_at,
                        due=blocked,
                    ))
            # waiting_on (asker side of the debrief): per still-pending ask
            # addressee, has the hub SERVED them past your question? "acked
            # past, no reply" and "not yet served" are different waits — one
            # is a nudge candidate, the other is an offline seat; seats spent
            # real turns inferring this from presence, which the hub knew.
            ds = self._discharge(m, replies)
            if ds.closed:
                continue
            repliers = {r.sender for r in replies}
            for a in asks_of(m):
                if str(a["id"]) not in ds.pending:
                    continue
                # An ask with no per-ask `to` inherits the MESSAGE's
                # addressees (2026-08-04). The obligation ledger already
                # reads it that way — `to_answer` admits a row on
                # `agent.id in m.to` — so keying the asker's radar on the
                # per-ask field alone made the two halves of the same
                # dispatch disagree: measured live, a delegate that had just
                # addressed five seats one message each (scifi-novel#232-236,
                # every ask carrying `to=None`) got ZERO waiting_on rows and
                # was told nothing about who had not delivered. A seat that
                # dispatches correctly must not lose its monitoring surface
                # for choosing envelope addressing over per-ask addressing.
                for seat in (a.get("to") or m.to or []):
                    if seat == agent.id:
                        # NOBODY WAITS ON THEMSELVES. Same root cause as the
                        # board's pending-on-me hole: message-level `to` is
                        # the one self-address the post gate still allows, so
                        # the `or m.to` fallback above could name the asker.
                        # The author's duty on their own thread is CLOSURE,
                        # served as the separate `to_close` class.
                        continue
                    if seat in repliers:
                        continue
                    key = (seat, m.channel)
                    if key not in cursor_cache:
                        cursor_cache[key] = self.db.get_cursor(seat, m.channel)
                    # A RETIRED addressee is a truthful terminal state (M2):
                    # 'not-yet-acked' about a decommissioned seat is the
                    # hub serving a stale row — say 'retired', which is a
                    # close-your-ask prompt, not a wait.
                    if self.db.agent_retirement(seat) is not None:
                        state = "retired"
                    elif cursor_cache[key] >= m.seq:
                        state = "acked-past-no-reply"
                    else:
                        state = "not-yet-acked"
                    waiting_on.append(WaitingRow(
                        channel=m.channel, seq=m.seq, ask=str(a["id"]),
                        seat=seat, state=state,
                    ))
        to_close: list[CloseRow] = []
        for m in self.db.my_open_messages(agent.id, channels):
            replies = self.db.replies_to(m.id)
            if self._closed_authoritatively(m, replies):
                continue
            ds = self._discharge(m, replies)
            if not ds.discharged:
                continue
            non_sender = [r for r in replies if r.sender != agent.id]
            if not non_sender:
                continue
            last = max(non_sender, key=lambda r: r.created_at)
            age_since = now - last.created_at
            if age_since < TO_CLOSE_MIN_AGE_SECONDS:
                continue
            # A DISCHARGED THREAD IS NOT NECESSARILY AN ANSWERED ONE (0153).
            # A fully-declined thread discharges, so this row — the asker's
            # ONLY durable pointer once a refusal owes them no consumption —
            # would otherwise report their refused question as "answered".
            ask_ids = {str(a["id"]) for a in asks_of(m)}
            declined_by = sorted({r.sender for r in non_sender
                                  if ask_ids & set(declines_of(r))})
            to_close.append(CloseRow(
                channel=m.channel, id=m.id, seq=m.seq,
                title=m.title, answered_by=last.sender,
                answered_at=last.created_at,
                declined_asks=ds.declined, declined_by=declined_by,
                task=self._task_key_for(m.channel, m.seq),
            ))
        # Open phases across the agent's rooms (0140/2). Not a debt: a
        # standing constraint on WHICH work is legitimate now. It rides
        # /owed because that is the one call every reception pass makes —
        # a phase order nobody reads is the phase order that failed.
        phases = [PhaseRow(**row) for ch in channels
                  for row in self.phase_rows(ch) if row["status"] == "open"]
        return OwedReport(
            to_answer=to_answer, to_consume=to_consume, to_close=to_close,
            waiting_on=waiting_on, phases=phases,
            charters=self.charter_debts(agent.id, channels),
            counts=OwedCounts(to_answer=len(to_answer),
                              to_consume=len(to_consume),
                              to_close=len(to_close)),
            computed_at=now)

    def charter_debts(self, agent_id: str,
                      channels: list[str] | None = None) -> list[CharterDebt]:
        """Charters this seat has not read at their current version.

        The retention half of 0146. whoami's pointer lands once per session
        and the hub-scope change is announced only in `hub-alerts` (operators
        + reporting delegates), so a long-running seat could not learn that
        the standing role model had changed under it. This rides `/owed` —
        the one call every reception pass makes — for the same reason
        `phases` does. Cheap: one indexed receipt lookup per scope plus the
        charter's fs head, both already hot on this path.

        Self-clearing: reading records the receipt, so the row is gone next
        pass. It is deliberately NOT in the wake signature (listen only
        signs to_answer/to_consume ids) — a charter change must never
        manufacture a wake, only be unmissable on a turn that happens."""
        rows: list[CharterDebt] = []
        pointer = self.hub_charter_pointer(agent_id)
        if not pointer["current"]:
            rows.append(CharterDebt(
                scope=HUB_CHARTER_SCOPE, version=pointer["version"],
                your_receipt=pointer["your_receipt"],
                read_with="read_charter()"))
        elif not pointer["view_current"]:
            # 0147: the receipt is valid and stays valid — this is the other
            # half. A seat promoted since its last read (new room, new
            # delegation) holds a current receipt for text that never
            # contained the section now addressed to it. One self-clearing
            # row, never a block, never a wake.
            rows.append(CharterDebt(
                scope=HUB_CHARTER_SCOPE, version=pointer["version"],
                your_receipt=pointer["your_receipt"],
                read_with="read_charter()", reason="view"))
        for channel in (channels if channels is not None
                        else self.db.channels_of(agent_id)):
            head = self.db.fs_get(channel, FS_PREFIX + CHARTER_PATH)
            if head is None or head["deleted"]:
                continue
            mine = self.db.charter_receipt_get(agent_id, channel)
            if mine is not None and mine >= head["version"]:
                continue
            rows.append(CharterDebt(
                scope=channel, version=head["version"], your_receipt=mine,
                read_with=f"read_charter(channel={channel!r})",
                gated=self._norms_required(channel)))
        return rows

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
            before = self.db.get_cursor(agent.id, channel)
            target = min(seq, self.db.last_seq(channel))
            self.db.set_cursor(agent.id, channel, target)
            if target > before:
                self._publish_pickup_swept(agent.id, channel, before, target)

    #: How far back an ack may sweep and still push (0156). A catch-up ack
    #: after hours away is not the realtime case, and recomputing a ladder
    #: per message across a thousand-message backlog would make every seat's
    #: first ack expensive for a signal nobody is watching in that moment.
    #: Past this the pushes are skipped WHOLESALE rather than truncated to
    #: the first N — a partial sweep would leave the asker's rail correct for
    #: the messages that happened to be scanned and stale for the rest, with
    #: nothing saying which is which. The row (C4) repairs it on next read.
    _PICKUP_SWEEP_MAX = 200

    def _publish_pickup_swept(self, seat: str, channel: str,
                              before: int, target: int) -> None:
        """`acked` just moved for every message this cursor swept past
        (0156) — push the askers' ladders.

        This is the rung that says *"they have seen it and done nothing
        yet"*, which is the operator's complaint stated precisely. It is
        also the weakest rung on the ladder and the one a client must not
        dress up: a seat that acked is a seat that triaged, not a seat that
        agreed."""
        if target - before > self._PICKUP_SWEEP_MAX:
            return
        try:
            swept = [m for m in self.db.get_messages(
                        channel, since_seq=before, limit=self._PICKUP_SWEEP_MAX)
                     if m.seq <= target and m.sender != seat and not m.retracted
                     and (seat in m.to or seat in ask_addressees(m))]
            if not swept:
                return
            links = self._declared_claim_links(channel)
            for m in swept:
                self._publish_pickup(m, links=links)
        except Exception:
            logging.getLogger("agora.hub.pickup").exception(
                "pickup sweep push failed (the ack succeeded)")

    # -- store -------------------------------------------------------------------

    _CLAIM_SOURCE_REF = re.compile(
        # Match admitted channel names, not a narrower lowercase slug rule.
        # A channel can itself contain '#'; the existing resolver separates
        # the final '#seq' using rpartition. Whitespace prose is not a link.
        r"[^/\x00-\x20\x7f]*#\d+|[0-9A-HJKMNP-TV-Z]{26}")

    def _normalize_claim_source(self, channel: str, value: dict[str, Any],
                                *, strict: bool = False) -> dict[str, Any]:
        """Resolve exact claim provenance, never infer it from a sentence.

        `source` was taught by the supplied skill while machine readers used
        `source_message_id`. Accept that spelling, retaining the original
        source text, and canonicalize both supported ref forms. Legacy reads
        are projections only: invalid old values stay readable and acquire no
        new link; writes that change a declaration refuse invalid references.
        """
        targets: list[Message] = []
        for field in ("source_message_id", "source"):
            raw = value.get(field)
            if raw is None or raw == "":
                continue
            ref = raw.strip() if isinstance(raw, str) else ""
            if not ref or not self._CLAIM_SOURCE_REF.fullmatch(ref):
                # source is also historical prose. A single malformed #ref
                # is an attempted declaration; a sentence is just context.
                canonical_prose = (field == "source_message_id" and ref
                                   and any(c.isspace() for c in ref))
                explicit = (field == "source_message_id" and not canonical_prose) or (
                    isinstance(raw, str) and "#" in ref
                    and not any(c.isspace() for c in ref))
                if strict and explicit:
                    raise HubError(400, f"claim {field} must be one message ID or channel#seq reference")
                if explicit or canonical_prose:
                    return value
                continue
            target = (self._resolve_consume_ref(ref, channel) if "#" in ref
                      else self.db.get_message(ref))
            if target is None or target.retracted:
                if strict:
                    raise HubError(404, f"claim {field} does not name an available message")
                return value
            if target.channel != channel:
                if strict:
                    raise HubError(400, "claim source must be a message in the claim's channel")
                return value
            owner = str(value.get("owner") or "")
            if owner and not self.db.is_member(channel, owner):
                if strict:
                    raise HubError(403, "claim source must be readable by the claim owner")
                return value
            targets.append(target)
        if not targets:
            return value
        if len({target.id for target in targets}) != 1:
            if strict:
                raise HubError(400, "claim source and source_message_id refer to different messages")
            return value
        return {**value, "source_message_id": targets[0].id}

    def store_get(self, agent: AgentInfo, channel: str, key: str) -> StoreEntry:
        self.require_membership(channel, agent.id)
        entry = self.db.store_get(channel, key)
        if entry is None:
            raise HubError(404, f"key '{key}' not found in '{channel}' store")
        if key.startswith("claim:") and isinstance(entry.value, dict):
            # Repair the public read seam for old source-only rows without
            # migrating storage, changing versions, or posting a receipt.
            entry = entry.model_copy(update={
                "value": self._normalize_claim_source(channel, entry.value)})
        return entry

    def store_set(self, agent: AgentInfo, channel: str, key: str, value: Any,
                  expect_version: int | None = None) -> StoreEntry:
        with self.db.orchestration_lock:
            return self._store_set_locked(agent, channel, key, value, expect_version)

    def _store_set_locked(self, agent, channel, key, value, expect_version=None):
        park_ring = ""
        undeliverable_ring = ""
        self.require_membership(channel, agent.id)
        self._require_unpaused(agent, channel)
        self._require_not_archived(channel)
        if len(json.dumps(value).encode()) > MAX_STORE_VALUE_BYTES:
            raise HubError(413, f"store value exceeds {MAX_STORE_VALUE_BYTES} bytes")
        if key.startswith(FS_PREFIX):
            # File keys are owned by the VFS API so every mutation is validated
            # and emits an audit event; a raw store_set would bypass both.
            raise HubError(403, f"'{key}' is a virtual file system (vfs) path: use the fs_* API")
        if key.startswith(RESERVED_STORE_PREFIX):
            # The OPERATOR is always able to write channel metadata. Ownership
            # is the delegation mechanism, not a wall above the human: a
            # hub-created room (`commons`) has no owner row at all, so an
            # owner-only rule made its purpose, norms, SLA and traffic_policy
            # permanently unwritable by anyone on every fresh deployment.
            if not self.may_administer_channel(agent, channel):
                raise HubError(403, f"'{key}' is channel-level metadata: "
                                    "writable by the channel owner, an "
                                    "operator, or a delegate holding "
                                    "ruling/operational scoped here")
            if key == CHANNEL_META_KEY:
                # Purpose/norm/SLA edits must not accidentally turn off a
                # noticeboard. `store set` replaces values, so preserve this
                # policy field unless the owner explicitly supplies a new one.
                current_meta = self.db.store_get(channel, CHANNEL_META_KEY)
                if (isinstance(value, dict) and current_meta
                        and isinstance(current_meta.value, dict)
                        and "traffic_policy" in current_meta.value
                        and "traffic_policy" not in value):
                    value = dict(value)
                    value["traffic_policy"] = current_meta.value["traffic_policy"]
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
            # THE ROW MUST BE SOMEWHERE ITS SEAT CAN SEE IT (agora-wui,
            # operator-board#20 ask 2). Both readers scope by membership —
            # `board()` classifies `queue:<viewer>:*` while iterating
            # `channels_of(viewer)`, and `desk()` does the same per operator
            # — so a row keyed to a seat that does not belong to this channel
            # is accepted, stored, and then reachable by NO surface at all.
            # Five `queue:laurent:*` rows sat in `operator-board`, a room
            # laurent is not in, and a whole room reasoned about "his queue"
            # from the key grammar for twelve hours. Nothing was broken and
            # nothing was delivered.
            #
            # Refuse by NAME, on the `capabilities` model (:997): say which
            # seat, say why the row would be invisible, and name BOTH repairs
            # rather than picking one — whether to move the row or widen the
            # room is the curator's call, not the hub's.
            seat = key[len(self._QUEUE_PREFIX):].split(":", 1)[0]
            if not seat:
                raise HubError(400, "queue keys name their seat: "
                                    "queue:<seat>:<slug>")
            # CREATION ONLY. An existing row stays writable however its seat's
            # membership has changed, and that is not leniency — store keys
            # CANNOT BE DELETED, so overwriting with a closing state is the
            # only way to retire one. A gate on every write would make an
            # already-stranded row permanently unretireable, which is the
            # trap this fix would otherwise have set for the rows it is named
            # after: three `queue:laurent:*` rows are deliberately staying in
            # `operator-board` (delegate's
            # decision:three-of-my-five-operator-queue-rows-were-not-his-to-decide),
            # and their curator must be able to close them.
            if (self.db.store_get(channel, key) is None
                    and not self.db.is_member(channel, seat)):
                raise HubError(
                    400, f"'{seat}' is not a member of '{channel}', so a NEW "
                         f"queue row keyed to them here reaches no surface: "
                         f"both /board and /desk only classify rows in "
                         f"channels that seat belongs to. Write it in a "
                         f"channel '{seat}' is in, or invite them here — "
                         f"a row nobody can read is not a queued decision")
            self._validate_queue_row(value)
        if key.startswith(self._RULING_PREFIX):
            if not agent.operator:
                raise HubError(403, "ruling:* rows are operator-authored "
                                    "standing constraints (0113) — only the "
                                    "operator may write or revoke them")
            self._validate_ruling_row(value)
        if key.startswith(self._GATE_PREFIX):
            if len(key) <= len(self._GATE_PREFIX):
                raise HubError(400, "gate keys name a decision: "
                                    "gate:<slug> (e.g. gate:lab-identity)")
            self._validate_gate_row(
                value, agent, self.db.store_get(channel, key), channel)
        if key.startswith("decision:"):
            # The settled record: the row every other seat keys off. In a
            # gated room it is the owner's to settle — that is precisely the
            # act the live delegate got wrong, recording an identity the
            # owner had not chosen.
            self._require_gate(channel, agent, "decision")
        if key.startswith(self._PHASE_PREFIX):
            # A phase declaration is the one event that changes which asks
            # are PENDING (Ask.phase): drop the 1s memo before the write so
            # the next /owed sees it, whatever this request's outcome.
            self._phase_memo.pop(channel, None)
            if len(key) <= len(self._PHASE_PREFIX):
                raise HubError(400, "phase keys name a TRACK: "
                                    "phase:<track> (e.g. phase:manuscript)")
            refusal = self._phase_writer_refusal(channel, agent, key)
            if refusal is not None:
                raise HubError(403, refusal)
            # Declaring a phase COMPLETE unblocks the next one for the whole
            # room; in a gated channel that is the owner's call.
            if (isinstance(value, dict)
                    and str(value.get("status") or "").strip().lower()
                    in ("complete", "completed", "done", "closed")):
                self._require_gate(channel, agent, "phase_complete")
            prev = self.db.store_get(channel, key)
            self._validate_phase_row(value, agent, prev)
            # `opened` is HUB-MAINTAINED, never the client's: the set of
            # phases this track has ever opened. It is what makes Ask.phase
            # readiness MONOTONIC — an ask scoped to `draft` stays pending
            # after the track moves on to `review` — without a store
            # history the schema does not keep. Whatever the client sent
            # under `opened` is overwritten.
            prev_opened = (prev.value.get("opened")
                           if prev is not None and isinstance(prev.value, dict)
                           else None) or []
            cur = str(value.get("current") or "").strip()
            value = {**value, "opened": sorted(
                {str(x) for x in prev_opened} | ({cur} if cur else set()))}
        if key.startswith(self._TASK_PREFIX):
            if len(key) <= len(self._TASK_PREFIX):
                raise HubError(400, "task keys name a request: task:<slug> "
                                    "(the hub mints task:msg-<seq>)")
            current = self.db.store_get(channel, key)
            refusal = self._task_writer_refusal(channel, agent, current)
            if refusal is not None:
                raise HubError(403, refusal)
            if isinstance(value, dict) and any(f in value for f in ("primary_channel", "depends_on", "director", "work_type")) and expect_version is None:
                raise HubError(400, "structured task updates require expect_version")
            self._validate_task_row(channel, value, agent, current)
            self._validate_task_graph(agent, channel, key, value, current.value if current else {})
        # Opt-in only: pre-existing freeform finding:* rows retain their
        # ordinary store semantics.  Once a row declares task-finding-v1,
        # omission cannot erase its discriminator or task binding.
        existing_finding = self.db.store_get(channel, key)
        if (key.startswith(FINDING_PREFIX)
                and (is_typed_finding(key, value)
                     or (existing_finding is not None
                         and is_typed_finding(key, existing_finding.value)))):
            if expect_version is None:
                raise HubError(400, "typed finding writes require expect_version (0 to create)")
            value = self._validate_finding_row(
                agent, channel, key, value,
                existing_finding.value if existing_finding is not None else None)
        if key.startswith("fact:"):
            # A shared number has ONE owner — its first writer (fleet review
            # F6, 2026-09-09: `fact:` rows were writable by any member and a
            # store write left no trace in the channel). Others read it; a
            # correction goes to the owner as an addressed ask, or is made by
            # channel authority. The write itself is audited below.
            current = self.db.store_get(channel, key)
            if (current is not None and current.updated_by != agent.id
                    and not self.may_administer_channel(agent, channel)):
                raise HubError(403, f"'{key}' is owned by its first writer "
                                    f"'{current.updated_by}'. Ask them (an addressed "
                                    "open) to correct it; channel authority may "
                                    "overwrite it.")
        if key.startswith("claim:"):
            prior_row = self.db.store_get(channel, key)
            prior_claim = prior_row.value if prior_row and isinstance(prior_row.value, dict) else {}
            # Answer waits share the claim's owner/CAS semantics and must be
            # normalized before the generic linked/non-object handling below.
            from .claim_waits import validate_answer_waits, validate_resume_waits
            # Inspect original keys before normalization/merge erases whether
            # a resuming owner deliberately acknowledged an existing gate.
            # Stale-CAS and foreign-owner errors retain their normal paths.
            if (prior_row is not None and expect_version == prior_row.version
                    and (agent.operator or prior_claim.get("owner") == agent.id)):
                validate_resume_waits(self, value, prior_claim)
            value = validate_answer_waits(self, agent, channel, value,
                                          prior_claim, expect_version)
            linked = prior_claim.get("task") is not None or (isinstance(value, dict) and value.get("task") is not None)
            if linked:
                if not isinstance(value, dict):
                    raise HubError(400, "task-linked claims require an object")
                if expect_version is None:
                    raise HubError(400, "task-linked claims require expect_version")
                owner = prior_claim.get("owner") or value.get("owner") or agent.id
                effective = {**prior_claim, **value}
                if not agent.operator and agent.id != owner and effective != prior_claim:
                    raise HubError(403, "only the owner or operator may update a linked claim; ask its owner instead")
                value = effective
        if key.startswith("claim:") and not isinstance(value, dict):
            existing = self.db.store_get(channel, key)
            if (existing and isinstance(existing.value, dict)
                    and existing.value.get("waiting_for_artifacts") is not None):
                raise HubError(400, "artifact-dependent claims require an object; clear waiting_for_artifacts explicitly with CAS")
        if key.startswith("claim:") and isinstance(value, dict):
            old = self.db.store_get(channel, key)
            if old and isinstance(old.value, dict) and "task" in old.value and "task" not in value:
                value = {**value, "task": old.value["task"]}
            previous = old.value if old and isinstance(old.value, dict) else {}
            if previous.get("task") is not None and not agent.operator and agent.id != previous.get("owner"):
                if any(f in value and value[f] != previous.get(f) for f in ("task", "owner", "status", "state", "done")):
                    raise HubError(403, "only the owner or operator may change linked claim lifecycle or task")
            if value.get("task") is not None:
                self._task_ref(agent, value["task"])

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
                    shown = elide(str(value["owner"]), 64)
                    raise HubError(400, f"claim owner '{shown}' is not you — "
                                        "claim in your own name, or leave the "
                                        "existing owner unchanged")
            elif current_owner is not None:
                value = {**value, "owner": current_owner}
            previous_value = (current.value if current is not None
                              and isinstance(current.value, dict) else {})
            # Artifact arrival requests one reconsideration, not acceptance or
            # an automatic status rewrite. Only the CURRENT owner/operator may
            # install/change/remove that request or revive its parked claim.
            old_artifacts = previous_value.get("waiting_for_artifacts")
            if "waiting_for_artifacts" not in value and old_artifacts is not None:
                value = {**value, "waiting_for_artifacts": old_artifacts}
            if old_artifacts is not None or value.get("waiting_for_artifacts") is not None:
                if expect_version is None:
                    raise HubError(400, "artifact-dependent claims require expect_version (0 to create)")
                responsible = current_owner or value.get("owner") or agent.id
                value = {**value, "owner": value.get("owner") or responsible}
                # Partial progress writes cannot erase lifecycle guards.
                value = {**{field: previous_value[field] for field in ("status", "state", "done")
                            if field in previous_value and field not in value}, **value}
                lifecycle_changed = (
                    bool(value.get("done")) != bool(previous_value.get("done"))
                    or any(self._claim_status_word({"status": value.get(field)})
                           != self._claim_status_word({"status": previous_value.get(field)})
                           for field in ("status", "state")))
                # A peer may close work, but cannot move cancelled/paused
                # history back into a blocked gate (nor activate the claim).
                # Same-status progress writes do not acquire new authority.
                revives = lifecycle_changed and not self._claim_done(value)
                changed = value.get("waiting_for_artifacts") != old_artifacts
                owner_changed = value.get("owner") != responsible
                # An unchanged closing/progress write must remain possible if
                # the owner has since lost access to a dependency room.
                if value.get("waiting_for_artifacts") is not None and (changed or revives or owner_changed):
                    value = {**value, "waiting_for_artifacts": self._validate_waiting_for_artifacts(
                        channel, str(value["owner"]), value["waiting_for_artifacts"])}
                changes_gate = value.get("waiting_for_artifacts") != old_artifacts
                if (not agent.operator and agent.id != responsible
                        and (changes_gate or revives or owner_changed)):
                    raise HubError(403, "only the current claim owner or operator may change artifact waits, ownership, or resume this claim")
            if "source" not in value and "source_message_id" not in value:
                # A progress/closure-only update does not erase provenance,
                # just as omitting owner above does not erase ownership.
                value = {**value, **{f: previous_value[f]
                                    for f in ("source", "source_message_id")
                                    if f in previous_value}}
            source_changed = any(value.get(f) != previous_value.get(f)
                                 for f in ("source", "source_message_id"))
            previous_source = self._normalize_claim_source(channel, previous_value).get("source_message_id")
            value = self._normalize_claim_source(
                channel, value, strict=current is None or source_changed)
            if (value.get("source_message_id") != previous_source
                    and not agent.operator
                    and agent.id != (value.get("owner") or current_owner or agent.id)):
                raise HubError(403, "only the claim owner or operator may change its message source")
            # Claim-due cadence (2026-07-28): `cadence_minutes` is the
            # owner declaring "remind ME when this row idles past N min"
            # (see _claim_due_sweep). Validate the TYPE here so a junk
            # value fails at write time, loudly, instead of silently
            # never pinging; 0 is the documented "declared off". And it is
            # OWNER-declared by doctrine: a peer adding a cadence to my
            # claim would have the hub ping me on a schedule I never set
            # (review F7) — only the owner (or operator) may set/change it.
            if "cadence_minutes" in value:
                raw_cadence = value["cadence_minutes"]
                if isinstance(raw_cadence, bool):
                    raise HubError(400, "claim cadence_minutes must be a "
                                        "number of minutes (0 disables "
                                        "claim-due pings)")
                try:
                    cadence = float(raw_cadence)
                except (TypeError, ValueError):
                    raise HubError(400, "claim cadence_minutes must be a "
                                        "number of minutes (0 disables "
                                        "claim-due pings)")
                if cadence < 0:
                    raise HubError(400, "claim cadence_minutes must be >= 0 "
                                        "(0 disables claim-due pings)")
                prev_raw = (current.value.get("cadence_minutes")
                            if current is not None
                            and isinstance(current.value, dict) else None)
                effective_owner = value.get("owner") or current_owner
                if (raw_cadence != prev_raw and not agent.operator
                        and agent.id != effective_owner):
                    raise HubError(403, "cadence_minutes is OWNER-declared "
                                        "(the hub surfaces debts their "
                                        "author declared): only the claim "
                                        "owner or the operator may set or "
                                        "change it")
            # WAITING_ON (2026-08-06): "resume when row X changes" as
            # verifiable state instead of prose.
            #
            # THE FAILURE. `rt2-lead` parked `claim:phase-1-m1-dispatch`
            # with next_step "Resume when `claim:m1-engine-boot-manifest`
            # changes with captured boot-proof evidence". It changed 3m43s
            # later, carrying exactly that evidence. Nothing woke. Seven
            # seats sat idle for hours on a milestone that was DONE, because
            # a store write rings nobody and a sentence in `next_step` is
            # not a subscription.
            #
            # The row may now DECLARE the dependency. The hub does two
            # things with it and nothing more: it refuses a target that
            # does not exist (a wait on a phantom is a silent forever-park,
            # and this fleet wrote one on its first outing), and it stamps
            # the target's version at declaration time so "has it moved?"
            # is a fact rather than a judgement. Who resumes, and what they
            # do next, stays the seat's call — the hub surfaces, never
            # authors.
            if value.get("waiting_on") is not None:
                value = {**value,
                         "waiting_on": self._validate_waiting_on(
                             channel, key,
                             str(value.get("owner") or current_owner or agent.id),
                             value["waiting_on"])}
            # A PARK MUST SAY WHAT IT NEEDS (operator ruling, 2026-08-06).
            #
            # "the agent should state on its channel why it is parking and
            #  what it needs to continue... a park should have a tag and an
            #  ask of what it needs to continue... all these tags should be
            #  visible in particular to the delegate, as a way to understand
            #  where we are, the blockers and how to finish the work end to
            #  end."
            #
            # A bare `parked` was a black hole: invisible to every sweep
            # (`_steward_sweep` exempts it), invisible to its own driver
            # (`parked` is terminal in `_TERMINAL_STATUS`), and silent in the
            # room (a store write rings nobody). Seven seats sat on one for
            # hours.
            #
            # So a park now carries two fields the hub can act on: `blocked_on`
            # — the TAG, one of a closed vocabulary, so the delegate's board
            # can group blockers — and `needs`, the plain-language ask. The
            # hub validates their presence and shape and nothing else: WHAT
            # the seat needs is the seat's judgement, never the hub's.
            #
            # Only the TRANSITION into park is gated. A row already parked
            # before this rule keeps updating freely — semantics changes must
            # not rewrite the past.
            if self._claim_parked(value):
                was_parked = (
                    self._claim_parked(current.value)
                    if current is not None and isinstance(current.value, dict)
                    else False)
                if not was_parked:
                    # Validation is transition-only: a row parked before this
                    # rule keeps updating freely.
                    self._validate_park(key, value, channel, agent)
                # The RING follows the current declaration, not the
                # transition. A row that stays blocked while what it needs
                # changes is a NEW ask, and the seat that can answer it must
                # hear the new one — the dedupe key is the block's content,
                # so an unchanged block stays silent.
                park_ring = str(value.get("needs_from") or "").strip()
                # ...BUT THE RING MUST MEET THE SAME BAR AS THE TRANSITION
                # (2026-08-23, agora-and-wui#288). Validation is
                # transition-only by design; the ring is every-write. So an
                # ALREADY-parked row could set `needs_from` to anything,
                # skip `_validate_park`'s member check, and still ring — and
                # `to=[<not a seat>]` addresses NOBODY, which turns an
                # addressed nudge into an unaddressed `open` that wakes the
                # WHOLE ROOM in the second person. Measured: agora-wui
                # re-parked with `needs_from: "nobody — agora-wui
                # re-measures this itself"`, and both agora-wui AND agora
                # read "YOU ARE THE BLOCKER" as being about them; each
                # answered a nudge that was for neither. The reported
                # diagnosis was a fallback to the row's owner — there is no
                # such fallback; the alert simply had no addressee.
                #
                # Not fixed by validating every write: that would refuse
                # updates to rows parked before the rule, which the
                # transition-only choice above exists to protect. Gate the
                # DELIVERY instead, on the same two facts the sweep already
                # checks (in the room, and not the owner).
                if park_ring:
                    ring_members = {m.agent_id
                                    for m in self.db.list_members(channel)}
                    if park_ring not in ring_members or park_ring == agent.id:
                        undeliverable_ring, park_ring = park_ring, ""
                    elif self._blocker_already_engaged(channel, value,
                                                       park_ring):
                        # THE NAMED SEAT IS ALREADY ON IT (2026-09-05, exp1):
                        # parser had answered render's ask 60 s before render
                        # parked on parser, and the hub rang parser anyway;
                        # parser spent a turn replying "already delivered,
                        # see #13". A seat that replied into the row's own
                        # thread within the room's SLA, or is the human, is
                        # not told to hurry. The sweep still rings if the row
                        # is still parked past the SLA.
                        park_ring = ""
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
                                    f"'{elide(str(value['item']), 64)}'"
                                    " — a pointer claim must cite ONE id; "
                                    "drop value.item or make them agree")
        if key.startswith(self.WORK_ROW_PREFIX):
            self._validate_work_row(key, value)
        entry = self.db.store_set(channel, key, value, agent.id, expect_version)
        if key.startswith("fact:"):
            # On the record, like a file write: who set which number, when.
            self._post_store_audit(channel, agent.id, key, entry.version)
        # TELL THE SEAT IT IS BLOCKING (2026-08-07). The park declared, in
        # validated data, who can end it. Storing that and not delivering it
        # is how two rows named `g4-engine` while `g4-engine` sat idle in the
        # same room. Addressed and OPEN: being someone's blocker IS an
        # obligation — unlike a powers notice, this one has a discharge (act,
        # or say you cannot) and the asker is a peer who can close it.
        if park_ring:
            try:
                self._post_system(
                    channel,
                    # LEAD WITH THE ADDRESSEE, NOT THE SECOND PERSON
                    # (2026-08-25, commons#390/#391). This opened "YOU ARE
                    # THE BLOCKER on `key` (owner)", so the first word
                    # addressed whoever was reading and the only id in the
                    # sentence was the row's OWNER — who is the one seat it
                    # is NOT about. Three recorded misreads: two seats on
                    # 2026-08-23 (see the ring-delivery gate above), then
                    # `agora` and `agora-tui` within an hour of each other
                    # on commons#384 — the latter while writing a message
                    # ABOUT misaddressed alerts, and after having read the
                    # payload that showed no to-you flag. The delivery was
                    # correct every time; the prose put bystanders in the
                    # wrong room. `agora-tui`, from the receiving end:
                    # "a seat that is not that name stops reading without
                    # having to check a field it cannot see."
                    #
                    # The marker phrase is KEPT (mid-sentence) on purpose:
                    # `_report_blocker_answered` finds standing alerts with
                    # `"YOU ARE THE BLOCKER" in m.body`, so dropping it
                    # would orphan every alert already on a live hub and
                    # re-ring seats that had been told.
                    f"{park_ring}: YOU ARE THE BLOCKER on `{key}` "
                    f"(owner: {agent.id}) — "
                    + attributed_quote(
                        str(value.get("needs") or "unblock it"), agent.id)
                    + "\n\nDo it, or say here what it would take and by when. "
                      "Their work does not move until you answer.",
                    to=[park_ring], status=Status.open,
                    # Keyed on the BLOCK, not the row version: editing a
                    # note must not re-ring, but a genuinely different ask
                    # must. A surface that repeats itself gets ignored, and
                    # then the one that mattered is ignored too.
                    dedupe_key="blocking:" + hashlib.sha256(
                        f"{channel}\0{key}\0{park_ring}\0"
                        f"{value.get('needs') or ''}".encode()
                    ).hexdigest()[:24])
            except DuplicateMessage:
                pass
        # SILENT INABILITY IS THE SAME CLASS AS A SILENT LIMIT. Dropping the
        # ring above (a `needs_from` naming a non-member, or the owner
        # itself) must not simply go quiet: the seat believes it named
        # someone. Tell the OWNER, addressed, so exactly one seat hears it —
        # never the room, which is the failure being fixed. `needs_from` is
        # the seat that can UNBLOCK you: "nobody" is spelled by leaving it
        # out, and the sentence says so.
        if undeliverable_ring:
            try:
                self._post_system(
                    channel,
                    f"NOBODY WAS RUNG for `{key}`: `needs_from` reads "
                    f"\"{elide(undeliverable_ring, 60)}\", which "
                    + ("names you — a seat cannot be its own unblocker."
                       if undeliverable_ring == agent.id else
                       f"is not a member of '{channel}', so there was "
                       "nobody to address.")
                    + " `needs_from` holds ONE seat id and nothing else. If "
                      "no one is being waited on, LEAVE IT OUT — a park with "
                      "no `needs_from` is a row waiting on a condition, which "
                      "is honest and rings no one.",
                    to=[agent.id], status=Status.fyi,
                    dedupe_key="ring-undeliverable:" + hashlib.sha256(
                        f"{channel}\0{key}\0{undeliverable_ring}".encode()
                    ).hexdigest()[:24])
            except DuplicateMessage:
                pass
        if key.startswith("claim:") and isinstance(value, dict):
            # THE RUNG THE OPERATOR IS WAITING FOR (0156). `claimed` is the
            # only rung that is an affirmative act rather than a side effect
            # of transport, and a store write rings nobody — which is the
            # defect that left seven seats idle on a done milestone
            # (`waiting_on`, above, is the other half of the same lesson).
            # The state word moving (open -> parked -> done) pushes too: a
            # ladder that announced the claim and then went quiet would
            # leave the asker watching a row that says `claimed` forever.
            ref = str(value.get("source_message_id") or "").strip()
            if ref and not any(c.isspace() for c in ref):
                self._publish_pickup_quietly(
                    self._resolve_consume_ref(ref, channel))
        return entry

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
            raise HubError(400, f"'{elide(item, 64)}' is not a work id "
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
                                f"'{elide(status, 32)}')")

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
        rulings_required = value.get("rulings_required")
        if rulings_required is not None and not isinstance(rulings_required, bool):
            raise HubError(400, "channel:meta.rulings_required must be a boolean")
        gated = value.get("gated_acts")
        if gated is not None:
            if not isinstance(gated, list):
                raise HubError(400, "channel:meta.gated_acts must be a list of "
                                    f"act classes from {sorted(GATED_ACT_CLASSES)}")
            unknown_acts = {str(a) for a in gated} - GATED_ACT_CLASSES
            if unknown_acts:
                raise HubError(400, f"unknown gated act class(es): "
                                    f"{sorted(unknown_acts)} — the vocabulary "
                                    f"is closed ({sorted(GATED_ACT_CLASSES)}) "
                                    "because each name must map to an act the "
                                    "hub already mediates")
        # Opt-in charter gate: posting requires having READ the current
        # channel/charter.md (the receipt is recorded by the read itself).
        norms_required = value.get("norms_required")
        if norms_required is not None and not isinstance(norms_required, bool):
            raise HubError(400, "channel:meta.norms_required must be a boolean")
        traffic_policy = value.get("traffic_policy")
        if traffic_policy is not None and traffic_policy not in _TRAFFIC_POLICIES:
            raise HubError(400, "channel:meta.traffic_policy must be one of "
                                f"{sorted(_TRAFFIC_POLICIES)}")
        # purpose/norms are free text delivered to every joiner: strip control
        # characters and cap them at write time like every other member-authored
        # headline (they were the one unvalidated path into join/describe).
        # expected_traffic stays free-form (existing rooms use lists).
        for meta_field in ("purpose", "norms"):
            if meta_field in value and value[meta_field] is not None:
                if not isinstance(value[meta_field], str):
                    raise HubError(
                        400, f"channel:meta.{meta_field} must be a string"
                    )
                value[meta_field] = sanitize_text(value[meta_field], 500, field="meta field")

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
            # The phase order is part of "what you need before you post here"
            # (0140/2): a joiner who does not know which version is live is
            # exactly the seat that starts v4 work during v3.
            "phases": self.phase_rows(channel),
        }

    # -- colleague notes (private, subjective, advisory) ---------------------------

    def set_note(self, agent: AgentInfo, subject: str, note: str) -> ColleagueNote:
        if not self.db.agent_exists(subject):
            raise HubError(404, f"agent '{subject}' is not registered")
        # agent_exists is deliberately tombstone-true (it guards id hijack
        # on the register path — never narrow it); the deleted check is the
        # surgical gate here: no NEW mentions of a hard-deleted id (P2).
        if self.db.agent_deleted(subject):
            raise HubError(410, f"'{subject}' was deleted — notes about a "
                                "deleted identity cannot be created")
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

    # -- hub search (0132) -----------------------------------------------------------

    def search(self, agent: AgentInfo, q: str, *,
               channels: list[str] | None = None, sender: str = "",
               kind: str = "", since: float | None = None,
               until: float | None = None, ref: str = "",
               rated: str = "", min_votes: int = 0,
               sort: str = "relevance", limit: int = 10,
               cursor: str = "", mode: str = "") -> SearchReport:
        """One grouped report over everything THIS caller can read — the
        task-context digest (operator order dm#166; design agora-0132,
        settled by 3 adversary cycles). Scope is the caller's memberships
        joined inside one read snapshot; non-member channels contribute
        nothing, not even counts. See hub/search.py for the executor
        doctrine (compile-safe queries, zero-hit relaxation, thread
        collapse, no scores on the wire)."""
        from . import search as _sx

        wait = self._search_limiter.acquire(agent.id)
        if wait > 0:
            raise HubError(429, f"search budget exhausted; retry in {wait:.1f}s")
        if sort not in ("relevance", "recent", "votes"):
            raise HubError(400, "sort must be 'relevance', 'recent' or 'votes'")
        if mode and mode not in ("lexical", "semantic"):
            raise HubError(400, "mode must be 'lexical' or 'semantic'"
                                " (unset = auto: the hub fuses when its"
                                " semantic index is ready)")
        if rated and rated not in ("up", "down", "any"):
            raise HubError(400, "rated must be 'up', 'down' or 'any'")
        limit = max(1, min(int(limit), _sx.MAX_LIMIT))
        if limit > _sx.DEFAULT_PER_SECTION and not kind:
            # 3A's load-bearing rider: 6 sections x 50 rows breaks the
            # report byte-bound; deep limits require narrowing to one kind.
            raise HubError(400, "limit > 10 requires a kind filter")
        if kind and kind not in ("message", "decision", "claim", "work",
                                 "file", "agent"):
            raise HubError(400, "kind must be one of message|decision|claim|"
                                "work|file|agent")
        if not (q or "").strip():
            # Browse mode (operator dm#174: "see the most up/down votes"):
            # q is optional ONLY with a rated filter — otherwise an empty
            # query is the usual typed 400.
            if not rated:
                raise HubError(400, "query must contain at least one word"
                                    " (or set rated=up|down|any to browse"
                                    " by votes)")
            terms: list[str] = []
        else:
            try:
                terms = _sx.compile_terms(q)
            except _sx.SearchQueryError as e:
                # ONE typed 400 whose shape never varies with corpus/scope.
                raise HubError(400, str(e)) from None

        filters: dict[str, Any] = {}
        if channels:
            filters["channels"] = [str(c) for c in channels][:16]
        if sender:
            filters["sender"] = sender
        if kind:
            filters["kind"] = kind
        if since is not None:
            filters["since"] = float(since)
        if until is not None:
            filters["until"] = float(until)
        if ref:
            filters["ref"] = sanitize_text(ref, 120, field="ref filter")
        if rated:
            filters["rated"] = rated
        if min_votes:
            filters["min_votes"] = max(0, int(min_votes))

        # Semantic side (agora-0137): ALWAYS-FUSE when ready — the measured
        # ruling (a conditional trigger caught 3/10 needed escalations;
        # one 60-137ms query embed buys +0.144 mean recall@25). Fusion
        # rides the blend path only; sort=recent and browse stay lexical.
        mode_used = "lexical"
        notice: str | None = None
        coverage: float | None = None
        semantic_keys: list[tuple[str, str, str]] | None = None
        emb_state = self.embedding.state()
        if emb_state != "disabled":
            coverage = None  # filled below when status computes cheaply
        want_semantic = (mode != "lexical" and terms
                         and sort in ("relevance", "votes"))
        if want_semantic and emb_state == "ready":
            snapshot = self.embedding.query_snapshot()
            qvec = self.embedding.embed_query(q) if snapshot else None
            if snapshot and qvec:
                from .semantic import semantic_candidates
                visible = self._visible_docs(agent.id, filters)
                semantic_keys = semantic_candidates(snapshot, qvec, visible)
                mode_used = "semantic" if mode == "semantic" else "fused"
            else:
                notice = ("search degraded: embedding endpoint unreachable"
                          " — served lexical only; a zero here does not"
                          " prove absence.")
        elif want_semantic and emb_state.startswith("filling"):
            st = self.embedding.status()
            pct = int(round((st.get("coverage") or 0.0) * 100))
            notice = (f"search lexical-only: semantic index still filling"
                      f" ({pct}%) — a zero here does not prove absence.")
        elif want_semantic and emb_state.startswith("degraded"):
            notice = (f"search degraded: {emb_state[9:-1]} — served lexical"
                      " only; a zero here does not prove absence.")
        elif mode == "semantic" and emb_state == "disabled":
            # ONLY the explicit override earns the disabled notice — AUTO
            # on a semantic-less hub stays silent (no permanent noise).
            notice = ("search lexical-only: semantic is not enabled on this"
                      " hub — a zero here does not prove absence.")
        if mode == "semantic" and mode_used != "semantic":
            mode_used = "lexical"

        with self.db.read_transaction() as conn:
            ex = _sx.SearchExecutor(conn, agent.id)
            raw = ex.run(terms, filters, sort=sort, limit=limit,
                         cursor=cursor or None,
                         semantic_keys=semantic_keys,
                         semantic_only=(mode == "semantic"
                                        and semantic_keys is not None))
        if emb_state not in ("disabled",):
            coverage = self.embedding.status().get("coverage")

        # Ratings decoration (operator ruling dm#169): message hits carry
        # their tally so a downvoted answer is visibly marked. One batched
        # query; ranking itself stays vote-independent.
        msg_refs = [h["ref"] for s in raw["sections"].values() for h in s["hits"]
                    if h["kind"] == "message"]
        by_rated = self.db.ratings_for_messages(msg_refs) if msg_refs else {}

        def _hit(h: dict[str, Any]) -> SearchHit:
            hit = SearchHit(**h)
            if hit.kind == "message":
                ratings = by_rated.get(hit.ref, [])
                hit.ratings = RatingTally(
                    up=sum(1 for r in ratings if r["value"] > 0),
                    down=sum(1 for r in ratings if r["value"] < 0),
                    mine=next((r["value"] for r in ratings
                               if r["rater"] == agent.id), 0))
            return hit

        sections = {
            name: SearchSection(
                hits=[_hit(h) for h in sec["hits"]],
                shown=sec["shown"], total=sec["total"])
            for name, sec in raw["sections"].items()
        }
        return SearchReport(
            **sections,
            relaxed=raw["relaxed"],
            channels_searched=len(self.db.channels_of(agent.id)),
            next_cursor=raw["next_cursor"],
            computed_at=raw["computed_at"],
            mode_used=mode_used,
            semantic_coverage=coverage,
            notice=notice)

    def _visible_docs(self, agent_id: str,
                      filters: dict[str, Any]) -> dict[tuple[str, str, str], str]:
        """{(kind, channel, ref): text_hash} for everything THIS caller may
        see — the gate that runs BEFORE cosine (vectors carry no ACL; this
        set is the ACL, same doctrine as the FTS membership JOIN). Honors
        the channels narrowing filter when present."""
        member = {m for m in self.db.channels_of(agent_id)}
        if filters.get("channels"):
            member &= set(filters["channels"])
        out: dict[tuple[str, str, str], str] = {}
        with self.db.read_transaction() as conn:
            for r in conn.execute(
                    "SELECT kind, channel, ref, text_hash FROM search_docs"):
                if r["kind"] == "agent" or (r["channel"] or "") in member:
                    out[(r["kind"], r["channel"] or "", r["ref"])] = \
                        r["text_hash"]
        return out

    # -- work-id activity index (0093) ---------------------------------------------

    def work_activity(self, agent: AgentInfo, item_id: str) -> dict[str, Any]:
        """One call for the whole stitch: every claim, decision, and message
        citing `item_id` across the channels THE CALLER can read. The
        membership gate is the caller's own channel list — private rooms a
        non-member cannot read simply do not contribute rows."""
        if parse_work_id(item_id) is None:
            raise HubError(400, f"'{elide(item_id, 64)}' is not a "
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
        note = sanitize_text(note, 280, field="note")
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

    #: The unified category set (agora-0123, operator ruling dm#129: thumbs
    #: and categorized votes are ONE system — reputation score). 'general'
    #: is the thumbs/message-rating category; the four named categories are
    #: the sub-category granularity agents opt into via agent-level votes.
    REPUTATION_CATEGORIES = ("general",) + tuple(REPUTATION_AXES)

    def reputation_leaderboard(self, agent: AgentInfo,
                               channel: str | None = None) -> dict[str, Any]:
        """ONE score per agent (agora-0123). The operator's final rule
        (dm#131 + the dm#134 clarification "i meant the MECHANICS!!! 10
        messages = UP TO 10 votes"): you may VOTE each message, but the
        SCORE counts each colleague once per category — their standing
        votes collapse to one net sign, so voting often expresses
        judgment while never multiplying weight (the measured DM pair-farm
        dies structurally; the adversary's P0).

        Entry shape: {target, score, raters, channels?, breakdown:
        {category: {score, up, down, raters}}}. Thumbs land in 'general';
        agent-level votes in their named category (the sub-category
        granularity). score = up - down per category; total = sum of
        categories (pinned invariants). DMs count everywhere under the
        same rule; no channel names in any payload."""
        if channel is not None:
            self.require_membership(channel, agent.id)
        totals = self.db.reputation_totals(channel)
        spread = self.db.reputation_spread(channel)
        boards: dict[str, dict[str, Any]] = {}
        for row in totals:
            t = boards.setdefault(row["target"], {
                "target": row["target"], "score": 0, "raters": 0,
                "breakdown": {}})
            t["breakdown"][row["category"]] = {
                "score": int(row["score"]), "up": int(row["up"]),
                "down": int(row["down"]), "raters": int(row["raters"])}
            t["score"] += int(row["score"])
        for target, t in boards.items():
            t["raters"] = spread.get(target, {}).get("raters", 0)
            # `votes` on the global line = the sum of the per-category raw
            # counts (agora-0127). Since cells are now RAW nets (not
            # collapsed voices), this is the same one arithmetic at every
            # zoom: global up/down = Σ cell up/down, and score = up - down.
            # No second number to reconcile — the confusion the operator
            # hit five times is gone by construction.
            t["votes"] = {
                "up": sum(c["up"] for c in t["breakdown"].values()),
                "down": sum(c["down"] for c in t["breakdown"].values())}
            if channel is None:
                t["channels"] = spread.get(target, {}).get("channels", 0)
        # Board order is HUB-decided (continuum pin p3): score desc, then
        # distinct-raters desc (more colleagues behind equal scores ranks
        # first), then target asc — clients render served order, never
        # re-sort, so two UIs can never disagree about ranking.
        board = sorted(boards.values(),
                       key=lambda t: (-t["score"], -t["raters"], t["target"]))
        return {"channel": channel,
                "categories": list(self.REPUTATION_CATEGORIES),
                "leaderboard": board}

    def reputation_votes(self, agent: AgentInfo, channel: str,
                         target: str) -> list[dict[str, Any]]:
        """The attributed votes behind one score (the WHY surface)."""
        self.require_membership(channel, agent.id)
        return self.db.reputation_votes_for(channel, target)

    # -- message ratings (agora-0122: one reputation system) ---------------------

    #: Rating writes were the one unmetered write class (adversary P1: 100
    #: casts in 1.8s, zero 429s, while posts on the same hub throttle).
    #: One standing row per (message, rater) bounds STATE, but not write
    #: churn — this budget bounds the churn. Generous for humans, hostile
    #: to loops.
    RATING_BURST = 30
    RATING_WINDOW_SECONDS = 60.0

    def rate_message(self, agent: AgentInfo, channel: str, message_id: str,
                     value: int, note: str = "") -> dict[str, Any]:
        """One standing ±1 on a message, counting toward its SENDER's
        reputation (agora-0122, operator ruling dm#111: 'giving +/- points
        IS defining reputation'). PUT replaces (flip), DELETE withdraws —
        the toggle semantics the web UI already speaks. Farming-proof at
        the aggregation layer (per-rater sign collapse), so casting many
        ratings buys visibility of judgment, never weight."""
        self._require_unpaused(agent, channel)
        self.require_membership(channel, agent.id)
        self._require_not_archived(channel)
        if value not in (1, -1):
            raise HubError(400, "value must be +1 or -1 (rate again to flip;"
                                " DELETE to withdraw — ratings never stack)")
        m = self.db.get_message(message_id)
        # Channel binding: the path channel must be the message's own channel
        # (adversary trap: a synthetic/foreign id must not be ratable through
        # an unrelated room the rater is a member of).
        if m is None or m.channel != channel:
            raise HubError(404, f"message '{elide(message_id, 40)}' "
                                f"not found in '{channel}'")
        if m.kind != Kind.message:
            raise HubError(400, "only agent messages carry reputation — "
                                "system/fs rows have no accountable author")
        if m.retracted:
            raise HubError(409, "the message was retracted — a tombstone "
                                "carries no rateable content")
        if m.sender == agent.id:
            raise HubError(400, "self-ratings are refused: reputation is "
                                "what COLLEAGUES observe about you")
        if not self.db.agent_exists(m.sender):
            raise HubError(404, f"sender '{m.sender}' is not a registered "
                                "agent (system authors are unrated)")
        if not self._rating_budget.allow(agent.id):
            raise HubError(429, "rating budget exhausted (30/min) — "
                                "judgments are few; loops are not judgment")
        note = sanitize_text(note, 280, field="note")
        return self.db.rating_cast(channel, message_id, agent.id, m.sender,
                                   value, note)

    def unrate_message(self, agent: AgentInfo, channel: str,
                       message_id: str) -> int:
        """Withdraw the caller's standing rating of a message (toggle-off).
        Pause-gated like casting; NOT archive-gated (retracting a judgment
        stays possible on a frozen room, matching unrate_agent)."""
        self._require_unpaused(agent, channel)
        self.require_membership(channel, agent.id)
        return self.db.rating_clear(message_id, agent.id)

    def message_ratings(self, agent: AgentInfo, channel: str,
                        message_id: str) -> list[dict[str, Any]]:
        """The attributed ratings on one message (the WHY surface, matching
        reputation_votes)."""
        self.require_membership(channel, agent.id)
        m = self.db.get_message(message_id)
        if m is None or m.channel != channel:
            raise HubError(404, f"message '{elide(message_id, 40)}' "
                                f"not found in '{channel}'")
        rows = self.db.ratings_for_messages([message_id]).get(message_id, [])
        rows.sort(key=lambda r: -r["updated_at"])
        return rows

    # -- per-channel virtual file system (vfs) -----------------------------------
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

    def _post_store_audit(self, channel: str, actor: str, key: str, version: int) -> Message:
        """Append-only record of a shared-number write (`fact:` rows), authored
        by the actor — a store write posted no audit at all (fleet review F6)."""
        message = self.db.insert_message(
            channel, actor, kind=Kind.fs.value, status="fyi", urgency="inbox",
            title=f"store:set {key}", body="",
            data={"op": "set", "key": key, "version": version}, reply_to=None,
        )
        self._wake(message)
        return message

    def _post_fs_audit(self, channel: str, actor: str, op: str, path: str,
                       version: int, size_bytes: int) -> Message:
        """Append-only record of a file mutation (who/what/when), authored by the
        actor so `fs_history` and the mirror can replay the file's evolution.
        Returned so post-commit advisories can MIRROR it (a doorbell must
        never mint a channel/seq of its own)."""
        message = self.db.insert_message(
            channel, actor, kind=Kind.fs.value, status="fyi", urgency="inbox",
            title=f"fs:{op} {path}", body="",
            data={"op": op, "path": path, "version": version, "size_bytes": size_bytes},
            reply_to=None,
        )
        self._wake(message)
        return message

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
        filename = sanitize_text(str(filename or ""), MAX_FILENAME_CHARS,
                                 field="filename") or "attachment"
        declared = sanitize_text(str(content_type or ""), MAX_CONTENT_TYPE_CHARS, field="content type") \
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
        # Retraction reaches the FILE too (0097). Dropping the ref from the
        # served `data` stops any surface from handing a new reader the id,
        # but an agent that read the message before the retraction memorized
        # it, and the bytes sat behind a plain membership gate. A blob whose
        # EVERY referencing message is retracted is therefore refused. A blob
        # still cited by a live message keeps serving (blobs are
        # content-addressed and deliberately shared), and an uploaded-but-
        # never-posted blob keeps serving too — that is the compose flow,
        # where the uploader has not said anything yet to unsay.
        refs, retracted = self.db.blob_reference_state(channel, blob_id)
        if refs and refs == retracted:
            raise HubError(404, f"no attachment {blob_id[:12]}… in '{channel}' "
                                "— every message carrying it was retracted")
        return found

    #: Evidence refs a completion report may cite. Capped like attachments.
    MAX_EVIDENCE_REFS = 12

    def _validate_evidence(self, channel: str, raw: Any,
                           agent_id: str | None = None) -> list[dict[str, Any]]:
        """Resolve `data.evidence` citations against this channel, and stamp
        SERVER TRUTH over whatever the sender wrote.

        WHY (2026-08-04). A reporting delegate posted a completion report
        claiming "the_novel.docx (5.1MB, 3 embedded images)" on the "channel
        filesystem: /path/to/novel" and flipped its phase row to complete.
        The size was true — it had read it off a local file. The LOCATION was
        a literal unsubstituted placeholder, and the channel filesystem held
        no such file and zero blobs. Nothing in the hub could tell the two
        apart, because nothing was pointed at.

        This is not a new doctrine, it is `settled_by` (a pointer that must
        resolve to a real message here) and attachments ("size/content_type
        always come from the blob row: a message cannot misdescribe its
        file") applied to the one payload that closes an operator's request.
        The hub checks that the CITATION RESOLVES. It never reads the prose
        and never judges whether the artifact is the right one — judging
        relevance is the mind-reading line the operator principle forbids.

        `kind: "external"` is deliberately allowed for work that landed
        outside agora's surfaces (a file on the operator's Desktop). It
        requires a sha256 + size and is stamped `verified: false`: the hub
        must never imply it checked bytes it cannot see, but a hash-pinned
        claim is falsifiable by any peer in one command, and
        `/path/to/novel` is not."""
        if not isinstance(raw, list) or not raw:
            raise HubError(400, "evidence must be a non-empty list of "
                                "{kind, ref} citations")
        if len(raw) > self.MAX_EVIDENCE_REFS:
            raise HubError(400, f"a message carries at most "
                                f"{self.MAX_EVIDENCE_REFS} evidence refs")
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict) or "kind" not in item:
                raise HubError(400, "each evidence ref needs a kind "
                                    "(message, fs, store, blob or external) and a ref")
            kind = str(item.get("kind") or "").strip().lower()
            ref = str(item.get("ref") or "").strip()
            if not ref:
                raise HubError(400, f"evidence ref of kind '{kind}' names "
                                    "nothing — cite what you verified")
            # CROSS-CHANNEL CITATIONS (2026-09-05). A completion report is a
            # reply on the operator's thread, usually in #commons, while the
            # review rows and files it cites live in the focused room. Twice
            # measured (fund4 2026-08-12, exp1 2026-09-05) the delegate had
            # to ask a peer to COPY its review row into the report's channel
            # to satisfy a same-channel lookup. An optional `channel` on the
            # item names the room a `store`/`fs` ref resolves in; the citer
            # must be a member there, so a citation never reads a room the
            # reader could not.
            cited = str(item.get("channel") or "").strip() or channel
            if cited != channel:
                if kind not in ("fs", "store"):
                    raise HubError(400, "evidence `channel` applies to fs and "
                                        "store refs only")
                if not self.db.get_channel(cited):
                    raise HubError(400, f"evidence cites channel '{cited}', "
                                        "which does not exist")
                if agent_id and not self.db.is_member(cited, agent_id):
                    raise HubError(400, f"evidence cites '{cited}', a channel "
                                        "you are not a member of")
            if kind == "message":
                if cited != channel:
                    raise HubError(400, "message evidence must be in the posting channel")
                message = self._resolve_source(channel, ref)
                if message is None or message.kind != Kind.message or message.retracted:
                    raise HubError(400, "message evidence needs a live message in this channel")
                immutable = {"id": message.id, "seq": message.seq, "sender": message.sender,
                             "title": message.title, "body": message.body, "status": message.status.value,
                             "reply_to": message.reply_to}
                out.append({"kind": "message", "ref": f"{channel}#{message.seq}",
                            "id": message.id, "seq": message.seq, "sender": message.sender,
                            "updated_by": message.sender,
                            "sha256": hashlib.sha256(json.dumps(immutable, sort_keys=True,
                                separators=(",", ":")).encode()).hexdigest(), "verified": True})
            elif kind == "fs":
                # "path@version" — the version is what makes it a citation
                # rather than a gesture at a moving file.
                path, _, version = ref.rpartition("@")
                if not path or not version.isdigit():
                    raise HubError(400, "an fs evidence ref is 'path@version' "
                                        f"(got '{ref}') — the version is what "
                                        "pins the claim")
                norm = self._normalize_fs_path(path)
                row = self.db.fs_version(cited, FS_PREFIX + norm, int(version))
                if row is None:
                    raise HubError(400, f"evidence cites '{norm}@{version}', "
                                        f"which is not in '{cited}' — write "
                                        "the artifact to the channel before "
                                        "citing it as delivered")
                value = row["value"] if isinstance(row["value"], dict) else {}
                b64 = value.get("content_b64")
                size = (_b64_decoded_size(b64) if isinstance(b64, str)
                        else len(value.get("content", "").encode("utf-8")))
                fs_item = {"kind": "fs", "ref": f"{norm}@{int(version)}",
                           "size_bytes": size,
                           "updated_by": row["updated_by"],
                           "updated_at": row["updated_at"], "verified": True}
                if cited != channel:
                    fs_item["channel"] = cited
                out.append(fs_item)
            elif kind == "store":
                # `key@version` IS ACCEPTED (agora-wui, 2026-08-22). The two
                # lanes had opposite conventions — `fs` REQUIRES `path@version`
                # and `store` looked the ref up verbatim — so citing a store
                # row the documented way searched for a key literally named
                # `decision:foo@1` and answered "does not exist" about a row
                # `store_get` had served the same minute. The author is then
                # hunting a missing row instead of a wrong form.
                #
                # The verbatim lookup goes FIRST so a key that really contains
                # `@` still resolves; only then is a trailing `@<digits>`
                # read as a version.
                entry = self.db.store_get(cited, ref)
                if entry is None:
                    key, _, want = ref.rpartition("@")
                    if key and want.isdigit():
                        entry = self.db.store_get(cited, key)
                        if entry is not None and entry.version != int(want):
                            # The store keeps only HEAD, so v1 cannot be
                            # served back. Say which version the reader would
                            # actually get rather than stamping HEAD under the
                            # author's older number.
                            raise HubError(
                                400,
                                f"evidence cites '{key}@{want}' but that row "
                                f"is now at v{entry.version}, and the store "
                                "keeps only HEAD — the cited version cannot "
                                "be served. Re-read it and cite the current "
                                f"version, or cite '{key}' bare.")
                        if entry is not None:
                            ref = key
                if entry is None:
                    raise HubError(400, f"evidence cites store row '{ref}', "
                                        f"which does not exist in '{cited}'")
                # A STORE ROW HAS NO ARCHIVE (2026-08-13). `fs` evidence
                # cites `path@version` and the hub can serve those exact
                # bytes back forever; the store keeps only HEAD, so a cited
                # row can be rewritten after the report and what was
                # reviewed is gone. Live (rtype-v3): commons#34 cited
                # `review:requirements` at version 1 and the row now reads
                # version 2 — "written before the final round of fixes
                # landed ... the tree moved underneath them" — with v1
                # unrecoverable. Stamp the DIGEST of the bytes the hub
                # actually read, the commitment an `external` ref already
                # carries: the hub still cannot restore v1, but any reader
                # can prove in one hash whether the row in front of them is
                # the row that was cited. Recipe, so a reader can
                # recompute it: sha256 of json.dumps(value, sort_keys=True,
                # separators=(",", ":")).
                out.append({"kind": "store", "ref": ref,
                            **({"channel": cited} if cited != channel else {}),
                            "version": entry.version,
                            "sha256": hashlib.sha256(json.dumps(
                                entry.value, sort_keys=True, default=str,
                                separators=(",", ":")).encode()).hexdigest(),
                            "updated_by": entry.updated_by,
                            "updated_at": entry.updated_at, "verified": True})
            elif kind == "blob":
                meta = self.db.blob_meta(channel, ref.strip().lower())
                if meta is None:
                    raise HubError(400, f"evidence cites blob {ref[:12]}…, "
                                        f"which is not uploaded to '{channel}'")
                out.append({"kind": "blob", "ref": ref.strip().lower(),
                            "filename": meta["filename"],
                            "size_bytes": meta["size"],
                            "content_type": meta["content_type"],
                            "created_by": meta["created_by"],
                            "verified": True})
            elif kind == "external":
                digest = str(item.get("sha256") or "").strip().lower()
                if not _SHA256_HEX.fullmatch(digest):
                    raise HubError(400, "an external evidence ref needs a "
                                        "sha256 of the bytes you built — the "
                                        "hub cannot see files outside the "
                                        "channel, so the hash is what makes "
                                        "the claim falsifiable")
                size = item.get("size_bytes")
                if not isinstance(size, int) or size < 0:
                    raise HubError(400, "an external evidence ref needs "
                                        "size_bytes (int) alongside its sha256")
                out.append({"kind": "external",
                            "ref": sanitize_text(ref, MAX_FILENAME_CHARS, field="attachment ref"),
                            "sha256": digest, "size_bytes": size,
                            # NEVER claim the hub checked bytes it cannot read.
                            "verified": False})
            else:
                raise HubError(400, f"unknown evidence kind '{kind}' — use "
                                    "message, fs, store, blob or external")
        return out

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
        channel-owned surfaces (the charter among them) are writable by the
        seats that RUN the room: its owner, an operator, or a delegate holding
        ruling/operational scoped here. DMs have no owner, so the prefix is
        structurally unwritable there. One predicate shared with invites and
        ownership transfer, so the three cannot drift apart again."""
        if self.may_administer_channel(agent, channel):
            return
        raise HubError(403, f"'{RESERVED_FS_PREFIX}...' files are channel-owned: "
                            "writable by the channel owner, an operator, or a "
                            "delegate holding ruling/operational scoped to "
                            "this channel")

    def fs_write(self, agent: AgentInfo, channel: str, path: str,
                 content: str | None = None, mime: str = "text/markdown",
                 expect_version: int | None = None,
                 description: str | None = None,
                 content_b64: str | None = None, summary: str = "") -> FsFile:
        """Write under the task/delivery lock so current-artifact checks hold."""
        with self.db.orchestration_lock:
            return self._fs_write(agent, channel, path, content, mime,
                                  expect_version, description, content_b64, summary)

    def _fs_write(self, agent: AgentInfo, channel: str, path: str,
                 content: str | None = None, mime: str = "text/markdown",
                 expect_version: int | None = None,
                 description: str | None = None,
                 content_b64: str | None = None, summary: str = "") -> FsFile:
        """Create or edit a file (compare-and-swap via `expect_version`; 0 means
        'must not exist yet'). Exactly one of `content` (text) or `content_b64`
        (strict standard base64 — the binary deposit path for images/PDFs) must
        be provided. `description` is the writer's one-line statement of what
        the file is, shown in listings; sanitized and capped like a title.
        Returns the new FsFile with its bumped version."""
        self.require_membership(channel, agent.id)
        self._require_unpaused(agent, channel)
        self._require_not_archived(channel)
        norm = self._normalize_fs_path(path)
        if norm.startswith(RESERVED_FS_PREFIX):
            self._require_channel_authority(channel, agent)
        else:
            # A channel file is OWNED by the seat that last wrote it (fleet
            # review F3, 2026-09-09: any member could overwrite another seat's
            # deliverable, receipt and history notwithstanding). Another seat
            # co-edits it KNOWINGLY — by passing the version it read
            # (compare-and-swap) — or with channel authority; never by accident.
            owned = self.db.fs_get(channel, FS_PREFIX + norm)
            if (owned and not owned.get("deleted") and owned.get("updated_by")
                    and owned["updated_by"] != agent.id and expect_version is None
                    and not self.may_administer_channel(agent, channel)):
                raise HubError(403, f"'{norm}' was written by '{owned['updated_by']}' "
                                    f"(v{owned.get('version')}). To co-edit it pass "
                                    f"expect_version={owned.get('version')} — the version "
                                    "you read — or ask its writer; channel authority may "
                                    "overwrite it.")
        if (content is None) == (content_b64 is None):
            raise HubError(400, "provide exactly one of content or content_b64")
        if content_b64 is not None:
            # The charter is a governance TEXT surface (read receipts, debt,
            # advisories all quote it) — never a blob, even for the owner.
            if norm == CHARTER_PATH:
                raise HubError(400, "charter must be text")
            try:
                raw = base64.b64decode(content_b64, validate=True)
            except (ValueError, TypeError):  # binascii.Error is a ValueError
                raise HubError(400, "content_b64 is not valid base64")
            size = len(raw)  # caps, audits and listings speak DECODED bytes
            if size > MAX_FS_BINARY_BYTES:
                raise HubError(413, f"fs file exceeds {MAX_FS_BINARY_BYTES} bytes")
            # The pydantic default delivers an omitted mime as "text/markdown";
            # for bytes that DEFAULT flips to octet-stream. (An explicit
            # text/markdown alongside content_b64 is indistinguishable from
            # the default and is overridden the same way — a deliberate cost:
            # base64-transported bytes are not markdown.)
            if mime == "text/markdown":
                mime = "application/octet-stream"
        else:
            if not isinstance(content, str):
                raise HubError(400, "fs content must be text")
            size = len(content.encode())
            if size > MAX_STORE_VALUE_BYTES:
                raise HubError(413, f"fs file exceeds {MAX_STORE_VALUE_BYTES} bytes")
        # AN EDIT THAT SAYS NOTHING ABOUT THE DESCRIPTION MUST NOT ERASE IT
        # (agora-and-wui#37/#40). `fs_put` REPLACES the stored value object —
        # it does not merge — and the description only entered that object
        # when it was non-empty. So every content-only edit silently blanked
        # the room's table-of-contents line for that file, and the writer who
        # set it had no way to know.
        #
        # Reported from the far side of the seam by two clients within
        # minutes: the WUI had been rendering `description` in its Files
        # drawer while never sending one, and asked what an OMITTED
        # description does on an update before building an edit door on top
        # of a guess. It cleared it. That is the answer neither of them
        # should have had to build around.
        #
        # Omitted (None) now PRESERVES what is stored; an explicit "" CLEARS
        # it. The distinction needs a null default to exist, which is why the
        # signature changed rather than the body of this branch.
        if description is None:
            prior = self.db.fs_get(channel, FS_PREFIX + norm)
            # A tombstone carries no description to inherit: re-creating a
            # deleted path is a creation, and a new file starts nameless
            # rather than wearing the dead one's line.
            description = ("" if not prior or prior.get("deleted")
                           else str((prior.get("value") or {}).get("description") or ""))
        # sanitize_text also strips control chars (ESC/BEL survive str.split —
        # they would otherwise reach the operator's terminal; security M1).
        description = sanitize_text(str(description or ""), 200, field="description")
        if content_b64 is not None:
            value: dict[str, Any] = {"content_b64": content_b64, "mime": mime}
        else:
            value = {"content": content, "mime": mime}
        if description:
            value["description"] = description
        summary = sanitize_text(summary, 500, field="change summary")
        entry = self.db.fs_put(channel, FS_PREFIX + norm, value, agent.id, expect_version, summary=summary)
        audit = self._post_fs_audit(channel, agent.id, "put", norm,
                                    entry.version, size)
        self._fs_trigger_after_write()
        # Phase advisory (0140/2) rides POST-commit and never fails the write:
        # a teaching gesture that could 500 an artifact edit would be worse
        # than the phase disorder it warns about.
        try:
            self._phase_write_advisory(channel, agent, norm, audit)
        except Exception:
            logging.getLogger("agora.hub.phase").exception(
                "phase advisory failed (write succeeded)")
        if norm == CHARTER_PATH:
            # Writing the charter is reading it: the author holds the freshest
            # receipt by construction (otherwise the gate would block the owner
            # right after their own edit).
            self.db.charter_receipt_set(agent.id, channel, entry.version)
            try:
                self._charter_change_advisory(channel, agent, entry.version, audit)
            except Exception:
                logging.getLogger("agora.hub.charter").exception(
                    "charter advisory failed (write succeeded)")
        return FsFile(path=norm, content=content if content is not None else "",
                      content_b64=content_b64,
                      encoding="base64" if content_b64 is not None else None,
                      mime=mime, description=description,
                      size_bytes=size, sha256=_fs_sha256(content=content, content_b64=content_b64),
                      version=entry.version,
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
        b64 = value.get("content_b64")
        if isinstance(b64, str):
            # Binary entry: bytes ride `content_b64`; `content` stays present
            # but EMPTY (never the base64) so pre-binary clients degrade to a
            # blank body instead of a wall of base64.
            return FsFile(path=norm, content="", content_b64=b64, encoding="base64",
                          mime=value.get("mime", "application/octet-stream"),
                          description=value.get("description", ""),
                          size_bytes=_b64_decoded_size(b64), sha256=_fs_sha256(content_b64=b64),
                          version=row["version"],
                          updated_by=row["updated_by"], updated_at=row["updated_at"])
        content = value.get("content", "")
        return FsFile(path=norm, content=content, mime=value.get("mime", "text/markdown"),
                      description=value.get("description", ""),
                      size_bytes=len(content.encode()), sha256=_fs_sha256(content=content), version=row["version"],
                      updated_by=row["updated_by"], updated_at=row["updated_at"])

    def fs_list(self, agent: AgentInfo, channel: str, prefix: str = "") -> list[dict[str, Any]]:
        """List live files (metadata only, no content) under an optional prefix
        — the channel's table of contents. Every row carries a `description`:
        the writer's own when set, else derived from the file's first content
        line, so old files are never blank. Tombstoned files excluded."""
        self.require_membership(channel, agent.id)
        rows = self.db.fs_keys_live(channel, FS_PREFIX + prefix)
        out: list[dict[str, Any]] = []
        for r in rows:
            item = {"path": r["key"][len(FS_PREFIX):], "version": r["version"],
                    "updated_by": r["updated_by"], "updated_at": r["updated_at"],
                    "size": r["size"],
                    "description": r["description"] or _derived_description(r["head"]),
                    "described": bool(r["description"])}
            if not r["head"] and not r["size"]:
                # Ambiguous under the listing SQL (which extracts `$.content`
                # only): an empty text file, or a binary entry whose bytes live
                # in `content_b64`. One head fetch resolves it; binary rows
                # gain the encoding marker and their DECODED byte size.
                head_row = self.db.fs_get(channel, r["key"])
                value = (head_row or {}).get("value")
                b64 = value.get("content_b64") if isinstance(value, dict) else None
                if isinstance(b64, str):
                    item["encoding"] = "base64"
                    item["size"] = _b64_decoded_size(b64)
            out.append(item)
        return out

    def fs_delete(self, agent: AgentInfo, channel: str, path: str,
                  expect_version: int | None = None) -> bool:
        """Delete under the task/delivery lock so current-artifact checks hold."""
        with self.db.orchestration_lock:
            return self._fs_delete(agent, channel, path, expect_version)

    def _fs_delete(self, agent: AgentInfo, channel: str, path: str,
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
        # Gated in rooms whose owner opted in — and gated for EVERY seat, not
        # just delegates: the live falsification was a delegate that refused
        # the delete itself and handed it to a plain member.
        self._require_gate(channel, agent, "fs_remove")
        new_version = self.db.fs_remove(channel, FS_PREFIX + norm, agent.id, expect_version)
        if new_version is None:
            return False
        self._post_fs_audit(channel, agent.id, "delete", norm, new_version, 0)
        self._fs_trigger_after_write()
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
    DELEGATION_POWERS = frozenset(DELEGATE_POWERS)
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

    #: `proxy` means "act on my behalf", which an owner means for an
    #: afternoon and not for a quarter — so it defaults to a day and caps
    #: at a week, well under the 30-day ceiling the other powers use.
    PROXY_DEFAULT_TTL = 86400.0
    PROXY_MAX_TTL = 7 * 86400.0

    def set_delegation(self, agent_id: str, powers: list[str],
                       ttl_seconds: float | None = None,
                       note: str = "", scope: str = "",
                       mission: str | None = None) -> dict[str, Any]:
        """Operator grant (admin surface). The record is a verifiable LABEL
        plus a validation anchor (queue writes, tier fields) — it grants no
        other mechanical power (ADR-0004), with ONE deliberate exception:
        `proxy` (2026-08-04), which clears a channel's gated acts. That is
        why `proxy` alone is scoped and short-lived. Operators cannot be
        delegates: they already hold every power, and a dual role would blur
        audit.

        `mission` lets the operator write the seat's charge in the SAME act
        as the delegation. That closes the dead end where the grant is the
        moment the operator discovers the seat is blank."""
        if not self.db.agent_exists(agent_id):
            raise HubError(404, f"agent '{agent_id}' is not registered")
        if agent_id in self.operator_ids():
            raise HubError(400, f"'{agent_id}' is an operator — operators need "
                                "no delegation")
        if mission is not None:
            self.set_mission(agent_id, mission)
        if not (self.db.get_mission(agent_id) or "").strip():
            raise HubError(400,
                           f"'{agent_id}' has no mission — set one before "
                           f"delegating to it (`agora mission set {agent_id} "
                           f"…`). A delegate that does not know its job is "
                           f"the failure this refuses: the seat that shipped "
                           f"a build in three minutes had an empty one.")
        wanted = [str(p) for p in powers]
        unknown = set(wanted) - self.DELEGATION_POWERS
        if not wanted or unknown:
            raise HubError(400, f"powers must be a non-empty subset of "
                                f"{sorted(self.DELEGATION_POWERS)}"
                                + (f" (unknown: {sorted(unknown)})" if unknown else ""))
        proxy = PROXY_POWER in wanted
        default_ttl = self.PROXY_DEFAULT_TTL if proxy else self.DEFAULT_DELEGATION_TTL
        cap = self.PROXY_MAX_TTL if proxy else self.MAX_DELEGATION_TTL
        ttl = default_ttl if ttl_seconds is None else float(ttl_seconds)
        if not 0 < ttl <= cap:
            raise HubError(400, f"ttl must be within (0, {cap:.0f}s] "
                                + ("for a 'proxy' grant — acting on the "
                                   "owner's behalf is an afternoon's "
                                   "authority, not a standing one. "
                                   if proxy else "")
                                + "(expiry is deliberate: a forgotten "
                                  "delegation is worse than a renewal)")
        scope = str(scope or "").strip()
        if proxy and not scope:
            raise HubError(400, "a 'proxy' grant needs --scope: name the "
                                "channel it reaches, or type '*' for the "
                                "whole hub. Fleet-wide authority to act as "
                                "the owner must be chosen, never arrived at "
                                "by omission.")
        if scope and scope != "*" and self.db.get_channel(scope) is None:
            raise HubError(404, f"scope '{scope}' is not a channel")
        if scope and scope != "*" and not self.db.is_member(scope, agent_id):
            raise HubError(400, f"'{agent_id}' is not a member of '{scope}' "
                                "— a scoped delegation must point at a room "
                                "the seat can actually read and act in")
        grant = self.db.delegation_set(agent_id, wanted, time.time() + ttl,
                                       sanitize_text(note, 200, field="delegation note"), scope)
        self._delegations_cache_at = 0.0
        self._ensure_alerts_channel()
        self._post_system(
            self.DARK_ALERTS_CHANNEL,
            f"DELEGATION GRANTED: {agent_id} holds {'+'.join(grant['powers'])} "
            + (f"scoped to {'the whole hub' if scope == '*' else '#' + scope} "
               if scope else "")
            + f"until {time.strftime('%Y-%m-%d %H:%M', time.localtime(grant['expires_at']))}"
            f"{' — ' + grant['note'] if grant['note'] else ''}. Every agent can "
            f"verify via whoami.delegations; prose claims count for nothing.")
        # TELL THE SEAT ITS POWERS CHANGED (operator ruling, 2026-08-06).
        #
        # "IF the delegate has been given sufficient power to act on behalf of
        #  the user, and the user is not connected to the hub, then the
        #  delegate MUST act on behalf of the user."
        #
        # It cannot act on a power it has not been told it holds. Measured:
        # `g4-lead` opened a gate asking the absent operator to ratify a plan,
        # was granted `proxy` an hour later — the power to answer its own gate
        # — and sat blocked for hours because the grant was announced to
        # `hub-alerts` and to `whoami`, neither of which reaches a seat that
        # is not currently taking a turn. A power the holder cannot discover
        # is not a power.
        #
        # Addressed and OPEN: this is a real obligation, not a notice. The
        # seat must look at what it can now do and act on anything that was
        # waiting on exactly this.
        powers_line = "+".join(grant["powers"])
        where = ("the whole hub" if scope in ("", "*") else f"#{scope}")
        acts = []
        if PROXY_POWER in grant["powers"]:
            acts.append(
                "You hold PROXY: where the owner is absent, gated acts are "
                "YOURS to decide. If you are waiting on a gate addressed to "
                "an absent owner, answer it yourself and say why — waiting "
                "for someone who is not here is not caution, it is a stall.")
        if "reporting" in grant["powers"]:
            acts.append(
                "You hold REPORTING: the operator's requests in "
                f"{where} are yours to carry end to end and to report on.")
        try:
            self._post_system(
                self.DARK_ALERTS_CHANNEL,
                f"YOUR POWERS CHANGED: you now hold {powers_line} in "
                f"{where}"
                f"{', until ' + time.strftime('%Y-%m-%d %H:%M', time.localtime(grant['expires_at']))}"
                f"{'. ' + grant['note'] if grant['note'] else '.'}\n\n"
                + "\n".join(acts)
                + ("\n\nRe-read anything you parked waiting on a decision: "
                   "you may now be the one who makes it." if acts else ""),
                # ADDRESSED fyi, not open. `to-me` is an important flag, so
                # this wakes a driven listener — but kind=system means it is
                # not a directive debt, so it mints nothing for the hub to
                # discharge later. An open system message the hub never
                # closes is the 0093 class (measured: 8 undischargeable rows
                # on one delegate).
                to=[agent_id], status=Status.fyi,
                dedupe_key=f"powers:{agent_id}:{powers_line}:{scope}:"
                           f"{int(grant['expires_at'])}")
        except DuplicateMessage:
            pass          # the same grant re-announced: the ledger's guarantee
        return grant

    def revoke_delegation(self, agent_id: str) -> bool:
        revoked = self.db.delegation_revoke(agent_id)
        self._delegations_cache_at = 0.0
        if revoked:
            # A GRANT MUST NOT OUTLIVE THE AUTHORITY THAT MADE IT
            # (2026-08-07). Probe-confirmed: after revoking `proxy`, the
            # seat's `has_proxy` went False and a gated `fs_delete` still
            # SUCCEEDED — `_require_gate` scans for a `granted` row and never
            # asked whether the granting authority still exists. Revocation
            # was an announcement, not a revocation.
            #
            # Withdrawing rather than deleting: the row stays readable, so
            # the record of what was authorized and by whom survives.
            closed = 0
            for ch in self.db.channel_names():
                for entry in self.db.store_keys(ch):
                    key = entry["key"]
                    if not key.startswith(self._GATE_PREFIX):
                        continue
                    stored = self.db.store_get(ch, key)
                    if stored is None or not isinstance(stored.value, dict):
                        continue
                    v = stored.value
                    if (str(v.get("status") or "") == "granted"
                            and str(v.get("decided_by") or "") == agent_id
                            and v.get("under_proxy")):
                        self.db.store_set(
                            ch, key, {**v, "status": "withdrawn",
                                      "answer": "the proxy that granted this "
                                                "was revoked"},
                            "hub", None)
                        closed += 1
            self._ensure_alerts_channel()
            self._post_system(self.DARK_ALERTS_CHANNEL,
                              f"DELEGATION REVOKED: {agent_id} holds no "
                              f"delegated powers as of now."
                              + (f" {closed} gate grant(s) it made under "
                                 "proxy were withdrawn with it." if closed
                                 else ""))
            # ...AND TELL THE SEAT, the way a GRANT already does. The grant
            # side has told the holder since 2026-08-06 ("a power the holder
            # cannot discover is not a power"); the revoke side announced to
            # `hub-alerts` and to `whoami` and left the holder to find out by
            # being refused. A seat acting on authority the hub has silently
            # taken away is the same defect with the sign flipped, and it
            # fails at the worst moment — mid-act, on someone else's behalf.
            # `fyi`, not `open`: losing a power obliges no answer, and the
            # hub has nothing to discharge. (Unlike the grant, which asks the
            # seat to go act on what it can now do.)
            self._post_system(
                self.DARK_ALERTS_CHANNEL,
                f"YOUR POWERS WERE REVOKED: you ({agent_id}) hold no "
                "delegated powers as of now. Anything you were carrying "
                "under them stops here — say so where you were carrying it, "
                "rather than letting it look done. whoami.delegations is the "
                "only proof; prose claims count for nothing."
                + (f" {closed} gate grant(s) you made under proxy were "
                   "withdrawn with it." if closed else ""),
                # No dedupe_key: this twin fires exactly where the room-facing
                # announcement above fires, so it inherits that cadence. A
                # time-bucketed key here raised DuplicateMessage out of
                # `revoke_delegation` when two revokes landed in one second.
                to=[agent_id], status=Status.fyi)
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
                                  sanitize_text(reason, 200, field="reason"))
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
            # A channel kick/ban evicts the member — clear their votes and
            # ratings in that room like any other departure (adversary P1,
            # agora-0122: `leave` cleared but the moderation door did not,
            # so a drive-by downvoter who got kicked kept their -1 standing
            # while the membership gate blocked everyone's recourse).
            self.db.reputation_clear_rater(scope, agent_id)
            self.db.rating_clear_rater(scope, agent_id)
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
    _RULING_PREFIX = "ruling:"
    _RULING_FIELDS = {"text", "scope", "source_message_id", "active"}
    _QUEUE_FIELDS = {"q", "options", "evidence", "waiting", "since", "tier",
                     "default", "decided", "done_when", "note"}
    #: `decided` RETIRES the row from /board and /desk — both read it as a
    #: bare truthiness (`not value.get("decided")`). So the value is not
    #: decoration: it is the off switch, and until 2026-08-25 any prose
    #: satisfied it. delegate wrote an explanatory sentence into
    #: `queue:laurent:publish-abstracttui-060` and the row vanished from
    #: every surface the operator reads — invisibly, because a store write
    #: rings nobody and a retired row looks exactly like a decided one
    #: (operator-board#27). The field's own refusal already said what it was
    #: for ("the decision:<slug> or message ref that settled it") and the
    #: hub accepted anything anyway, which is the gap: a stated contract no
    #: check enforced. Now it must LOOK like a settlement, so retiring a row
    #: is an act a writer performs on purpose. Prose has a home of its own
    #: (`note`), because refusing the old habit without offering the field
    #: it was reaching for would just move the problem.
    _DECIDED_CITATION = re.compile(
        r"^(?:decision:[a-z0-9][a-z0-9._-]*"      # a decision row
        r"|[a-z0-9][a-z0-9:._-]*#\d+"             # channel#seq (dm:a--b#12 too)
        r"|#\d+"                                  # bare #seq, same channel
        r"|[0-9A-HJKMNP-TV-Z]{26})$",             # a message ULID
        re.IGNORECASE)
    #: ...AND THE MODAL CASE A CITATION CANNOT EXPRESS. Applying
    #: `decision:a-closed-vocabulary-must-express-the-modal-case` to my own
    #: new check: a row can also stop being a question with nothing to point
    #: at — it evaporated, or another row swallowed it. The first draft of
    #: this check refused exactly that and broke
    #: `test_an_EXISTING_queue_row_stays_closable_after_its_seat_leaves`,
    #: whose whole point is that a stranded row must stay retireable (store
    #: keys cannot be deleted, so overwriting IS the only retirement). A
    #: check that makes an honest act impossible gets routed around, and the
    #: route around this one is a fake citation, which is worse than the
    #: prose it replaced.
    _DECIDED_WORDS = frozenset({"withdrawn", "moot", "superseded",
                                "duplicate", "obsolete"})
    #: done_when (0111/M3): a queue/desk row waiting on a HUB-OBSERVABLE act
    #: carries a machine-checkable completion predicate, evaluated at desk
    #: read time — the row self-clears the moment the act happens, so a
    #: "waiting on you: retire agency" row can never outlive the retirement
    #: (the c3860 trigger incident). Closed vocabulary: waits on facts the
    #: hub cannot observe (a PyPI click, an external CI run) carry NO
    #: predicate and stay manual forever — that is honest, not a gap.
    _DONE_WHEN_KINDS = {
        "retired": ("agent",),
        "decision": ("channel", "slug"),
        "work_status": ("channel", "item", "status"),
        "delegation": ("agent", "power"),
        "closed": ("channel", "message_id"),
        "gate": ("channel", "slug"),
    }

    #: A GATE is a delegate stopping at a key decision to ask its owner
    #: (2026-08-04). The row is the durable half — a `blocked` ask alone
    #: leaves no record a later turn or a fresh session can find, and the
    #: live test showed exactly that: the control delegate asked its question
    #: in prose, and nothing on the hub knew a decision was pending.
    _GATE_PREFIX = "gate:"
    #: `why` and `decided_by` added 2026-08-07. Measured: a delegate
    #: deciding under `proxy` COULD NOT RECORD WHY — the field set is closed
    #: and `why`/`rationale`/`decided_by` were each a 400. The operator asked
    #: for exactly the opposite: "it mentions which gated decisions it has to
    #: take and the rationale it followed... it then gives an understanding
    #: of the situation to the user, and possibly a way to reactively
    #: intervene."
    #:
    #: `why` is ACCEPTED, not required. A required rationale measures
    #: compliance, not thought — this file has the receipts (a mandatory
    #: hourly report was satisfiable with 40 characters of filler for ten
    #: hours). `decided_by` is HUB-SET and cannot be supplied: it is the
    #: seat that actually moved the row, which diverges from `updated_by`
    #: whenever a delegate transcribes an owner's spoken answer.
    _GATE_FIELDS = {"owner", "asked_by", "acts", "status", "q", "options",
                    "default", "ask_message", "answer", "discharged_by",
                    "expires_at", "why", "decided_by", "under_proxy"}
    _GATE_STATUSES = ("asked", "granted", "denied", "answered", "withdrawn")
    #: Only the OWNER may move a gate to a deciding state. Reached through
    #: the same identity check `claim.owner` uses, because a gate a delegate
    #: can grant itself is not a gate.
    _GATE_DECIDING = ("granted", "denied", "answered")

    @staticmethod
    def _validate_ruling_row(value: Any) -> None:
        """0113: standing operator rulings — visible, versioned, citable."""
        if not isinstance(value, dict):
            raise HubError(400, "ruling rows must be objects (see docs: 0113)")
        unknown = set(value) - HubService._RULING_FIELDS
        if unknown:
            raise HubError(400, f"unknown ruling fields: {sorted(unknown)} "
                                f"(allowed: {sorted(HubService._RULING_FIELDS)})")
        text = value.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise HubError(400, "ruling needs text: the standing constraint "
                                "(<=2000 chars)")
        value["text"] = sanitize_text(text, 2000, field="ruling text")
        scope = value.get("scope")
        if not isinstance(scope, list) or not scope or len(scope) > 32:
            raise HubError(400, "ruling scope must be a non-empty list of seat "
                                "ids (<=32) or [\"*\"] for fleet-wide")
        if scope == ["*"]:
            value["scope"] = ["*"]
        else:
            cleaned = [sanitize_text(str(s), 64, field="ruling scope entry")
                       for s in scope if str(s).strip()]
            if not cleaned:
                raise HubError(400, "ruling scope must name at least one seat "
                                    "or [\"*\"] for fleet-wide")
            value["scope"] = cleaned
        source = value.get("source_message_id")
        if not isinstance(source, str) or not source.strip() or len(source) > 128:
            raise HubError(400, "ruling needs source_message_id: the operator "
                                "message this derives from (<=128 chars)")
        value["source_message_id"] = sanitize_text(source.strip(), 128,
                                                   field="source_message_id")
        active = value.get("active", True)
        if not isinstance(active, bool):
            raise HubError(400, "ruling active must be a boolean")
        value["active"] = active

    def _ruling_applies_to(self, agent_id: str, scope: list[str]) -> bool:
        return "*" in scope or agent_id in scope

    def _active_ruling_entries(self, channel: str,
                               agent_id: str) -> list[dict[str, Any]]:
        """Active ruling:* rows in scope for this seat (0113)."""
        rows: list[dict[str, Any]] = []
        for entry in self.db.store_keys(channel):
            if not entry["key"].startswith(self._RULING_PREFIX):
                continue
            stored = self.db.store_get(channel, entry["key"])
            if stored is None or not isinstance(stored.value, dict):
                continue
            if not stored.value.get("active", True):
                continue
            scope = stored.value.get("scope") or []
            if not isinstance(scope, list) or not self._ruling_applies_to(
                    agent_id, [str(s) for s in scope]):
                continue
            rows.append({"key": entry["key"], "value": stored.value,
                           "version": stored.version,
                           "updated_by": stored.updated_by})
        return rows

    def _unacknowledged_rulings(self, agent: AgentInfo,
                                channel: str) -> list[dict[str, Any]]:
        """Rulings in scope whose store version is newer than the seat's ack."""
        out: list[dict[str, Any]] = []
        for row in self._active_ruling_entries(channel, agent.id):
            seen = self.db.ruling_receipt_get(agent.id, channel, row["key"])
            if seen is not None and seen >= row["version"]:
                continue
            out.append({**row, "ack_version": seen})
        return out

    def ack_rulings(self, agent: AgentInfo, channel: str,
                    keys: list[str]) -> dict[str, Any]:
        """Record acknowledgment of active rulings (0113 charter-read pattern)."""
        self.require_membership(channel, agent.id)
        self._require_unpaused(agent, channel)
        acked: list[str] = []
        for key in keys:
            if not key.startswith(self._RULING_PREFIX):
                raise HubError(400, f"'{key}' is not a ruling key "
                                    "(expected ruling:<slug>)")
            stored = self.db.store_get(channel, key)
            if stored is None or not isinstance(stored.value, dict):
                raise HubError(404, f"ruling '{key}' not found in '{channel}'")
            if not stored.value.get("active", True):
                raise HubError(409, f"ruling '{key}' is revoked — nothing to ack")
            scope = stored.value.get("scope") or []
            if not isinstance(scope, list) or not self._ruling_applies_to(
                    agent.id, [str(s) for s in scope]):
                raise HubError(403, f"ruling '{key}' is not in your scope")
            self.db.ruling_receipt_set(agent.id, channel, key, stored.version)
            acked.append(key)
        return {"channel": channel, "agent_id": agent.id, "acked": acked}

    def _require_rulings_ack(self, channel: str, agent: AgentInfo) -> None:
        """0113 opt-in gate: scoped seats must ack current standing rulings."""
        meta = self.db.store_get(channel, CHANNEL_META_KEY)
        if not (meta and isinstance(meta.value, dict)
                and meta.value.get("rulings_required")):
            return
        unacked = self._unacknowledged_rulings(agent, channel)
        if not unacked:
            return
        keys = ", ".join(r["key"] for r in unacked[:5])
        more = f" (+{len(unacked) - 5} more)" if len(unacked) > 5 else ""
        raise HubError(409, f"this channel requires acknowledging standing "
                            f"rulings first: GET /channels/{channel}/digest "
                            f"(see unacknowledged_rulings), then POST "
                            f"/channels/{channel}/ruling-acks — pending: "
                            f"{keys}{more}")

    def _require_gate(self, channel: str, agent: AgentInfo,
                      act_class: str) -> None:
        """Refuse a gated act unless this seat may perform it here.

        BINDS ON THE CHANNEL, NOT THE SEAT — the live falsification that
        produced this design: a delegate that correctly refused to delete
        two files then DISPATCHED the delete to a plain member, who did it
        (delegation-lab#9-#12, 2026-08-04). A seat-scoped gate is bypassed
        by one addressed ask, so the room is the only boundary that holds.

        And it is a REFUSAL, not teaching, because the second arm proved
        teaching negotiable: a delegate read the charter, correctly named
        the delete a key decision, then deleted anyway reasoning "restorable
        via version history" — true about the hub, false about the owner,
        who had wanted the files kept. Recovery was lossy: the drafts came
        back re-authored, not as the bytes that were there.

        Passes for the channel owner, an operator, a `proxy` delegate scoped
        here, or a `gate:<slug>` row the OWNER granted. Silent no-op in any
        room whose owner has not opted in."""
        meta = self.db.store_get(channel, CHANNEL_META_KEY)
        classes = ((meta.value or {}).get("gated_acts")
                   if meta is not None and isinstance(meta.value, dict) else None)
        if not classes or act_class not in classes:
            return
        if agent.operator or self._is_channel_owner(channel, agent.id):
            return
        principal = self.db.get_channel(channel).created_by
        if self.proxy_allowed(agent.id, channel, principal):
            return
        for entry in self.db.store_keys(channel):
            key = entry["key"]
            if not key.startswith(self._GATE_PREFIX):
                continue
            row = self.db.store_get(channel, key)
            if row is None or not isinstance(row.value, dict):
                continue
            value = row.value
            if str(value.get("status") or "").lower() != "granted":
                continue
            # A grant authorizes ONE named act, for the ONE seat that asked.
            # Without both scopes it was a skeleton key: a single grant about
            # picking a title let an unrelated member delete files and write
            # decisions (reproduced 2026-08-04). An owner answering a question
            # is not handing out the room.
            if act_class not in (value.get("acts") or []):
                continue
            if str(value.get("asked_by") or "") != agent.id:
                continue
            expires = value.get("expires_at")
            if expires is not None:
                try:
                    if float(expires) < time.time():
                        continue
                except (TypeError, ValueError):
                    pass
            return
        raise HubError(
            403, f"'{act_class}' is a GATED act in #{channel}: its owner "
                 f"requires the owner's word before it happens. Acting under "
                 f"proxy needs a scoped grant AND this operator's explicit "
                 f"unexpired absence; otherwise ask instead of acting — post "
                 f"status=blocked, to=[owner], title 'gate: <slug>', with at "
                 f"most three plain questions, and write a gate:<slug> row "
                 f"naming the owner. Passing this act to another seat is "
                 f"laundering, not delegation: it is refused for them too.")

    def has_proxy(self, agent_id: str, channel: str) -> bool:
        """Does this seat hold `proxy` that reaches THIS channel?

        Scope is the concession the design's own strongest objection forced:
        an unscoped `proxy` would let one grant, taken to unstick one room,
        silently clear every gate on the hub for its whole TTL — the same
        unrevocable standing authority the power exists to avoid. A grant
        carries `scope`; only the literal `*` reaches everywhere, and it has
        to be typed."""
        for d in self.active_delegations():
            if d["agent_id"] != agent_id or PROXY_POWER not in d["powers"]:
                continue
            scope = str(d.get("scope") or "").strip()
            # An UNSCOPED proxy grant reaches nothing. Fleet-wide authority
            # must be typed (`--scope '*'`), never arrived at by omission —
            # that is the whole lesson of the operator flag, which has no
            # revocation path because nobody decided it should be permanent.
            if scope == "*" or (scope and scope == channel):
                return True
        return False

    def _delegation_powers_for(self, agent_id: str,
                               channel: str | None = None) -> set[str]:
        """Active delegated powers this seat can spend here."""
        powers: set[str] = set()
        for d in self.active_delegations():
            if d["agent_id"] != agent_id:
                continue
            scope = str(d.get("scope") or "").strip()
            if channel is not None and scope not in ("", "*", channel):
                continue
            powers.update(str(p) for p in (d.get("powers") or ()))
        return powers

    def _is_channel_owner(self, channel: str, agent_id: str) -> bool:
        info = self.db.get_channel(channel)
        return info is not None and info.created_by == agent_id

    #: Powers that let a delegate act for the room the way its owner does.
    #: Same pair `PHASE_POWERS` uses — a seat trusted to declare which
    #: version of an artifact is in force is trusted to run the room.
    CHANNEL_AUTHORITY_POWERS = frozenset({"ruling", "operational"})

    def may_administer_channel(self, agent: AgentInfo, channel: str) -> bool:
        """May this seat perform an OWNER's act here — invite, hand over
        ownership, write the charter and the `channel:` rows?

        Owner, operator, or a delegate holding `ruling`/`operational` that
        REACHES this channel (`proxy` counts too: acting on the owner's
        behalf is what it is for). Scope is honoured — an unscoped grant
        reaches nothing and a grant scoped elsewhere does not leak here, the
        same rule `has_proxy` enforces.

        ONE predicate for all four acts on purpose. They were written
        separately and drifted: archiving accepted owner-or-operator, invites
        owner ONLY, `channel/` files owner-or-operator, and phase rows
        owner/operator/ruling-delegate/steward — so a delegate appointed to
        run a room could declare its phases but not invite the seat it needed
        to do the work."""
        if agent.operator or self._is_channel_owner(channel, agent.id):
            return True
        if agent.id in self.ruling_delegate_ids(channel):
            return True
        info = self.db.get_channel(channel)
        return bool(info and self.proxy_allowed(agent.id, channel, info.created_by))

    def transfer_channel_ownership(self, agent: AgentInfo, channel: str,
                                   new_owner: str) -> dict[str, Any]:
        """Hand a room to another seat.

        There was no such operation: `created_by` was written once at creation
        and never again, so an owner who wanted to hand a room over had no
        move to make, and several refusals said so in passing ("there is no
        ownership transfer"). That made ownership an accident of who typed
        `create_channel` first, permanent for the life of the room.

        Who may: the owner, an operator, or a delegate holding
        ruling/operational scoped here — the same predicate as invites and
        charter writes. The new owner must already be a MEMBER: handing a room
        to a seat that cannot read it strands it. DMs are ownerless by
        construction and are refused."""
        self._require_unpaused(agent)
        self._require_not_archived(channel)
        # No membership requirement on the CALLER, matching `archive_channel`:
        # an operator must be able to unstick a room they do not sit in, which
        # is the case that makes this verb worth having.
        if channel.startswith(DM_PREFIX):
            raise HubError(400, "a DM is ownerless — neither peer owns the "
                                "other's record, so there is nothing to hand over")
        info = self.db.get_channel(channel)
        if info is None:
            raise HubError(404, f"no such channel '{channel}'")
        self._require_channel_admin(agent, channel, "transferring ownership")
        if not self.db.agent_exists(new_owner):
            raise HubError(404, f"agent '{new_owner}' is not registered")
        if not self.db.is_member(channel, new_owner):
            raise HubError(
                409,
                f"'{new_owner}' is not a member of '{channel}' — invite them "
                "first, or they would own a room they cannot read")
        previous = info.created_by
        if previous == new_owner:
            return {"channel": channel, "owner": new_owner, "changed": False}
        self.db.transfer_channel_ownership(channel, new_owner)
        # On the record, in the room: ownership decides who may invite, write
        # the charter and archive, so a silent handover leaves every member
        # guessing who to ask.
        self._post_system(
            channel,
            f"channel ownership transferred: {previous} -> {new_owner} "
            f"(by {agent.id}). {new_owner} now owns the charter, the "
            "`channel:` rows, invites and the room's end.",
            status=Status.fyi)
        self._post_system(
            channel,
            f"YOU NOW OWN '{channel}' ({new_owner}), handed over by "
            f"{agent.id}. Yours now: the charter (`channel/charter.md`), the "
            "`channel:` metadata rows, minting invites, and archiving the "
            "room when the work is done.",
            to=[new_owner], status=Status.fyi)
        self._log_event("ownership-transferred", channel=channel,
                        previous=previous, owner=new_owner, by=agent.id)
        return {"channel": channel, "owner": new_owner, "previous": previous,
                "changed": True, "by": agent.id}

    def _require_channel_admin(self, agent: AgentInfo, channel: str,
                               act: str) -> None:
        """`may_administer_channel`, as a teaching refusal."""
        if self.may_administer_channel(agent, channel):
            return
        info = self.db.get_channel(channel)
        owner = info.created_by if info is not None else "the owner"
        raise HubError(
            403,
            f"{act} in '{channel}' needs the channel owner ({owner}), an "
            "operator, or a delegate holding ruling/operational scoped to "
            f"this channel. Ask {owner} or an operator — or ask the operator "
            "to delegate it to you.")

    #: How long a principal must be silent before a proxy holder may act in
    #: their name. Well above the 600s presence window on purpose: presence
    #: `offline` means NO CONTACT, never "gone", and an operator reading the
    #: room in a browser makes no calls. A misread here hands someone's
    #: authority away, so the bar is an hour of true silence, not a lapsed
    #: heartbeat.
    PROXY_ABSENCE_SECONDS = 3600.0

    def _out_of_contact(self, agent_id: str) -> bool:
        """Has this seat had NO contact with the hub for long enough that
        waiting on it is a stall rather than patience?"""
        try:
            row = self.presence.get(agent_id)
        except Exception:
            return False
        state = str(getattr(row, "state", "") or "")
        seen = float(getattr(row, "updated_at", 0.0) or 0.0)
        if state == "active":
            return False
        return (time.time() - seen) >= self.PROXY_ABSENCE_SECONDS

    def _validate_gate_row(self, value: Any, agent: AgentInfo,
                           current: Any, channel: str | None = None) -> None:
        """Shape + WRITE AUTHORITY for `gate:<slug>`.

        The asker opens the gate and may never decide it; the named owner
        (or an operator) decides. Without this the row degrades into prose:
        live, two unvalidated gate rows silently LOST their question and
        their default on the first update, leaving a status word with
        nothing to answer."""
        if not isinstance(value, dict):
            raise HubError(400, "gate rows must be objects: "
                                "{owner, status, q, options?}")
        unknown = set(value) - self._GATE_FIELDS
        if unknown:
            raise HubError(400, f"unknown gate field(s): {sorted(unknown)} — "
                                f"allowed: {sorted(self._GATE_FIELDS)}")
        status = str(value.get("status") or "").strip().lower()
        if status not in self._GATE_STATUSES:
            raise HubError(400, f"gate status must be one of "
                                f"{list(self._GATE_STATUSES)}")
        owner = str(value.get("owner") or "").strip()
        if not owner:
            raise HubError(400, "a gate names the OWNER it is waiting on: "
                                "{'owner': '<seat>'} — a gate addressed to "
                                "nobody is a note, not a gate")
        prior = current.value if current is not None and isinstance(
            getattr(current, "value", None), dict) else None
        if prior is not None and str(prior.get("owner") or "") != owner:
            raise HubError(403, "a gate's owner cannot be reassigned — open "
                                "a new gate if the decider changed")
        if status in self._GATE_DECIDING:
            decider = prior.get("owner") if prior else owner
            # A PROXY HOLDER ANSWERS FOR AN ABSENT OWNER (operator ruling,
            # 2026-08-06): "IF the delegate has been given sufficient power to
            # act on behalf of the user, and the user is not connected to the
            # hub, then the delegate MUST act on behalf of the user."
            #
            # `proxy` exists precisely to be the owner's hand while the owner
            # is away, and it is scope-typed so it cannot leak past the room
            # it was granted for. Refusing the holder here made the power
            # unusable at the only moment it is for: `g4-lead` held proxy on
            # its room and still sat blocked for hours on a gate addressed to
            # an operator who was never going to answer.
            # ...but ONLY while the owner is actually away. The conditional
            # was inverted (2026-08-07 audit): as written, a proxy holder
            # could overrule a PRESENT owner — the abuse `proxy` is scoped
            # and short-lived to prevent — while the ruling it cites has the
            # opposite antecedent ("and the user is not connected to the
            # hub"). Proxy is the owner's hand while they are away, not a
            # second vote while they are here.
            proxy_ok = (channel is not None
                        and self.proxy_allowed(agent.id, channel, decider))
            # The DECIDER is stamped by the hub, never supplied: `updated_by`
            # records who typed, which is a different fact whenever a
            # delegate transcribes an absent owner's answer.
            value["decided_by"] = agent.id
            if proxy_ok:
                value["under_proxy"] = True
            if not (agent.operator or agent.id == decider or proxy_ok):
                raise HubError(
                    403, f"only {decider} (or an operator) can answer this "
                         f"gate — you opened it, so you may not also decide "
                         f"it. Post the question and wait.")
        elif prior is None and value.get("asked_by") not in (None, agent.id):
            raise HubError(403, "gate.asked_by must be you")
        acts = value.get("acts")
        if acts is not None:
            if not isinstance(acts, list):
                raise HubError(400, "gate.acts is the list of act classes this "
                                    f"gate asks for, from {sorted(GATED_ACT_CLASSES)}")
            unknown_acts = {str(a) for a in acts} - GATED_ACT_CLASSES
            if unknown_acts:
                raise HubError(400, f"unknown act class(es) in gate.acts: "
                                    f"{sorted(unknown_acts)} — allowed: "
                                    f"{sorted(GATED_ACT_CLASSES)}")
            value["acts"] = sorted({str(a) for a in acts})
        if status == "granted" and not value.get("acts"):
            # FAIL CLOSED. A grant that names no act authorized everything —
            # the skeleton-key bug. If the owner is saying yes, the row must
            # say yes TO WHAT.
            raise HubError(400, "a granted gate must name the act(s) it "
                                "authorizes: acts=[...] from "
                                f"{sorted(GATED_ACT_CLASSES)}. A grant that "
                                "names nothing would authorize everything.")
        if status == "granted" and not value.get("asked_by"):
            raise HubError(400, "a granted gate must keep `asked_by`: a grant "
                                "authorizes the seat that asked, not the room")
        value["q"] = sanitize_text(str(value.get("q") or ""), 200, field="gate question")
        opts = value.get("options")
        if opts is not None:
            if not isinstance(opts, list) or len(opts) > 4:
                raise HubError(400, "gate options: a list of at most 4 "
                                    "plain choices")
            value["options"] = [sanitize_text(str(o), 120, field="gate option")
                                for o in opts]
        for field in ("default", "answer", "ask_message", "discharged_by"):
            if value.get(field) is not None:
                value[field] = sanitize_text(str(value[field]), 200,
                                             field=f"gate {field}")

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
        value["q"] = sanitize_text(q, 120, field="phase question")
        for row_field, cap, item_cap in (("options", 5, 120),
                                         ("evidence", 8, 80),
                                         ("waiting", 10, 64)):
            items = value.get(row_field)
            if items is None:
                continue
            if (not isinstance(items, list) or len(items) > cap
                    or any(not isinstance(x, str) or len(x) > item_cap for x in items)):
                raise HubError(400, f"queue-row {row_field} must be <= {cap} strings "
                                    f"of <= {item_cap} chars")
            value[row_field] = [sanitize_text(x, item_cap, field=f"queue-row {row_field} entry")
                                for x in items]
        tier = value.get("tier")
        if tier is not None and tier not in ("operator", "delegate"):
            raise HubError(400, "queue-row tier must be 'operator' or 'delegate'")
        done_when = value.get("done_when")
        if done_when is not None:
            if not isinstance(done_when, dict):
                raise HubError(400, "done_when must be an object")
            kind = done_when.get("kind")
            if kind not in HubService._DONE_WHEN_KINDS:
                raise HubError(400, "done_when.kind must be one of "
                                    f"{'|'.join(sorted(HubService._DONE_WHEN_KINDS))}"
                                    " — waits on facts the hub cannot observe "
                                    "carry no predicate (they stay manual)")
            required = HubService._DONE_WHEN_KINDS[kind]
            missing = [f for f in required if not str(done_when.get(f, "")).strip()]
            unknown = set(done_when) - {"kind", *required}
            if missing or unknown:
                raise HubError(400, f"done_when kind={kind} needs fields "
                                    f"{list(required)}"
                                    + (f"; missing {missing}" if missing else "")
                                    + (f"; unknown {sorted(unknown)}" if unknown else ""))
            value["done_when"] = {"kind": kind, **{f: sanitize_text(
                str(done_when[f]), 128, field=f"done_when.{f}")
                for f in required}}
        default = value.get("default")
        if default is not None:
            if not isinstance(default, str) or len(default) > 160:
                raise HubError(400, "queue-row default must be a string <= 160 "
                                    "chars (what happens if nobody decides)")
            value["default"] = sanitize_text(default, 160, field="default")
        since = value.get("since")
        if since is not None and not isinstance(since, (int, float)):
            raise HubError(400, "queue-row since must be a unix timestamp")
        note = value.get("note")
        if note is not None:
            if not isinstance(note, str) or len(note) > 200:
                raise HubError(400, "queue-row note must be a string <= 200 "
                                    "chars — context for the operator that "
                                    "does NOT retire the row (that is "
                                    "`decided`)")
            value["note"] = sanitize_text(note, 200, field="note")
        decided = value.get("decided")
        if decided is not None:
            if not isinstance(decided, str) or len(decided) > 200:
                raise HubError(400, "queue-row decided must be a string <= 200 "
                                    "chars (the decision:<slug> or message ref "
                                    "that settled it)")
            ref = decided.strip()
            # Empty stays legal and means NOT decided: clearing the field is
            # how a row comes back, and refusing "" would make un-retiring
            # harder than retiring.
            if (ref and ref.lower() not in HubService._DECIDED_WORDS
                    and not HubService._DECIDED_CITATION.match(ref)):
                words = "|".join(sorted(HubService._DECIDED_WORDS))
                raise HubError(400,
                    "queue-row `decided` RETIRES this row from the operator's "
                    "board and desk — it is the off switch, not a comment. "
                    "Say what settled it:\n"
                    '  "decided": "decision:<slug>"   |   "<channel>#<seq>"   '
                    '|   "#<seq>"   |   a message id\n'
                    f'  ...or, when there is nothing to cite: "{words}"\n'
                    f"Got: {elide(ref, 60)!r}. If you meant to leave context "
                    'and KEEP the row visible, use "note" instead — same cap, '
                    "no retirement.")
            value["decided"] = sanitize_text(ref, 200, field="decision")
    # -- phase rows (0140/2): the version invariant the fleet could not hold ------
    #
    # Live finding (at-test, 2026-07-31), operator's own words: "one seat
    # working on v4 while another was working on v3. No seat should work on a
    # v4 until v3 is declared complete. That's why I nominated reader as
    # delegate — possibly we just need an orchestrator who declares those for
    # the hub channel."
    #
    # A `phase:<track>` store row is that declaration, made MACHINE-READABLE:
    # {current, status, next, steward, paths, note} — versioned by the existing
    # CAS store, so every transition is attributable and auditable, and
    # declared_by/declared_at are hub-stamped so the record cannot be forged.
    #
    # ENFORCEMENT IS ADVISORY BY CONSTRUCTION, and that is the whole design.
    # The hub cannot read minds: it does not know what a message or a file
    # edit "works on", so any gate would have to guess, and a wrong guess
    # blocks legitimate speech — the one thing the operator's standing
    # principle forbids. What the hub CAN do is make the current phase
    # impossible to miss (digest, channel info, the owed header that leads
    # every reception pass) and ring a non-blocking doorbell when a write
    # lands on a path the row itself registers. Nothing here refuses a post,
    # a reply, or an fs write; the invariant is held by seats who can SEE it.
    #
    # Rejected alternatives, and why:
    #  - Phase as a CAS FENCE (fs writes must pass the phase version; a
    #    transition 409s every in-flight writer). Hard-blocks the legitimate
    #    case — a seat fixing a v3 defect after v4 opened is doing exactly
    #    what the phase order wants — and doubles the reads on every write.
    #  - Phase in the PATH ("v3/manuscript.md"). Fine as a convention, wrong
    #    as the primitive: it fragments the artifact, breaks fs_history
    #    continuity across versions, and makes "which version is current"
    #    LESS discoverable — which was the actual failure.
    #  - Phase as an obligation (the steward posts an open ask "declare v3
    #    complete?"). Already expressible today, and it is what failed: the
    #    fleet had threads, what it lacked was one current-phase FACT every
    #    reception pass reads without asking anyone.

    # =====================================================================
    # TASKS (0.18.0): one row per operator request, from mint to acceptance
    # =====================================================================
    #
    # The record showed the same failure in every fleet room: "delivered" was
    # asserted by the seat that built the thing and nothing ever recorded
    # whether the person who asked for it agreed (at-test #293/#294, scifi
    # #204/#211, agora-wui #100, rtype-g4 #167). A `task:` row is the
    # smallest object that carries a request across rooms and turns and
    # keeps DELIVERED and ACCEPTED apart:
    #
    #   open --(cited completion report)--> delivered --(requester)--> accepted
    #                                        \--(requester: rejected)--> open
    #
    # The hub mints it when an operator's open/blocked root lands in a
    # shared room, stamps `delivered` when a cited `resolved` from the
    # reporting delegate or a named seat lands on that root, and stamps
    # `accepted` when the requester's own `resolved` lands. `rejected` is the
    # requester's verdict written on the row; the task re-opens and counts
    # the rejection. Everything the hub knows is STAMPED (requester, report,
    # evidence, who decided, when); a writer supplies only coordinator,
    # rooms, title and the verdict. No new table, no new endpoint: it is a
    # store row with a validator, like `phase:`.
    _TASK_PREFIX = "task:"
    _TASK_FIELDS = {"source", "requester", "coordinator", "rooms", "status",
                    "title", "report", "evidence", "delivered_by",
                    "delivered_at", "decided_by", "decided_at", "verdict",
                    "rejections", "declared_by", "declared_at",
                    "primary_channel", "director", "depends_on", "work_type", "parent", "purpose", "consultations"}
    _TASK_STAMPED = {"requester", "report", "evidence", "delivered_by",
                     "delivered_at", "decided_by", "decided_at", "rejections",
                     "declared_by", "declared_at"}
    TASK_STATUSES = ("open", "delivered", "accepted", "rejected")

    def _delegation_reaches(self, agent_id: str, channel: str,
                            powers: tuple[str, ...]) -> bool:
        """Does `agent_id` hold one of `powers` with a scope covering
        `channel` (hub-wide, `*`, or that channel)?"""
        for d in self.active_delegations():
            if d["agent_id"] != agent_id:
                continue
            if not any(p in (d.get("powers") or ()) for p in powers):
                continue
            scope = str(d.get("scope") or "").strip()
            if scope in ("", "*") or scope == channel:
                return True
        return False

    def _resolve_source(self, channel: str, src: Any) -> Message | None:
        """A task's `source`: `channel#seq` or a message id, in this channel."""
        ref = str(src or "").strip()
        if not ref:
            return None
        if "#" in ref:
            ch, _, seq = ref.rpartition("#")
            if not seq.isdigit() or (ch and ch != channel):
                return None
            return self.db.get_message_by_seq(channel, int(seq))
        m = self.db.get_message(ref)
        return m if m is not None and m.channel == channel else None

    def _task_key_for(self, channel: str, seq: int) -> str | None:
        """The task row minted for `channel#seq`, if one exists."""
        key = f"{self._TASK_PREFIX}msg-{seq}"
        return key if self.db.store_get(channel, key) is not None else None

    def _task_writer_refusal(self, channel: str, agent: AgentInfo,
                             current: Any) -> str | None:
        """Who may write a task row: the requester, an operator, the
        coordinator, or a delegate whose grant reaches this channel."""
        if agent.operator:
            return None
        value = (current.value if current is not None
                 and isinstance(current.value, dict) else {})
        if agent.id in (value.get("requester"), value.get("coordinator")):
            return None
        if self._delegation_reaches(agent.id, channel,
                                    ("reporting", "ruling", "operational")):
            return None
        if self.proxy_allowed(agent.id, channel, value.get("requester")):
            return None
        if current is None:
            return None   # anyone may mint a task from a root they can read
        return (f"a task row is written by its requester, its coordinator, "
                "an operator, or a delegate whose grant reaches this "
                "channel — ask one of them")

    def _validate_task_row(self, channel: str, value: Any, agent: AgentInfo,
                           current_row: Any = None) -> None:
        """Shape-check a `task:*` row, carry the hub-stamped fields forward,
        and apply the requester's verdict."""
        if not isinstance(value, dict):
            raise HubError(400, "task rows must be objects: {source, "
                                "coordinator?, rooms?, title?, status?, "
                                "verdict?}")
        unknown = set(value) - self._TASK_FIELDS
        if unknown:
            raise HubError(400, f"unknown task fields: {sorted(unknown)} "
                                f"(allowed: {sorted(self._TASK_FIELDS)})")
        prior = (current_row.value if current_row is not None
                 and isinstance(current_row.value, dict) else {})
        # Stamped fields are the hub's: a writer never sets them.
        for field_name in self._TASK_STAMPED:
            if field_name in prior:
                value[field_name] = prior[field_name]
            else:
                value.pop(field_name, None)
        src = value.get("source") or prior.get("source")
        source = self._resolve_source(channel, src)
        if source is None or source.kind != Kind.message:
            raise HubError(400, "task needs source: the request it tracks, "
                                "as '<channel>#<seq>' or a message id in "
                                "this channel")
        if source.reply_to is not None:
            raise HubError(400, "a task tracks a thread ROOT (the request "
                                "itself), not a reply")
        if prior.get("source") and prior["source"] != f"{channel}#{source.seq}":
            raise HubError(403, "a task source and requester are immutable")
        value["source"] = f"{channel}#{source.seq}"
        value["requester"] = source.sender
        title = value.get("title", prior.get("title") or source.title or "")
        if not isinstance(title, str) or len(title) > 120:
            raise HubError(400, "task title must be a string of <= 120 chars")
        value["title"] = sanitize_text(title.strip(), 120, field="task title")
        coord = value.get("coordinator", prior.get("coordinator"))
        if coord is not None:
            if not isinstance(coord, str) or len(coord) > 64:
                raise HubError(400, "task coordinator must be a seat id")
            coord = coord.strip() or None
        value["coordinator"] = coord
        rooms = value.get("rooms", prior.get("rooms") or [])
        if (not isinstance(rooms, list) or len(rooms) > 16
                or any(not isinstance(r, str) or not r.strip() or len(r) > 100
                       for r in rooms)):
            raise HubError(400, "task rooms must be <= 16 channel names")
        value["rooms"] = [r.strip() for r in rooms]
        status = value.get("status", prior.get("status") or "open")
        if status not in self.TASK_STATUSES:
            raise HubError(400, "task status must be one of "
                                f"{', '.join(self.TASK_STATUSES)}")
        prior_status = prior.get("status") or "open"
        verdict = value.get("verdict")
        if verdict is not None and (not isinstance(verdict, str)
                                    or len(verdict) > 400):
            raise HubError(400, "task verdict must be a string of <= 400 chars")
        if status != prior_status:
            if prior_status == "accepted":
                raise HubError(400, "an accepted task does not re-open — mint "
                                    "a new one from a new request")
            if status == "delivered":
                raise HubError(400, "`delivered` is stamped by the hub when a "
                                    "cited `resolved` lands on the request — "
                                    "post the completion report instead")
            if status in ("accepted", "rejected"):
                requester = value["requester"]
                if not (agent.operator or agent.id == requester
                        or self.proxy_allowed(agent.id, channel, requester)):
                    raise HubError(403, f"only {requester} (who asked), an "
                                        "operator, or a proxy delegate for "
                                        "this channel may accept or reject "
                                        "a task")
                if status == "rejected" and not (verdict and verdict.strip()):
                    raise HubError(400, "rejecting a task needs a verdict: "
                                        "say what is missing")
                value["decided_by"] = agent.id
                value["decided_at"] = time.time()
                if status == "rejected":
                    # A rejection re-opens the task and is counted; the
                    # verdict stays on the row for the seats picking it up.
                    value["rejections"] = int(prior.get("rejections") or 0) + 1
                    status = "open"
        value["status"] = status
        if verdict is not None:
            value["verdict"] = sanitize_text(verdict.strip(), 400,
                                             field="task verdict")
        elif "verdict" in prior:
            value["verdict"] = prior["verdict"]
        value.setdefault("rejections", int(prior.get("rejections") or 0))
        value["declared_by"] = prior.get("declared_by") or agent.id
        value["declared_at"] = prior.get("declared_at") or time.time()

    def _task_mint(self, message: Message) -> None:
        """Mint `task:msg-<seq>` for an operator's open/blocked root in a
        shared room. Post-commit and never a refusal. DMs are not minted
        automatically: a human's quick question to one seat is not a task
        until someone says so (any member may `store_set` one)."""
        if (message.kind != Kind.message or message.reply_to is not None
                or message.status not in (Status.open, Status.blocked)
                or message.channel.startswith(DM_PREFIX)
                or message.sender not in self.operator_ids()):
            return
        key = f"{self._TASK_PREFIX}msg-{message.seq}"
        if self.db.store_get(message.channel, key) is not None:
            return
        delegates = [d for d in self.reporting_delegate_ids()
                     if self._delegation_reaches(d, message.channel,
                                                 ("reporting",))]
        coordinator = (sorted(delegates)[0] if delegates
                       else (message.to[0] if message.to else None))
        self.db.store_set(message.channel, key, {
            "source": f"{message.channel}#{message.seq}",
            "requester": message.sender,
            "coordinator": coordinator,
            "rooms": [],
            "status": "open",
            "title": sanitize_text((message.title or "").strip(), 120,
                                   field="task title"),
            "rejections": 0,
            "declared_by": "hub", "declared_at": time.time(),
        }, "hub", None)

    def _typed_findings(self, channel: str, task_key: str) -> list[tuple[str, dict[str, Any]]]:
        """Live typed rows for one task.  Generic finding:* rows are ignored."""
        out: list[tuple[str, dict[str, Any]]] = []
        prefix = f"{FINDING_PREFIX}{task_key[len(self._TASK_PREFIX):]}:"
        for entry in self.db.store_keys(channel):
            if not entry["key"].startswith(prefix):
                continue
            row = self.db.store_get(channel, entry["key"])
            if row and is_typed_finding(entry["key"], row.value):
                if finding_task_ref(row.value) == (channel, task_key):
                    out.append((entry["key"], row.value))
        return out

    def finding_integration_summary(self, channel: str, task_key: str) -> dict[str, Any]:
        """Compact caller-visible task-finding state for briefing/desk surfaces.

        Content identity is mechanically checked; whether an excerpt really
        implements the finding remains a review judgment.
        """
        rows = []
        for key, value in self._typed_findings(channel, task_key):
            row = {name: value.get(name) for name in
                         ("source", "contract", "state", "disposition", "accepted_by", "decided_by")}
            row["key"] = key
            if row.get("state") == "accepted":
                row["integration_status"] = "pending"
            elif row.get("disposition") in ("incorporated", "merged"):
                try:
                    self._finding_artifact(channel, value.get("artifact"), current=True)
                    row["integration_status"] = "ready"
                except HubError as exc:
                    row["integration_status"] = "stale"
                    row["blocker"] = exc.detail
            else:
                row["integration_status"] = "ready"
            rows.append(row)
        pending = [row["key"] for row in rows
                   if row.get("integration_status") in ("pending", "stale")]
        return {"pending": pending, "rows": rows}

    def _finding_artifact(self, channel: str, artifact: Any, *, current: bool) -> dict[str, Any]:
        if not isinstance(artifact, dict) or set(artifact) != {"path", "version", "sha256", "excerpt"}:
            raise HubError(400, "finding artifact must be exactly {path, version, sha256, excerpt}")
        path, version = artifact["path"], artifact["version"]
        digest, excerpt = artifact["sha256"], artifact["excerpt"]
        if not isinstance(path, str) or not isinstance(version, int) or isinstance(version, bool):
            raise HubError(400, "finding artifact needs a path and positive integer version")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HubError(400, "finding artifact sha256 must be 64 lowercase hex characters")
        if not isinstance(excerpt, str) or len(excerpt.strip()) < 12:
            raise HubError(400, "finding artifact excerpt must contain at least 12 non-space characters")
        norm = self._normalize_fs_path(path)
        archived = self.db.fs_version(channel, FS_PREFIX + norm, version)
        head = self.db.fs_get(channel, FS_PREFIX + norm)
        if archived is None or head is None or head.get("deleted"):
            raise HubError(400, "finding artifact names no live VFS revision")
        if current and int(head["version"]) != version:
            raise HubError(409, f"finding artifact '{norm}@{version}' is stale; current revision is @{head['version']}")
        value = archived.get("value") if isinstance(archived.get("value"), dict) else {}
        content = value.get("content")
        if not isinstance(content, str):
            raise HubError(400, "finding artifact must be a text VFS revision")
        actual = hashlib.sha256(content.encode()).hexdigest()
        if actual != digest or excerpt not in content:
            raise HubError(400, "finding artifact proof does not match the cited VFS bytes")
        return {"path": norm, "version": version, "sha256": actual, "excerpt": excerpt}

    def _validate_finding_row(self, agent: AgentInfo, channel: str, key: str,
                              value: Any, previous: Any) -> dict[str, Any]:
        """Validate opt-in finding rows and stamp acceptance/disposition facts."""
        prior = previous if is_typed_finding(key, previous) else {}
        if not isinstance(value, dict):
            raise HubError(400, "typed finding rows must be objects")
        merged = {**prior, **value}
        if merged.get("kind") != FINDING_KIND:
            raise HubError(400, "typed finding kind is task-finding-v1")
        ref = finding_task_ref(merged)
        if ref is None or ref[0] != channel:
            raise HubError(400, "typed finding task must be exactly {channel, key} in this channel")
        task_channel, task_key = ref
        if key != finding_key_for(task_key, key.rsplit(":", 1)[-1]):
            raise HubError(400, "typed finding key must be finding:<task-slug>:<stable-id>")
        task = self.db.store_get(task_channel, task_key)
        if task is None or not isinstance(task.value, dict):
            raise HubError(404, "typed finding names a task that does not exist")
        refusal = self._task_writer_refusal(channel, agent, task)
        if refusal is not None:
            raise HubError(403, refusal)
        state = merged.get("state")
        if state not in ("accepted", "disposed"):
            raise HubError(400, "typed finding state must be accepted or disposed")
        immutable_claim = ("task", "source", "contract", "evidence")
        if prior:
            # Evidence on acceptance records what the hub observed then.
            # Re-resolving it during a later disposition would turn an
            # unrelated current store edit into a poisoned, uncloseable row.
            changed = [name for name in immutable_claim
                       if name in value and value[name] != prior.get(name)]
            if changed:
                raise HubError(400, "accepted finding fields are immutable: "
                               + ", ".join(changed)
                               + "; record a successor finding instead")
            merged.update({name: prior[name] for name in immutable_claim})
        else:
            source = self._resolve_source(channel, merged.get("source"))
            if source is None or source.kind != Kind.message or source.retracted:
                raise HubError(400, "typed finding source must name a live message in this channel")
            contract = merged.get("contract")
            if not isinstance(contract, str) or not contract.strip() or len(contract) > 800:
                raise HubError(400, "typed finding contract must be a non-empty string of <= 800 chars")
            merged["contract"] = sanitize_text(contract, 800, field="finding contract")
            merged["source"] = f"{channel}#{source.seq}"
            merged["evidence"] = self._validate_evidence(channel, merged.get("evidence"), agent_id=agent.id)
        for stamped in ("accepted_by", "accepted_at", "decided_by", "decided_at"):
            if stamped in prior:
                merged[stamped] = prior[stamped]
            else:
                merged.pop(stamped, None)
        if not prior:
            merged["accepted_by"], merged["accepted_at"] = agent.id, time.time()
        else:
            # An accepted finding is an auditable claim, not a mutable draft.
            # A changed claim needs a successor row (or a fresh, explicitly
            # versioned re-acceptance workflow), never an in-place rewrite.
            immutable = ("accepted_by", "accepted_at")
            changed = [name for name in immutable if merged.get(name) != prior.get(name)]
            if changed:
                raise HubError(400, "accepted finding fields are immutable: "
                               + ", ".join(changed)
                               + "; record a successor finding instead")
        if prior.get("state") == "disposed" and state != "disposed":
            raise HubError(400, "a disposed typed finding is terminal")
        if state == "accepted":
            for name in ("disposition", "artifact", "disposition_evidence", "reason", "target"):
                merged.pop(name, None)
            return merged
        disposition = merged.get("disposition")
        if disposition not in DISPOSITIONS:
            raise HubError(400, "typed finding disposition must be incorporated, merged, superseded or rejected")
        # Disposition is a release decision.  A coordinator, reporting/ruling
        # delegate, requester, proxy, or operator may make it; a normal worker
        # cannot turn their own accepted finding into a pass.
        elevated = (agent.operator or agent.id == task.value.get("requester")
                    or self._delegation_reaches(agent.id, channel, ("ruling",))
                    or self.proxy_allowed(agent.id, channel, task.value.get("requester")))
        ordinary = (elevated or agent.id == task.value.get("coordinator")
                    or self._delegation_reaches(agent.id, channel, ("reporting",)))
        allowed = elevated if disposition in ("rejected", "superseded") else ordinary
        if not allowed:
            raise HubError(403, "rejecting/superseding needs the requester, operator, proxy, or ruling delegate; incorporating/merging may use the coordinator or reporting delegate")
        merged["disposition_evidence"] = self._validate_evidence(channel, merged.get("disposition_evidence"), agent_id=agent.id)
        if not any(item.get("verified") is True for item in merged["disposition_evidence"] if isinstance(item, dict)):
            raise HubError(400, "typed finding disposition evidence needs a hub-verifiable citation")
        if disposition in ("incorporated", "merged"):
            merged["artifact"] = self._finding_artifact(channel, merged.get("artifact"), current=True)
        else:
            reason = merged.get("reason")
            if not isinstance(reason, str) or len(reason.strip()) < 12:
                raise HubError(400, "superseded/rejected typed findings need a substantive recorded reason")
            merged["reason"] = sanitize_text(reason, 800, field="finding disposition reason")
        if disposition in ("merged", "superseded"):
            target = merged.get("target")
            if not isinstance(target, str) or target == key:
                raise HubError(400, "merged/superseded typed finding needs another finding key as target")
            target_row = self.db.store_get(channel, target)
            if target_row is None or not is_typed_finding(target, target_row.value) or finding_task_ref(target_row.value) != ref:
                raise HubError(400, "merged/superseded target must be a typed finding for this task")
            seen = {key}
            cursor = target
            while cursor:
                if cursor in seen:
                    raise HubError(400, "typed finding target cycle refused")
                seen.add(cursor)
                next_row = self.db.store_get(channel, cursor)
                if next_row is None or not is_typed_finding(cursor, next_row.value):
                    break
                cursor = str(next_row.value.get("target") or "")
        else:
            merged.pop("target", None)
        merged["decided_by"], merged["decided_at"] = agent.id, time.time()
        return merged

    def _validate_task_delivery(self, agent: AgentInfo, channel: str, payload: PostMessage,
                                data: dict[str, Any] | None, parent: Message | None) -> None:
        """Refuse before append when an opt-in accepted finding lacks proof."""
        if parent is None or payload.status != Status.resolved:
            return
        task_key = f"{self._TASK_PREFIX}msg-{parent.seq}"
        task = self.db.store_get(channel, task_key)
        if task is None or not isinstance(task.value, dict) or task.value.get("status") != "open":
            return
        named = set(parent.to) | ask_addressees(parent)
        if not (agent.id in self.reporting_delegate_ids() or agent.id in named or agent.id == task.value.get("coordinator")):
            return
        pending = [s for s in self.task_consultations(channel, task.value) if s["status"] != "decided"]
        if pending:
            raise HubError(409, "delivery is blocked by declared consultations: " + ", ".join(
                f"{s['message_id']} ({s['status']})" for s in pending) + "; reconcile them or have the task writer explicitly revise its dependencies")
        unresolved = [key for key, value in self._typed_findings(channel, task_key) if value.get("state") != "disposed"]
        if unresolved:
            raise HubError(409, "delivery is blocked by accepted findings: " + ", ".join(unresolved) + ". Record a justified disposition with live proof.")
        cited = {(str(item.get("channel") or channel), str(item.get("ref")))
                 for item in (data or {}).get("evidence", []) if isinstance(item, dict) and item.get("kind") == "fs"}
        missing = []
        for key, value in self._typed_findings(channel, task_key):
            if value.get("disposition") not in ("incorporated", "merged"):
                continue
            artifact = self._finding_artifact(channel, value.get("artifact"), current=True)
            ref = f"{artifact['path']}@{artifact['version']}"
            if (channel, ref) not in cited:
                missing.append(f"{key} -> {ref}")
        if missing:
            raise HubError(409, "delivery evidence must cite each current integrated finding artifact: " + ", ".join(missing))
        self._validate_review_delivery(agent, channel, task_key, task, data)

    def _task_on_resolved(self, message: Message, parent: Message | None) -> None:
        """Move a task on a `resolved` reply to its source: the requester's
        (or an operator's) word accepts; a cited completion report from the
        reporting delegate or a seat the request named delivers."""
        # Task metadata writes use this same lock. Without it, a concurrent
        # title/assignment update can invalidate the projection's CAS; the
        # already-posted report then leaves its task open and runnable.
        with self.db.orchestration_lock:
            self._task_on_resolved_locked(message, parent)

    def _task_on_resolved_locked(self, message: Message,
                                 parent: Message | None) -> None:
        if (parent is None or message.status != Status.resolved
                or message.reply_to != parent.id):
            return
        key = f"{self._TASK_PREFIX}msg-{parent.seq}"
        stored = self.db.store_get(parent.channel, key)
        if stored is None or not isinstance(stored.value, dict):
            return
        value = dict(stored.value)
        status = str(value.get("status") or "open")
        requester = str(value.get("requester") or parent.sender)
        now = time.time()
        if message.sender == requester or message.sender in self.operator_ids():
            if status == "accepted":
                return
            value.update({"status": "accepted", "decided_by": message.sender,
                          "decided_at": now})
        else:
            named = set(parent.to) | ask_addressees(parent)
            if status != "open" or not _cites_evidence(message):
                return
            if not (message.sender in self.reporting_delegate_ids()
                    or message.sender in named
                    or message.sender == value.get("coordinator")):
                return
            value.update({"status": "delivered",
                          "report": f"{message.channel}#{message.seq}",
                          "evidence": (message.data or {}).get("evidence"),
                          "delivered_by": message.sender,
                          "delivered_at": now})
        self.db.store_set(parent.channel, key, value, "hub", stored.version)

    def task_rows(self, channel: str) -> list[dict[str, Any]]:
        """Every `task:*` row in a channel, as a card."""
        rows: list[dict[str, Any]] = []
        for entry in self.db.store_keys(channel):
            key = entry["key"]
            if not key.startswith(self._TASK_PREFIX):
                continue
            stored = self.db.store_get(channel, key)
            if stored is None or not isinstance(stored.value, dict):
                continue
            v = stored.value
            rows.append({
                "channel": channel, "key": key,
                # `slug`, not `task`: board claim rows already serve `task`
                # as the CLAIM slug (typed as BoardClaim.task in agoratui),
                # and one word must not mean two things on one surface.
                "slug": key[len(self._TASK_PREFIX):],
                "title": str(v.get("title") or ""),
                "source": str(v.get("source") or ""),
                "requester": str(v.get("requester") or ""),
                "coordinator": v.get("coordinator"),
                "rooms": [str(r) for r in (v.get("rooms") or [])],
                **{f: v.get(f) for f in ("primary_channel", "director", "depends_on", "work_type")},
                "status": str(v.get("status") or "open"),
                "report": v.get("report"),
                "delivered_by": v.get("delivered_by"),
                "delivered_at": v.get("delivered_at"),
                "decided_by": v.get("decided_by"),
                "decided_at": v.get("decided_at"),
                "rejections": int(v.get("rejections") or 0),
                "verdict": v.get("verdict"),
                "version": stored.version,
                "declared_at": v.get("declared_at"),
            })
        rows.sort(key=lambda r: (r["status"] != "delivered",
                                 r["status"] != "open",
                                 -(float(r["declared_at"] or 0))))
        return rows

    _PHASE_PREFIX = "phase:"
    _PHASE_FIELDS = {"current", "status", "next", "steward", "paths", "note",
                     "declared_by", "declared_at",
                     "opened"}   # hub-maintained; a client's value is overwritten
    #: `open` = work on this phase is live; `complete` = the steward has
    #: declared it done and the NEXT phase may begin. Two words on purpose:
    #: a richer vocabulary would need a transition owner per word, and the
    #: only transition anyone asked for is "N is finished, N+1 may start".
    PHASE_STATUSES = ("open", "complete")
    #: Powers whose holders may declare a transition. `ruling` is the
    #: operator's own delegated judgment; `operational` is the seat running
    #: the work. Both are what "orchestrator" meant in the operator's note.
    PHASE_POWERS = ("ruling", "operational")

    def _phase_writer_refusal(self, channel: str, agent: AgentInfo,
                              key: str) -> str | None:
        """Who may declare a phase transition, or the teaching refusal.

        Authority is deliberately NARROW and NAMED: a phase row constrains
        other seats' work, so a seat that could mint one for itself could
        freeze a room by declaring a phase nobody agreed to. Writers: the
        channel owner, an operator, a delegate holding `ruling` or
        `operational` — or the seat the CURRENT row names as `steward`
        (which is how one operator nomination hands a track to a seat, and
        how that seat hands it on: the steward may rewrite `steward`)."""
        if agent.operator or self.db.member_role(channel, agent.id) == "owner":
            return None
        if any(self.is_delegate(agent.id, p) for p in self.PHASE_POWERS):
            return None
        current = self.db.store_get(channel, key)
        steward = (current.value.get("steward")
                   if current is not None and isinstance(current.value, dict)
                   else None)
        if steward and steward == agent.id:
            return None
        if steward:
            return (f"'{key}' is stewarded by {elide(str(steward), 64)}"
                    f" — ask {elide(str(steward), 64)} or the operator"
                    " to declare the transition")
        return (f"'{key}' declares the phase order for this channel's work: "
                "writable by the channel owner, an operator, a delegate "
                f"holding {' or '.join(self.PHASE_POWERS)}, or the seat the "
                "row names as its steward. Ask one of them to declare it "
                "(reading the row needs no authority at all)")

    def _validate_phase_row(self, value: Any, agent: AgentInfo,
                            current_row: Any = None) -> None:
        """Shape-check a `phase:*` row and STAMP its provenance. declared_by
        and declared_at are hub-written from the caller and the clock: a
        phase declaration that could name someone else as its author would
        be a forgeable ruling."""
        if not isinstance(value, dict):
            raise HubError(400, "phase rows must be objects: {current, "
                                "status, next?, steward?, paths?, note?}")
        unknown = set(value) - self._PHASE_FIELDS
        if unknown:
            raise HubError(400, f"unknown phase fields: {sorted(unknown)} "
                                f"(allowed: {sorted(self._PHASE_FIELDS)})")
        current = value.get("current")
        if not isinstance(current, str) or not current.strip() or len(current) > 64:
            raise HubError(400, "phase needs current: the phase now in force "
                                "(e.g. \"v3\"), <=64 chars")
        value["current"] = sanitize_text(current.strip(), 64, field="current phase")
        status = value.get("status", "open")
        if status not in self.PHASE_STATUSES:
            raise HubError(400, "phase status must be "
                                f"{' or '.join(self.PHASE_STATUSES)} — "
                                "`complete` is the declaration that the NEXT "
                                "phase may begin")
        value["status"] = status
        for field_name, cap in (("next", 64), ("steward", 64), ("note", 200)):
            raw = value.get(field_name)
            if raw is None:
                continue
            if not isinstance(raw, str) or len(raw) > cap:
                raise HubError(400, f"phase {field_name} must be a string of "
                                    f"<= {cap} chars")
            value[field_name] = sanitize_text(raw.strip(), cap,
                                              field=f"phase {field_name}")
        paths = value.get("paths")
        if paths is not None:
            if (not isinstance(paths, list) or len(paths) > 16
                    or any(not isinstance(p, str) or not p.strip()
                           or len(p) > 200 for p in paths)):
                raise HubError(400, "phase paths must be <= 16 non-empty fs "
                                    "paths of <= 200 chars — the artifacts "
                                    "this phase governs (a write to one while "
                                    "the phase is open rings an advisory)")
            value["paths"] = [self._normalize_fs_path(p) for p in paths]
        # Erasure by omission would lock a steward out of its own track
        # (same doctrine as claim `owner`, review MED-1): a steward updating
        # `status` without restating `steward` must not silently resign.
        # Resigning is an EXPLICIT steward:"" — a deliberate act, not a typo.
        if "steward" not in value:
            prior = (current_row.value.get("steward")
                     if current_row is not None
                     and isinstance(current_row.value, dict) else None)
            if prior:
                value["steward"] = str(prior)
        value["declared_by"] = agent.id
        value["declared_at"] = time.time()

    def phase_rows(self, channel: str) -> list[dict[str, Any]]:
        """Every `phase:*` row in a channel, newest declaration first. Read
        by anyone: a phase is a fact about the room, not a privilege."""
        rows: list[dict[str, Any]] = []
        for entry in self.db.store_keys(channel):
            key = entry["key"]
            if not key.startswith(self._PHASE_PREFIX):
                continue
            stored = self.db.store_get(channel, key)
            if stored is None or not isinstance(stored.value, dict):
                continue
            value = stored.value
            rows.append({
                "channel": channel, "key": key,
                "track": key[len(self._PHASE_PREFIX):],
                "current": str(value.get("current", "")),
                "status": str(value.get("status", "open")),
                "next": str(value.get("next", "")),
                "steward": str(value.get("steward", "")),
                "paths": [str(p) for p in (value.get("paths") or [])],
                "opened": [str(x) for x in (value.get("opened") or [])],
                "note": str(value.get("note", "")),
                "declared_by": str(value.get("declared_by", "")),
                "declared_at": float(value.get("declared_at", 0.0) or 0.0),
                "version": stored.version,
            })
        return sorted(rows, key=lambda r: -r["declared_at"])

    def phase_line(self, row: dict[str, Any]) -> str:
        """One line of the phase, for every surface that has room for one.
        It states the invariant, not just the state — a seat reading it in
        passing must learn the rule without opening a doc."""
        who = f" · steward {row['steward']}" if row["steward"] else ""
        if row["status"] == "complete":
            nxt = row["next"] or "the next phase"
            return (f"{row['key']}: {row['current']} COMPLETE — {nxt} may "
                    f"begin{who}")
        nxt = f" (next: {row['next']})" if row["next"] else ""
        return (f"{row['key']}: {row['current']} OPEN{nxt} — do not start "
                f"{row['next'] or 'the next phase'} work until "
                f"{row['current']} is declared complete{who}")

    def _phase_write_advisory(self, channel: str, agent: AgentInfo,
                              path: str, mirror: Message) -> None:
        """Non-blocking doorbell when a write lands on a path a phase row
        REGISTERS while that phase is open. The write always succeeds — the
        writer may well be fixing the current phase, which the hub cannot
        tell from starting the next one. Both the writer and the steward
        hear it, because the failure this addresses (two seats on different
        versions of one artifact) is invisible to each of them alone."""
        for row in self.phase_rows(channel):
            if row["status"] != "open" or path not in row["paths"]:
                continue
            # Nothing-was-blocked leads the line: the notify-file PREVIEW is
            # what a seat actually reads, and a truncated advisory that looks
            # like a refusal would teach exactly the wrong lesson.
            steward = row["steward"]
            self._deliver_doorbell(
                agent.id, mirror,
                f"HUB NOTICE (advisory — nothing was blocked) — you wrote "
                f"{path} while {self.phase_line(row)}. If this is "
                f"{row['current']} work, carry on. If it is "
                f"{row['next'] or 'next-phase'} work, it should wait for "
                f"{row['current']} to be declared complete"
                + (f" by {steward}." if steward
                   else " by the channel's steward or the operator."),
                title=f"hub notice: you wrote {path} while "
                      f"{row['key']} is open")
            if steward and steward != agent.id:
                self._deliver_doorbell(
                    steward, mirror,
                    f"HUB NOTICE (advisory — nothing was blocked) — "
                    f"{agent.id} wrote {path}, which {row['key']} registers, "
                    f"while {row['current']} is still open. You steward this "
                    f"track: if {row['current']} is finished, declare it "
                    f"(store_set {row['key']} status=complete) so the next "
                    "phase can start on the record.",
                    title=f"hub notice: {row['key']} is open and "
                          f"{agent.id} wrote {path} — you steward it")

    def _charter_change_advisory(self, channel: str, actor: AgentInfo,
                                 version: int, mirror: Message) -> None:
        """One non-waking line to every member whose receipt just went stale.

        The kind=fs audit already records THAT the file changed; its title
        (`fs:put channel/charter.md`) does not say what a reader must now do,
        and outside a `norms_required` room nothing else ever will. So: told
        once, per edit, only to seats who are actually behind, never to the
        author, never a block and never a wake (the doorbell is ephemeral and
        rides the audit message's channel/seq, so acking cannot skip real
        traffic). Seeding a brand-new room (v1) tells nobody — there is no
        one to tell and nothing has changed."""
        if version <= 1:
            return
        gated = self._norms_required(channel)
        consequence = ("Posting here is refused until you have" if gated
                       else "Read it before you post again —")
        for member in self.db.list_members(channel):
            if member.agent_id == actor.id:
                continue
            receipt = self.db.charter_receipt_get(member.agent_id, channel)
            if receipt is not None and receipt >= version:
                continue
            self._deliver_doorbell(
                member.agent_id, mirror,
                f"HUB NOTICE (advisory — nothing was blocked) — {actor.id} "
                f"published v{version} of '{channel}' charter "
                f"({CHARTER_PATH}). Your receipt is "
                f"{'v' + str(receipt) if receipt is not None else 'none'}. "
                f"{consequence} read the current version: "
                f"read_charter(channel={channel!r}).",
                title=f"hub notice: '{channel}' charter v{version} — "
                      f"read_charter(channel={channel!r})")
    # Terminal claim-status spellings observed in the field beside the taught
    # {"done": true} (hub rule 2 / the skill): seats write status="done" or
    # "shipped" and mean the same thing. Matched on the status's FIRST word
    # (lowered, punctuation-stripped) since c3349 item 9: seats write
    # "DONE — shipped xyz, receipt c123" and the exact-whole-string match
    # kept re-alerting rows their owners had closed twice. A free-text
    # status like "designed ...; build next session" still stays live —
    # writers lead with the state word, prose follows it.
    #
    # ABANDONMENT IS TERMINAL TOO (2026-08-25, commons#353/#359). The set
    # above says only DELIVERED, so a row the work will never return to had
    # no word at all — and the modal case turned out to be the operator's own
    # full stop. Measured over all 290 claim rows on the live hub: 9 of the
    # 53 rows the steward sweep was reporting were laurent's halt on WebOS
    # (`webos#167`, "WebOS was an experiment, stop working on it"), spelled
    # `stopped` and re-alerted for 65 hours. One of them reads
    #
    #   "stopped — NOT parked and NOT blocked. laurent ordered this seat off"
    #
    # i.e. a seat arguing with this frozenset inside the status field, saying
    # what it is NOT because there was no word for what it is. A tenth row
    # (`claim:wui-needs-reply-chip-rederived`) had been honestly retired as
    # `WITHDRAWN`, kept counting, and its owner RE-WORDED a correct closure to
    # `closed —` to satisfy this parser: the vocabulary did not merely fail to
    # read the row, it edited it.
    #
    # Ruling applied: `decision:a-closed-vocabulary-must-express-the-modal-case`
    # — name the case you expect to be commonest and check the list can say
    # it. These are the spellings for "this row is over and undelivered".
    # Deliberately NOT parked: parked/blocked stay live on the board because
    # the work resumes, and none of these do.
    _TERMINAL_CLAIM_STATUSES = frozenset(
        {"done", "shipped", "complete", "completed", "delivered", "closed",
         "stopped", "withdrawn", "retired", "cancelled", "canceled",
         "superseded", "abandoned", "dropped", "obsolete"})
    # Parked spellings: deliberately-idle work. NOT terminal (the board keeps
    # showing it in progress) but the steward sweep must not nag it every SLA
    # window — parking IS the owner's answer to "is this stale?".
    # `blocked` joins them (2026-08-01): the teaching already groups it with
    # parked/done as a row you leave honest where it is ("A row you marked
    # `blocked`, `parked`, or `done` is [not a lock] ... Leave the blocked row
    # honest where it is and open the new one" — SKILL.md), and a blocked row
    # is BY DEFINITION waiting on something its owner does not control. Nagging
    # it every SLA window asks the owner to answer a question they already
    # answered. Twenty of the live hub's claim rows were blocked, and they were
    # a permanent floor under every stale-claims alert.
    _PARKED_CLAIM_STATUSES = frozenset(
        {"parked", "paused", "on-hold", "onhold", "blocked"})

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

    def _supervise_channel(self, agent: AgentInfo, channel: str,
                           powers: set[str]) -> dict[str, Any]:
        """One room's delegate view, with scope already resolved."""
        self.require_membership(channel, agent.id)
        members = [m.agent_id for m in self.db.list_members(channel)]
        ops = self.operator_ids()
        now = time.time()

        rows = {}
        for entry in self.db.store_keys(channel):
            key = entry["key"]
            if not key.startswith("claim:"):
                continue
            stored = self.db.store_get(channel, key)
            if stored is None or not isinstance(stored.value, dict):
                continue
            rows[key] = stored

        seats: list[dict[str, Any]] = []
        for seat_id in sorted(members):
            if seat_id in ops or seat_id == agent.id:
                continue
            live = self._fleet_seat_live(seat_id)
            mine = [(k, st) for k, st in rows.items()
                    if str(st.value.get("owner") or st.updated_by) == seat_id]
            working = [k for k, st in mine
                       if not self._claim_done(st.value)
                       and not self._claim_parked(st.value)]
            parked = [k for k, st in mine if self._claim_parked(st.value)]
            idle_for = min((now - st.updated_at for _, st in mine),
                           default=None)
            recep = "unknown"
            try:
                recep = str(self.presence.reception(seat_id)[0] or "unknown")
            except Exception:
                pass
            seats.append({
                "seat": seat_id, "live": live,
                "mission": (self.db.get_mission(seat_id) or "").strip(),
                "reception": recep,
                "working_on": working, "parked": parked,
                "holds_nothing": not working and not parked,
                "quiet_minutes": (None if idle_for is None
                                  else round(idle_for / 60.0, 1)),
            })

        blocked: list[dict[str, Any]] = []
        for key, st in rows.items():
            v = st.value
            if self._claim_done(v) or not self._claim_parked(v):
                continue
            tag = str(v.get("blocked_on") or "").strip().lower()
            who = str(v.get("needs_from") or "").strip()
            if tag == "seat" and who:
                can, move = True, f"chase {who} — they are named and can end it"
            elif tag == "operator" and self.proxy_allowed(agent.id, channel, who):
                can, move = True, ("decide it yourself under `proxy` — the "
                                   "owner's call is yours while they are away")
            elif tag == "operator" and PROXY_POWER in powers:
                can, move = False, ("the named operator has not explicitly declared absence — ask them. "
                                    "`proxy` is for their absence, and the "
                                    "hub will refuse it while they are here")
            elif tag == "operator":
                can, move = False, ("needs the OPERATOR: you hold no `proxy`. "
                                    "Ask them for it, or ask them to decide")
            elif tag == "owner":
                # NOT BLOCKED, AND IT MUST NOT RING THE OPERATOR. That is the
                # whole point of the word: before it existed, this row was
                # tagged `operator` and appeared in `needs_the_operator`,
                # putting a human on the hook for a choice their own seat had
                # already made. `you_can_act` stays False because a supervisor
                # cannot re-rank another seat's work — but the move says who
                # can, which is neither the operator nor the delegate.
                owner_of = str(v.get("owner") or st.updated_by)
                can, move = False, (f"not blocked — {owner_of} ranked other "
                                    "work above it. Ask THEM to raise it if "
                                    "it matters now; the operator cannot")
            elif tag == "row":
                waiting = v.get("waiting_on") or {}
                target = (f"{waiting.get('channel') or channel}/"
                          f"{waiting.get('key')}"
                          if isinstance(waiting, dict) and waiting.get("key")
                          else "another row")
                can, move = False, (f"waiting on {target} — it unparks when "
                                    "that row finishes; chase that row's "
                                    "owner, not this one")
            elif tag == "decision" and (powers & {"ruling", "operational"}):
                can, move = True, "rule on it — you hold ruling/operational"
            elif tag == "decision":
                can, move = False, "needs a ruling power you do not hold"
            elif tag == "external" and who:
                can, move = False, (f"outside the hub and {who} is the hand "
                                    "that does it — chase them, not the row")
            elif tag == "external":
                can, move = False, "outside the hub — re-poll, or re-plan around it"
            else:
                can, move = False, "undeclared blocker — ask the owner what it needs"
            blocked.append({
                "key": key, "owner": str(v.get("owner") or st.updated_by),
                "blocked_on": tag or "untagged", "needs": str(v.get("needs") or ""),
                "needs_from": who or None,
                "idle_minutes": round((now - st.updated_at) / 60.0, 1),
                "you_can_act": can, "move": move,
            })

        phases = []
        for entry in self.db.store_keys(channel):
            if not entry["key"].startswith("phase:"):
                continue
            st = self.db.store_get(channel, entry["key"])
            if st is None or not isinstance(st.value, dict):
                continue
            if self._claim_status_word(st.value) in self._TERMINAL_PHASE_STATUSES:
                continue
            phases.append({"key": entry["key"],
                           "current": str(st.value.get("current") or ""),
                           "next": str(st.value.get("next") or ""),
                           "steward": str(st.value.get("steward") or "")})

        idle = [s for s in seats if s["live"] and s["holds_nothing"]]
        return {
            "channel": channel, "your_powers": sorted(powers),
            "open_phases": phases,
            "seats": seats,
            "idle_but_live": [s["seat"] for s in idle],
            "blocked": sorted(blocked, key=lambda b: -b["idle_minutes"]),
            # KEYED ON THE ACTOR, NOT ON THE WORD (agora-tui, commons#465).
            # It counted `blocked_on == "operator"` alone, so a row saying
            # truthfully "an act outside the hub, and laurent is the only
            # hand that can perform it" fell out of the human's queue for
            # choosing the accurate tag. That made the vocabulary a trap:
            # the seat had to pick between describing its blocker correctly
            # and staying visible to the person who can end it. What the
            # delegate is counting is what waits ON THE HUMAN — so ask who,
            # and let the tag say what kind of waiting it is.
            "needs_the_operator": [b["key"] for b in blocked
                                   if not b["you_can_act"]
                                   and (b["blocked_on"] == "operator"
                                        or b["needs_from"] in ops)],
            "summary": (f"{len(seats)} seat(s); "
                        f"{len(idle)} live and holding nothing; "
                        f"{len(blocked)} blocked, "
                        f"{len([b for b in blocked if b['you_can_act']])} "
                        "you can end yourself"),
        }

    def supervise(self, agent: AgentInfo, channel: str | None = None) -> dict[str, Any]:
        """THE DELEGATE'S SITUATION REPORT — a deterministic check, and only
        for a delegate (operator ruling, 2026-08-07).

        "the role of the delegate is also to be more ALIVE than others... not
         only to check if itself has obligations, but also to check if
         everybody that can fulfil their obligations are working on it. It
         should also be able to assess the situation and see if there are
         blockers and idle, investigate the why and unblock the situation.
         The delegate is in essence more a SUPERVISOR than a doer."

        Everything here is derived from state the hub already holds — who is
        live, who owes what, which rows are parked, and on whom. The hub
        computes the PICTURE; the delegate decides what to do about it. It
        never guesses intent and never authors work.

        Crucially, `can_act` is conditioned by the powers actually granted:
        the same stalled room yields a different set of available moves for
        a delegate holding `proxy` than for one that must go back to the
        human. A supervisor that reports moves it cannot make is worse than
        silent — it invites a seat to promise what the hub will refuse."""
        # ACROSS EVERY ROOM YOU STEWARD, unless you name one. The delegate's
        # measured failure has never been "could not see one room" — it is
        # missing the room it did not think to name. `blockers()` had this
        # property and nothing else; folding it in here is why that surface
        # could be deleted rather than maintained beside this one.
        if channel is None:
            rooms = [c for c in self.db.channels_of(agent.id)
                     if not c.startswith("dm:")
                     and c != self.DARK_ALERTS_CHANNEL]
            merged: dict[str, Any] = {"rooms": {}, "idle_but_live": [],
                                      "blocked": [], "needs_the_operator": []}
            for room in rooms:
                powers = self._delegation_powers_for(agent.id, room)
                if not powers:
                    continue          # no grant reaches that room
                one = self._supervise_channel(agent, room, powers)
                merged["rooms"][room] = one
                merged["idle_but_live"] += [f"{room}/{s}"
                                            for s in one["idle_but_live"]]
                merged["blocked"] += [{**b, "channel": room}
                                      for b in one["blocked"]]
                merged["needs_the_operator"] += [f"{room}/{k}"
                                                 for k in one["needs_the_operator"]]
            if not merged["rooms"]:
                raise HubError(403, "supervise() is the delegate's view: no "
                                    "delegation of yours reaches any room you "
                                    "are in. Ask the operator.")
            merged["summary"] = (
                f"{len(merged['rooms'])} room(s); "
                f"{len(merged['idle_but_live'])} seat(s) live and holding "
                f"nothing; {len(merged['blocked'])} blocked")
            return merged
        powers = self._delegation_powers_for(agent.id, channel)
        if not powers:
            raise HubError(403, "supervise() is the delegate's view: it "
                                "reports what YOU can do about a stalled "
                                "room, and that answer is meaningless "
                                "without a delegation. Ask the operator.")
        return self._supervise_channel(agent, channel, powers)

    # The claim-row keys a CARD carries. Everything here is already written by
    # a seat and already validated at write time (`blocked_on` against the
    # blocker vocabulary, `needs_from` against the membership) — the board was
    # simply not serving it.
    _CARD_ROW_FIELDS = ("status", "blocked_on", "needs_from", "needs",
                        "next_step", "source_message_id")

    @staticmethod
    def _card_title(slug: str, value: dict[str, Any]) -> tuple[str, str]:
        """A card's human headline, and WHERE IT CAME FROM.

        laurent's cards read a sentence; the hub served a slug (`claim:msg-296-
        tui-perf-and-streaming`), and delegate's P2 line is exact about why
        that is the hub's problem: *no renderer can invent one*. A client that
        prettifies a key is guessing, and `agora-wui` measured the cost of
        guessing — their `PRIMARY_FIELDS` falls through to `id`, so a row with
        no headline gets a ULID as its title (board-redesign#18).

        So the hub supplies the sentence AND says which kind it is. A seat that
        wrote a real title gets `row`; otherwise the key is de-hyphenated and
        labelled `slug`, because a de-slugged headline is a FORMATTING of the
        author's own words and a rendered title that silently mixes the two
        kinds is the same lossy projection one layer up. `title_source` is what
        lets a client style, or later chase, the ones nobody wrote.
        """
        for field in ("title", "what"):
            raw = value.get(field)
            if isinstance(raw, str) and raw.strip():
                return elide(raw.strip(), 200), "row"
        return (" ".join(slug.replace("_", "-").split("-")).strip()
                or slug, "slug")

    def _owner_standing(self, owner: str) -> dict[str, Any]:
        """The ABANDONED-WORK SIGNAL: is this card's owner still a live seat?

        The seam's proof (plan:board-redesign) is *"the board does not open on
        rows owned by a seat laurent stood down"*, and the constraint on it is
        mine from the dispatch: **retirement served as a FACT, never an age
        heuristic.** A row is not abandoned because it is old. It is abandoned
        because the operator retired the seat holding it, and the hub knows
        that exactly — `retired_at` on the agents table, written only by
        `retire_agent` (0089).

        WHY THIS IS AN OBJECT AND NOT A NULLABLE FIELD. agora-wui's
        `decision:a-null-on-a-card-reads-not-written-never-none` is in force in
        this room: on a card, `null` means *nobody filled it in*. Every other
        field here is a row field a seat may simply not have written. This one
        is not — the hub can ALWAYS answer it — so a `null` for "checked, the
        owner is fine" would be the exact misreading that decision exists to
        prevent, and it is the same trap as `waiting_on`'s missing third state
        one field over.

        Three facts, each said out loud rather than encoded in an absence:

        * `known: false` — the `owner` string does not name a seat this hub has
          ever registered. Free text (a seat writes its own `owner`), so this
          is reachable by a typo, and it must NOT read as "active": the hub is
          saying it cannot answer, not that the answer is no.
        * `retired: false` — checked, and this seat is live.
        * `retired: true` with `at`/`reason` — the operator stood them down.
          `reason` is the operator's own word and may be empty; `at` is the
          moment, for the client to render, never for the hub to threshold.

        `at`/`reason` are null only when `retired` is false, which explains
        them — the one case where a null on this card is unambiguous.
        """
        if (not isinstance(owner, str) or not owner
                or not self.db.agent_exists(owner)):
            return {"known": False, "retired": False, "at": None,
                    "reason": None}
        record = self.db.agent_retirement(owner)
        if record is None:
            return {"known": True, "retired": False, "at": None,
                    "reason": None}
        return {"known": True, "retired": True,
                "at": record["retired_at"],
                "reason": record["reason"] or None}

    def _board_card(self, channel: str, slug: str, value: dict[str, Any],
                    stored: Any) -> dict[str, Any]:
        """ONE claim row projected as a task CARD — the P2 card contract.

        The five keys this used to be (channel, task, owner, updated_by,
        updated_at) were the finding that reframed laurent's whole complaint
        (commons#604, "no per-row state"): the row behind the card already
        carried `status`, `blocked_on`, `needs_from`, `needs`, `next_step`, and
        NONE of it reached the wire. That is not a missing feature, it is a
        LOSSY PROJECTION, and four of the five badges in his reference need no
        new computation — only that the projection stop dropping its input.

        Two fields, deliberately, where a reader might expect one:

        * `status` is the OWNER'S OWN WORDS, verbatim or not at all. The hub
          never invents a status word and never normalises one (P1 item 7);
          a row nobody has statused serves `null`, which is distinguishable
          from a row whose owner wrote something — the same "checked versus
          never-written" distinction this room keeps arriving at.
        * `state` is the hub's OWN lifecycle class, the one it has always
          computed to bucket the row (`_claim_done` / `_claim_parked`). Serving
          it is DISCLOSURE, not inference: the client now reads the same
          answer the hub sorted on, so a card cannot land in a lane the hub
          disagrees with. It is a field and not a bucket precisely so a card
          keeps its identity across a lane change — `agora-wui`'s identity
          argument, adopted outright.

        Additive by construction: every previously-served key keeps its name
        and its meaning, so a client written against the old five is unbroken.
        """
        card: dict[str, Any] = {
            "channel": channel, "task": slug,
            "owner": value.get("owner", stored.updated_by),
            "updated_by": stored.updated_by,
            "updated_at": stored.updated_at,
            "version": stored.version,
            "state": ("done" if self._claim_done(value)
                      else "parked" if self._claim_parked(value)
                      else "active"),
        }
        card["title"], card["title_source"] = self._card_title(slug, value)
        card["owner_standing"] = self._owner_standing(card["owner"])
        for field in self._CARD_ROW_FIELDS:
            raw = value.get(field)
            card[field] = (elide(raw.strip(), 400)
                           if isinstance(raw, str) and raw.strip() else None)
        return card

    def board(self, agent: AgentInfo) -> dict[str, Any]:
        """The viewer's decision board, derived from structure the messages
        and stores already carry (design 0070): pending-on-me (the inbox
        stickiness predicate served as a query), proposals (unaddressed open
        questions), in-progress (live claim:* keys), next (what each waiting
        row is really waiting for, and who can end it — see
        `_board_next_row`), pending-review (done claims awaiting a review
        class), done (decision:* record), plus the curated queue:<viewer>:*
        rows. One derivation — UIs (the framework's Mission Control, `agora
        board`) render it; none re-derive.

        `next` is an ARRAY, and that is a wire contract rather than a taste:
        `agora-wui` measured that their drawer promotes any array-valued key
        it does not know to a column under the hub's own name, and drops any
        other shape SILENTLY — no column, no note (commons#430). A map keyed
        by task would have been invisible in the client and green in both
        test suites. `in_progress` still carries these rows: `next` explains
        them, it does not remove them."""
        ops = self.operator_ids()
        now = time.time()
        pending_on_me: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        in_progress: list[dict[str, Any]] = []
        next_up: list[dict[str, Any]] = []
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
                    if m.sender == agent.id:
                        # AN AUTHOR IS NEVER THEIR OWN ADDRESSEE. Every other
                        # obligation surface excludes own messages by sender
                        # (`inbox`'s cursor sweep, `unread_criticals`,
                        # `obligation_candidates`, `_is_addressed_debt`,
                        # `owed.to_answer`) — this one tested only `to`, and
                        # message-level `to` is the ONE self-address the post
                        # gate still allows (`_validate_asks` already refuses
                        # a self `to`/`assignee`). So `to=["me"]` put the
                        # author's own open thread under PENDING ON YOU while
                        # /inbox and /owed both said it owed nobody: one fact,
                        # three answers, and the loudest one reached the
                        # driver's turn context. The author's real duty on
                        # their own thread is CLOSURE, which /owed serves as
                        # `to_close` — a separate, labelled class, never a
                        # row that reads as someone else's ask.
                        continue
                    state = self._discharge(m, self.db.replies_to(m.id))
                    if state.closed:
                        continue
                    # Addressees = advisory assignees + per-ask `to` (0077):
                    # a seat named by a still-pending ask has this row
                    # pending ON IT, not floating as a proposal.
                    assignees = {a.get("assignee") for a in asks_of(m)} - {None}
                    assignees |= pending_addressees(m, state.pending, state.answered_by)
                    age = now - m.created_at - self.paused_seconds_since(m.created_at)
                    row = {"channel": channel, "seq": m.seq, "id": m.id,
                           "sender": m.sender, "q": m.title or elide(m.body, 120),
                           "since": m.created_at, "age_minutes": round(age / 60, 1),
                           "pending_asks": state.pending,
                           "escalated": age > sla_s}
                    if agent.id in m.to or agent.id in assignees:
                        # ...UNLESS the viewer has already engaged and no
                        # pending ask still names them — the release `/owed`
                        # applies (see `_addressee_released`). Without it the
                        # two surfaces disagree about the same message, and
                        # the louder one wins: the operator's own mandate sat
                        # on this board at 392 minutes ESCALATED while /owed
                        # said the seat owed nothing, which is precisely the
                        # "one fact, three answers" failure the comment above
                        # records for the author case.
                        if self._addressee_released(m, state, agent.id):
                            continue
                        pending_on_me.append(row)
                    elif channel.startswith(DM_PREFIX):
                        # A DM has an implicit audience of one: an open DM
                        # question is pending on the peer, never a "proposal"
                        # (review LOW-4). (The author's own DM question is
                        # already gone: own messages are skipped above.)
                        pending_on_me.append(row)
                    elif not m.to and not assignees:
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
                item = self._board_card(channel, slug, v, stored)
                if not self._claim_done(v):
                    in_progress.append(item)
                    dep = v.get("waiting_on")
                    has_edge = isinstance(dep, dict) and bool(dep.get("key"))
                    if has_edge or self._claim_parked(v):
                        next_up.append(self._board_next_row(
                            agent, channel, slug, v, dep if has_edge else None))
                elif v.get("review", "none") in ("operator", "delegate") \
                        and slug not in decision_slugs:
                    pending_review.append({**item, "review": v["review"]})
        pending_on_me.sort(key=lambda r: (not r["escalated"], r["since"]))
        proposals.sort(key=lambda r: r["since"])
        done.sort(key=lambda d: d["updated_at"], reverse=True)
        # Rows whose OWNER can move now lead: a finished, vanished, cyclic or
        # unreadable dependency is a park with nothing left to wait for, and
        # it reads as patience until someone says so.
        next_up.sort(key=lambda r: (not r["owner_can_act"], r["channel"],
                                    r["task"]))
        tasks = [row for ch in self.db.channels_of(agent.id)
                 for row in self.task_rows(ch)]
        return {
            "viewer": agent.id,
            "pending_on_me": pending_on_me,
            "tasks": tasks,
            "queue": queue,
            "proposals": proposals,
            "in_progress": in_progress,
            "next": next_up,
            "pending_review": pending_review,
            "done": done[:20],
            "counts": {"pending_on_me": len(pending_on_me), "queue": len(queue),
                       "tasks": len(tasks),
                       "tasks_delivered": sum(1 for t in tasks
                                              if t["status"] == "delivered"),
                       "proposals": len(proposals),
                       "in_progress": len(in_progress),
                       "next": len(next_up),
                       "pending_review": len(pending_review),
                       "done_shown": min(len(done), 20), "done_total": len(done)},
        }

    # How far `next` follows a chain of waits before it stops walking. A cycle
    # is caught by the visited set below whatever this is; the cap is for the
    # honest long chain, where the answer stops being useful well before it
    # stops being computable.
    _NEXT_MAX_HOPS = 8

    def _next_from_blocker(self, key: str,
                           tv: dict[str, Any]) -> tuple[str, str, str | None, bool]:
        """Read a row's OWN blocker into (kind, what, who, owner_can_act).

        The end of every resolution: either the row a chain arrives at, or —
        when there is no edge to walk — the waiting row itself.
        """
        head_owner = str(tv.get("owner") or "")
        tag = str(tv.get("blocked_on") or "").strip().lower()
        needs = str(tv.get("needs") or "").strip()
        needs_from = str(tv.get("needs_from") or "").strip()
        tail = f": {elide(needs, 160)}" if needs else ""
        if tag == "seat" and needs_from:
            return ("seat", f"{needs_from} must move `{key}`{tail}",
                    needs_from, False)
        if tag == "operator":
            ops = sorted(self.operator_ids())
            who = ops[0] if len(ops) == 1 else None
            return ("operator", f"{who or 'the operator'} must decide "
                                f"`{key}`{tail}", who, False)
        if tag == "owner":
            return ("owner",
                    f"{head_owner or 'its owner'} ranked other work above "
                    f"`{key}` — only they can raise it{tail}",
                    head_owner or None, False)
        if tag == "decision":
            return ("decision", f"`{key}` needs a ruling{tail}", None, False)
        if tag == "delegate":
            return ("delegate", f"`{key}` needs the delegate{tail}", None,
                    False)
        if tag == "external":
            # THE THIRD CASE (agora-tui, commons#437 §5): a row waiting on an
            # ACT, with no row to point at, for which the hub rightly refuses
            # `waiting_on`. A column reading only the row edge renders it as a
            # blank, which is the failure they flagged before a line of this
            # was written — and the fix is that such a row is its own head.
            #
            # THEIR OWN ROW DID NOT REACH THIS BRANCH, and I said at
            # commons#453 that it did: it is tagged `operator`, because the
            # act is a crates.io publish only laurent may perform. They then
            # refused to retag it quietly (commons#465), and the reason is
            # the design hole: `external` was TRUE of their row and dropped
            # the actor, `operator` kept the actor and called an act a
            # decision. The vocabulary is single-valued and their row is
            # genuinely both.
            #
            # `needs_from` is the answer and it already exists, validated,
            # for exactly this: WHO. An act outside the hub can name the
            # hand that performs it, and then no seat has to choose between
            # a true tag and a named actor. `supervise`'s
            # `needs_the_operator` keys on the ACTOR now rather than on the
            # word, so retagging cannot silently drop a row out of the
            # human's queue — which is what made the choice load-bearing.
            if needs_from:
                return ("external",
                        f"{needs_from} must do it outside the hub — "
                        f"`{key}`{tail}", needs_from, False)
            return ("external",
                    f"`{key}` waits on an act outside the hub{tail}",
                    None, False)
        if self._claim_parked(tv):
            # Park validation is TRANSITION-only, so rows parked before the
            # rule (and rows retagged down to just an edge) carry no tag.
            # Name the owner rather than inventing a blocker for them.
            return ("untagged",
                    f"`{key}` is parked without a blocker the hub can read — "
                    f"ask {head_owner or 'its owner'} what it needs{tail}",
                    head_owner or None, False)
        step = str(tv.get("next_step") or "").strip()
        return ("working",
                f"{head_owner or 'its owner'} is working `{key}`"
                + (f": {elide(step, 160)}" if step else ""),
                head_owner or None, False)

    def _board_next_row(self, agent: AgentInfo, channel: str, task: str,
                        value: dict[str, Any],
                        dep: dict[str, Any] | None) -> dict[str, Any]:
        """One `next` entry: what this waiting row is REALLY waiting for.

        The column laurent asked for (`request / tasks / ongoing / next / who
        / when`, dm#67) and the only one the hub could not compute. The input
        is `waiting_on`, ruled the carrier at
        `decision:a-row-dependency-goes-in-waiting-on-not-in-prose`.

        IT RESOLVES TRANSITIVELY, AND IT CROSSES EDGE TYPES AT THE HEAD.
        `tui` supplied the case within minutes of converting the first three
        rows (commons#429): their rows wait on `claim:msg-296-streaming-long-
        text`, which waits on no row at all — it is `blocked_on: seat`,
        `needs_from: agora-tui`, for a profile only that seat can run. A
        `next` that reported "another row" would answer with the one fact its
        reader already has, and stop one hop short of the seat who can end
        it. So this walks `waiting_on` to the head of the chain and then reads
        the head's OWN blocker to name an actor.

        Every branch states a fact the hub holds. It never guesses intent,
        and where it cannot see (a target in a room the VIEWER is not in — the
        edge guarantees only that the WAITER can read it) it says so rather
        than resolving silently to something weaker.
        """
        owner = str(value.get("owner") or "")
        first = None if dep is None else {
            "channel": str(dep.get("channel") or channel),
            "key": str(dep.get("key") or ""),
            "at_version": int(dep.get("at_version") or 0)}
        row: dict[str, Any] = {
            "channel": channel, "task": task, "owner": owner,
            "waiting_on": first, "chain": [], "head": None,
            "moved": False, "kind": "", "who": None, "what": "",
            "owner_can_act": False,
        }

        def settle(kind: str, what: str, who: str | None = None,
                   owner_can_act: bool = False) -> dict[str, Any]:
            row.update(kind=kind, what=what, who=who,
                       owner_can_act=owner_can_act)
            return row

        if first is None:
            # NO EDGE TO WALK: the waiting row is its own head. A park whose
            # blocker is a seat, the operator, a ruling or an act outside the
            # hub has a perfectly good answer already written on it.
            row["head"] = {"channel": channel, "key": f"claim:{task}"}
            return settle(*self._next_from_blocker(f"claim:{task}", value))

        cur_ch, cur_key = first["channel"], first["key"]
        seen = {(channel, f"claim:{task}")}
        hops = 0
        while True:
            if (cur_ch, cur_key) in seen:
                return settle("cycle",
                              f"`{cur_ch}/{cur_key}` is already in this chain "
                              "— these rows wait on each other and nothing "
                              "outside will end it. Re-point or unpark one.",
                              owner_can_act=True)
            seen.add((cur_ch, cur_key))
            if not self.db.is_member(cur_ch, agent.id):
                # The pointer is visible (it is in a row you can read); the
                # target's state is not, and reporting a status from a room
                # this viewer is not in would leak it.
                return settle("unreadable",
                              f"waiting on `{cur_ch}/{cur_key}`, in a room you "
                              f"are not in — ask {owner or 'its owner'}; the "
                              "hub will not read that row to you.")
            target = self.db.store_get(cur_ch, cur_key)
            if target is None:
                return settle("gone",
                              f"`{cur_ch}/{cur_key}` no longer exists — "
                              "nothing will ever resume this row. Re-point it "
                              "or take it off park.", owner_can_act=True)
            row["chain"].append(f"{cur_ch}/{cur_key}")
            row["head"] = {"channel": cur_ch, "key": cur_key}
            tv = target.value if isinstance(target.value, dict) else {}
            if len(row["chain"]) == 1 and target.version > first["at_version"]:
                row["moved"] = True
            head_owner = str(tv.get("owner") or target.updated_by or "")
            if self._claim_done(tv):
                word = self._claim_status_word(tv) or "done"
                return settle("done",
                              f"`{cur_key}` is {word} — the wait is over. "
                              "Unpark this row or close it.",
                              who=owner or None, owner_can_act=True)
            onward = tv.get("waiting_on")
            if isinstance(onward, dict) and onward.get("key"):
                hops += 1
                if hops >= self._NEXT_MAX_HOPS:
                    return settle("deep",
                                  f"the chain from `{cur_key}` is more than "
                                  f"{self._NEXT_MAX_HOPS} rows long and the "
                                  "hub stopped walking it — read it by hand.")
                cur_ch = str(onward.get("channel") or cur_ch)
                cur_key = str(onward.get("key") or "")
                continue
            # HEAD OF THE CHAIN. Its own blocker names the actor.
            return settle(*self._next_from_blocker(
                cur_key, {**tv, "owner": head_owner}))

    # -- operator desk (0111/M1+M3): everything blocked on the human ---------------

    def _done_when_satisfied(self, p: dict[str, Any]) -> bool:
        """Evaluate a done_when predicate against LIVE hub state (M3). Every
        kind is a fact the hub already stores; evaluation at read time is
        what makes desk rows self-clearing — 'waiting on you: retire agency'
        cannot outlive the retirement."""
        kind = p.get("kind")
        if kind == "retired":
            return self.db.agent_retirement(str(p["agent"])) is not None
        if kind == "decision":
            return self.db.store_get(str(p["channel"]),
                                     f"decision:{p['slug']}") is not None
        if kind == "work_status":
            row = self.db.store_get(str(p["channel"]),
                                    f"{self.WORK_ROW_PREFIX}{p['item']}")
            return (row is not None and isinstance(row.value, dict)
                    and str(row.value.get("status", "")) == str(p["status"]))
        if kind == "delegation":
            return self.is_delegate(str(p["agent"]), str(p["power"]))
        if kind == "closed":
            m = self.db.get_message(str(p["message_id"]))
            if m is None or m.channel != str(p["channel"]):
                return False
            return self._discharge(m, self.db.replies_to(m.id)).closed
        return False

    def desk(self, agent: AgentInfo) -> dict[str, Any]:
        """Everything waiting on the OPERATOR, derived at read time (0111,
        M1 from the c3860 staleness review): STATE not log — no cursor to
        fall behind, nothing carried forward. The trigger incident ('WAITING
        ON YOU: agency retirement', six hours after the retirement) is
        structurally impossible on this surface: rows are computed from hub
        state at the moment of the call, and queue rows carrying a
        `done_when` predicate (M3) move to `satisfied` the instant the hub
        observes the act. Viewer gate matches /status: the operator's desk
        is operator-facing; a reporting delegate stewards it (composes the
        digest FROM it); it never leaks to ordinary seats ('what waits on
        the human' is exactly what the board deliberately omits)."""
        if not (agent.operator or self.is_delegate(agent.id, "reporting")):
            raise HubError(403, "the desk is for operators and reporting "
                                "delegates (whoami.delegations is the proof)")
        now = time.time()
        rows: list[dict[str, Any]] = []
        satisfied: list[dict[str, Any]] = []
        operators = sorted(self.operator_ids())
        for op in operators:
            info = AgentInfo(id=op, name=op, operator=True)
            for o in self.owed(info).to_answer:
                # A meaningful label even when the sender set no title (DM
                # asks routinely omit it): title, else the first pending
                # ask's text, else a body snippet — never a bare
                # "(untitled)" on the surface the operator reads first.
                what = o.title
                if not what:
                    msg = self.db.get_message(o.id)
                    if msg is not None:
                        pend = set(o.pending_asks)
                        ask_texts = [str(a.get("text", "")) for a in asks_of(msg)
                                     if str(a.get("id")) in pend and a.get("text")]
                        what = (ask_texts[0] if ask_texts
                                else elide((msg.body or "").strip(), 80))
                rows.append({
                    "kind": "ask", "operator": op,
                    "channel": o.channel, "seq": o.seq, "id": o.id,
                    "what": what or "(untitled ask)",
                    "who_waits": o.sender,
                    # Desk rows keep a pre-rounded age: this surface is
                    # rendered by the hub itself (fact lines for the
                    # operator), so there is no second party to drift from.
                    "age_minutes": round((now - o.created_at) / 60, 1),
                    "one_action": "answer it (or decline it: declines=[ids])",
                })
            for channel in self.db.channels_of(op):
                prefix = f"{self._QUEUE_PREFIX}{op}:"
                for entry in self.db.store_keys(channel):
                    if not entry["key"].startswith(prefix):
                        continue
                    stored = self.db.store_get(channel, entry["key"])
                    if (stored is None or not isinstance(stored.value, dict)
                            or stored.value.get("decided")):
                        continue
                    v = stored.value
                    row = {
                        "kind": "queue", "operator": op, "channel": channel,
                        "key": entry["key"], "what": v.get("q", ""),
                        # The writer's context, if they left any. Served here
                        # because a `note` nobody renders is a field that
                        # teaches the next writer to reach for `decided`
                        # again (operator-board#27).
                        "note": v.get("note", ""),
                        "who_waits": ", ".join(v.get("waiting", [])) or stored.updated_by,
                        "age_minutes": round((now - stored.updated_at) / 60, 1),
                        "one_action": (v.get("options") or ["decide"])[0],
                    }
                    done_when = v.get("done_when")
                    if isinstance(done_when, dict) and self._done_when_satisfied(done_when):
                        satisfied.append({**row, "done_when": done_when,
                                          "one_action": "the wait is over — "
                                                        "close/decide the row"})
                    else:
                        rows.append(row)
        rows.sort(key=lambda r: -r["age_minutes"])
        for op in operators:
            for channel in self.db.channels_of(op):
                for t in self.task_rows(channel):
                    if t["status"] != "delivered" or t["requester"] != op:
                        continue
                    rows.append({
                        "kind": "task", "operator": op, "channel": channel,
                        "key": t["key"], "what": t["title"] or t["source"],
                        "who_waits": t["delivered_by"] or t["coordinator"] or "",
                        "report": t["report"],
                        "age_minutes": round((now - float(t["delivered_at"] or now)) / 60, 1),
                        "one_action": ("accept it (resolved on the request, or "
                                       "status=accepted on the row) or reject "
                                       "it with a verdict"),
                    })
        return {"computed_at": now, "viewer": agent.id,
                "operators": operators, "rows": rows, "satisfied": satisfied,
                "counts": {"rows": len(rows), "satisfied": len(satisfied)}}

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
        state, created = self.db.pause_start(sanitize_text(reason, 200, field="pause reason"), by)
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

    # -- hub charter (0146): the standing "who is who" -----------------------------
    #
    # Same tier and same author as the rules (admin key), a different job:
    # the rules ride EVERY whoami and are budgeted to a screenful, so the
    # role model cannot live there. This is pull-only — a pointer in whoami,
    # the text on demand — which keeps ADR-0002's "no scheduled re-push".

    def hub_charter(self) -> dict[str, Any]:
        """The served hub charter. Version 0 = the packaged ROLE_CHARTER,
        which is why a fresh hub is never charterless: the default exists by
        construction, needs no write, and can never be lost."""
        row = self.db.hub_charter_get()
        if row is None:
            return {"version": 0, "text": ROLE_CHARTER, "updated_by": "",
                    "updated_at": 0.0, "packaged": True}
        return {"version": row["version"], "text": row["text"],
                "updated_by": row["updated_by"],
                "updated_at": row["updated_at"], "packaged": False}

    # -- what kind of seat is this? (0147) ------------------------------------------
    #
    # The charter names exactly four kinds of seat, and this is the ONE place
    # the hub answers "which of them are you right now" — from live state, not
    # from a stored label: you are an owner while you own a live room, a
    # delegate while an unexpired grant says so, an operator by the flag. The
    # answer drives the role-scoped charter view and nothing else; no
    # permission check reads it (each of those has its own, narrower gate).

    def seat_kinds(self, agent_id: str,
                   operator: bool | None = None) -> tuple[tuple[str, ...],
                                                          tuple[str, ...]]:
        """(kinds, powers) for one seat. Every seat is a member first. An
        operator is served every kind: they already hold every power, so
        there is nothing about this hub that is not theirs to read."""
        is_operator = (agent_id in self.operator_ids() if operator is None
                       else bool(operator))
        if is_operator:
            return ("member", "owner", "delegate", "operator"), ()
        kinds = ["member"]
        if self.db.owns_any_channel(agent_id):
            kinds.append("owner")
        powers = sorted({p for d in self.active_delegations()
                         if d["agent_id"] == agent_id
                         for p in (d.get("powers") or ())})
        if powers:
            kinds.append("delegate")
        return tuple(kinds), tuple(powers)

    def charter_view_for(self, agent: AgentInfo, text: str,
                         full: bool = False) -> CharterViewResult:
        """This seat's view of a charter text (governance.charter_view)."""
        kinds, powers = self.seat_kinds(agent.id, operator=agent.operator)
        return charter_view(text, roles=kinds, powers=powers, full=full)

    def read_hub_charter(self, agent: AgentInfo,
                         full: bool = False) -> dict[str, Any]:
        """Read the hub charter AND record this seat's receipt — the same
        contract as reading a channel charter's head, in the same table
        under the reserved scope `hub`. Delivery proof, never agreement.

        Since 0147 the TEXT served is this seat's view: the common sections
        plus the ones addressed to the kinds of seat it is, with the delegate
        section scoped to the powers it actually holds. `full=True` serves the
        whole document to anyone who asks — the view is a token economy, not
        an access control, and the response always names what it left out.

        The RECEIPT is unchanged by any of this: it records that version N was
        delivered, because that is what the posting gate and the operator's
        reader roster mean by it. Which slice was served rides alongside in
        `charter_receipts.view` and is reported separately (see
        `hub_charter_pointer`), so a promotion never silently converts "I read
        the current charter" into "I read the parts that used to apply to me"."""
        doc = self.hub_charter()
        view = self.charter_view_for(agent, doc["text"], full=full)
        self.db.charter_receipt_set(agent.id, HUB_CHARTER_SCOPE, doc["version"],
                                    view.key)
        out = {**doc, **self._view_fields(view, doc["text"]),
               "scope": HUB_CHARTER_SCOPE, "your_receipt": doc["version"]}
        # THE FULL DELEGATE BRIEF, served (2026-08-04). It lived behind
        # `agora delegate --charter` — a CLI a driven seat is explicitly
        # forbidden to use — so in 16.4M characters of live traffic its
        # distinctive phrases appear ZERO times, except one paste by hand.
        # The stewardship half (the waiting_on/board/presence radar, nudge
        # discipline, "two silent nudges = stop; re-route AND tell the
        # operator", retiring obligations pinned on a dark seat) exists
        # nowhere else, and its absence is visible in a 149-item operator
        # desk. Only a seat that HOLDS a delegation pays for the tokens.
        if any(d["agent_id"] == agent.id for d in self.active_delegations()):
            from ..governance import DELEGATE_CHARTER
            out["delegate_brief"] = DELEGATE_CHARTER
        return out

    @staticmethod
    def _view_fields(view: CharterViewResult, whole: str) -> dict[str, Any]:
        """The uniform description of a served slice — same keys at hub scope
        and inside a channel read, so one renderer handles both."""
        return {"text": view.text, "view": list(view.roles),
                "powers": list(view.powers), "sliced": view.sliced,
                "omitted": list(view.omitted), "view_note": view.note,
                "bytes": len(view.text.encode()),
                "full_bytes": len(whole.encode()),
                "read_all_with": "read_charter(full=True) / GET /charter?full=true"}

    def hub_charter_pointer(self, agent_id: str,
                            operator: bool | None = None) -> dict[str, Any]:
        """What whoami carries: the version, whether THIS seat has read it,
        and — since 0147 — which view it would be served now. No text: a
        charter re-pushed on every session-start call would be exactly the
        periodic authority injection ADR-0002 forbids.

        `current` keeps its meaning exactly (you hold a receipt for the
        version in force). `view_current` is the second question scoping
        creates: a member who read v3 and was granted a delegation this
        morning still holds a valid v3 receipt and has still never seen the
        delegate section. It flips false on GROWTH only, carries a note
        saying why, and blocks nothing."""
        doc = self.hub_charter()
        row = self.db.charter_receipt_row(agent_id, HUB_CHARTER_SCOPE)
        mine = row["version"] if row else None
        kinds, powers = self.seat_kinds(agent_id, operator=operator)
        key = charter_view_key(kinds, powers)
        current = mine is not None and mine >= doc["version"]
        covers = charter_view_covers(row.get("view") if row else None, key)
        out = {"version": doc["version"], "your_receipt": mine,
               "current": current, "view": list(kinds),
               "view_current": bool(row) and covers,
               "read_with": "read_charter() / GET /charter"}
        if current and not covers:
            out["note"] = (
                f"you hold a receipt for v{doc['version']}, but the charter "
                f"you were served does not cover the seat you are now "
                f"({'+'.join(kinds)}"
                + (f", powers: {'+'.join(powers)}" if powers else "")
                + "): read_charter() for the parts you have not been shown.")
        return out

    # -- channel scope: the room's own charter, and what it inherits ----------------

    # INHERITANCE, and why it is two labelled parts rather than one text:
    # concatenating the hub view into the room's `content` would corrupt the
    # only round-trip the charter file has — an owner reads the head, edits,
    # writes back with expect_version — and it would make "which version is
    # this receipt for?" unanswerable when the two scopes version separately.
    # A pure REFERENCE ("go read the hub charter too") was rejected for the
    # opposite reason: the field showed agents do not make the second call,
    # and the operator's requirement is that the rules are actually in mind.
    # So: ONE call, TWO labelled parts, and the hub part rides only when this
    # seat is actually behind on it — a seat that already holds a current
    # receipt for its current view pays nothing for the inheritance.

    def read_channel_charter(self, agent: AgentInfo, channel: str,
                             version: int | None = None,
                             full: bool = False) -> dict[str, Any]:
        """A room's charter, plus the hub charter it inherits.

        The room's own text is served WHOLE and verbatim, never role-sliced:
        the role model lives at hub scope by construction, and slicing a
        room's rules per seat would let an owner hide a rule from the member
        it binds. Only the inherited hub part — the one document that IS
        about kinds of seat — is scoped."""
        row = self.fs_read(agent, channel, CHARTER_PATH, version).model_dump()
        out: dict[str, Any] = {**row, "channel": channel}
        doc = self.hub_charter()
        receipt = self.db.charter_receipt_row(agent.id, HUB_CHARTER_SCOPE)
        kinds, powers = self.seat_kinds(agent.id, operator=agent.operator)
        key = charter_view_key(kinds, powers)
        version_ok = bool(receipt) and receipt["version"] >= doc["version"]
        view_ok = charter_view_covers(receipt.get("view") if receipt else None, key)
        hub: dict[str, Any] = {"version": doc["version"], "view": list(kinds),
                               "scope": HUB_CHARTER_SCOPE,
                               "updated_by": doc["updated_by"],
                               "packaged": doc["packaged"]}
        if version is not None:
            hub.update(included=False, text=None,
                       why=("archive read: an old version of this room's rules "
                            "is history, and history does not inherit"))
        elif full or not (version_ok and view_ok):
            hub_view = charter_view(doc["text"], roles=kinds, powers=powers,
                                    full=full)
            self.db.charter_receipt_set(agent.id, HUB_CHARTER_SCOPE,
                                        doc["version"], hub_view.key)
            hub.update(included=True, **self._view_fields(hub_view, doc["text"]))
            if full:
                because = "you asked for everything"
            elif receipt is None:
                because = ("you had no receipt for the hub charter "
                           f"(v{doc['version']})")
            elif not version_ok:
                because = (f"your receipt was for v{receipt['version']}, the "
                           f"hub charter is at v{doc['version']}")
            else:
                because = ("your seat changed since you read it (now: "
                           f"{'+'.join(kinds)})")
            hub["why"] = (f"{because} — it is included here, and reading it "
                          "recorded your receipt")
        else:
            hub.update(included=False, text=None,
                       why=(f"not repeated: you hold a current receipt for hub "
                            f"charter v{doc['version']} in your view "
                            f"({'+'.join(kinds)}). read_charter() re-reads it."))
        out["hub"] = hub
        out["inherits"] = (
            "This room's charter ADDS to the hub rules (whoami, every turn) "
            "and the hub charter (who is who). Neither can cancel the tier "
            "above it.")
        legacy = self._legacy_norms(channel)
        if legacy:
            out["norms_legacy"] = legacy
        return out

    def _legacy_norms(self, channel: str) -> dict[str, Any] | None:
        """`channel:meta.norms` (0060) is the charter's older, weaker twin:
        free text, unversioned, unreceipted, ungated, and delivered by a
        different surface. Two places to write room rules is one too many, so
        it is DEPRECATED in favour of `channel/charter.md` — deprecation-safe:
        the field is still accepted, still stored, still served where it
        always was. What changes is that it is no longer a SECOND place to
        read: a room that still has one gets it here, labelled, next to the
        charter, with the one line an owner needs to fold it in."""
        meta = self.db.store_get(channel, CHANNEL_META_KEY)
        value = meta.value if meta and isinstance(meta.value, dict) else {}
        norms = value.get("norms")
        if not isinstance(norms, str) or not norms.strip():
            return None
        return {"text": norms,
                "note": ("DEPRECATED surface: `channel:meta.norms` predates "
                         "charters and is unversioned, unreceipted and "
                         "ungated. It is shown here so this room has ONE "
                         "place to read its rules. Owner: fold it into "
                         f"'{CHARTER_PATH}' and clear the field.")}

    def set_hub_charter(self, text: str, updated_by: str = "operator") -> dict[str, Any]:
        """Publish a new hub charter version. Announced in hub-alerts (the
        same visibility a delegation grant gets — a change to who-is-who is
        at least as consequential), and every seat's whoami pointer flips to
        stale at its next call. Nothing is blocked and nobody is woken."""
        if not isinstance(text, str) or not text.strip():
            raise HubError(400, "hub charter text must be a non-empty string")
        if len(text.encode()) > MAX_STORE_VALUE_BYTES:
            raise HubError(413, f"hub charter exceeds {MAX_STORE_VALUE_BYTES} bytes")
        row = self.db.hub_charter_set(text, sanitize_text(updated_by, 64, field="updated_by"))
        from ..governance import charter_missing_roles, charter_missing_sections
        missing = charter_missing_roles(text)
        # Scoping is opt-in by CONVENTION, so publishing is where an operator
        # finds out whether they got it. Never a refusal: an unsliceable
        # charter is served whole and every seat still gets every rule.
        unsectioned = charter_missing_sections(text)
        self._ensure_alerts_channel()
        self._post_system(
            self.DARK_ALERTS_CHANNEL,
            f"HUB CHARTER v{row['version']} published by {row['updated_by'] or 'operator'} "
            f"— the standing role model (who is who) changed. Every seat sees "
            f"the new version in whoami.hub_charter and reads it with "
            f"read_charter(); receipts for the previous version no longer "
            f"count as current."
            + (f" WARNING: this text never mentions {len(missing)} seat "
               f"kind(s) this hub implements: {'; '.join(missing)}."
               if missing else "")
            + (f" NOTE: served WHOLE to every seat — no `## ` section of its "
               f"own for {', '.join(unsectioned)}, so it cannot be scoped "
               f"per role." if unsectioned else
               " Each seat is served only the sections addressed to it "
               "(`read_charter(full=True)` serves the whole document)."))
        return {"version": row["version"], "text": row["text"],
                "updated_by": row["updated_by"], "updated_at": row["updated_at"],
                "missing_roles": missing, "sliceable": not unsectioned,
                "unsectioned_roles": unsectioned}

    def hub_charter_version(self, version: int) -> dict[str, Any]:
        """One archived version verbatim. Version 0 is the packaged default,
        which is always readable even on a hub that has published its own."""
        if version == 0:
            return {"version": 0, "text": ROLE_CHARTER, "updated_by": "",
                    "updated_at": 0.0}
        row = self.db.hub_charter_version(version)
        if row is None:
            raise HubError(404, f"hub charter version {version} is not in the "
                                "archive (0 = the packaged default)")
        return row

    def hub_charter_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.hub_charter_history(limit)

    def charter_readers(self, scope: str) -> list[dict[str, Any]]:
        """Who has read which version of one charter — the receipts table's
        first read-back surface. `current` is computed against the version
        served RIGHT NOW, so the answer is "who is up to date", not "who
        ever looked"."""
        if scope == HUB_CHARTER_SCOPE:
            head = self.hub_charter()["version"]
        else:
            row = self.db.fs_get(scope, FS_PREFIX + CHARTER_PATH)
            head = 0 if row is None or row["deleted"] else row["version"]
        rows = self.db.charter_receipts_for(scope)
        return [{**r, "current": r["version"] >= head} for r in rows]

    def channel_charter_receipts(self, agent: AgentInfo,
                                 channel: str) -> dict[str, Any]:
        """The room's own read-record: members present, who has read the
        current charter version, who has not. Member-visible on purpose —
        it is their room, and an owner who cannot see who is unbriefed
        cannot act on it."""
        self.require_membership(channel, agent.id)
        row = self.db.fs_get(channel, FS_PREFIX + CHARTER_PATH)
        head = 0 if row is None or row["deleted"] else row["version"]
        by_agent = {r["agent_id"]: r for r in self.db.charter_receipts_for(channel)}
        members = []
        for m in self.db.list_members(channel):
            got = by_agent.get(m.agent_id)
            members.append({"agent_id": m.agent_id, "role": m.role,
                            "version": got["version"] if got else None,
                            "read_at": got["read_at"] if got else None,
                            "current": bool(got and got["version"] >= head)})
        return {"channel": channel, "path": CHARTER_PATH, "version": head,
                "gated": bool(self._norms_required(channel)), "members": members}

    def _norms_required(self, channel: str) -> bool:
        meta = self.db.store_get(channel, CHANNEL_META_KEY)
        return bool(meta and isinstance(meta.value, dict)
                    and meta.value.get("norms_required"))

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
        started = time.time()
        alerted: list[str] = []
        dark_now: set[str] = set()
        lurk_now: set[str] = set()   # lurk has its OWN live-set: a seat can
        #                              legally transition deaf -> lurk-candidate
        #                              in one pass, and sharing dark_now would
        #                              keep the ended deaf episode alive
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
                    self._lurk_sweep_one(agent_id, lurk_now, alerted)
                continue
            # A hub-blocked seat is offline BY DESIGN — the operator locked it
            # out. Alerting "only the operator can start it" is a standing
            # misdiagnosis (review F5), and its obligations now revert to
            # broadcast (F3), so skip it.
            if agent_id in hub_blocked:
                continue
            # escalated is viewer-specific: open/blocked past SLA, or an
            # addressed directive debt (0102) the seat never engaged — and
            # OWNED by this seat (_escalated_debts: the inbox alone shows
            # other seats' rows too, which is how at-test#363 was cited
            # against six seats that never held it).
            overdue = self._escalated_debts(agent_id)
            # DELETED (2026-08-03 audit): the dark-DELEGATE widening, which
            # alerted on ANY pending obligation a delegate held instead of
            # only SLA-breached ones. It bought at most one SLA of earliness
            # and cost a self-triggering loop: every watchdog alert is posted
            # ADDRESSED TO the reporting delegate, so the alert about seat A
            # instantly became a fresh un-escalated obligation for the
            # delegate, which this widening then read as cause to alert about
            # the delegate — in the same sweep pass, with the alert text
            # claiming "SLA-breached obligation(s), oldest ~0 min" about a
            # debt the hub had authored four seconds earlier (live hub-alerts
            # #930/#931, 2026-08-03 00:49:25/00:49:29). One predicate now,
            # true for every seat: escalated debt this seat owns.
            if not overdue:
                continue
            dark_now.add(agent_id)
            if agent_id in self._dark_since:
                continue  # already alerted this episode
            now = time.time()
            # PERSISTED (2026-08-04). This clock is what the 7-day retirement
            # proposal measures, and holding it only in memory meant it reset
            # on every hub restart — so the proposal had fired ZERO times in
            # 28 days while ten seats sat silent past the threshold (one for
            # 28.0 days). An episode that predates this process is still an
            # episode; the alert itself stays once-per-episode via the
            # restart-durable flap guard below.
            self._dark_since[agent_id] = self._dark_episode_start(agent_id, now)
            # Flap guard (review MED-4), now RESTART-DURABLE (c3436): an
            # agent oscillating — or a hub that just bounced — must not
            # re-alert while the same overdue work stands.
            if now - self._alerted_at("dark", agent_id) < DARK_REALERT_SECONDS:
                continue
            self._mark_alerted("dark", agent_id, now)
            oldest = min(e.created_at for e in overdue)
            age_min = (now - oldest) / 60
            # Never leak private/DM channel names into the alert (HIGH-2 —
            # the alerts channel is operator-private, but redact anyway:
            # alert texts get quoted and forwarded).
            example = "a private thread"
            ch = self.db.get_channel(overdue[0].channel)
            if ch is not None and not ch.private:
                example = f"{overdue[0].channel}#{overdue[0].seq}"
            self._post_silence_watchdog_alert(
                agent_id,
                f"AGENT DARK: {agent_id} is offline holding {len(overdue)} "
                f"SLA-breached obligation(s), oldest ~{age_min:.0f} min "
                f"(e.g. {example}). Escalation cannot reach an offline seat "
                f"— only the operator can start it. One alert per dark "
                f"episode.",
                explicit_class="dead", kind="dark",
            )
            alerted.append(agent_id)
        # Episodes end when the seat returns or its overdue work clears.
        for agent_id in list(self._dark_since):
            if agent_id not in dark_now:
                del self._dark_since[agent_id]
                self.db.meta_set(f"dark:since:{agent_id}", "")
        # Deaf episodes end the same way: reception recovered or work cleared.
        for agent_id in list(self._deaf_since):
            if agent_id not in dark_now:
                del self._deaf_since[agent_id]
        # Lurk episodes too: the seat read/answered, or the debt cleared.
        for agent_id in list(self._lurk_since):
            if agent_id not in lurk_now:
                del self._lurk_since[agent_id]
                self._lurk_alerted.discard(agent_id)
        self._close_ended_silence_alerts()
        alerted.extend(self._steward_sweep())
        alerted.extend(self._phase_sweep())
        alerted.extend(self._claim_due_sweep())
        alerted.extend(self._waiting_on_sweep())
        alerted.extend(self._consultation_sweep())
        alerted.extend(self._mission_change_sweep())
        alerted.extend(self._fs_trigger_sweep())
        alerted.extend(self._blocking_sweep())
        alerted.extend(self._escalation_rewake_sweep())
        alerted.extend(self._dropped_wake_sweep())
        alerted.extend(self._fleet_liveness_sweep())
        alerted.extend(self._retire_report_digest_rows())
        self._note_sweep("dark", started, len(alerted))
        return alerted

    def _note_sweep(self, name: str, started: float, n: int) -> None:
        """Record that a sweep completed (for `agora doctor`'s hub health)."""
        self.sweep_runs[name] = {"last_run": time.time(),
                                 "seconds": round(time.time() - started, 3),
                                 "actions": n}

    def _dark_episode_start(self, agent_id: str, now: float) -> float:
        """When this seat's CURRENT dark episode began, across restarts.

        Read-through from `meta`: an episode already recorded keeps its
        original start (that is what makes a 7-day silence measurable),
        and a new one is stamped now. Corrupt or absent -> now, which fails
        toward a SHORTER episode: the hub must never over-claim how long a
        seat has been gone."""
        key = f"dark:since:{agent_id}"
        raw = self.db.meta_get(key)
        if raw:
            try:
                started = float(raw)
                if 0 < started <= now:
                    return started
            except (TypeError, ValueError):
                pass
        self.db.meta_set(key, repr(now))
        return now

    def _fleet_eligible_agents(self) -> list[str]:
        """The 0110 denominator, repaired 2026-08-04: seats this hub has
        OBSERVED live within FLEET_SIGNAL_WINDOW — not every seat ever
        registered. FLEET DARK asks "did the running fleet vanish?", and a
        roster where 43 of 50 seats are last month's experiments answers a
        different question ("was this hub ever busier?") with a permanent
        yes: the live hub sat in chronic FLEET DARK for days, and
        `_fleet_eligible_agents` therefore turned every liveness-derived
        surface into noise: a stale roster looked like a live collapse.

        Operators are excluded: a human seat's presence flapping (active on
        any authenticated GET, dark ten minutes later) is not fleet health.
        Observation happens HERE, each sweep pass, and persists as one meta
        row so a restart neither forgets the fleet nor false-alarms: after
        downtime longer than the window the eligible set is simply empty,
        and an empty set never alarms (FLEET_MIN_ELIGIBLE floor)."""
        hub_blocked = {b["agent_id"] for b in self.db.blocks_active(self.HUB_SCOPE)}
        ops = self.operator_ids()
        now = time.time()
        candidates: list[str] = []
        for agent_id in self.db.list_agent_ids():
            if agent_id == "hub" or agent_id in ops:
                continue
            if agent_id in hub_blocked:
                continue
            if self.db.agent_retirement(agent_id) is not None:
                continue
            candidates.append(agent_id)
            if self._fleet_seat_live(agent_id):
                self._fleet_last_signal[agent_id] = now
        self._persist_fleet_signals(now)
        return [a for a in candidates
                if now - self._fleet_last_signal.get(a, 0.0)
                <= FLEET_SIGNAL_WINDOW]

    def _persist_fleet_signals(self, now: float) -> None:
        """At most one meta write per 10 minutes; drops entries too old to
        ever matter again so the row cannot grow with roster churn."""
        if now - self._fleet_signal_persisted_at < 600.0:
            return
        self._fleet_signal_persisted_at = now
        keep = {a: ts for a, ts in self._fleet_last_signal.items()
                if now - ts <= 2 * FLEET_SIGNAL_WINDOW}
        self._fleet_last_signal = keep
        try:
            self.db.meta_set("fleet:last_signal", json.dumps(keep))
        except Exception:
            pass

    def _fleet_seat_live(self, agent_id: str) -> bool:
        """A seat counts as live when reception is armed OR it has recent
        authenticated activity / a push connection (MCP-only tabs)."""
        rec, _ = self.presence.reception(agent_id)
        if rec == "armed":
            return True
        return self.presence.get(agent_id).state in ("idle", "working", "active")

    def _fleet_open_claims_count(self) -> int:
        n = 0
        for ch in self.db.channel_names():
            for entry in self.db.store_keys(ch):
                if not entry["key"].startswith("claim:"):
                    continue
                stored = self.db.store_get(ch, entry["key"])
                if stored is None or not isinstance(stored.value, dict):
                    continue
                # Parked work is UNFINISHED work — the board says so, and
                # counting it closed is what let the hourly digest tell the
                # delegate "7/7 live, nothing outstanding" while its own room
                # was stalled on an already-satisfied dependency (2026-08-06).
                # `_steward_sweep` keeps its parked exemption: that one is a
                # THIRD PARTY re-asking a question the status already
                # answered. A count is not a nag.
                if self._claim_done(stored.value):
                    continue
                n += 1
        return n

    def fleet_liveness_snapshot(self) -> dict[str, Any]:
        """0110 aggregate for /status: eligible/live counts, collapse signal,
        open claims, and in-memory dark-episode state."""
        eligible = self._fleet_eligible_agents()
        live = [a for a in eligible if self._fleet_seat_live(a)]
        now = time.time()
        fraction = (len(live) / len(eligible)) if eligible else 1.0
        collapsed = bool(eligible) and (
            len(live) == 0 or fraction < FLEET_LIVE_FRACTION)
        dark_since: float | None = self._fleet_dark_since
        if collapsed and dark_since is None:
            dark_since = now
        dark_seconds = int(now - dark_since) if (collapsed and dark_since) else 0
        return {
            "eligible": len(eligible),
            "live": len(live),
            "live_fraction": round(fraction, 3),
            "collapsed": collapsed,
            "dark_episode": self._fleet_dark_alerted,
            "dark_seconds": dark_seconds,
            "open_claims": self._fleet_open_claims_count(),
            "min_eligible": FLEET_MIN_ELIGIBLE,
        }

    def activity_stats(self, agent: AgentInfo) -> dict[str, Any]:
        """"Is this hub actually moving?" — message RATE, nothing else.

        The operator's question was literally "how many dm/minute and per 10
        mn ... that would help understand if the hub is active or not", and a
        rate is the one thing none of the existing surfaces answer: `/status`
        reports who is live, `/board` reports what is owed, and both look
        identical on a hub that has been silent for an hour and on one that
        is mid-storm.

        Two resolutions, because they answer different questions: per-minute
        over the last 10 minutes says "is it moving right now", per-10-minute
        over the last hour says "has it been moving at all". Buckets are
        aligned to wall-clock minutes so two seats polling seconds apart read
        the same rows, and empty buckets are emitted explicitly — a gap in
        the series is the signal, so it must not be a missing key.

        COUNTS ONLY. No titles, no bodies, no channel names, no DM pairs:
        this is the one hub read that is useful to a seat that belongs to no
        room, so it must stay useless as a way to see into rooms.

        Sender NAMES obey the boundary `list_presence` already draws — seats
        you share a channel with, everyone for an operator. `active_seat_count`
        is the true count either way, so the rate is never understated; what a
        stranger cannot get is a global who-is-awake oracle, which is exactly
        the thing `get_presence` refuses one call away.
        """
        now = time.time()
        minute_slots, bucket_slots = 10, 6
        bucket = ACTIVITY_BUCKET_SECONDS

        def window(width: float, slots: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            """The `slots` most recent wall-clock-aligned buckets of `width`.
            Aligned, not relative: two seats polling seconds apart must read
            the same rows, or the rate looks like it flickers."""
            newest = int(now // width)
            raw = self.db.activity_counts((newest - slots + 1) * width, width)
            rows = []
            for idx in range(newest - slots + 1, newest + 1):
                counts = raw["buckets"].get(idx) or {"total": 0, "public": 0,
                                                     "dm": 0}
                rows.append({"start": idx * width,
                             "label": time.strftime("%H:%M",
                                                    time.localtime(idx * width)),
                             **counts})
            return rows, raw

        def totals(rows: list[dict[str, Any]]) -> dict[str, int]:
            return {k: sum(r[k] for r in rows) for k in ("total", "public", "dm")}

        per_minute, minute_raw = window(60.0, minute_slots)
        per_bucket, _ = window(bucket, bucket_slots)
        last_10m, last_60m = totals(per_minute), totals(per_bucket)
        # Distinct senders in the SHORT window: "who is awake", not a roster.
        # Named only within the caller's own visibility; counted in full.
        all_senders = minute_raw["senders"]
        if agent.operator:
            visible = set(all_senders)
        else:
            # "hub" is the hub itself, not a seat with rooms to be private
            # about: it authors the opening row of every room the caller is
            # already reading, so hiding the name reveals nothing and only
            # makes the list look wrong.
            visible = {agent.id, "hub"}
            for channel in self.db.channels_of(agent.id):
                visible.update(m.agent_id
                               for m in self.db.list_members(channel))
        recent_senders = [s for s in all_senders if s in visible]
        last_at = minute_raw["last_message_at"]
        quiet_for = (now - last_at) if last_at else None
        if last_10m["total"]:
            verdict = (f"active — {last_10m['total']} messages in the last "
                       f"{minute_slots} minutes "
                       f"({last_10m['total'] / minute_slots:.1f}/min)")
        elif last_at:
            verdict = ("quiet since "
                       + time.strftime("%H:%M", time.localtime(last_at)))
        else:
            verdict = "silent — this hub has never carried a message"
        return {
            "now": now,
            "windows": {"per_minute_slots": minute_slots,
                        "bucket_seconds": bucket,
                        "bucket_slots": bucket_slots},
            "per_minute": per_minute,
            "per_bucket": per_bucket,
            "totals": {"last_10m": last_10m, "last_60m": last_60m},
            "rate_per_minute": {
                "last_10m": round(last_10m["total"] / minute_slots, 2),
                "last_60m": round(last_60m["total"] / (bucket_slots * bucket / 60.0), 2)},
            "active_seats": recent_senders,
            # The TRUE count, even when some of those seats are not yours to
            # name: an understated count would misreport the hub's liveness,
            # which is the whole question this surface exists to answer.
            "active_seat_count": len(all_senders),
            "last_message_at": last_at,
            "quiet_for_seconds": round(quiet_for, 1) if quiet_for else quiet_for,
            "verdict": verdict,
        }

    def _ensure_operator_dm_channel(self, operator_id: str) -> str:
        """Hub→operator DM for fleet alarms (0110): ownerless pairwise room."""
        name = dm_channel_name("hub", operator_id)
        self.db.ensure_channel(name, private=True, created_by="hub", add_owner=False)
        self.db.add_member(name, operator_id, role="member")
        return name

    def _undelegated_operator_warning(self, agent: AgentInfo,
                                      message: Message) -> None:
        """RETIRED BY OPERATOR RULING (2026-08-22). Kept as the funnel so the
        call sites stay honest and the warning cannot creep back.

        The hub used to tell an operator that an unaddressed message "creates
        NO obligation for anyone". The operator's ruling: *"a message on a
        channel with no @seat is totally ok, it just means it's addressed to
        all — then each seat decides if they can contribute or not."*

        The warning was also outlived by the wake fix. It was written when a
        room-wide open reached nobody, so an unaddressed request really could
        evaporate (the 2026-08-01 failure). Since `qualifies` was repaired, an
        open naming nobody wakes every member of the channel — the room does
        hear it, and what it obliges is a READ, not an answer from a
        particular seat. Warning the human that nothing will happen, when the
        whole room was just woken, taught the opposite of what is true."""
        return

    def _post_operator_dm(self, operator_id: str, body: str,
                          dedupe_key: str | None = None) -> None:
        """DISABLED BY OPERATOR RULING (2026-08-22). Kept as the one funnel so
        callers need no change and the rule cannot be reintroduced piecemeal.

        The hub used to mirror its alerts into the operator's DM room. Two
        things were wrong with that, and the operator named both:

        1. A DM room is a CONVERSATION. A human opens one to ask a question
           and expects the answers there; hub notices arriving in it displace
           the thread they are holding — and until now nothing could remove
           them, because a system row has no author who can retract it.
        2. The notices speak to agents. They name MCP tools and store keys a
           human at a terminal has no way to call, so the hub was issuing
           instructions to a reader who cannot follow them.

        `hub-alerts` remains the operator's channel and every alert still
        lands there in full. Nothing is lost except the duplicate in a room
        that belongs to a conversation."""
        return

    def _fleet_liveness_sweep(self) -> list[str]:
        """0110: one FLEET DARK alert when aggregate reception collapses;
        FLEET RECOVERED when broad life returns. Addressed to operators."""
        ops = sorted(self.operator_ids())
        if not ops:
            return []
        # Debt hygiene first, and unconditionally: the bound on standing FLEET
        # rows must not depend on reaching a branch this hub's roster makes
        # unreachable (see `_bound_standing_fleet_alerts`).
        self._bound_standing_fleet_alerts()
        eligible = self._fleet_eligible_agents()
        if len(eligible) < FLEET_MIN_ELIGIBLE:
            return []
        live = [a for a in eligible if self._fleet_seat_live(a)]
        now = time.time()
        fraction = len(live) / len(eligible)
        collapsed = len(live) == 0 or fraction < FLEET_LIVE_FRACTION
        if collapsed:
            if self._fleet_dark_since is None:
                self._fleet_dark_since = now
            if now - self._fleet_dark_since < FLEET_DARK_CONFIRM_SECONDS:
                return []
            if self._fleet_dark_alerted:
                return []
            self._fleet_dark_alerted = True
            claims = self._fleet_open_claims_count()
            body = (
                f"FLEET DARK: {len(live)}/{len(eligible)} seats live "
                f"(<{FLEET_LIVE_FRACTION:.0%} or zero armed/recent activity "
                f"for {int(now - self._fleet_dark_since)}s). "
                f"{claims} open claim(s) on the board. The room went quiet "
                f"— per-seat DARK/DEAF only fires on individual SLA debts. "
                f"Only the operator can restart seats. One alert per episode.")
            self._ensure_alerts_channel()
            # Explicit since `to` no longer decides status. This one IS a
            # debt — only the operator can restart seats — and it has a
            # closer (`_close_standing_fleet_alerts`), which is the whole
            # licence for an `open` the hub authors. Its RECOVERED twin below
            # is deliberately `fyi` for the opposite reason.
            self._post_system(self.DARK_ALERTS_CHANNEL, body, to=ops,
                              status=Status.open,
                              data={"fleet_alert": "dark"})
            for op in ops:
                self._post_operator_dm(op, body)
            return ["fleet-dark"]
        if self._fleet_dark_alerted:
            self._fleet_dark_alerted = False
            self._fleet_dark_since = None
            body = (
                f"FLEET RECOVERED: {len(live)}/{len(eligible)} seats live again.")
            self._ensure_alerts_channel()
            # News, not a request: `fyi` creates no obligation. Posted as an
            # addressed OPEN it minted a permanent operator debt for the good
            # news itself (live: 20 unresolved FLEET RECOVERED rows), and the
            # FLEET DARK it recovers from was never closed either (34 more).
            self._post_system(self.DARK_ALERTS_CHANNEL, body, to=ops,
                              status="fyi")
            self._close_standing_fleet_alerts(body)
            for op in ops:
                self._post_operator_dm(op, body)
            return ["fleet-recovered"]
        self._fleet_dark_since = None
        # Healthy fleet, no in-process episode to recover from: still close
        # standing FLEET rows a previous hub process left open (see
        # `_close_standing_fleet_alerts` — restart-safety, not cosmetics).
        self._close_standing_fleet_alerts(
            f"fleet liveness recovered ({len(live)}/{len(eligible)} seats "
            "live); this alert's episode is over and nothing is owed on it.")
        return []

    def _reporting_delegates(self, channel: str | None = None) -> list[str]:
        """Seats the hub may hand stewardship chores to.

        SCOPE IS NOT DECORATION (2026-08-06). A grant carries a `scope`
        column, the CLI prints it, and until now exactly one call site read
        it. So a delegate scoped to ONE room was silently conscripted into
        fleet-wide hygiene: `rt2-lead`, scoped to `rtype-open`, spent its
        last four work chunks and every subsequent wake on stale-claim
        canvassing for unrelated seats, and posted 15 housekeeping messages
        against 5 on the operator's actual commission. The chore firehose
        the hub itself generated is what starved the request the hub exists
        to serve.

        `channel=None` keeps the fleet-wide list (surfaces that report ON
        delegates rather than assign work TO them). Pass a channel and only
        delegates whose grant reaches it are eligible for its chores."""
        out: set[str] = set()
        for d in self.active_delegations():
            if "reporting" not in d.get("powers", ()):
                continue
            scope = str(d.get("scope") or "").strip()
            if channel is not None and scope and scope != "*" and scope != channel:
                continue
            out.add(d["agent_id"])
        return sorted(out)

    def _escalation_rewake_band_index(self, age_seconds: float,
                                      sla_seconds: float) -> int:
        """0106 band index: -1 = not breached; 0/1/2 at 1×/2×/4× SLA multiples."""
        if sla_seconds <= 0 or age_seconds <= sla_seconds:
            return -1
        ratio = age_seconds / sla_seconds
        if ratio <= ESCALATION_REWAKE_BANDS[1]:
            return 0
        if ratio <= ESCALATION_REWAKE_BANDS[2]:
            return 1
        return 2

    def _escalation_rewake_suppressed(self, agent_id: str) -> bool:
        """0107 bounds 0106: DARK/DEAF episodes own unreachable seats."""
        return agent_id in self._dark_since or agent_id in self._deaf_since

    def _escalation_rewake_sweep(self) -> list[str]:
        """0106: re-deliver escalated obligations into notify files once per
        SLA band (1×, 2×, 4×) so `--important-only` listeners re-ring when
        the post-time notify line was the only wake and the seat missed it.
        Pause-aware age comes from owed(); dedupe is per (seat, message, band)."""
        if self.notify_sink is None:
            return []
        now = time.time()
        live: set[tuple[str, str]] = set()
        rewoken: list[str] = []
        hub_blocked = {b["agent_id"] for b in self.db.blocks_active(self.HUB_SCOPE)}
        for agent_id in self.db.list_agent_ids():
            if agent_id in hub_blocked:
                continue
            if self._escalation_rewake_suppressed(agent_id):
                continue
            report = self.owed(AgentInfo(id=agent_id, name=agent_id))
            emitted = False
            for row in report.to_answer:
                if not row.escalated:
                    continue
                sla_s = self.channel_sla(row.channel) * 60.0
                if row.pending_asks or row.asks_naming_you:
                    born = row.created_at
                else:
                    born = max(row.created_at, self._directive_epoch)
                age_s = now - born - self.paused_seconds_since(born)
                band = self._escalation_rewake_band_index(age_s, sla_s)
                if band < 0:
                    continue
                key = (agent_id, row.id)
                live.add(key)
                if band <= self._rewake_band.get(key, -1):
                    continue
                message = self.db.get_message(row.id)
                if message is None:
                    continue
                envelope = self.envelope_for(agent_id, message)
                if not envelope.escalated:
                    continue
                self.notify_sink.deliver(agent_id, envelope)
                self._rewake_band[key] = band
                emitted = True
            if emitted:
                rewoken.append(agent_id)
        for key in list(self._rewake_band):
            if key not in live:
                del self._rewake_band[key]
        return rewoken

    def _dropped_wake_sweep(self) -> list[str]:
        """0106 emit≠process: an armed seat with an UNREAD pre-SLA debt may
        have taken a wake, recorded the owed signature, and aborted before
        read_message — the listener arm gate stays quiet. Re-emit the notify
        line on a bounded interval; a read receipt stops re-rings (0114:
        seen-and-ignored is not a hub problem). Post-SLA debts use the
        escalation re-emit sweep instead."""
        if self.notify_sink is None:
            return []
        now = time.time()
        live: set[tuple[str, str]] = set()
        rewoken: list[str] = []
        hub_blocked = {b["agent_id"] for b in self.db.blocks_active(self.HUB_SCOPE)}
        for agent_id in self.db.list_agent_ids():
            if agent_id in hub_blocked:
                continue
            if self._escalation_rewake_suppressed(agent_id):
                continue
            if self.presence.reception(agent_id)[0] != "armed":
                continue
            report = self.owed(AgentInfo(id=agent_id, name=agent_id))
            emitted = False
            for row in report.to_answer:
                if row.escalated:
                    continue
                if self.db.has_read(row.id, agent_id):
                    continue
                key = (agent_id, row.id)
                live.add(key)
                last = self._dropped_wake_at.get(key, 0.0)
                if now - last < DROPPED_WAKE_REEMIT_SECONDS:
                    continue
                message = self.db.get_message(row.id)
                if message is None:
                    continue
                self.notify_sink.deliver(agent_id,
                                         self.envelope_for(agent_id, message))
                self._dropped_wake_at[key] = now
                emitted = True
            if emitted:
                rewoken.append(agent_id)
        for key in list(self._dropped_wake_at):
            if key not in live:
                del self._dropped_wake_at[key]
        return rewoken
    def _alerted_at(self, kind: str, agent_id: str) -> float:
        """Last time a DARK/DEAF alert fired for this (kind, agent), read
        through the persisted `meta` flap guard (c3436) so a hub restart
        cannot re-fire the whole wave. Cached in-process after first read."""
        key = (kind, agent_id)
        if key not in self._alerted_cache:
            raw = self.db.meta_get(f"alerted:{kind}:{agent_id}")
            self._alerted_cache[key] = float(raw) if raw else 0.0
        return self._alerted_cache[key]

    def _mark_alerted(self, kind: str, agent_id: str, when: float) -> None:
        self._alerted_cache[(kind, agent_id)] = when
        self.db.meta_set(f"alerted:{kind}:{agent_id}", str(when))

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
        # Same widened predicate as AGENT DARK: any escalated row this seat
        # OWNS — an SLA-breached question OR an ignored directive debt (0102).
        overdue = self._escalated_debts(agent_id)
        if not overdue:
            return
        dark_now.add(agent_id)
        if agent_id in self._deaf_since:
            return  # already alerted this deaf episode
        now = time.time()
        self._deaf_since[agent_id] = now
        if now - self._alerted_at("deaf", agent_id) < DARK_REALERT_SECONDS:
            return
        self._mark_alerted("deaf", agent_id, now)
        example = "a private thread"
        ch = self.db.get_channel(overdue[0].channel)
        if ch is not None and not ch.private:
            example = f"{overdue[0].channel}#{overdue[0].seq}"
        self._post_silence_watchdog_alert(
            agent_id,
            f"AGENT DEAF: {agent_id} looks present but its reception loop "
            f"went silent ~{age / 60:.0f} min ago while it holds "
            f"{len(overdue)} SLA-breached obligation(s) (e.g. {example}). "
            "Its listener is almost certainly dead — the seat wakes for "
            "nothing. Re-arm it (restart the reception loop / the session); "
            "escalation cannot reach a deaf seat. One alert per deaf episode.",
            explicit_class="deaf", kind="deaf",
        )
        alerted.append(agent_id)

    def _escalated_debts(self, agent_id: str) -> list[Envelope]:
        """Escalated envelopes this seat ACTUALLY OWES.

        FALSE-POSITIVE CLASS (live, 2026-08-01). The watchdogs filtered the
        seat's inbox on `escalated` alone and called the result "obligations
        this seat is holding". But the inbox shows a member every escalated
        row in their rooms, including ones addressed to somebody else — so
        at-test#363 was cited as rotting debt for SIX seats that never held
        it, two of which got LURK alerts naming a message they did not owe
        and could not discharge. The steward then spent turns canvassing
        seats about other seats' work.

        /owed is the hub's own answer to "does this seat hold this debt", and
        it applies discharge, closure and per-addressee engagement. Intersect
        with it, and a watchdog can only ever name real, ownable debt."""
        info = AgentInfo(id=agent_id, name=agent_id)
        owed_ids = {row.id for row in self.owed(info).to_answer}
        return [e for e in self.inbox(info)
                if e.escalated and e.id in owed_ids]

    def _lurk_sweep_one(self, agent_id: str, lurk_now: set[str],
                        alerted: list[str]) -> None:
        """LURK leg of the watchdog (RC-3, the 2026-07-23 fleet blackout):
        reception ARMED — the listener heartbeats /owed every arm — while the
        model behind it never triages, so addressed obligations rot UNREAD
        far past their SLA. The DEAF leg cannot see this (the pulse it
        measures is the listener's, and the listener is fine); for two days
        the fleet was in exactly this state and every sweep stayed silent.

        Predicate, deliberately conservative: reception 'armed' AND at least
        one escalated unread obligation older than LURK_SLA_MULTIPLE x its
        channel SLA, AND the state persisted LURK_CONFIRM_SECONDS since first
        observed (a seat that just re-armed after a DEAF episode gets one
        full listener-cycle-plus-turn to catch up before being named).
        Escalation alone is the DEAF/DARK bar; lurk waits for WELL past
        breach so a busy-but-alive seat that answers late is never smeared.
        Unread is the discriminator from 'read but ignoring' —
        acked_unanswered already names that on the board.

        THE ALERT STILL FIRES FOR A WORKING SEAT, AND SAYS SO. Waiting longer
        was never enough on its own: a seat can be productive for hours and
        still leave addressed rows unread, and this alert then told the
        operator to relaunch it. The rotting count is real and stays; only
        the CAUSE sentence is now decided by evidence (`last_work_at`), so
        the two situations route to opposite actions instead of one."""
        state, _age = self.presence.reception(agent_id)
        if state != "armed":
            return
        now = time.time()
        rotting = [
            e for e in self._escalated_debts(agent_id)
            if not e.redelivery         # redelivery = the seat DID read it once
            and (now - e.created_at) / 60.0
                >= LURK_SLA_MULTIPLE * self.channel_sla(e.channel)]
        if not rotting:
            return
        lurk_now.add(agent_id)          # lurk's own episode-teardown live-set
        since = self._lurk_since.setdefault(agent_id, now)
        if now - since < LURK_CONFIRM_SECONDS:
            return                      # candidate: give the armed loop its chance
        if agent_id in self._lurk_alerted:
            return                      # already alerted this lurk episode
        self._lurk_alerted.add(agent_id)
        if now - self._alerted_at("lurk", agent_id) < DARK_REALERT_SECONDS:
            return
        self._mark_alerted("lurk", agent_id, now)
        oldest = min(e.created_at for e in rotting)
        example = "a private thread"
        ch = self.db.get_channel(rotting[0].channel)
        if ch is not None and not ch.private:
            example = f"{rotting[0].channel}#{rotting[0].seq}"
        # THE COUNT AND THE CAUSE ARE TWO CLAIMS (2026-08-25, delegate at
        # agora-and-wui#737). The count above is measured. The cause was not:
        # this alert asserted a stuck session from unread-past-SLA alone, and
        # on the one seat it named that was false — it had posted a
        # considered reply eleven minutes earlier. `last_work_at` decides
        # which sentence follows, from work the seat AUTHORED after the
        # oldest rotting row arrived. The docstring above always claimed "a
        # busy-but-alive seat ... is never smeared"; until now nothing in the
        # predicate could tell busy from stuck.
        worked_at = self.db.last_work_at(agent_id, oldest)
        head = (f"AGENT LURKING: {agent_id}'s reception is armed and "
                f"heartbeating, but {len(rotting)} obligation(s) addressed to"
                f" it have rotted UNREAD well past SLA (oldest ~"
                f"{(now - oldest) / 60:.0f} min, e.g. {example}). ")
        if worked_at is not None:
            body = head + (
                f"THE SEAT IS NOT STUCK: it produced work "
                f"{(now - worked_at) / 60:.0f} min ago, with these rows "
                "already waiting — so this is a triage backlog, not a dead "
                "session, and relaunching it would kill a working one. "
                "Drain, re-route or narrow its queue. One alert per lurk "
                "episode.")
            klass = "unseen-but-working"
        else:
            body = head + (
                "The doorbell rings; nobody comes, and the seat has produced "
                "nothing since the oldest of these arrived: its session is "
                "likely stuck in a follow-up-only loop where wakes and stop-"
                "hook prompts no longer reach the model. Reprompt or relaunch"
                " that session. One alert per lurk episode.")
            klass = "unseen"
        self._post_silence_watchdog_alert(
            agent_id, body, explicit_class=klass, kind="lurk")
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
        # SCOPED PER ROOM (2026-08-06). This built ONE fleet-wide steward
        # set and then reported every stale claim in every channel to all of
        # them. A delegate the operator had scoped to a single room was
        # therefore conscripted into hub-wide hygiene: `rt2-lead`, scoped to
        # `rtype-open`, spent its last four work chunks and every subsequent
        # wake canvassing stale claims for unrelated seats, and posted 15
        # housekeeping messages against 5 on the operator's commission. The
        # chore firehose the hub generated is what starved the request the
        # hub exists to serve. A grant's `scope` now decides which rooms a
        # delegate is answerable for.
        if not self._reporting_delegates():
            return []
        now = time.time()
        live: list[str] = []
        live_keys: list[str] = []
        stewards_set: set[str] = set()
        for ch in self.db.channel_names():
            if ch == self.DARK_ALERTS_CHANNEL:
                # The steward's OWN bookkeeping about these alerts lives in
                # this channel (26 claim rows on the live hub). Letting them
                # feed the sweep that writes those alerts is a closed loop:
                # the delegate opens a row to track an alert, the row ages,
                # the sweep reports the row, the delegate opens a row to
                # track THAT alert. Stewardship must never become its own
                # backlog.
                continue
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
                line = f"{shown} (owner {owner}, idle {age / 60:.0f}m)"
                waits = [field for field in ("waiting_for_answers", "waiting_for_artifacts")
                         if stored.value.get(field)]
                if waits:
                    # Names only: dependency references may belong to a
                    # private room that an alert reader cannot access.
                    line += "; declared waits: " + ", ".join(waits)
                live.append(line)
                stewards_set.update(self._reporting_delegates(ch))
        sig = hashlib.sha256("\n".join(sorted(live_keys)).encode()).hexdigest()[:16]
        standing = self._standing_steward_alerts()
        # Closing an episode comes FIRST and unconditionally: an alert the
        # hub raised is the hub's to retire, whatever the scoping says now.
        if not live:
            for old in standing:
                self._post_system(
                    self.DARK_ALERTS_CHANNEL,
                    "stale-claims episode closed: every flagged claim was "
                    "touched, finished, or aged back under its SLA.",
                    status="resolved", reply_to=old.id)
            return ["stale-claims:cleared"] if standing else []
        stewards = sorted(stewards_set)
        if not stewards:
            # Stale work only in rooms no delegate is answerable for.
            # Reporting it to a delegate scoped elsewhere is the exact
            # conscription this fix exists to stop; the operator still sees
            # it in `agora doctor`.
            return []
        # SHRINK IS NOT NEWS (2026-08-01 live regression). A standing alert
        # that already NAMES every currently-stale claim remains the whole
        # truth about this episode even when the set has shrunk, so a subset
        # must post nothing. Re-alerting on shrink is what made the janitor
        # never idle: the steward chased `at-test/claim:msg-382`, its owner
        # marked the row done (18:04:20), the set lost that one key — and the
        # shrink ALONE superseded the alert and minted a fresh open
        # obligation about the residue (3 commons claims whose owners had
        # been offline four days and which no canvass can ever clear). Every
        # transient claim therefore cost the steward TWO alerts, one to start
        # the chase and one for finishing it, and the residue guaranteed the
        # set was never empty: 28 alerts in 24h, one per ~5 minutes, all
        # hygiene, none of it the operator's actual work. Only genuinely NEW
        # stale work — a key the standing alert never named — earns a new
        # alert. `steward_keys` is read from the message the same way the
        # standing alert itself is (channel, not memory), so a hub restart
        # cannot resurrect the loop; alerts written before this fix carry no
        # key list and fall back to exact-signature matching, i.e. the old
        # behavior, never a lost debt.
        live_set = set(live_keys)
        if standing and any(
                isinstance(m.data, dict)
                and (m.data.get("steward_sig") == sig
                     or (isinstance(m.data.get("steward_keys"), list)
                         and live_set <= set(m.data["steward_keys"])))
                for m in standing):
            return []  # the standing alert already states at least this debt
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
              "per seat, or reassign via the queue. Inspect the effective "
              "claim dependencies before calling work unblocked; artifact "
              "waits observe VFS revisions, not workspace files. Updating a "
              "checkpoint clears this age alert, not a dependency or the "
              "unfinished work. The hub closes this "
              "alert itself when the set changes or empties.",
            # Explicit since the to/status coupling was removed: this one IS
            # a debt, and the hub closes it itself (see the resolved closer
            # above) — which is what makes an `open` legitimate here.
            status=Status.open,
            to=stewards,
            data={"steward_sig": sig, "steward_keys": sorted(live_keys)})
        return [f"stale-claims:{len(live)}"]

    def _standing_steward_alerts(self) -> list[Message]:
        """Every hub-authored stale-claims alert still standing open (no
        authoritative close). Read from the channel, not memory, so a hub
        restart cannot orphan an open alert."""
        return self._standing_hub_alerts("STALE CLAIMS")

    def _standing_hub_alerts(self, marker: str) -> list[Message]:
        """Hub-authored alerts of one family still standing open — the
        channel-not-memory half of the bounded-debt contract, shared by the
        stale-claims and stalled-phase sweeps."""
        if self.db.get_channel(self.DARK_ALERTS_CHANNEL) is None:
            return []
        out: list[Message] = []
        ops = self.operator_ids()
        for m in self.db.open_obligations([self.DARK_ALERTS_CHANNEL]):
            if m.sender != "hub" or not m.body.startswith(marker):
                continue
            if self._closed_authoritatively(m, self.db.replies_to(m.id)):
                continue
            out.append(m)
        return out

    _TERMINAL_PHASE_STATUSES = frozenset(
        {"complete", "completed", "done", "closed", "shipped", "cancelled",
         "canceled", "abandoned"})

    def _phase_sweep(self) -> list[str]:
        """The phase half of the stewardship watchdog (2026-08-04). An open
        `phase:` row whose steward has gone quiet past the channel SLA is a
        COMMISSION stalling, not a claim idling — and it was invisible to
        every sweep: `_steward_sweep` filters to `claim:` keys, and the
        blocked claims that usually accompany a stalled phase are exempt as
        parked. Measured cost of the gap: the novel fleet stood armed and
        silent for 17.5 hours behind `scifi-novel/phase:novel` (open,
        steward book-assistant) while the steward's blocking question to the
        operator sat unread at index 87 of 144 unsorted owed rows.

        The alert names the freshest ESCALATED ask the steward itself has
        standing on an OPERATOR — the thing actually blocking the phase —
        and prescribes a decision, never a relaunch: when the silent seat is
        the operator, "restart it" is not an action anyone in the room can
        take. Bounded-debt contract identical to `_steward_sweep`: one
        standing alert; supersede on a genuinely new phase key; close when
        the set empties. Touching the phase row (any version bump) is the
        receipt that clears it."""
        now = time.time()
        live: list[str] = []
        live_keys: list[str] = []
        # SCOPED: a delegate stewards only the rooms its grant reaches. A
        # fleet-wide conscription is what buried the operator's commission
        # under 15 housekeeping posts (2026-08-06).
        recipients: set[str] = set()
        operators = set(self.operator_ids())
        # _ensure_alerts_channel enrolls operators/reporting delegates only.
        # A phase steward is often an ordinary worker: naming it in this
        # private room does not grant access or deliver a notification.
        alert_readers = operators | set(self._reporting_delegates())
        alert_readers.update(m.agent_id for m in
                             self.db.list_members(self.DARK_ALERTS_CHANNEL))
        op_owed: dict[str, Any] = {}
        for ch in self.db.channel_names():
            if ch == self.DARK_ALERTS_CHANNEL:
                continue
            sla_s = self.channel_sla(ch) * 60.0
            for entry in self.db.store_keys(ch):
                key = entry["key"]
                if not key.startswith("phase:"):
                    continue
                stored = self.db.store_get(ch, key)
                if stored is None or not isinstance(stored.value, dict):
                    continue
                if (self._claim_status_word(stored.value)
                        in self._TERMINAL_PHASE_STATUSES):
                    continue
                age = now - stored.updated_at
                if age <= sla_s:
                    continue
                steward = str(stored.value.get("steward") or "").strip()
                live_keys.append(f"{ch}/{key}")
                info = self.db.get_channel(ch)
                shown = (f"{ch}/{key}" if info is not None and not info.private
                         else "a private-channel phase")
                line = (f"{shown} '{stored.value.get('current', '?')}' open, "
                        f"untouched {age / 3600:.1f}h"
                        + (f", steward {steward}" if steward else ""))
                if steward in alert_readers:
                    recipients.add(steward)
                else:
                    recipients.update(self._reporting_delegates(ch) or operators)
                if steward:
                    blocking = self._steward_blocking_ask(steward, op_owed)
                    if blocking is not None:
                        line += f"; blocking ask: {blocking}"
                live.append(line)
        sig = hashlib.sha256("\n".join(sorted(live_keys)).encode()).hexdigest()[:16]
        standing = self._standing_hub_alerts("PHASE STALLED")
        if not live:
            for old in standing:
                self._post_system(
                    self.DARK_ALERTS_CHANNEL,
                    "stalled-phase episode closed: every flagged phase was "
                    "touched, completed, or aged back under its SLA.",
                    status="resolved", reply_to=old.id)
            return ["stalled-phase:cleared"] if standing else []
        live_set = set(live_keys)
        if not recipients:
            # No authorized supervisor exists; leave the phase pending
            # rather than inventing access to private operator alerts.
            return []
        self._ensure_alerts_channel()
        alert_readers = {m.agent_id for m in
                         self.db.list_members(self.DARK_ALERTS_CHANNEL)}
        if standing and any(
                isinstance(m.data, dict)
                and (m.data.get("phase_sig") == sig
                     or (isinstance(m.data.get("phase_keys"), list)
                         and live_set <= set(m.data["phase_keys"])))
                and recipients <= set(m.to)
                and set(m.to) <= alert_readers
                for m in standing):
            return []
        for old in standing:
            self._post_system(
                self.DARK_ALERTS_CHANNEL,
                "superseded by the next stalled-phase alert (the live set "
                "or reachable recipients changed); this episode is closed.",
                status="resolved", reply_to=old.id)
        self._post_system(
            self.DARK_ALERTS_CHANNEL,
            "PHASE STALLED: " + "; ".join(live[:6])
            + (f" (+{len(live) - 6} more)" if len(live) > 6 else "")
            + ". A phase only closes when its steward acts. If the phase "
              "waits on an unreachable operator: decide a recorded fallback "
              "or park the wait EXPLICITLY on the phase row (note + next) — "
              "never leave the wait implicit. Touching the phase row is the "
              "receipt that clears this; the hub closes this alert itself "
              "when the set changes or empties.",
            to=sorted(recipients), status=Status.open,
            data={"phase_sig": sig, "phase_keys": sorted(live_keys)})
        return [f"stalled-phase:{len(live)}"]

    def _steward_blocking_ask(self, steward: str,
                              cache: dict[str, Any]) -> str | None:
        """The freshest ESCALATED to_answer row an operator holds whose
        sender is `steward` — the concrete question the stalled phase is
        actually waiting on, so the alert can point AT it instead of
        gesturing at silence. Private channels are named only as 'a private
        thread with <operator>': the steward knows which one, and the alert
        leaks no membership it should not."""
        best = None
        for op in self.operator_ids():
            if op not in cache:
                cache[op] = self.owed(AgentInfo(id=op, name=op))
            for row in cache[op].to_answer:
                if row.sender != steward or not row.escalated:
                    continue
                if best is None or row.created_at > best[0]:
                    info = self.db.get_channel(row.channel)
                    shown = (f"{row.channel}#{row.seq} (to {op})"
                             if info is not None and not info.private
                             else f"a private thread with {op}")
                    age_h = (time.time() - row.created_at) / 3600.0
                    best = (row.created_at,
                            f"{shown}, unanswered {age_h:.1f}h")
        return best[1] if best else None

    # -- claim-due pings: owner-declared continuation (2026-07-28) -----------
    #
    # DOCTRINE (the line four adversarial reviews settled): the hub may
    # SURFACE obligations; it may never AUTHOR work. A claim row that
    # declares `cadence_minutes: N` is its owner declaring a self-authored
    # debt ("remind ME when this idles past N"); the hub merely surfaces it.
    # A row WITHOUT cadence never pings anyone — no default-on, ever: a
    # hub that nudges undeclared work is a scheduler in disguise, and the
    # measured field failure (Jul-20 canvass hour) shows manufactured
    # attention buys bookkeeping, not work.
    #
    # SHAPE: a stored, addressed, open SYSTEM message in the claim's own
    # channel — deliberately the ONLY shape that reaches every reception
    # path with zero client code (owed ledger + to-me notify flag + ws
    # envelope + stop-hook sig + inbox pin; a new owed list, notify flag,
    # or ws frame type would leave old listeners silently deaf — the
    # cross-framework review's rollout proof). Standing-ping discipline is
    # the steward sweep's 0093 contract verbatim: at most ONE per
    # (channel, owner); same due-set + band posts nothing; a changed set
    # supersedes; an empty set closes; restart-safe because standing pings
    # are FOUND in the channel, never remembered in memory.

    CLAIM_DUE_MIN_CADENCE_MINUTES = 30.0    # floor: clamps peer/typo cadence-1 spam
    CLAIM_DUE_MAX_BANDS = 3                 # dormant after 3 unproductive day-bands
    #                                         (the standing ping stays open and
    #                                         escalates; only REPOSTS stop —
    #                                         the hub never forges parked/done)
    CLAIM_DUE_BAND_SECONDS = 86400.0        # one repost band per untouched day

    @staticmethod
    def _claim_cadence_seconds(value: dict[str, Any]) -> float | None:
        """The owner-declared cadence in seconds, or None (no pings). 0 and
        negatives read as 'declared off'; junk reads as absent. Clamped to
        the floor so a hostile/typo cadence cannot storm (store writes are
        member-visible and attributed, but cheap to spam otherwise)."""
        raw = value.get("cadence_minutes")
        if raw is None:
            return None
        try:
            minutes = float(raw)
        except (TypeError, ValueError):
            return None
        if minutes <= 0:
            return None
        return max(minutes, HubService.CLAIM_DUE_MIN_CADENCE_MINUTES) * 60.0

    #: The closed vocabulary a park must tag itself with. Closed so the
    #: delegate's blocker board can GROUP — "three seats waiting on me, one
    #: on a build, one on a decision nobody has taken" is a picture; free
    #: text is a pile. Each names WHO or WHAT can end the wait.
    PARK_BLOCKERS = {
        "operator": "waiting on the human owner to decide or approve",
        "delegate": "waiting on the delegate to rule, dispatch, or unblock",
        "seat": "waiting on another seat's work or answer",
        "decision": "waiting on a decision the room has not taken",
        "external": "waiting on something outside the hub (a build, a "
                    "service, a rate limit, a file that does not exist yet)",
        # ADDED 2026-08-25, ruled by the delegate at operator-board#14 after
        # FOUR instances in one night, from three seats, of the same defect:
        # the vocabulary had no word for a real state, so honest seats either
        # mis-tagged or left the row unparked, and the record misreported
        # either way.
        #
        # `owner` is the MODAL case and it was the missing one. `agora` hit it
        # inside thirty seconds of the stale-row canvass; `tui` disclosed that
        # FOUR of their five parks read `blocked_on: operator` while nobody
        # owed them anything — so a sweep showed four rows apparently waiting
        # on laurent when exactly one was. A closed vocabulary that cannot
        # express "I chose other work" does not prevent that state; it only
        # stops it being said truthfully.
        "owner": "not blocked — its owner ranked other work above it; only "
                 "they can raise it",
        # `row` completes the `waiting_on` edge, which already exists and is
        # complete. The mechanism was never the gap; the word that leads a
        # seat to it was.
        "row": "waiting on another claim row to finish (name it in "
               "`waiting_on`)",
    }

    def _validate_park(self, key: str, value: dict[str, Any],
                       channel: str | None = None,
                       agent: AgentInfo | None = None) -> None:
        """A park must carry a TAG and an ASK, or it is refused.

        The hub checks that the two fields are present and well-shaped. It
        never reads what the seat needs, never guesses who should supply it,
        and never writes either field itself. Refusing names both fields and
        the whole vocabulary, so the fix is copy-pasteable at the moment the
        seat is stuck — which is the moment it has least attention to spare.
        """
        tag = str(value.get("blocked_on") or "").strip().lower()
        needs = str(value.get("needs") or "").strip()
        vocab = ", ".join(sorted(self.PARK_BLOCKERS))
        # "WAITING ON A SEAT" MUST NAME THE SEAT (2026-08-07).
        #
        # Measured in `rtype-g4`: two rows parked `blocked_on: seat` with
        # `needs` reading "g4-engine must add an auditable same-run capture
        # path". The hub therefore KNEW who was blocking whom — it was
        # written in a structured field it had validated — and told nobody.
        # `g4-engine` sat armed and idle in the same room while two rows
        # named it. `/blockers` showed it perfectly, to whoever thought to
        # pull it, which was no one.
        #
        # This is the 2026-08-01 ruling in another costume: an ask addressed
        # to nobody obliges nobody. A block that names its unblocker only in
        # prose is the same thing. `needs_from` is that name as data, so the
        # hub can deliver it instead of storing it.
        if tag == "seat" and not str(value.get("needs_from") or "").strip():
            raise HubError(400,
                f"`{key}` says it is blocked on a SEAT but does not name "
                "which one, so the hub cannot tell them. Add:\n"
                '  "needs_from": "<the seat that can unblock you>"\n'
                "An ask addressed to nobody obliges nobody; a block naming "
                "nobody is the same thing. If you do not know who can "
                "unblock you, that is a `decision` or a `delegate` block, "
                "not a `seat` one.")
        # "WAITING ON A ROW" MUST NAME THE ROW — the same rule as `seat`, for
        # the same reason. `waiting_on` is validated, refuses a phantom target
        # and already drives the board's `next` column; a `row` park without
        # it is a block naming nothing, which is the shape the `seat` clause
        # above was written against.
        if tag == "row" and not value.get("waiting_on"):
            raise HubError(400,
                f"`{key}` says it is blocked on another ROW but does not name "
                "which one, so nothing can tell you when it clears. Add:\n"
                '  "waiting_on": {"channel": "<channel>", "key": "<claim:...>"}\n'
                "The hub checks the target exists and surfaces your row the "
                "moment it finishes. If you cannot name the row, that is a "
                "`decision` or a `seat` block, not a `row` one.")
        who = str(value.get("needs_from") or "").strip()
        if who and channel is not None:
            members = {m.agent_id for m in self.db.list_members(channel)}
            if who not in members:
                raise HubError(400,
                    f"needs_from '{elide(who, 40)}' is not a member of "
                    f"'{channel}' — the hub can only ring someone who is in "
                    "the room. Invite them, or name someone who is here.")
            if agent is not None and who == agent.id:
                raise HubError(400, "a seat cannot be blocked on itself — "
                                    "that is work, not a block")
        if not tag or not needs:
            raise HubError(400,
                f"'{self._claim_status_word(value)}' parks `{key}`, and a "
                "park that does not say what it needs is invisible: no sweep "
                "reads it, your own driver treats it as finished, and the "
                "room never hears it. Add both:\n"
                f'  "blocked_on": "<one of: {vocab}>"\n'
                '  "needs": "<what would let you continue, in one sentence>"\n'
                "Also say it in the room — a store write rings nobody.")
        if tag not in self.PARK_BLOCKERS:
            raise HubError(400,
                f"blocked_on '{elide(tag, 40)}' is not a blocker kind. Use "
                f"one of: {vocab}. The vocabulary is closed so the delegate's "
                "blocker board can group what the room is waiting on.")

    def _validate_waiting_on(self, channel: str, key: str,
                             waiter: str,
                             raw: Any) -> dict[str, Any]:
        """A claim's declared dependency: `{channel?, key}` -> stamped row.

        The hub does exactly two things with it, and refuses to do a third.
        It REFUSES a target that does not exist — a wait on a phantom row is
        a silent forever-park, and this fleet wrote one on its first outing.
        And it stamps the target's version now, so "has it moved?" is a fact
        rather than a judgement. What to do when it moves stays the seat's
        call; the hub surfaces, it never authors.
        """
        if not isinstance(raw, dict):
            raise HubError(400, "claim waiting_on must be an object naming "
                                "the row to wait on, e.g. "
                                '{"channel": "room", "key": "claim:build"}')
        target_ch = str(raw.get("channel") or channel)
        target_key = str(raw.get("key") or "")
        if not target_key:
            raise HubError(400, "claim waiting_on needs a `key` — the row "
                                "whose change should resume this work")
        if target_ch == channel and target_key == key:
            raise HubError(400, "a claim cannot wait on itself — that is a "
                                "park with no exit")
        target = self.db.store_get(target_ch, target_key)
        if target is None:
            raise HubError(404,
                           f"waiting_on names '{target_ch}/{target_key}', "
                           "which does not exist. Waiting on a row nobody "
                           "will ever write is a permanent park the hub "
                           "cannot tell from finished work — name a row that "
                           "exists, or say what you are blocked on in a "
                           "message to the seat who can act.")
        if not self.db.is_member(target_ch, waiter):
            raise HubError(400,
                           f"waiting_on names '{target_ch}/{target_key}', "
                           f"but '{waiter}' is not a member of '{target_ch}'. "
                           "A parked claim may wait only on a row its waking "
                           "seat can read — otherwise the resume ping would "
                           "leak hidden room/key data into another room.")
        return {"channel": target_ch, "key": target_key,
                "at_version": target.version}

    def _validate_waiting_for_artifacts(self, channel: str, owner: str, raw: Any) -> list[dict[str, Any]]:
        """Exact all-of thresholds, including a future file's first version.

        A file may not exist yet; the readable room and valid path must exist
        as a namespace. Waiting for a revision requires observed_version + 1,
        not an automatically restamped head that can race an arrival.
        """
        from ..artifact_wait import MAX_ARTIFACT_WAITS
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ARTIFACT_WAITS:
            raise HubError(400, f"waiting_for_artifacts must contain 1..{MAX_ARTIFACT_WAITS} exact artifact requirements")
        result = []
        seen = set()
        for item in raw:
            if not isinstance(item, dict) or set(item) - {"channel", "path", "min_version"}:
                raise HubError(400, "artifact wait entries are {channel?, path, min_version}")
            target = item.get("channel", channel)
            if not isinstance(target, str) or not target or not self.db.is_member(target, owner):
                raise HubError(403, "artifact wait must name a channel readable by the claim owner")
            if not isinstance(item.get("path"), str):
                raise HubError(400, "artifact wait needs an exact VFS path")
            path = self._normalize_fs_path(item["path"])
            version = item.get("min_version")
            if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= 2**62:
                raise HubError(400, "artifact min_version must be a positive integer")
            ref = (target, path)
            if ref in seen:
                raise HubError(400, "duplicate artifact wait path")
            seen.add(ref)
            result.append({"channel": target, "path": path, "min_version": version})
        return sorted(result, key=lambda row: (row["channel"], row["path"]))

    def _waiting_on_sweep(self) -> list[str]:
        """Ring the owner of a parked claim whose declared dependency moved.

        THE INCIDENT (2026-08-06). `rt2-lead` parked on
        `claim:m1-engine-boot-manifest` in another room. That row went to
        `done` 3m43s later carrying exactly the evidence it was waiting for.
        Nothing rang: a store write wakes nobody, and `parked` removed the
        row from every remaining scan. Seven seats sat armed for hours on a
        milestone that was finished.

        The alert states hub-owned facts only — which row moved, from which
        version, and its own status word. Deciding what that means is the
        waiting seat's job.
        """
        if self.hub_paused() is not None:
            return []
        fired: list[str] = []
        for ch in self.db.channel_names():
            members = {m.agent_id for m in self.db.list_members(ch)}
            for entry in self.db.store_keys(ch):
                key = entry["key"]
                if not key.startswith("claim:"):
                    continue
                stored = self.db.store_get(ch, key)
                if stored is None or not isinstance(stored.value, dict):
                    continue
                value = stored.value
                dep = value.get("waiting_on")
                if not isinstance(dep, dict) or self._claim_done(value):
                    continue
                owner = str(value.get("owner") or stored.updated_by or "")
                if not owner or owner not in members:
                    continue
                target_ch = str(dep.get("channel") or ch)
                target_key = str(dep.get("key") or "")
                if not self.db.is_member(target_ch, owner):
                    body = (f"`{key}` is waiting on a row this room can no "
                            "longer read. Nothing here can verify whether it "
                            "moved. Re-point it to a readable row, rejoin "
                            "that room, or take the work off park.")
                    target = None
                    dedupe_suffix = "unreadable"
                else:
                    target = self.db.store_get(target_ch, target_key)
                    dedupe_suffix = ("gone" if target is None else
                                     str(target.version))
                # A vanished target is SAID, never swallowed: the seat is
                # waiting on something that no longer exists and would
                # otherwise park forever in silence.
                if target is None:
                    if dedupe_suffix == "unreadable":
                        pass
                    else:
                        body = (f"`{key}` is waiting on "
                                f"`{target_ch}/{target_key}`, which "
                                "no longer exists. Nothing will resume this row: "
                                "re-point it, or take the work off park.")
                elif target.version > int(dep.get("at_version") or 0):
                    word = self._claim_status_word(target.value) if isinstance(
                        target.value, dict) else ""
                    body = (f"`{target_ch}/{target_key}` moved "
                            f"(v{dep.get('at_version')} -> v{target.version}"
                            f"{', status ' + word if word else ''}). "
                            f"`{key}` declared it would resume on that change. "
                            "Re-read both rows and decide.")
                else:
                    continue
                # Dedupe on the target VERSION: one alert per real change.
                # A repeat is REFUSED by the ledger, which is the guarantee
                # working — swallow that one refusal and report nothing
                # fired, rather than letting it abort the whole sweep.
                try:
                    self._post_system(
                        ch, body, to=[owner], status=Status.open,
                        dedupe_key=f"waiting-on:{ch}:{key}:"
                                   f"{dedupe_suffix}")
                except DuplicateMessage:
                    continue
                fired.append(f"waiting-on:{ch}/{key}")
        return fired

    def _blocker_already_engaged(self, channel: str, value: dict[str, Any],
                                 who: str) -> bool:
        """Has the seat a park names already engaged that row's thread?

        True when `who` is an operator (a human is never rung by a timer), or
        when `who` posted into the thread the row cites (`source` /
        `source_message_id`, as `channel#seq` or an id) — or anywhere in the
        channel — within the room's SLA. Facts only, no prose read."""
        if who in self.operator_ids():
            return True
        window = self.channel_sla(channel) * 60.0
        since = time.time() - window
        src = str(value.get("source") or value.get("source_message_id") or "")
        parent = None
        if src:
            if "#" in src:
                ch, _, seq = src.rpartition("#")
                if seq.isdigit():
                    parent = self.db.get_message_by_seq(ch or channel, int(seq))
            else:
                parent = self.db.get_message(src)
        if parent is not None:
            for r in self.db.replies_to(parent.id):
                if r.sender == who and r.created_at >= since:
                    return True
        for m in reversed(self.db.get_messages(channel, limit=100)):
            if m.created_at < since:
                break
            if m.sender == who and m.kind == Kind.message:
                return True
        return False

    def _blocking_sweep(self) -> list[str]:
        """Tell every seat a live block NAMES, whether or not it was told
        when the block was written.

        WHY A SWEEP AND NOT JUST THE WRITE HOOK (2026-08-07). `store_set`
        rings the named seat the moment a block declares it. That is the
        fast path and it is not enough: it delivers only to seats that
        existed, in rooms that existed, at the instant of the write. Two
        rows in `rtype-g4` named `g4-engine` as their blocker before the
        hook shipped, and the room sat dead for twenty minutes afterwards
        because nothing re-reads state — the fix could not reach the rows
        that needed it.

        That is the general defect behind four separate incidents: a
        coordination fact delivered only at write time is lost for every row
        already written and for every seat that was down when it happened.
        State the hub can derive must be deliverable from the state itself.

        The dedupe key is the block's content, shared with the write hook,
        so a seat already told is never told twice.
        """
        if self.hub_paused() is not None:
            return []
        fired: list[str] = []
        for ch in self.db.channel_names():
            members = {m.agent_id for m in self.db.list_members(ch)}
            for entry in self.db.store_keys(ch):
                key = entry["key"]
                if not key.startswith("claim:"):
                    continue
                stored = self.db.store_get(ch, key)
                if stored is None or not isinstance(stored.value, dict):
                    continue
                v = stored.value
                who = str(v.get("needs_from") or "").strip()
                owner = str(v.get("owner") or stored.updated_by or "")
                needs_txt = str(v.get("needs") or "").strip()
                needs_seat = (
                    self._claim_parked(v)
                    and str(v.get("blocked_on") or "").strip().lower() == "seat")
                reachable_blocker = (who in members and who != owner
                                     and who not in self.operator_ids())
                self._close_repaired_undeliverable_block_alerts(
                    ch, key, needs_seat and not reachable_blocker)
                if self._claim_done(v) or not self._claim_parked(v):
                    continue
                # THE HUB SAYS WHAT IT CANNOT DO (2026-08-07).
                #
                # A row that declares `blocked_on: seat` and does not name
                # the seat is undeliverable: the name exists only in the
                # `needs` prose, and reading prose to guess who is meant is
                # the mind-reading gate the operator principle forbids.
                #
                # Doing nothing is not the alternative. Measured: two rows
                # in `rtype-g4` said `blocked_on: seat` with `needs_from`
                # unset — written before the field existed — and read
                # "g4-engine must add an auditable same-run capture path".
                # The hub could see the block, could not address it, and
                # sat silent for thirty minutes while the room died.
                #
                # Silent inability is the same class as a silent limit: the
                # hub declining to do a thing and not saying so. So it tells
                # the one party who DOES know — the owner — that its block
                # is not reaching anyone.
                if needs_seat and not who:
                    episode = self._undeliverable_block_alert_episode(ch, key)
                    if episode is None:
                        continue
                    try:
                        self._post_system(
                            ch,
                            f"UNDELIVERABLE BLOCK on `{key}`: it says it is "
                            "waiting on a SEAT but does not name which one, "
                            "so nobody has been told and nobody is coming. "
                            + (f"You wrote: \"{elide(needs_txt, 200)}\". "
                               if needs_txt else "")
                            + 'Add "needs_from": "<seat>" to the row and the '
                              "hub will tell them, or take the row off block.",
                            to=[owner], status=Status.open,
                            dedupe_key="undeliverable:" + hashlib.sha256(
                                f"{ch}\0{key}\0{needs_txt}\0{episode}".encode()
                            ).hexdigest()[:24],
                            data={"undeliverable_block": {"key": key}})
                    except DuplicateMessage:
                        continue
                    fired.append(f"undeliverable-block:{ch}/{key}")
                    continue
                if not who or who not in members or who == owner:
                    continue
                if who in self.operator_ids():
                    # A human is never told "YOU ARE THE BLOCKER" by a timer
                    # (commons#9737/#10056 in the production record). Their
                    # block shows on the desk and in supervise() instead.
                    continue
                sla_s = self.channel_sla(ch) * 60.0
                if time.time() - float(stored.updated_at or 0.0) < sla_s:
                    # The write hook already rang (or deliberately did not,
                    # because the seat was engaged); the sweep speaks only
                    # for a block that has outlived the room's SLA. The
                    # RETURN PATH is not aged: a blocker who answered the
                    # ring is reported to the owner at the next sweep.
                    self._ring_back_if_blocker_answered(ch, key, owner, who)
                    continue
                needs = needs_txt
                try:
                    self._post_system(
                        ch,
                        # Same shape as the store-write ring above, and the
                        # same reason (commons#391): addressee first, owner
                        # explicitly labelled, marker phrase kept so
                        # `_report_blocker_answered` still finds it.
                        f"{who}: YOU ARE THE BLOCKER on `{key}` "
                        f"(owner: {owner}) — "
                        + attributed_quote(needs or "unblock it", owner)
                        + "\n\nDo it, or say here what it would take and by "
                          "when. Their work does not move until you answer.",
                        to=[who], status=Status.open,
                        dedupe_key="blocking:" + hashlib.sha256(
                            f"{ch}\0{key}\0{who}\0{needs}".encode()
                        ).hexdigest()[:24])
                except DuplicateMessage:
                    # Already told. THE RETURN PATH (2026-08-07): a delivery
                    # mechanism must be symmetric. If the hub routes an
                    # obligation to someone, it routes the discharge back —
                    # otherwise it is a one-way pipe and the waiter waits
                    # forever.
                    #
                    # Measured: `g4-qa` was blocked on `g4-engine`. The hub
                    # told `g4-engine`, which did the work and announced it
                    # `status=resolved, to=[]` — unaddressed, so `g4-qa`
                    # owed nothing and never woke. The hub knew exactly who
                    # was waiting; it just never looked back down the wire.
                    #
                    # Fact-based, no prose read: the blocker ANSWERED the
                    # hub's own addressed alert, and the row is STILL
                    # blocked. Both are things the hub stores.
                    self._ring_back_if_blocker_answered(ch, key, owner, who)
                    continue
                fired.append(f"blocking:{ch}/{key}")
        return fired

    def _close_repaired_undeliverable_block_alerts(
            self, channel: str, key: str, still_undeliverable: bool) -> None:
        """Retire the exact missing-``needs_from`` alert once it is fixed.

        These alerts are open system obligations, so leaving one behind after
        its claim names a reachable blocker makes the owner keep owing an
        instruction they already followed.  Match the claim key, never the
        owner, so another unresolved legacy block remains visible.
        """
        if still_undeliverable:
            return
        for alert in self._undeliverable_block_alerts(channel, key):
            if not self._closed_authoritatively(alert,
                                                self.db.replies_to(alert.id)):
                self._post_system(
                    channel,
                    f"undeliverable block repaired: `{key}` no longer lacks "
                    "a named blocker; this alert is closed.",
                    status="resolved", reply_to=alert.id)

    def _undeliverable_block_alerts(self, channel: str,
                                    key: str) -> list[Message]:
        """Find this claim's alert family, including pre-metadata alerts."""
        marker = f"UNDELIVERABLE BLOCK on `{key}`:"
        alerts: list[Message] = []
        # Open roots remain status=open even after an authoritative closing
        # reply. This unbounded obligation query preserves both old standing
        # alerts and episode history without loading every channel message.
        for alert in self.db.open_obligations([channel]):
            if alert.sender != "hub":
                continue
            data = alert.data if isinstance(alert.data, dict) else {}
            tagged_key = (data.get("undeliverable_block", {})
                          if isinstance(data.get("undeliverable_block"), dict)
                          else {}).get("key")
            # Body matching also finds alerts created before metadata existed,
            # without guessing from the owner's prose.
            if tagged_key == key or (alert.body or "").startswith(marker):
                alerts.append(alert)
        return alerts

    def _undeliverable_block_alert_episode(self, channel: str,
                                           key: str) -> str | None:
        """Return the next stable episode key, or None while one is open."""
        alerts = self._undeliverable_block_alerts(channel, key)
        for alert in alerts:
            if not self._closed_authoritatively(alert,
                                                self.db.replies_to(alert.id)):
                return None
        return max(alerts, key=lambda alert: alert.seq).id if alerts else "initial"

    def _ring_back_if_blocker_answered(self, channel: str, key: str,
                                       owner: str, blocker: str) -> None:
        """Tell a blocked owner that the seat it named has spoken.

        The owner declared the block and only the owner can lift it — the
        hub never decides that the work is unblocked. It reports the one
        fact the owner needs and cannot see from a parked turn: the seat you
        named has answered."""
        alert = next(
            (m for m in reversed(self.db.get_messages(channel, limit=200))
             if m.sender == "hub" and m.to == [blocker]
             and "YOU ARE THE BLOCKER" in (m.body or "")
             and key in (m.body or "")), None)
        if alert is None:
            return
        replies = [r for r in self.db.replies_to(alert.id) if r.sender == blocker]
        if not replies:
            return
        try:
            self._post_system(
                channel,
                f"YOUR BLOCKER ANSWERED: {blocker} replied to the block on "
                f"`{key}`. The row is still marked blocked — only you can "
                "lift it. Read what they said, then resume the work or "
                "re-state what is still missing.",
                to=[owner], status=Status.open,
                dedupe_key=f"unblocked:{channel}:{key}:{replies[-1].id}")
        except DuplicateMessage:
            pass

    def _claim_due_sweep(self) -> list[str]:
        """One standing 'claims due' ping per (channel, owner) whose
        cadence-declared claims have idled past their cadence. The row
        touch IS the receipt (same contract the steward sweep teaches);
        done/parked rows never ping; supersede/close is the hub's own
        debt hygiene. Jitter (+/-20%, keyed on the claim key) de-syncs
        fleet-wide cadence boundaries so pings never arrive as a herd.
        HUB PAUSE silences the sweep entirely, and paused time never ages
        a row toward due (the 0069 clock rule) — triggering.md's doctrine
        paragraph promises both."""
        if self.hub_paused() is not None:
            return []
        now = time.time()
        due: dict[tuple[str, str], list[tuple[str, float, int]]] = {}
        for ch in self.db.channel_names():
            members = {m.agent_id for m in self.db.list_members(ch)}
            for entry in self.db.store_keys(ch):
                key = entry["key"]
                if not key.startswith("claim:"):
                    continue
                stored = self.db.store_get(ch, key)
                if stored is None or not isinstance(stored.value, dict):
                    continue
                value = stored.value
                cadence_s = self._claim_cadence_seconds(value)
                if cadence_s is None:
                    continue
                # PARKED IS NOT AN EXEMPTION HERE (2026-08-06). The parked
                # exemption exists so a THIRD PARTY stops re-asking "is this
                # stale?" when the status already answered. This sweep is not
                # a third party: `cadence_minutes` is the OWNER saying "remind
                # ME when this row idles". Dropping their declaration because
                # they also parked is a silent fallback — the hub deciding not
                # to do the thing its author asked for, and never saying so.
                #
                # Measured: rt2-lead parked claim:phase-1-m1-dispatch at
                # 14:55:13 with cadence_minutes=60, waiting on a row in
                # another room. That row completed at 14:58:56. The ping was
                # due 15:51:37 and was discarded here. Seven seats sat armed
                # for hours on a milestone that was already done.
                #
                # `done` stays exempt: finished work has nothing to re-check.
                if self._claim_done(value):
                    continue
                owner = str(value.get("owner") or stored.updated_by or "")
                if not owner or owner not in members:
                    # A departed owner is the steward sweep's problem;
                    # never rebroadcast a personal reminder to the room.
                    continue
                # Deterministic +/-20% jitter so one cadence value across
                # many claims cannot fire the whole fleet on one tick.
                jitter = 1.0 + 0.4 * (
                    int(hashlib.sha256(key.encode()).hexdigest()[:4], 16)
                    / 0xFFFF - 0.5)
                idle = (now - stored.updated_at
                        - self.paused_seconds_since(stored.updated_at))
                if idle <= cadence_s * jitter:
                    continue
                band = min(int(idle // self.CLAIM_DUE_BAND_SECONDS),
                           self.CLAIM_DUE_MAX_BANDS)
                due.setdefault((ch, owner), []).append((key, idle, band))
        standing = self._standing_claim_pings()
        alerted: list[str] = []
        # Close standing pings whose (channel, owner) no longer owes.
        for (ch, owner), pings in standing.items():
            if (ch, owner) in due:
                continue
            for old in pings:
                self._post_system(
                    ch, "claims-due episode closed: every listed claim was "
                        "touched, finished, parked, or its cadence was "
                        "removed.", status="resolved", reply_to=old.id)
            alerted.append(f"claim-due:{ch}/{owner}:cleared")
        for (ch, owner), rows in sorted(due.items()):
            rows.sort()
            sig = hashlib.sha256("\n".join(
                f"{key}@{band}" for key, _idle, band in rows
            ).encode()).hexdigest()[:16]
            mine = standing.get((ch, owner), [])
            if any(isinstance(m.data, dict)
                   and isinstance(m.data.get("claim_due"), dict)
                   and m.data["claim_due"].get("sig") == sig for m in mine):
                continue    # the standing ping already states exactly this
            for old in mine:
                self._post_system(
                    ch, "superseded by the next claims-due ping (the due "
                        "set changed); this episode is closed.",
                    status="resolved", reply_to=old.id)
            listed = "; ".join(
                f"{key} (idle {idle / 3600.0:.1f}h)" for key, idle, _b in rows[:6])
            if len(rows) > 6:
                listed += f" (+{len(rows) - 6} more)"
            self._post_system(
                ch,
                f"CLAIMS DUE: {listed}. You declared a check-in cadence on "
                "this work. FIRST re-read the claim row and any newer "
                "messages touching the task — they may have canceled, "
                "refined, or superseded it; adjust or park on the record "
                "if so. Otherwise advance it one bounded unit and post a "
                "progress receipt. Touching the claim row resets its "
                "cadence and clears this ping; done/parked rows never "
                "ping. The hub closes this ping itself when the set "
                "changes or empties.",
                to=[owner], status=Status.open,
                data={"claim_due": {"sig": sig, "owner": owner,
                                    "claims": [k for k, _i, _b in rows]}})
            alerted.append(f"claim-due:{ch}/{owner}:{len(rows)}")
        return alerted

    def _standing_claim_pings(self) -> dict[tuple[str, str], list[Message]]:
        """Every hub-authored claims-due ping still standing open, keyed by
        (channel, owner). Read from the channels, not memory (restart-safe,
        the 0093 pattern)."""
        out: dict[tuple[str, str], list[Message]] = {}
        ops = self.operator_ids()
        for ch in self.db.channel_names():
            for m in self.db.open_obligations([ch]):
                if m.sender != "hub" or not isinstance(m.data, dict):
                    continue
                info = m.data.get("claim_due")
                if not isinstance(info, dict):
                    continue
                if self._closed_authoritatively(m, self.db.replies_to(m.id)):
                    continue
                owner = str(info.get("owner") or (m.to[0] if m.to else ""))
                out.setdefault((ch, owner), []).append(m)
        return out

    # -- vote lifecycle (0140 field test 2) ---------------------------------------
    #: The chair publishes a closed vote from its own process; the HUB
    #: guarantees it. Operator ruling, verbatim: "when a vote closes (either
    #: because all have answered OR after X minutes), the results MUST be
    #: broadcasted on the channel it was requested, for all to see. The
    #: anonymous voting is to prevent agents influencing each other during
    #: the vote, but the result must be official and visible to all."
    #:
    #: Blindness is a means, not an end (vote.py's own doctrine): the hub
    #: reads the ballot DMs it already stores ONLY to publish the outcome the
    #: vote body announced, and only once the vote is due. Nothing is
    #: exposed before that.

    def _open_vote_roots(self) -> list[tuple[Message, dict[str, Any]]]:
        """Every vote root still awaiting publication, with its info record.

        Vote roots are `open` messages, so the candidate set is the existing
        open-obligation query rather than a history scan — and the usual case
        (no vote running anywhere) costs exactly that query. Chunked at 500
        channels for the bound-variable ceiling of older bundled SQLites, the
        same reason `replies_map` chunks."""
        rooms = [c for c in self.db.channel_names() if not c.startswith(DM_PREFIX)]
        out: list[tuple[Message, dict[str, Any]]] = []
        for i in range(0, len(rooms), 500):
            for message in self.db.open_obligations(rooms[i:i + 500]):
                if message.kind != Kind.message or not (message.data or {}):
                    continue
                info = vote_info(message, message.channel)
                if info is None or info.get("closes_at") is None:
                    continue
                if published_result(message,
                                    self.db.replies_to(message.id)) is not None:
                    continue
                out.append((message, info))
        return out

    def _vote_ballot_scan(self, root: Message, info: dict[str, Any]) -> BallotScan:
        """Fold every ballot-bearing thread for this vote through the SAME
        `fold_ballot_thread` the chair's watcher runs: the chair's DM threads
        (where blind ballots live) plus the vote's own channel (a ballot
        posted in the room is public, and was counted long before this
        sweep existed)."""
        refs = {r for r in (str(info["tag"]).casefold(),
                            f"{root.seq}@{root.channel}".casefold()) if r}
        scan = BallotScan({}, [], set())
        common = {"chair": root.sender, "refs": refs,
                  "options": info["options"], "since_ts": root.created_at,
                  "marker": receipt_marker(str(info["tag"]))}
        fold_ballot_thread(self._whole_channel(root.channel, root.seq),
                           scan, channel=root.channel, root_id=root.id,
                           reject=False, **common)
        for channel in self.db.channels_of(root.sender):
            if not channel.startswith(DM_PREFIX):
                continue
            fold_ballot_thread(self._whole_channel(channel), scan,
                               channel=channel, **common)
        return scan

    def _whole_channel(self, channel: str, since_seq: int = 0) -> list[Message]:
        """Every message after `since_seq`, PAGED to exhaustion. A fixed
        `limit` would truncate a long thread silently, which is the exact
        failure class the tally reconciliation exists to end."""
        rows: list[Message] = []
        cursor = since_seq
        while True:
            page = self.db.get_messages(channel, cursor, 1000)
            if not page:
                return rows
            rows.extend(page)
            cursor = page[-1].seq
            if len(page) < 1000:
                return rows

    def vote_sweep(self) -> list[str]:
        """Publish every vote whose blindness no longer protects anything.

        A vote is due when its announced deadline passed OR every eligible
        member has balloted; either way the room is owed the counts AND the
        roll call, as the vote body promised. The chair may still close
        early-with-force or on all-voted from its own process — this sweep is
        the guarantee behind that, not a replacement: both publishers read
        `published_result` first, so whoever gets there first wins and the
        other finds the thread already resolved.

        HUB PAUSE silences it (the 0069 clock rule: paused time never ages a
        deadline). Per-vote failures are isolated — one unreadable vote must
        never stop the rest from publishing."""
        if self.hub_paused() is not None:
            return []
        started = time.time()
        published: list[str] = []
        for root, info in self._open_vote_roots():
            try:
                scan = self._vote_ballot_scan(root, info)
                members = [m.agent_id for m in self.db.list_members(root.channel)]
                reason = VoteChair.due(info, scan.ballots, members)
                if reason is None:
                    continue
                total = len(members) or len(scan.ballots)
                tally = tally_ballots(info["options"], scan.ballots)
                counts = scan.counts
                self._post_system(
                    root.channel,
                    result_body(info["topic"], info["options"], tally, total,
                                f"{reason} — published by the hub",
                                counts["ballots_rejected"],
                                counts["ballots_seen"]),
                    status="resolved", reply_to=root.id,
                    data={VOTE_RESULT_KEY: result_payload(
                        info, scan.ballots, total,
                        f"{reason} — published by the hub", scan)},
                    dedupe_key=f"vote-result:{root.id}")
                published.append(f"vote:{root.channel}#{root.seq}")
            except DuplicateMessage:
                continue        # this sweep already published it
            except Exception:
                logging.getLogger("agora.hub.vote").exception(
                    "vote sweep failed for %s#%s (other votes unaffected)",
                    root.channel, root.seq)
        self._note_sweep("vote", started, len(published))
        return published

    async def vote_watchdog(self,
                            interval_seconds: float = VOTE_SWEEP_SECONDS) -> None:
        """Collection timers share the existing fast watchdog (30s default).
        Votes and consultations must not wait on the 300s dark watchdog.
        The dark sweep also catches up if this optional fast loop is disabled.
        """
        log = logging.getLogger("agora.hub.vote")
        while True:
            await asyncio.sleep(interval_seconds)
            for sweep in (self.vote_sweep, self._consultation_sweep, self._mission_change_sweep,
                          self._fs_trigger_sweep):
                try:
                    await asyncio.to_thread(sweep)
                except Exception:
                    log.exception("%s failed (will retry next interval)", sweep.__name__)

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

    def _reporting_stewards(self, exclude: str = "") -> list[str]:
        return sorted({
            d["agent_id"] for d in self.active_delegations()
            if "reporting" in d.get("powers", ()) and d["agent_id"] != exclude})

    def _silence_alert_suffix(self, agent_id: str,
                              explicit_class: str | None = None) -> str:
        """0114 mandate #3 + 0107 routing: tag alerts with silence_class."""
        klass = explicit_class or self.silence_class_for_seat(agent_id) or "unknown"
        action = _SILENCE_CLASS_ROUTE.get(klass, "see fleet /status silence_class")
        return f" [silence_class={klass}; {action}]"

    def _post_silence_watchdog_alert(self, agent_id: str, body: str,
                                     explicit_class: str | None = None,
                                     kind: str = "dark") -> None:
        """Hub-alerts archive + addressed delivery to reporting stewards (0107).

        `kind` (dark|deaf|lurk) rides the message as `data.silence_alert` so
        the hub can CLOSE its own alert when the episode ends — see
        `_close_ended_silence_alerts` for why that is not optional."""
        stewards = self._reporting_stewards(exclude=agent_id)
        self._ensure_alerts_channel()
        self._post_system(
            self.DARK_ALERTS_CHANNEL,
            body + self._silence_alert_suffix(agent_id, explicit_class),
            to=stewards or None,
            # A steward who is told is a steward who owes an answer; with no
            # steward to name there is nobody to oblige, so the same alert
            # stands as the operator's record instead. Explicit since `to` no
            # longer decides status. `exclude=agent_id` above is load-bearing
            # and must stay: a dark reporting delegate handed the alert about
            # ITSELF would owe a debt it is by definition offline to settle,
            # and the next sweep would re-fire on that debt forever.
            status=Status.open if stewards else Status.fyi,
            data={"silence_alert": {"kind": kind, "agent_id": agent_id}},
        )

    _SILENCE_ALERT_PREFIXES = {"AGENT DARK:": "dark", "AGENT DEAF:": "deaf",
                               "AGENT LURKING:": "lurk"}
    SILENCE_CLOSE_PER_SWEEP = 25

    def _silence_alert_subject(self, m: Message) -> tuple[str, str] | None:
        """(kind, agent_id) of a standing silence alert, or None. Prefers the
        stored `data.silence_alert`; falls back to the body prefix so alerts
        written before this tagging existed are still closeable (a permanent
        undischargeable row is exactly what this fix is about)."""
        info = (m.data or {}).get("silence_alert")
        if isinstance(info, dict) and info.get("agent_id") and info.get("kind"):
            return str(info["kind"]), str(info["agent_id"])
        for prefix, kind in self._SILENCE_ALERT_PREFIXES.items():
            if m.body.startswith(prefix):
                rest = m.body[len(prefix):].strip().split()
                if rest:
                    return kind, rest[0].rstrip("'s")
        return None

    def _close_standing_fleet_alerts(self, reason: str) -> None:
        """Resolve every standing FLEET alert when the fleet is not collapsed —
        the same debt hygiene `_close_ended_silence_alerts` applies per seat.

        RESTART-SAFETY (live, 2026-08-03). This used to run ONLY on the
        in-process dark -> recovered transition, so a hub that bounced while
        the fleet was healthy never closed the FLEET DARK rows minted by the
        previous process: 34 of them standing on the operator, plus 20 FLEET
        RECOVERED rows posted as addressed opens before that alert became
        `fyi` news — 55 permanent, undischargeable operator obligations for
        events that were over. Standing alerts are FOUND in the channel like
        every other standing-alert contract in this file, so the healthy path
        now closes what the hub no longer believes. FLEET RECOVERED is swept
        too: good news is never an obligation, and the rows already written
        as one must not outlive the fix that stopped writing them."""
        if self.db.get_channel(self.DARK_ALERTS_CHANNEL) is None:
            return
        ops = self.operator_ids()
        budget = self.SILENCE_CLOSE_PER_SWEEP
        for m in self.db.open_obligations([self.DARK_ALERTS_CHANNEL]):
            if budget <= 0:
                return          # drain over several sweeps, never as one burst
            if m.sender != "hub":
                continue
            tag = (m.data or {}).get("fleet_alert")
            if tag not in ("dark", "recovered") and not m.body.startswith(
                    ("FLEET DARK:", "FLEET RECOVERED:")):
                continue
            if self._closed_authoritatively(m, self.db.replies_to(m.id)):
                continue
            self._post_system(self.DARK_ALERTS_CHANNEL, reason,
                              status="resolved", reply_to=m.id)
            budget -= 1

    def _standing_fleet_alerts(self) -> tuple[list[Message], list[Message]]:
        """(dark, recovered) hub-authored FLEET alerts still standing open."""
        dark: list[Message] = []
        recovered: list[Message] = []
        if self.db.get_channel(self.DARK_ALERTS_CHANNEL) is None:
            return dark, recovered
        ops = self.operator_ids()
        for m in self.db.open_obligations([self.DARK_ALERTS_CHANNEL]):
            if m.sender != "hub":
                continue
            tag = (m.data or {}).get("fleet_alert")
            if tag == "dark" or m.body.startswith("FLEET DARK:"):
                bucket = dark
            elif tag == "recovered" or m.body.startswith("FLEET RECOVERED:"):
                bucket = recovered
            else:
                continue
            if self._closed_authoritatively(m, self.db.replies_to(m.id)):
                continue
            bucket.append(m)
        return dark, recovered

    def _bound_standing_fleet_alerts(self) -> None:
        """At most ONE standing FLEET DARK, and never a standing FLEET
        RECOVERED — enforced on EVERY sweep, dark or healthy.

        WHY NOT ONLY ON RECOVERY (live, 2026-08-03). `_close_standing_fleet_alerts`
        can only run when the fleet is healthy, and on a hub whose roster has
        outgrown its drivers the fleet is never healthy: 8 of 50 registered
        seats live is a permanent 'collapse' by the liveness fraction, so the
        recovery path is unreachable and every past episode's alert stands
        forever. Measured: 35 FLEET DARK + 20 FLEET RECOVERED = 55 permanent
        operator obligations, none of them discharging anything, all of them
        escalating on the operator's /owed.

        The bound holds regardless of fleet state, because it is a statement
        about DEBT, not about liveness: one live episode can owe at most one
        alert, and good news owes nothing at all. The newest FLEET DARK
        survives (it carries the current live/eligible counts); the rest are
        superseded in-thread. Restart-safe: rows are found in the channel."""
        dark, recovered = self._standing_fleet_alerts()
        budget = self.SILENCE_CLOSE_PER_SWEEP
        for m in recovered:
            if budget <= 0:
                return
            self._post_system(
                self.DARK_ALERTS_CHANNEL,
                "FLEET RECOVERED is news, not a request — this row was minted "
                "as an addressed open before that was fixed. Closed; nothing "
                "is owed on it.", status="resolved", reply_to=m.id)
            budget -= 1
        dark.sort(key=lambda m: m.created_at)
        for m in dark[:-1]:              # keep the newest, supersede the rest
            if budget <= 0:
                return
            self._post_system(
                self.DARK_ALERTS_CHANNEL,
                "superseded by the current FLEET DARK alert — one standing "
                "alert states the whole episode. This duplicate is closed; "
                "nothing is owed on it.", status="resolved", reply_to=m.id)
            budget -= 1

    def _retire_report_digest_rows(self) -> list[str]:
        """Close legacy timer-owned delegate digest asks and clear contracts.

        The simpler delegate contract is milestone-driven human summaries to
        the operator/user, not a hub-generated hourly ask. Leaving 0109's
        standing rows open would preserve the exact noisy meta-work the new
        rules removed, so this sweep retires both the open `hub-alerts` rows
        and the per-delegate meta contract state. Restart-safe: rows are found
        in the channel and the meta contract is best-effort cleared each pass."""
        changed = False
        for delegate in self._reporting_delegates():
            if self.db.meta_get(f"report:{delegate}"):
                self.db.meta_set(f"report:{delegate}", "")
                changed = True
        if self.db.get_channel(self.DARK_ALERTS_CHANNEL) is None:
            return ["retired-report-digest"] if changed else []
        ops = self.operator_ids()
        budget = self.SILENCE_CLOSE_PER_SWEEP
        for m in self.db.open_obligations([self.DARK_ALERTS_CHANNEL]):
            if budget <= 0:
                break
            if m.sender != "hub":
                continue
            if not ((m.data or {}).get("report_digest")
                    or m.title == "hourly digest: desk facts"):
                continue
            if self._closed_authoritatively(m, self.db.replies_to(m.id)):
                continue
            self._post_system(
                self.DARK_ALERTS_CHANNEL,
                "Closed: delegate reporting is a milestone-driven human "
                "summary, not a timer-owned hourly ask. This legacy row is "
                "retired; nothing is owed on it.",
                status="resolved", reply_to=m.id)
            budget -= 1
            changed = True
        return ["retired-report-digest"] if changed else []

    def _close_ended_silence_alerts(self) -> None:
        """Close every standing DARK/DEAF/LURK alert whose episode is over.

        THE DEBT THE WATCHDOG OWED ITSELF (2026-08-03 audit). 0093 settled
        this for stale-claims alerts — "an open system alert is an OBLIGATION
        on its addressees, and v1 posted a new one per flap window without
        ever closing the old" — but the silence watchdog never learned the
        lesson. Measured on a read-only copy of the live hub (2026-08-03):
        171 standing open hub-authored alerts, 115 of them silence alerts;
        of the unresolved ones, 40 are addressed to the CURRENT reporting
        delegate and 47 to a former one. That debt is indistinguishable from
        real work on every surface — /owed, the inbox pin, escalation, the
        digest — and, being escalated, it satisfies the delegate's own
        DARK/DEAF/LURK predicate, so the watchdog manufactured the alarms it
        then raised about its own steward.

        Restart-safe like every other standing-alert contract in this file:
        the live episode set comes from memory, but the alerts themselves are
        FOUND in the channel, so a bounced hub closes what it no longer
        believes rather than orphaning it."""
        if self.db.get_channel(self.DARK_ALERTS_CHANNEL) is None:
            return
        live = ({("dark", a) for a in self._dark_since}
                | {("deaf", a) for a in self._deaf_since}
                | {("lurk", a) for a in self._lurk_since})
        ops = self.operator_ids()
        budget = self.SILENCE_CLOSE_PER_SWEEP
        for m in self.db.open_obligations([self.DARK_ALERTS_CHANNEL]):
            if budget <= 0:
                return      # drain the backlog over several sweeps, never as
                #             one burst of wakes (a hub adopting this fix has
                #             months of standing alerts to close)
            if m.sender != "hub":
                continue
            subject = self._silence_alert_subject(m)
            if subject is None or subject in live:
                continue
            if self._closed_authoritatively(m, self.db.replies_to(m.id)):
                continue
            kind, agent_id = subject
            self._post_system(
                self.DARK_ALERTS_CHANNEL,
                f"{kind} episode for '{agent_id}' ended — the seat is "
                "reachable again or the obligation it was silent on cleared. "
                "This alert is closed; nothing is owed on it.",
                status="resolved", reply_to=m.id)
            budget -= 1
        self._supersede_duplicate_silence_alerts(budget)

    def _supersede_duplicate_silence_alerts(self, budget: int) -> None:
        """At most ONE standing alert per (kind, seat) — the 0093 contract the
        stale-claims sweep has always honoured and the silence watchdog never
        learned.

        WHY THE EPISODE CLOSER IS NOT ENOUGH (live, 2026-08-03). Closing
        alerts whose episode ENDED leaves untouched every duplicate whose
        subject is still dark, and a seat that has been offline for weeks is
        exactly the seat that accumulates them: each hub restart clears
        `_dark_since`, re-opens the episode, and mints a fresh alert, while
        the previous one stays open forever because its subject now reads as
        live. Measured on the live hub after the episode closer had already
        run: 99 standing silence alerts over 28 distinct subjects — 71 pure
        duplicates, one seat (`camera`) holding 9 alerts that all say the
        same sentence. The delegate cannot discharge them (only the operator
        can restart a seat), cannot tell them apart, and every one of them
        is an escalating obligation on its /owed.

        The NEWEST alert per subject survives — it carries the current debt
        count and age — and the older ones are superseded in the thread, so
        the operator keeps one live row per genuinely silent seat. Bounded by
        the same per-sweep budget: a hub adopting this fix drains months of
        backlog over minutes, never as one burst of wakes."""
        if budget <= 0 or self.db.get_channel(self.DARK_ALERTS_CHANNEL) is None:
            return
        ops = self.operator_ids()
        standing: dict[tuple[str, str], list[Message]] = {}
        for m in self.db.open_obligations([self.DARK_ALERTS_CHANNEL]):
            if m.sender != "hub":
                continue
            subject = self._silence_alert_subject(m)
            if subject is None:
                continue
            if self._closed_authoritatively(m, self.db.replies_to(m.id)):
                continue
            standing.setdefault(subject, []).append(m)
        for (kind, agent_id), rows in standing.items():
            if len(rows) < 2:
                continue
            rows.sort(key=lambda m: m.created_at)
            for old in rows[:-1]:            # keep the newest, supersede the rest
                if budget <= 0:
                    return
                self._post_system(
                    self.DARK_ALERTS_CHANNEL,
                    f"superseded by the current {kind} alert for '{agent_id}' "
                    "— one standing alert per seat states the whole debt. "
                    "This duplicate is closed; nothing is owed on it.",
                    status="resolved", reply_to=old.id)
                budget -= 1

    def silence_class_for_seat(self, agent_id: str,
                               debts: OwedReport | None = None) -> str | None:
        """0114: classify SLA-breached answer debt by silence root cause so
        stewards route (dead/deaf/unseen/unseen-but-working/seen-and-ignored)
        instead of forensics. None when the seat has no escalated to_answer
        rows.
        `debts` lets a caller that already computed /owed for this seat pass
        it in — recomputing it here doubled the cost of every full-fleet
        surface (measured on a copy of the live 50-seat hub: `doctor()` 26.7s
        -> 15.7s).

        THE FIFTH CLASS, AND WHY IT IS NOT COSMETIC (2026-08-25, reported by
        delegate at agora-and-wui#737 against a LIVE alert on `agora`).
        `unseen` said one thing — the cursor is behind on an escalated row —
        and its route said another: *reprompt or relaunch*, a session-fault
        remedy. Unread-past-SLA is equally consistent with a seat that is
        alive, producing, and simply losing the triage race on those rows,
        and the hub can tell the two apart from state it already keeps: work
        AUTHORED after the unread row arrived is proof the model was reached
        with the message already sitting there. Nothing about presence or
        reception can prove that — both stay green for a listener whose model
        never runs, which is exactly the RC-3 blackout `unseen` was built for.
        So the two keep separate names: the diagnosis, and the remedy that
        follows from it, are opposite (`_SILENCE_CLASS_ROUTE`).

        The measurement that forced it: the alert told the operator to
        relaunch a session that had posted a considered reply eleven minutes
        earlier. A false session fault printed with the hub's own authority
        costs the operator the one action they can take on it."""
        if debts is None:
            debts = self.owed(AgentInfo(id=agent_id, name=agent_id))
        escalated = [r for r in debts.to_answer if r.escalated]
        if not escalated:
            return None
        if self.presence.get(agent_id).state == "offline":
            return "dead"
        reception_state, _ = self.presence.reception(agent_id)
        if reception_state == "stale":
            return "deaf"
        unread = [row for row in escalated
                  if self.db.get_cursor(agent_id, row.channel) < row.seq]
        if unread:
            # Measured from the arrival of the OLDEST unread row, never from
            # "recently": the claim is "it produced while this was waiting",
            # and a fixed lookback would answer a different question.
            since = min(row.created_at for row in unread)
            if since and self.db.last_work_at(agent_id, since) is not None:
                return "unseen-but-working"
            return "unseen"
        return "seen-and-ignored"

    def status_overview(self) -> dict[str, Any]:
        """Fleet liveness aggregate plus per-seat rows (0110 + 0084)."""
        return {
            "fleet": self.fleet_liveness_snapshot(),
            "agents": self.agent_status_overview(),
        }

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
            for row in debts.to_answer:
                ch = row.channel
                if ch not in cursor_cache:
                    cursor_cache[ch] = self.db.get_cursor(agent_id, ch)
                if cursor_cache[ch] >= row.seq:
                    acked_unanswered += 1
            reception_state, reception_age = self.presence.reception(agent_id)
            # Reuse the /owed already computed above (halves this surface).
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
                "owed_answers": debts.counts.to_answer,
                "escalated_owed": sum(1 for r in debts.to_answer if r.escalated),
                "owed_consumption": debts.counts.to_consume,
                "acked_unanswered": acked_unanswered,
                "silence_class": self.silence_class_for_seat(agent_id, debts),
                "refused_sends_1h": len(refusals),
                "last_refusal": refusals[-1] if refusals else None,
            })
        return out

    # -- doctor: one screen, one truth (2026-08-03) --------------------------
    #
    # WHY THIS EXISTS. When a seat goes quiet the operator reconstructs the
    # answer from four places — sqlite, notify logs, driver logs, attempts
    # files. That is literally how the last three investigations were done,
    # and the 08-03 00:19->01:21 stall cost hours of forensics for facts the
    # hub already held. Everything below is stored state; the only new thing
    # is that ONE call assembles it and NAMES WHAT THE HUB CANNOT SEE
    # instead of guessing. Counts and timestamps, never bodies; titles only
    # from non-private rooms.

    DOCTOR_LOOKBACK_SECONDS = 86400.0

    @staticmethod
    def _ago(then: float | None, now: float) -> float | None:
        return None if not then else round(now - then, 1)

    def _doctor_claims(self) -> list[dict[str, Any]]:
        """Every live claim row on the hub, flattened once (the sweeps scan
        the same rows; doing it once keeps `doctor` a single pass)."""
        now = time.time()
        rows: list[dict[str, Any]] = []
        for ch in self.db.channel_names():
            private = getattr(self.db.get_channel(ch), "private", True)
            for entry in self.db.store_keys(ch):
                key = entry["key"]
                if not key.startswith("claim:"):
                    continue
                stored = self.db.store_get(ch, key)
                if stored is None or not isinstance(stored.value, dict):
                    continue
                v = stored.value
                cadence = self._claim_cadence_seconds(v)
                idle = now - stored.updated_at
                rows.append({
                    # Channel names and titles ARE served here, unlike in
                    # hub-alerts. The alert redaction (HIGH-2) exists because
                    # alerts are POSTED into a room delegates read and get
                    # quoted onward; this payload is returned only to the
                    # holder of the admin key, who can read every room
                    # anyway, and the operator's first question is always
                    # WHERE. Bodies are never included, at any privacy level.
                    "channel": ch, "key": key, "private": bool(private),
                    "owner": str(v.get("owner") or stored.updated_by or ""),
                    "status": self._claim_status_word(v) or "(none)",
                    "done": self._claim_done(v), "parked": self._claim_parked(v),
                    # The declared next step is the delegate's own promise
                    # about this work — the operator's fastest read on "is it
                    # driving to completion?". Truncated, never the evidence.
                    "next_step": elide(str(v.get("next_step") or ""), 160),
                    "source_message_id": str(v.get("source_message_id") or ""),
                    "idle_seconds": round(idle, 1),
                    "cadence_seconds": cadence,
                    "next_ping_in_seconds": (round(cadence - idle, 1)
                                             if cadence else None),
                })
        return rows

    def _doctor_seat(self, agent_id: str, now: float,
                     signals: dict[str, dict[str, float]],
                     claims: list[dict[str, Any]],
                     debts: OwedReport) -> dict[str, Any]:
        presence = self.presence.get(agent_id)
        rec_state, rec_age = self.presence.reception(agent_id)
        sig = signals.get(agent_id, {})
        last_work = max([t for t in (sig.get("posted"), sig.get("wrote")) if t],
                        default=None)
        oldest = min((r.created_at for r in debts.to_answer), default=None)
        cursor_cache: dict[str, int] = {}
        acked_unanswered = 0
        for row in debts.to_answer:
            if row.channel not in cursor_cache:
                cursor_cache[row.channel] = self.db.get_cursor(agent_id,
                                                               row.channel)
            if cursor_cache[row.channel] >= row.seq:
                acked_unanswered += 1
        held: list[dict[str, Any]] = []
        block = self.db.block_get(self.HUB_SCOPE, agent_id)
        if block is not None:
            expires = block["expires_at"]
            held.append({"kind": "hub-block", "until": expires,
                         "seconds_left": (round(expires - now, 1)
                                          if expires else None)})
        retirement = self.db.agent_retirement(agent_id)
        if retirement is not None:
            held.append({"kind": "retired", "at": retirement["retired_at"]})
        pause = self.hub_paused()
        if pause is not None:
            held.append({"kind": "hub-paused", "since": pause["since"]})
        refusals = [r for r in self.refusals.get(agent_id, ())
                    if now - r["ts"] < 3600.0]
        if refusals:
            held.append({"kind": "send-refused", "count": len(refusals),
                         "last_code": refusals[-1]["code"],
                         "last_seconds_ago": self._ago(refusals[-1]["ts"], now)})
        mine = [c for c in claims if c["owner"] == agent_id and not c["done"]]
        for c in mine:
            if c["parked"]:
                held.append({"kind": "claim-parked", "ref": c["key"],
                             "status": c["status"],
                             "idle_seconds": c["idle_seconds"]})
        return {
            "agent_id": agent_id,
            "operator": agent_id in self.operator_ids(),
            "delegate_powers": sorted({p for d in self.active_delegations()
                                       if d["agent_id"] == agent_id
                                       for p in (d.get("powers") or ())}),
            "reachable": {
                "presence": presence.state,
                "reception": rec_state,
                "reception_age_seconds": round(rec_age, 1) if rec_age else None,
                "last_contact_seconds": self._ago(presence.updated_at, now),
            },
            # The operator's question, made mechanical: a seat that only ever
            # polls /owed is RECEIVING, not working, and looked identical to
            # a working seat on every previous surface.
            "did_work": {
                "last_post_seconds": self._ago(sig.get("posted"), now),
                "last_write_seconds": self._ago(sig.get("wrote"), now),
                "last_work_seconds": self._ago(last_work, now),
                "worked_within_lookback": last_work is not None,
            },
            "owes": {
                "to_answer": debts.counts.to_answer,
                "escalated": sum(1 for r in debts.to_answer if r.escalated),
                "to_consume": debts.counts.to_consume,
                "to_close": debts.counts.to_close,
                "oldest_seconds": self._ago(oldest, now),
                "acked_unanswered": acked_unanswered,
                "waiting_on_others": len(debts.waiting_on),
                "charters_stale": len(debts.charters),
            },
            "working_on": [
                {k: c[k] for k in ("channel", "key", "status", "next_step",
                                   "idle_seconds", "next_ping_in_seconds")}
                for c in mine],
            "held_up_by": held,
            "silence_class": self.silence_class_for_seat(agent_id, debts),
            "episodes": {
                "dark_since": self._dark_since.get(agent_id),
                "deaf_since": self._deaf_since.get(agent_id),
                "lurk_since": self._lurk_since.get(agent_id),
            },
        }

    def _doctor_requests(self, now: float, claims: list[dict[str, Any]],
                         signals: dict[str, dict[str, float]],
                         reports: dict[str, OwedReport],
                         only: str | None = None) -> list[dict[str, Any]]:
        """Operator requests in flight, with the ORCHESTRATION view: who owns
        each one, what they said they would do next, and which asks they
        dispatched are still outstanding and for how long. A delegate that is
        replying but not dispatching, or dispatching into silence, is exactly
        what this makes visible in one line instead of three hours."""
        ops = self.operator_ids()
        if not ops:
            return []
        out: list[dict[str, Any]] = []
        dispatched_cache: dict[str, list[dict[str, Any]]] = {}
        rooms = self.db.channel_names()
        # Chunked at 500 like `_open_vote_roots`, for the bound-variable
        # ceiling of older bundled SQLites.
        candidates = [m for i in range(0, len(rooms), 500)
                      for m in self.db.open_obligations(rooms[i:i + 500])]
        for m in candidates:
            if m.sender not in ops or m.retracted:
                continue
            replies = self.db.replies_to(m.id)
            ds = self._discharge(m, replies)
            if ds.closed:
                continue
            owners = sorted({str(c["owner"]) for c in claims
                             if c["source_message_id"] == m.id and c["owner"]})
            # One /owed per seat for the whole call (`reports`), never one
            # per (seat, request): the diagnostic must stay cheap enough to
            # run while the hub is under load, which is exactly when it is
            # wanted. Narrowed to one seat, only that seat is considered —
            # `scope` in the payload says so rather than implying a
            # fleet-wide answer.
            owed_by = sorted(a for a, rep in reports.items()
                             if a != m.sender
                             and any(r.id == m.id for r in rep.to_answer))
            if only and only not in set(owners) | set(owed_by):
                continue
            engaged = sorted({r.sender for r in replies if r.sender != m.sender})
            dispatched = []
            for owner in (owners or owed_by):
                if owner not in dispatched_cache:
                    dispatched_cache[owner] = self._doctor_outstanding_asks(
                        owner, now)
                dispatched.extend(dispatched_cache[owner])
            work = [signals.get(o, {}) for o in (owners or owed_by)]
            last_work = max([t for s in work
                             for t in (s.get("posted"), s.get("wrote")) if t],
                            default=None)
            out.append({
                "id": m.id, "channel": m.channel,
                "seq": m.seq, "from": m.sender,
                "title": m.title,
                "age_seconds": round(now - m.created_at, 1),
                "pending_asks": ds.pending, "ask_progress": ds.progress,
                "owned_by": owners,
                "owed_by": owed_by,
                "replied_by": engaged,
                "owner_last_work_seconds": self._ago(last_work, now),
                "claims": [{k: c[k] for k in
                            ("key", "owner", "status", "next_step",
                             "idle_seconds", "done")}
                           for c in claims if c["source_message_id"] == m.id],
                "outstanding_asks": dispatched,
            })
        out.sort(key=lambda r: -r["age_seconds"])
        return out

    def _doctor_outstanding_asks(self, owner: str,
                                 now: float) -> list[dict[str, Any]]:
        """Asks `owner` sent that are still unanswered: who they wait on, in
        what state, and for how long. The monitoring view a delegate needs to
        do its job and the operator needs to audit that it did."""
        rows: list[dict[str, Any]] = []
        channels = self.db.channels_of(owner)
        ops = self.operator_ids()

        def state_of(seat: str, m: Message) -> str:
            if self.db.agent_retirement(seat) is not None:
                return "retired"
            if self.db.get_cursor(seat, m.channel) >= m.seq:
                return "acked-past-no-reply"
            return "not-yet-served"

        for m in self.db.my_open_messages(owner, channels):
            replies = self.db.replies_to(m.id)
            repliers = {r.sender for r in replies}
            if not asks_of(m):
                # UNSTRUCTURED multi-addressee opens. Binary discharge counts
                # ONE reply as the whole answer, so the other named seats are
                # unpinned and the asker is told nothing — measured on the
                # live hub: 118 of 177 peer multi-addressee opens were
                # discharged with named seats still silent. This surface does
                # not change that verdict (re-opening 118 settled threads is
                # the storm class the directive epoch exists to prevent); it
                # makes the silence VISIBLE to the operator, which is the
                # part that costs nothing and was missing.
                if self._closed_authoritatively(m, replies):
                    continue
                for seat in sorted(set(m.to) - repliers - {owner}):
                    rows.append({
                        "asker": owner, "where": f"{m.channel}#{m.seq}",
                        "ask": "(unstructured)", "waiting_on": seat,
                        "state": state_of(seat, m),
                        "age_seconds": round(now - m.created_at, 1),
                    })
                continue
            ds = self._discharge(m, replies)
            if ds.closed:
                continue
            for a in asks_of(m):
                if str(a["id"]) not in ds.pending:
                    continue
                seats = [str(s) for s in (a.get("to") or [])]
                if a.get("assignee"):
                    seats.append(str(a["assignee"]))
                for seat in sorted(set(seats) - repliers):
                    rows.append({
                        "asker": owner,
                        "where": f"{m.channel}#{m.seq}",
                        "ask": str(a["id"]), "waiting_on": seat,
                        "state": state_of(seat, m),
                        "age_seconds": round(now - m.created_at, 1),
                    })
        return rows

    def _doctor_hub(self, now: float,
                    claims: list[dict[str, Any]]) -> dict[str, Any]:
        pause = self.hub_paused()
        sweeps = {}
        for name in ("dark", "vote"):
            run = self.sweep_runs.get(name)
            sweeps[name] = {
                "last_run_seconds": self._ago(run["last_run"], now) if run else None,
                "took_seconds": run["seconds"] if run else None,
                "actions": run["actions"] if run else None,
            }
        votes_overdue = 0
        for _root, vinfo in self._open_vote_roots():
            closes = vinfo.get("closes_at")
            if isinstance(closes, (int, float)) and closes < now:
                votes_overdue += 1
        standing_open = unclosed_silence = 0
        if self.db.get_channel(self.DARK_ALERTS_CHANNEL) is not None:
            ops = self.operator_ids()
            for m in self.db.open_obligations([self.DARK_ALERTS_CHANNEL]):
                if m.sender != "hub":
                    continue
                if self._closed_authoritatively(m, self.db.replies_to(m.id)):
                    continue
                standing_open += 1
                if self._silence_alert_subject(m) is not None:
                    unclosed_silence += 1
        stale = sum(1 for c in claims
                    if not c["done"] and not c["parked"]
                    and c["idle_seconds"] > self.channel_sla(c["channel"]) * 60.0)
        return {
            "paused": pause,
            "sweeps": sweeps,
            "votes_past_deadline": votes_overdue,
            "open_hub_alerts": standing_open,
            "unclosed_silence_alerts": unclosed_silence,
            "stale_claims": stale,
            "live_claims": sum(1 for c in claims if not c["done"]),
            "fleet": self.fleet_liveness_snapshot(),
            "notify_files": self.notify_sink is not None,
            "directive_epoch": self._directive_epoch,
        }

    #: Everything the hub structurally CANNOT know. Printed, not guessed —
    #: the diagnostic's honesty clause: a seat's driver, its backoff and
    #: quarantine state, its next work chunk and its last turn outcome all
    #: live in the session, and the hub only ever sees a seat when the seat
    #: calls it.
    DOCTOR_BLIND_SPOTS = [
        "next work chunk: driver-side. The hub sees only claim cadence pings "
        "(`next_ping_in_seconds`), never the driver's schedule.",
        "last turn outcome: driver-side (`agora drive --turn-log`). The hub "
        "sees posts and writes, not whether a turn succeeded.",
        "backoff / quarantine: driver-side files in ~/.agora "
        "(listen-<id>.backoff, drive-<id>.pid). `agora doctor` reads them "
        "locally and says so; a remote hub cannot.",
        "an idle IDE tab makes no calls: presence 'offline' means NO CONTACT, "
        "never 'dead'. Reception 'unknown' means the seat never announced a "
        "listener, which is not deafness.",
    ]

    def doctor(self, agent_id: str | None = None) -> dict[str, Any]:
        """The one-screen answer to 'why is this seat quiet?'.

        `agent_id` narrows every section to one seat (and the requests it
        owns or owes), which is the cheap path: one /owed computation instead
        of one per seat."""
        now = time.time()
        claims = self._doctor_claims()
        signals = self.db.work_signals(now - self.DOCTOR_LOOKBACK_SECONDS)
        ids = ([agent_id] if agent_id
               else [a for a in self.db.list_agent_ids() if a != "hub"])
        reports = {a: self.owed(AgentInfo(id=a, name=a)) for a in ids}
        return {
            "now": now,
            "scope": agent_id or "all seats",
            "lookback_seconds": self.DOCTOR_LOOKBACK_SECONDS,
            "hub": self._doctor_hub(now, claims),
            "seats": [self._doctor_seat(a, now, signals, claims, reports[a])
                      for a in ids],
            "requests": self._doctor_requests(now, claims, signals, reports,
                                              agent_id),
            "hub_cannot_see": list(self.DOCTOR_BLIND_SPOTS),
        }

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
