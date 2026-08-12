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
from typing import Any, Literal

from pydantic import BaseModel, Field

MAX_BODY_BYTES = 64 * 1024
MAX_DATA_BYTES = 64 * 1024     # structured payload cap (mirrors body; prevents DB-fill DoS)
MAX_STORE_VALUE_BYTES = 256 * 1024  # per channel-store value cap
MAX_TITLE_CHARS = 120          # the title is guaranteed-read: cap the injection/clickbait surface
INLINE_BODY_BYTES = 1200       # below this, envelope-only delivery costs more than the body
ADDRESSED_INLINE_BYTES = 4096  # replies/messages addressed to you inline up to this size

MAX_ABOUT_CHARS = 500          # self-descriptions are read by every joiner: same hygiene as titles
#: A seat's standing MISSION is a different object from a self-description:
#: it is the operator's charge, closer to a system prompt than to a bio, and
#: it carries process ("never decide alone", "prove it before you claim it").
#: Measured 2026-08-06: a 3-rule delegate charge was silently cut mid-word at
#: 500 — the seat received one and a half rules and no one was told.
MAX_MISSION_CHARS = 4000
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

# Machine-readable noticeboard root categories. Keep this one tuple as the
# runtime/CLI validation source; the Literal below preserves the generated API
# schema for typed clients.
NOTICE_KINDS = (
    "job", "announcement", "problem", "resolution",
    "consensus", "milestone", "delivery",
)

_TEXT_CLEAN = re.compile(r"[\x00-\x1f\x7f]+")


class TextTooLong(ValueError):
    """A write was refused because the text exceeds its cap.

    THE RULE (operator, standing): no truncation, no silent fallback, no
    silent limit stopping or disrupting a process of any kind. A cap may
    REFUSE a write. It may never quietly deliver less than was written and
    let the author believe it arrived.

    Why this is an exception and not a slice: on 2026-08-06 an operator set
    a three-rule mission on a delegate seat. The 500-char `about` cap cut it
    mid-word at rule 2. The write returned 200. The seat ran for an hour
    holding one and a half rules, and the only way anyone found out was
    reading the stored value by hand."""

    #: Mirrors HubError's shape so every boundary — HTTP, CLI, in-process —
    #: reports the same 400 without each one re-deriving it.
    status_code = 400

    def __init__(self, field: str, length: int, cap: int) -> None:
        self.field, self.length, self.cap = field, length, cap
        super().__init__(
            f"{field} is {length} characters; the cap is {cap}. Shorten it — "
            f"the hub will not choose which {length - cap} characters to drop.")
        self.detail = str(self)


def sanitize_text(text: str, cap: int, *, field: str = "text") -> str:
    """Sender-authored text that others are guaranteed to read: plain, single
    line, capped. REFUSES over-cap input (TextTooLong); never trims it.

    For deliberately shortening text for DISPLAY, use `elide` — it is a
    different function on purpose, and it leaves a visible mark."""
    cleaned = _TEXT_CLEAN.sub(" ", text).strip()
    if len(cleaned) > cap:
        raise TextTooLong(field, len(cleaned), cap)
    return cleaned


def sanitize_block(text: str, cap: int, *, field: str = "text") -> str:
    """Same contract as sanitize_text, but LINE BREAKS SURVIVE.

    For operator-authored text whose structure is part of its meaning — a
    seat's mission, with numbered rules the model is meant to be able to
    count. Control characters still go; blank runs collapse to one."""
    lines = [_TEXT_CLEAN.sub(" ", ln).rstrip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln.strip() or (out and out[-1].strip()):
            out.append(ln)
    cleaned = "\n".join(out).strip()
    if len(cleaned) > cap:
        raise TextTooLong(field, len(cleaned), cap)
    return cleaned


def elide(text: str, limit: int, *, marker: str = "…") -> str:
    """Shorten for DISPLAY, visibly.

    The ONLY sanctioned way to shorten text in this codebase, and it is
    named so that a reviewer can see it at the call site. Legitimate uses
    are a preview line, a table cell, or quoting an offending value back
    inside an error message — cases where the full record is still reachable
    and nothing was stored short. Never use it on a write path."""
    text = str(text)
    return text if len(text) <= limit else text[:max(0, limit - len(marker))] + marker


# Work-item id grammar (0093, S0 ruling): `<package>-<NNNN>` — URL-safe
# slug, LAST-hyphen parse, all-digits tail. The one shared definition for
# the /work endpoint, item_ref validation, and claim-key consistency; `#`
# forms were rejected at S0 because they break the endpoint path.
_WORK_ID = re.compile(r"^([a-z0-9][a-z0-9_.-]*)-(\d+)$")


def parse_work_id(text: str) -> tuple[str, str] | None:
    """(package, number) for a ruled work id, else None. Last-hyphen parse:
    'abstract-core-0017' -> ('abstract-core', '0017')."""
    m = _WORK_ID.match(text)
    return (m.group(1), m.group(2)) if m else None


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
    # Retraction (0097): true once the author/an operator retracts. On every
    # agent-facing surface the title/body/data are already redacted to a
    # tombstone by the time this is set — the flag lets clients render the
    # dimmed state and exclude it from unread/vigilance counts.
    retracted: bool = False
    retracted_at: float | None = None


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


class Notice(BaseModel):
    """A noticeboard root's machine-checkable event identity."""

    kind: Literal[
        "job", "announcement", "problem", "resolution",
        "consensus", "milestone", "delivery",
    ]
    key: str = Field(min_length=1, max_length=160)


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
    consumes: list[str] | None = None   # 0140/3: consumption debts this ONE
    #                                     message settles (message ids or
    #                                     channel#seq refs) — the batch form
    #                                     that replaces N ceremonial receipts
    attachments: list[AttachmentRef] | None = None  # refs to uploaded channel blobs (0091)
    notice: Notice | None = None      # typed/idempotent noticeboard root
    signature: str | None = None        # RESERVED: opaque authorship token (enforcement later)
    address_dark: bool = False          # 0107: suppress the dark-addressee
    #                                     sender advisory (delivery itself is
    #                                     never gated — operator ruling
    #                                     2026-07-28)


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
    - to_me:       hub-computed "this is yours now" — sender `to`, pending
                   ask-level `to`, plus hub-routed delegate duty for operator
                   requests. It is still a delivery hint, not free priority:
                   a sender can address you, but cannot bypass budgets or
                   obligation semantics.
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
    addressed: bool = False              # the message names SOMEONE (to non-empty):
    #                                      an addressed open/blocked is the named
    #                                      seats' debt — the room's wake rule narrows
    #                                      to them (agora-0135: 62% of commons wakes
    #                                      were addressed opens waking everyone)
    from_operator: bool = False          # the sender is a HUMAN operator seat.
    #                                      Their room-wide asks still carry special
    #                                      authority, but named asks narrow to the
    #                                      named seats plus any hub-routed delegate.
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
    retracted: bool = False              # author/operator retracted it (0097):
                                         # body already redacted to a tombstone;
                                         # dim it, drop it from unread/vigilance
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


class RatingTally(BaseModel):
    """Per-message rating tally served on rows (agora-0122): counts of
    standing ±1 ratings plus the VIEWER's own standing rating (0 = none) —
    what a thumbs UI renders without a single extra read."""

    up: int = 0
    down: int = 0
    mine: int = 0   # -1 | 0 | +1


class MessageRow(Message):
    """A history-page row (`GET /channels/{c}/messages`): the immutable
    Message plus the two thread-derived facts every client was re-deriving
    from its own reply scans (parity move 2, agora-0118). The hub already
    computes both for envelopes and digests; serving them here deletes
    continuum's `replied_ids` walk and chat's `_pending_ask_ids`.

    All decorations are OPTIONAL with null meaning "the hub made no
    statement" (adversary P2-3/P2-5): a retracted tombstone carries no
    thread state, and a new client parsing an OLD hub's rows must be able
    to represent "not served" instead of misreading absence as "nothing
    pending". Non-retracted rows from a current hub always carry values."""

    pending_asks: list[str] | None = None
    # ^ ask ids still undischarged (empty list = question fully settled;
    #   null = no statement: retracted row, or a pre-0.12.30 hub).
    has_resolved_reply: bool | None = None
    # ^ an authoritative closure exists downthread — render the thread as
    #   settled without fetching the replies. Null = no statement.
    ratings: RatingTally | None = None
    # ^ standing ±1 tally + the viewer's own rating (agora-0122). Null = no
    #   statement (retracted row, or a pre-0.12.31 hub).
    read: bool | None = None
    # ^ VIEWER-specific: has this viewer a deliberate read receipt on this
    #   message (the reads table — what owed/to_consume already derive
    #   from)? Serves the acked-but-never-read fact clients could never
    #   compute: cursor >= seq AND read == False is the burst-skip badge
    #   (dm#151: the operator's cursor swept 46 messages he never opened,
    #   including a shipped-feature receipt). Null = no statement: the
    #   viewer's OWN messages (authorship needs no reading) or a pre-0.12.40
    #   hub. Read state never leaks across viewers — each caller sees only
    #   their own receipts.


class ObligationRow(BaseModel):
    """ONE row shape for 'this message waits on a seat' (parity move 3,
    agora-0118): /owed.to_answer today, board/desk/digest surfaces as they
    migrate. Field notes:

    - `sender` is the only name for the author — the same field name the
      envelope and Message use. (The `from` alias this row also emitted
      through 0.13 is gone at agora/0.4: one name per fact.)
    - `created_at` is the truth an age is derived from: render
      `report.computed_at - row.created_at`. The hub no longer serves a
      pre-rounded `age_minutes` — two numbers for one fact is how the two
      drift. The JUDGEMENT stays hub-side and pause-adjusted: `escalated`
      already excludes operator-pause time, which a client cannot compute.
    """

    channel: str
    id: str
    seq: int
    sender: str
    title: str = ""
    pending_asks: list[str] = Field(default_factory=list)
    asks_naming_you: list[str] = Field(default_factory=list)
    created_at: float = 0.0
    escalated: bool = False


class ConsumeRow(BaseModel):
    """An answer to YOUR OWN open question that you have not used (0078)."""

    channel: str
    id: str
    seq: int
    title: str = ""
    your_asks: list[str] = Field(default_factory=list)
    answered_by: str
    answer_id: str
    answer_seq: int
    #: When the ANSWER landed (not the question): the debt is "you have not
    #: read this reply yet", so its age runs from the reply. Ages derive
    #: from `report.computed_at` (see ObligationRow).
    answer_created_at: float = 0.0


class WaitingRow(BaseModel):
    """Asker-side wait state for one still-pending ask addressee."""

    channel: str
    seq: int
    ask: str
    seat: str
    state: str  # "not-yet-acked" | "acked-past-no-reply" | "retired"


class PhaseRow(BaseModel):
    """A channel's declared phase order (agora-0140/2): `phase:<track>` says
    which version of the work is in force and whether the next one may start.
    Advisory by construction — the hub cannot know what a message works on —
    so its whole power is being IMPOSSIBLE TO MISS on every reception pass."""

    channel: str
    key: str
    track: str
    current: str
    status: str = "open"          # "open" | "complete"
    next: str = ""
    steward: str = ""
    paths: list[str] = Field(default_factory=list)
    note: str = ""
    declared_by: str = ""         # hub-stamped: a phase author is not forgeable
    declared_at: float = 0.0
    version: int = 0


class OwedCounts(BaseModel):
    to_answer: int = 0
    to_consume: int = 0
    to_close: int = 0


class CharterDebt(BaseModel):
    """One charter this seat has NOT read at its current version (0146/2).

    The whoami pointer already says this — but whoami is a session-start
    call, so a seat that read v1 and then ran for six hours never learned v2
    existed, and the hub-scope change is announced only in `hub-alerts`
    (operators + reporting delegates). Carrying it on `/owed` puts it on the
    ONE call every reception pass makes, exactly like `phases`.

    Self-clearing by construction: the read records the receipt, so the row
    disappears on the next pass and nobody is nagged twice. Never a debt that
    escalates, never part of the wake signature (it must not manufacture a
    wake), never a block — attention, not a gate."""

    #: "hub" (the standing role model) or a channel name.
    scope: str
    version: int = 0
    #: The version this seat last read; None = never read this charter.
    your_receipt: int | None = None
    #: The exact call that clears it — served, not guessed by the client.
    read_with: str = "read_charter()"
    #: True when the room sets `norms_required`: posting is already refused
    #: until the read. Advisory rows say so; nothing here does the refusing.
    gated: bool = False
    #: Why this row exists. "version" (the default sense: a charter you have
    #: never read at its current version) or "view" — 0147: your receipt is
    #: current, but your SEAT grew since you read it (you became an owner,
    #: or were granted a delegation), so the scoped text you were served
    #: never contained the section that now applies to you. Same self-
    #: clearing read, same non-blocking posture.
    reason: str = "version"


class CloseRow(BaseModel):
    """Asker-side hygiene (agora-0116): your own open/blocked thread is fully
    discharged (every ask answered or binary reply received) but not
    authoritatively closed — advisory only, never wakes."""

    channel: str
    id: str
    seq: int
    title: str = ""
    answered_by: str = ""
    #: When the last answer landed; ages derive from `report.computed_at`
    #: (uniform across every row in this report since agora/0.4).
    answered_at: float = 0.0


class OwedReport(BaseModel):
    """The `/owed` response, typed (parity move 1, agora-0118): the served
    OpenAPI now states this shape instead of `additionalProperties: true`,
    so TS clients generate their types from the artifact instead of
    hand-keeping shapes that drift."""

    to_answer: list[ObligationRow] = Field(default_factory=list)
    to_consume: list[ConsumeRow] = Field(default_factory=list)
    to_close: list[CloseRow] = Field(default_factory=list)
    waiting_on: list[WaitingRow] = Field(default_factory=list)
    #: OPEN phase declarations across the agent's channels (0140/2). Not a
    #: debt — a standing constraint on which work is legitimate right now,
    #: carried here because /owed is the one call every reception pass makes.
    phases: list[PhaseRow] = Field(default_factory=list)
    #: Charters this seat is BEHIND on (0146/2) — hub scope first, then its
    #: rooms. Same reasoning as `phases`: not a debt, a standing constraint
    #: that only works if it is impossible to miss on the reception pass.
    charters: list[CharterDebt] = Field(default_factory=list)
    counts: OwedCounts = Field(default_factory=OwedCounts)
    computed_at: float = 0.0


class CategoryCell(BaseModel):
    """One category's cell on a leaderboard entry (agora-0123). up/down are
    collapsed-RATER voice counts (each colleague's standing votes collapse
    to one net sign), so `score = up - down` is a pinned invariant; `raters`
    counts engaged colleagues including net-zero stances (engagement
    without weight)."""

    score: int = 0
    up: int = 0
    down: int = 0
    raters: int = 0


class RawVoteCounts(BaseModel):
    """Uncollapsed up/down tally on the GLOBAL score line (agora-0126,
    operator ruling dm#145): the collapsed `score` can read +1 while an
    agent took four downvotes — this makes the displeasure visible without
    weakening the anti-farm score. Global only; per-category cells stay
    collapsed voices."""

    up: int = 0
    down: int = 0


class LeaderboardEntry(BaseModel):
    """One agent's unified reputation (agora-0123): `score` = sum of
    category scores (pinned invariant); `breakdown` keys are categories
    ('general' = message thumbs; trust/wisdom/thorough/helper = agent-level
    votes). `votes` is the RAW uncollapsed up/down count on the global line
    (0126). `channels` rides hub-wide entries only (distinct channels with
    any input; never their names — privacy fold)."""

    target: str
    score: int = 0
    raters: int = 0
    votes: RawVoteCounts = Field(default_factory=RawVoteCounts)
    breakdown: dict[str, CategoryCell] = Field(default_factory=dict)
    channels: int | None = None


class LeaderboardReport(BaseModel):
    """The `/reputation` and `/channels/{c}/reputation` response, typed
    (continuum's parity note on 0123: MessageRow got the 0121 treatment,
    the board did not — now it does). Order is HUB-decided: score desc,
    raters desc, target asc; clients render served order."""

    channel: str | None = None
    categories: list[str] = Field(default_factory=list)
    leaderboard: list[LeaderboardEntry] = Field(default_factory=list)


class SearchHit(BaseModel):
    """One search result (agora-0132). A SIBLING of MessageRow, never a
    subclass: identical field names and types for everything shared
    (channel, seq, sender, status, created_at — clients that key renderers
    on field names get badges and thread-jump for free), with the
    kind-discriminated null-field-group rule: message hits carry
    seq/sender/status; store/file/agent hits leave them null. NO body
    (fetch through the read path — no stale copies), NO score (bm25 is a
    measured cross-tenant side channel; order is advisory)."""

    kind: str                      # message|decision|claim|work|file|agent
    channel: str | None = None     # null only for kind=agent (roster scope)
    ref: str                       # message id | store key | fs path | agent id
    title: str = ""
    created_at: float = 0.0
    snippet: str = ""
    highlights: list[list[int]] = Field(default_factory=list)
    # ^ code-point [start, len] offsets into `snippet` as served — never
    #   markup, never sentinel bytes (those are model-render-only).
    seq: int | None = None         # message kinds only
    sender: str | None = None
    status: str | None = None
    thread_hits: int | None = None  # >1 when a thread collapsed into this row
    ratings: RatingTally | None = None
    # ^ operator ruling dm#169 ("remember we have also the downvotes"): a
    #   downvoted answer is visibly marked when it surfaces. Ranking stays
    #   vote-independent — coordinated downvoting must not bury content.


class SearchSection(BaseModel):
    """One section of the grouped report, with LOUD truncation (the
    check_inbox RC-4 lesson: silent cuts teach seats their list was
    complete when it was not)."""

    hits: list[SearchHit] = Field(default_factory=list)
    shown: int = 0
    total: int = 0


class SearchReport(BaseModel):
    """The `GET /search` response (agora-0132): six FIXED sections, always
    served — the grouping IS the task-context digest. Structural sections
    (decisions, open_threads, work, files) order newest-first; messages
    and people ride advisory relevance order. `relaxed` is the loud flag
    that the strict query found nothing and the terms were re-run as OR
    (F1: natural questions returned zero under implicit AND)."""

    decisions: SearchSection = Field(default_factory=SearchSection)
    open_threads: SearchSection = Field(default_factory=SearchSection)
    work: SearchSection = Field(default_factory=SearchSection)
    people: SearchSection = Field(default_factory=SearchSection)
    files: SearchSection = Field(default_factory=SearchSection)
    messages: SearchSection = Field(default_factory=SearchSection)
    relaxed: bool = False
    channels_searched: int = 0
    next_cursor: str | None = None
    computed_at: float = 0.0
    # Semantic honesty fields (agora-0137) — ADDITIVE ONLY (continuum pins
    # undeclared-field absence on SearchHit; the top level takes the new
    # facts). `mode_used` is an OPEN string, not an enum: policy evolves
    # hub-side without re-vendoring clients. None ≠ 0.0 on coverage: None
    # means "no semantic layer", 0.0 means "enabled, nothing embedded yet".
    mode_used: str = "lexical"       # fused | lexical | semantic
    semantic_coverage: float | None = None
    notice: str | None = None


class WhoamiReport(BaseModel):
    """The `/whoami` response, typed (parity move 1, agora-0118).
    `protocol` is the WHOLE capability statement — the `semantics` stamp
    list that used to ride here was deleted at agora/0.4, because a served
    list invites clients to diff it, and a client that diffs capability
    strings reports a hub as "missing" whatever the fold renamed. Sub-objects
    that are still evolving governance surfaces (hub_rules, hub_state,
    delegations) stay loosely typed until their own migration wave."""

    id: str
    name: str = ""
    about: str = ""
    #: The OPERATOR's standing charge for this seat: what it is FOR. Rides
    #: whoami because that is the one call a fresh harness session makes
    #: before it acts, and a seat that does not know its job improvises one
    #: from the room. Read-only to the seat — see Database.set_mission.
    mission: str = ""
    operator: bool = False
    created_at: float = 0.0
    version: str
    protocol: str
    hub_rules: dict[str, Any] = Field(default_factory=dict)
    #: 0146 — a POINTER to the hub charter (version + this seat's receipt),
    #: never its text. Pre-0146 hubs omit it; a client must treat an absent
    #: or empty dict as "this hub has no charter surface", not as v0.
    hub_charter: dict[str, Any] = Field(default_factory=dict)
    hub_state: dict[str, Any] = Field(default_factory=dict)
    delegations: list[dict[str, Any]] = Field(default_factory=list)


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
    #: The OPERATOR's standing charge for this seat — what perspective it
    #: holds and what it is FOR. Read-only here, and read-only everywhere:
    #: only the operator writes it. It rides the member list because the
    #: delegate charter tells a delegate to "ask the seats holding the other
    #: perspectives", and until 2026-08-06 there was no surface that could
    #: resolve that phrase to a list. `about` could not serve: the seat
    #: writes it, and one seat had already replaced "if you end a phase
    #: having agreed with everyone, you did not do your job" with a tidy
    #: summary of itself.
    mission: str = ""
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
