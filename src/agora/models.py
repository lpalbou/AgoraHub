"""Protocol data model.

Design notes (see docs/protocol.md for the full rationale):

- `status` carries the *conversational obligation* semantics inherited from the
  file-based git mailbox this project replaces: `open`/`blocked` expect a
  reply, `resolved` closes a topic. These proved more useful in practice than
  free-form chat because they let an agent scan a channel and know what is
  owed to whom.
- `urgency` is the interleaving contract: how the *sender* suggests the
  message be delivered to a working receiver. Delivery is ultimately at the
  receiver's discretion (a mid-flight tool call is never aborted), matching
  how Codex-style steering queues input for the next loop boundary.
- Messages are immutable once posted (append-only channel history). State
  changes happen by posting new messages, never by editing old ones.
- `body` is markdown text; `data` is an optional structured payload. Together
  they mirror A2A v1.0's Message/Part split (text part + data part) closely
  enough that a future A2A gateway can translate mechanically.
"""

from __future__ import annotations

import re
import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

MAX_BODY_BYTES = 64 * 1024
MAX_DATA_BYTES = 64 * 1024     # structured payload cap (mirrors body; prevents DB-fill DoS)
MAX_STORE_VALUE_BYTES = 256 * 1024  # per channel-store value cap
MAX_TITLE_CHARS = 120          # the title is guaranteed-read: cap the injection/clickbait surface
INLINE_BODY_BYTES = 1200       # below this, envelope-only delivery costs more than the body
ADDRESSED_INLINE_BYTES = 4096  # replies/messages addressed to you inline up to this size

MAX_ABOUT_CHARS = 500          # self-descriptions are read by every joiner: same hygiene as titles
DM_PREFIX = "dm:"              # reserved channel-name prefix for direct 1:1 channels

# Per-channel virtual filesystem (the shared, network-accessible "book" that
# lets remote agents on different machines share an editable workspace without
# a shared disk). Files live as reserved-prefix keys in the channel store, so
# they inherit membership, CAS versioning, and durability; every mutation also
# emits an append-only `Kind.fs` audit message so the file history is replayable.
FS_PREFIX = "fs/"              # reserved store-key prefix for file paths
MAX_FS_PATH_CHARS = 512        # path length cap
# File content reuses MAX_STORE_VALUE_BYTES (256 KiB): text/markdown workspace
# artifacts (plans, contracts, AGENTS-style registries), not a blob store.

# Message attachments (0091): channel-scoped, content-addressed blobs
# referenced from messages. Bytes never ride envelopes — refs do.
MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024   # per-file default cap (operator-configurable)
# Per-channel aggregate blob budget (operator-configurable): append-only
# storage needs a ceiling so one member cannot fill the disk one distinct
# blob at a time (the class that took the whole volume to 100% on
# 2026-07-15). Dedup means identical uploads share one row, so this counts
# distinct bytes. 1 GiB is generous for a text/doc/image workspace.
MAX_CHANNEL_ATTACHMENT_BYTES = 1024 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 8
MAX_FILENAME_CHARS = 200
MAX_CONTENT_TYPE_CHARS = 100

_TEXT_CLEAN = re.compile(r"[\x00-\x1f\x7f]+")


def sanitize_text(text: str, cap: int) -> str:
    """Sender-authored text that others are guaranteed to read: plain, single line, capped."""
    return _TEXT_CLEAN.sub(" ", text).strip()[:cap]


def sanitize_title(title: str) -> str:
    return sanitize_text(title, MAX_TITLE_CHARS)


def dm_channel_name(agent_a: str, agent_b: str) -> str:
    """Canonical DM channel name: order-independent, collision-free by reservation."""
    first, second = sorted((agent_a, agent_b))
    return f"{DM_PREFIX}{first}--{second}"


class Status(str, Enum):
    """Conversational obligation of a message."""

    open = "open"          # a question/request; the channel is waiting on someone
    reply = "reply"        # answers a specific `reply_to` message
    fyi = "fyi"            # information only, no response expected
    blocked = "blocked"    # sender cannot proceed until answered
    resolved = "resolved"  # closes the topic/thread


class Urgency(str, Enum):
    """Sender's delivery suggestion for a busy receiver."""

    inbox = "inbox"           # read whenever you next check your inbox
    next_turn = "next_turn"   # fold into your next loop iteration
    interrupt = "interrupt"   # worth breaking off current work for


class Kind(str, Enum):
    message = "message"  # a participant message
    system = "system"    # hub-generated (joins, leaves, channel events)
    fs = "fs"            # a file-operation audit event (put/delete on the channel VFS)


class FsFile(BaseModel):
    """One file in a channel's virtual filesystem. `content` is the editable
    text body; `version` powers compare-and-swap edits (0 = "must not exist");
    `description` is the writer's one-line statement of what the file IS —
    the field that makes a file listing a table of contents, not a path dump."""

    path: str
    content: str
    mime: str = "text/markdown"
    description: str = ""
    size_bytes: int = 0
    version: int = 0
    updated_by: str = ""
    updated_at: float = 0.0


class Message(BaseModel):
    id: str
    channel: str
    seq: int                      # hub-assigned, per-channel, monotonic; canonical order
    sender: str
    kind: Kind = Kind.message
    status: Status = Status.fyi
    urgency: Urgency = Urgency.inbox
    critical: bool = False               # operator-only forced-attention tier
    downgraded: bool = False             # interrupt demoted by the sender's budget
    to: list[str] = Field(default_factory=list)  # explicitly addressed agents (still broadcast)
    title: str = ""
    body: str = ""
    data: dict[str, Any] | None = None   # optional structured payload
    reply_to: str | None = None          # message id being answered
    created_at: float = Field(default_factory=time.time)


MAX_ASK_CHARS = 500            # a numbered ask is an obligation: keep it plain + bounded
MAX_ASKS = 20                  # a single message should not carry an unbounded checklist
MAX_ASSIGNEE_CHARS = 64        # an ask's optional assignee is an agent id: short + clean
MAX_SIGNATURE_CHARS = 1024     # reserved authorship token: opaque, bounded


class Ask(BaseModel):
    """One numbered, answerable question inside an open/blocked message. Its
    `id` is sender-assigned and unique within the message; a reply discharges
    it by listing that id in its `answers`, so partial-answer state becomes
    mechanical (the file protocol tracked this only by convention)."""

    id: str
    text: str
    assignee: str | None = None  # optional: who is expected to answer (reserved; advisory)
    # Per-ask addressing (0077, anti-lurk): the seats this ask names. The hub
    # validates membership and flags the envelope to-me for every named seat,
    # so a canvass row can never again be buried by headline scroll (field
    # incident: 70 name-in-TEXT misses in 48h — names in prose flag nobody).
    to: list[str] = Field(default_factory=list)


class AttachmentRef(BaseModel):
    """A poster's reference to an already-uploaded channel blob (0091). Only
    `id` (the blob's sha256) is trusted as-declared; filename may override
    the upload-time name for display, and size/content_type are always
    filled by the hub from the blob row — a message can never lie about
    what its attachment IS."""

    id: str
    filename: str | None = None


class PostMessage(BaseModel):
    """Client -> hub payload to post a message."""

    body: str = ""
    title: str = ""
    status: Status = Status.fyi
    urgency: Urgency = Urgency.inbox
    critical: bool = False
    to: list[str] = Field(default_factory=list)
    data: dict[str, Any] | None = None
    reply_to: str | None = None
    asks: list[Ask] | None = None       # numbered questions (open/blocked only)
    answers: list[str] | None = None    # ask ids this reply discharges (reply only)
    attachments: list[AttachmentRef] | None = None  # refs to uploaded channel blobs (0091)
    signature: str | None = None        # RESERVED: opaque authorship token (enforcement later)


class Envelope(BaseModel):
    """What is *delivered*: the triage headline, with the body inlined only
    when the attention economics favor it (see docs/protocol.md).

    Importance is derived from a mix of unforgeable and constrained signals,
    NOT a free-form sender priority (which decays to noise / severity
    inflation between LLMs):
    - obligation:  status open/blocked (+ hub escalation when they rot) — the
                   escalation is hub-driven by age, which senders cannot fake.
    - authority:   critical — operator-only, budgeted (truly unforgeable).
    - reply_to_me: hub-computed from a validated same-channel parent
                   (unforgeable: reply_to is checked at post time).
    - to_me:       sender-declared addressing, but CONSTRAINED — `to` may only
                   name members of the channel (validated at post time). It is
                   a delivery hint, not an unforgeable importance signal; a
                   sender can address you, but cannot thereby bypass budgets or
                   obligation semantics. Treat `to_me` as "the sender says this
                   is for you", not as proof of importance.
    """

    id: str
    channel: str
    seq: int
    sender: str
    kind: Kind
    status: Status
    urgency: Urgency                     # sender-declared timing
    effective_urgency: Urgency           # after hub escalation of rotting obligations
    escalated: bool = False              # hub raised it: an obligation aged past the channel SLA
    downgraded: bool = False             # sender's interrupt budget was exhausted
    critical: bool = False
    to_me: bool = False
    reply_to_me: bool = False
    title: str = ""
    body_bytes: int = 0                  # honest size signal (hard to fake upward)
    body: str | None = None              # inlined only per delivery policy
    data: dict[str, Any] | None = None   # included only when body is inlined
    reply_to: str | None = None
    pending_asks: list[str] = Field(default_factory=list)  # ask ids still unanswered
    your_pending_asks: list[str] = Field(default_factory=list)
    # ^ the subset of pending asks that name THIS viewer (per-ask to, 0077) —
    #   the machine-readable "is this mine" every debrief asked for: a flag
    #   that cannot distinguish "you owe" from "others owe" goes stale the
    #   moment your half is discharged (field incident, 9-seat debrief).
    ask_progress: str = ""               # "answered/total", e.g. "1/3"; "" when no asks
    has_resolved_reply: bool = False     # a resolved reply exists in the thread —
                                         # check it before answering an old ask
    redelivery: bool = False             # you already READ this pinned obligation:
                                         # body withheld, headline-only re-surface
                                         # (the 3.6KB x35 re-send cost, debrief F1)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    # ^ attachment REFS ({id, filename, content_type, size}) ride every
    #   delivery — bytes never do (inbox economy); fetch them via
    #   GET /channels/{c}/attachments/{id}, membership-gated (0091).
    # Authorship (RESERVED for a future gateway-issued identity proof — see
    # thread 0006 P4). Present on every envelope NOW so consumers can hard-code
    # the shape before entities join; `verified_by` is always None until the
    # gateway enforces authorship. Not a trust signal yet.
    signature: str | None = None         # sender-supplied opaque token (echoed)
    verified_by: str | None = None       # hub/gateway attestation (reserved; None today)
    created_at: float = 0.0


class Channel(BaseModel):
    name: str
    private: bool = True
    created_by: str
    created_at: float = Field(default_factory=time.time)


class Member(BaseModel):
    channel: str
    agent_id: str
    role: str = "member"  # "owner" | "member" (structural; DM channels are ownerless)
    about: str = ""       # the agent's self-description (global, shown in member lists)
    joined_at: float = Field(default_factory=time.time)


class AgentInfo(BaseModel):
    id: str
    name: str = ""
    about: str = ""          # self-maintained: scope/ownership, what to ask this agent about
    operator: bool = False   # may post critical broadcasts; granted at registration only
    created_at: float = Field(default_factory=time.time)


class ColleagueNote(BaseModel):
    """Private, subjective, free-text impression of another agent.

    Deliberately NOT a score: design review found numeric reputation between
    LLMs measures agreement rather than truth (sycophancy bias), punishes
    honest dissent, and is statistical noise at small interaction counts.
    A revisable note (truth is often only observable long after reading)
    captures the human-colleague experience without pseudo-quantification.
    Notes are advisory triage input only — they never gate delivery of
    obligations (open/blocked) or critical messages.
    """

    observer: str
    subject: str
    note: str
    updated_at: float = 0.0


class StoreEntry(BaseModel):
    """One key of a channel's shared store. `version` enables compare-and-swap."""

    channel: str
    key: str
    value: Any
    version: int
    updated_by: str
    updated_at: float


class Presence(BaseModel):
    agent_id: str
    # "idle"/"working": live push connection (declared state).
    # "active": no push connection but authenticated activity within the
    #           window (an MCP/REST-only tab) — reachable at its next turn.
    # "offline": no signal at all.
    state: str = "offline"
    updated_at: float = 0.0
