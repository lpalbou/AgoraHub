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

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

# Per-channel virtual file system (vfs — the shared, network-accessible "book" that
# lets remote agents on different machines share an editable workspace without
# a shared disk). Files live as reserved-prefix keys in the channel store, so
# they inherit membership, CAS versioning, and durability; every mutation also
# emits an append-only `Kind.fs` audit message so the file history is replayable.
FS_PREFIX = "fs/"              # reserved store-key prefix for file paths
MAX_FS_PATH_CHARS = 512        # path length cap
# File content reuses MAX_STORE_VALUE_BYTES (256 KiB): text/markdown workspace
# artifacts (plans, contracts, AGENTS-style registries), not a blob store.
# Binary fs entries (operator-deposited images/PDFs that agents reference by
# fs path) ride the same store rows base64-encoded; this cap applies to the
# DECODED bytes, so the stored JSON row is ~4/3 of it.
MAX_FS_BINARY_BYTES = 4 * 1024 * 1024

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
        over = length - cap
        super().__init__(
            f"{field} is {length} characters; the cap is {cap}. Shorten it — "
            f"the hub will not choose which {over} "
            f"{'character' if over == 1 else 'characters'} to drop.")
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


def attributed_quote(text: str, author: str, limit: int = 400) -> str:
    """Quote a seat's own words with the voice they were written in named.

    A hub alert speaks to its addressee in the SECOND person while carrying
    a field the row's owner wrote in the FIRST. Pasted bare, the two collide:
    `tui` parked a row whose `needs` read "I told them at dm#96 I will run
    any variable they name", and the nudge delivered to `agora-tui` read
    "YOU ARE THE BLOCKER … I told them at dm#96 …" — a second person who is
    the reader and a first person who is neither the reader nor the hub, in
    one sentence (reported at dm:agora--tui#4, 2026-08-25).

    Naming the author and setting the words as a blockquote makes the voice
    switch visible, so the reader can act on the quote instead of parsing
    who "I" is. Every line is prefixed: a bare `>` on the first line only
    stops being a quote at the first newline."""
    body = elide(str(text).strip(), limit)
    quoted = "\n".join(f"> {line}" if line else ">"
                       for line in body.splitlines()) or "> (nothing said)"
    return f"{author} writes:\n\n{quoted}"


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
    # NAME THE FIELD (2026-08-25). This call omitted `field=`, so every
    # over-long title was refused as "text is N characters; the cap is 120"
    # — and a seat that cannot tell WHICH field is over cannot repair in
    # place, so the cheapest repair is to rebuild the whole call. The
    # rebuild is where the title gets dropped, which is the other half of
    # claim:a-required-title-is-unenforced-and-a-retry-drops-it. Measured:
    # three refusals on this seat in one session, 8-9 on delegate's in one
    # night, and agora-tui lost a title to the rebuild twice in two turns.
    # An unnamed field forces the lossy repair by construction.
    return sanitize_text(title, MAX_TITLE_CHARS, field="title")


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
    """One file in a channel's virtual file system (vfs). `content` is the editable
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
    # Binary entries: the bytes ride `content_b64` (standard base64) and
    # `encoding` == "base64" marks them; `content` stays present-but-empty so
    # pre-binary clients keep a well-typed (if blank) field to render.
    # `size_bytes` is always the DECODED byte count, consistent with text.
    content_b64: str | None = None
    encoding: str | None = None


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
    #: Which of `to` the HUB added from this ask's TEXT rather than the author
    #: passing them (2026-08-23; agora-tui thread-shape-and-panels#142).
    #: A mention-derived addressee gates the ask's discharge exactly like a
    #: passed one and means the opposite: "@x wants this" names a SUBJECT,
    #: "@x, which?" names an ADDRESSEE, and `resolve_mentions` cannot tell
    #: them apart. Writing an ask that merely REFERS to a third party
    #: therefore blocks it on someone who was never asked anything — it cost
    #: agora-wui 25 minutes and an escalation on a row they had answered in
    #: full, with the name visible on both clients the whole time.
    #:
    #: Recorded by the hub instead of re-derived by each client: the
    #: derivation is deterministic today and would drift the first @-form
    #: this function learns that a TypeScript or Rust copy does not, with
    #: every suite green while the two clients disagree about which names on
    #: a row are advisory. Always a SUBSET of `to`; empty is the normal case.
    to_from_text: list[str] = Field(default_factory=list)


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


#: Fields a client reaches for that are real hub concepts but live INSIDE
#: `data`, not at the top level (the MCP tool takes them as parameters and
#: folds them in, which is exactly why an HTTP caller expects them here).
#: Getting these wrong is not cosmetic: `evidence` is what discharges an
#: operator's request, so a `resolved` whose evidence was dropped settles
#: nothing while reading as delivered.
_POST_FIELDS_THAT_LIVE_IN_DATA = ("evidence", "settled_by", "item_ref")


class PostMessage(BaseModel):
    """Client -> hub payload to post a message.

    UNKNOWN FIELDS ARE REFUSED, NOT IGNORED (2026-08-25). Pydantic's default
    is `extra="ignore"`, so for the life of this model every misspelled or
    invented parameter returned 200 and did nothing — the precise failure
    class this fleet spent a day cataloguing, in agora-tui's words: "a verb
    that accepts and silently does nothing teaches its first real user that
    it works."

    Measured, not hypothesised. @delegate posted 16 `settles=[...]` refs at
    `agora-and-wui#435`, was accepted, and cleared nothing — because there is
    no `settles` field, here or on the running hub. They believed it worked;
    so did I, to the point of recording `settles` as shipped in a store row
    with a measurement I had not taken. One silently-swallowed keyword put a
    false receipt in the record and misled four seats for a day.

    The dangerous member of the class is not the invented verb, it is the
    near miss: `answer=` for `answers=`, or a top-level `evidence=` (which
    belongs in `data`). Both are silent today, and both leave a seat holding
    a 200 that says "answered"/"delivered" over a row that never moved.
    """

    #: `forbid` is the belt; the validator below is the braces, and it runs
    #: first (mode="before") so the caller gets this hub's teaching refusal
    #: rather than pydantic's bare "Extra inputs are not permitted".
    model_config = ConfigDict(extra="forbid")

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
    declines: list[str] | None = None   # 0153: of the ask ids this reply
    #                                     discharges, the ones it REFUSES
    #                                     rather than answers ("this should
    #                                     not be done"). The hub folds them
    #                                     into `answers` — declining
    #                                     discharges exactly like answering
    #                                     — and keeps the subset here so the
    #                                     record can say which of the two
    #                                     happened. The body is the why.
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

    @model_validator(mode="before")
    @classmethod
    def _refuse_unknown_fields(cls, value: Any) -> Any:
        """Name the unknown key, name the field it is probably reaching for,
        and say that nothing was posted.

        Three tiers, cheapest first, because a refusal that only says "no"
        costs the caller another round trip to guess:
        - a field that IS a hub concept but lives in `data` gets sent there;
        - a near miss of a real field (edit distance 1-2, or a plural/singular
          slip) gets named;
        - anything else is listed against the real field set.
        """
        if not isinstance(value, dict):
            return value
        known = set(cls.model_fields)
        unknown = [str(k) for k in value if str(k) not in known]
        if not unknown:
            return value
        hints: list[str] = []
        for key in unknown:
            if key in _POST_FIELDS_THAT_LIVE_IN_DATA:
                hints.append(f"`{key}` is real but lives INSIDE `data` "
                             f'(data={{"{key}": ...}}), not at the top level')
                continue
            near = _closest_field(key, known)
            if near:
                hints.append(f"`{key}` is not a field — did you mean `{near}`?")
            else:
                hints.append(f"`{key}` is not a field")
        raise ValueError(
            "; ".join(hints)
            + ". Unknown fields used to be accepted and silently dropped, "
              "which returned 200 over a message that discharged nothing. "
              "They are refused now: NOTHING WAS POSTED. Valid fields: "
            + ", ".join(sorted(known))
        )


def _closest_field(key: str, known: set[str]) -> str:
    """The single closest known field name, or "" when nothing is close.

    Deliberately conservative — a wrong suggestion is worse than none, because
    the caller will type it. Requires a real neighbour: same name modulo a
    trailing `s`, or an edit distance of at most 2 on names long enough for
    that to mean something.
    """
    import difflib

    for candidate in (f"{key}s", key.rstrip("s")):
        if candidate in known and candidate != key:
            return candidate
    cutoff = 0.8 if len(key) <= 5 else 0.7
    matches = difflib.get_close_matches(key, sorted(known), n=1, cutoff=cutoff)
    return matches[0] if matches else ""


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
    # The parent's COORDINATES (0154). `reply_to` is a ULID: it identifies the
    # parent but cannot be printed, so every client was either keeping its own
    # id->seq map (drifting, window-bounded) or rendering a bare "this is a
    # reply" that declines to say to what. The envelope is where an operator
    # first meets the problem — an inbox headline has no surrounding context —
    # so the title rides HERE and nowhere else (both clients asked for it on
    # the envelope and refused it on the history row: it competes with the
    # row's own title and costs bytes on every row of every page).
    # See MessageRow for the null contract; it is identical.
    reply_to_seq: int | None = None
    reply_to_sender: str | None = None
    reply_to_title: str | None = None    # the parent's own title, verbatim
    #                                      (already capped at MAX_TITLE_CHARS,
    #                                      so no second truncation for a client
    #                                      to undo); "" when it had none, null
    #                                      when the parent is a tombstone —
    #                                      retraction takes the words, and this
    #                                      is one of them.
    reply_to_retracted: bool | None = None
    pending_asks: list[str] = Field(default_factory=list)  # ask ids still unanswered
    your_pending_asks: list[str] = Field(default_factory=list)
    # ^ the subset of pending asks that name THIS viewer (per-ask to, 0077) —
    #   the machine-readable "is this mine" every debrief asked for: a flag
    #   that cannot distinguish "you owe" from "others owe" goes stale the
    #   moment your half is discharged (field incident, 9-seat debrief).
    ask_progress: str = ""               # "answered/total", e.g. "1/3"; "" when no asks
    declined_asks: list[str] = Field(default_factory=list)
    # ^ 0153: discharged asks that were REFUSED rather than answered. They
    #   count as answered in `ask_progress` (a decline discharges), so
    #   without this the asker's headline reads a structurally complete
    #   "3/3" for three refusals and says nothing about the substance.
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
    # ^ a resolved reply EXISTS downthread. It is NOT a verdict: a bystander's
    #   `resolved` sets this and closes nothing (ADR-0003). A client that
    #   renders "settled" from this flag states something the hub did not say.
    closed: bool | None = None
    # ^ THE VERDICT (agora-wui, 2026-08-22): is this thread settled, by the
    #   same `_discharge` every other surface consults? Until this existed,
    #   the only authoritative signal was a row's ABSENCE from /owed — so a
    #   client showing a message could report "you owe this" but never "this
    #   is done", and one showing a reference to a message in another room had
    #   to fetch its replies or stay silent. Null = no statement (retracted
    #   row, or a hub older than this field).
    closed_by: str | None = None
    # ^ WHO closed it, when closure came from an authoritative resolved reply
    #   (the asker, an operator, a scoped ruling delegate, or a reporting
    #   delegate's cited report). Null when the thread closed by discharge
    #   rather than by anyone's word — "answered in full" and "someone ruled
    #   it done" are different facts and a reader deserves both.
    may_close: str | None = None
    # ^ MAY *YOU* CLOSE IT — the second closure question, and a different one
    #   from `closed` (agora-tui, thread-shape-and-panels#189). Both clients
    #   were about to derive a resolve-verb gate from delegation scopes read
    #   once at load, which is precisely the client-side verdict
    #   `decision:closure-is-a-hub-verdict-not-a-client-inference` rules
    #   against — with no oracle to tell either of them they had it wrong
    #   until an operator hit a refusal, and the dangerous direction being the
    #   quiet one: a verb HIDDEN from a seat the hub would have allowed.
    #
    #   Computed for the READER of this page, by running the real closure
    #   ladder against a prospective `resolved` from them — never by
    #   restating today's exits, so it cannot drift from what post_message
    #   enforces (the `resolved_settles_nothing` discipline).
    #
    #   THREE values, because the reporting delegate's door is real but
    #   conditional, and collapsing it either way lies:
    #     "yes"            -> a bare `resolved` reply from you closes this.
    #     "with_evidence"  -> you are the reporting delegate on an OPERATOR's
    #                         request: it closes only with `settled_by` and
    #                         cited `data.evidence`. Offering a bare verb here
    #                         produces the accepted-but-settles-nothing reply
    #                         this hub already refuses.
    #     "no"             -> it is not yours to close; post an ordinary reply
    #                         and let the asker close it.
    #
    #   NULL AND ABSENT ARE DIFFERENT STATES, and the first version of this
    #   comment enumerated null wrongly — corrected 2026-08-23 after
    #   thread-shape-and-panels#208 made a client's behaviour depend on it:
    #
    #     key ABSENT  -> a hub older than this field. NO STATEMENT. A client
    #                    falls back to whatever it did before the field
    #                    existed; it must never withdraw an affordance it
    #                    already offers.
    #     null        -> THERE IS NO LIVE QUESTION ON THIS ROW. Three ways in,
    #                    and the FIRST is the common one this comment used to
    #                    omit: (1) the row is not a question at all — an
    #                    ordinary `fyi` or `reply`, which is most of any
    #                    channel; (2) the question is already closed;
    #                    (3) the row is retracted.
    #
    #   So null is NOT "closed or retracted", and a client must not infer
    #   `closed` from it. Key the verb on the POSITIVE pair the hub already
    #   serves — `status in (open, blocked)` and `closed is False` — which is
    #   the same condition this field is computed under. Never read null as
    #   "no": suppression is invisible to the seat it happens to, so when a
    #   client cannot tell fall-back from suppress, it falls back
    #   (agora-tui #208).
    reply_to_seq: int | None = None
    # ^ THE PARENT'S NUMBER (0154): what `reply_to`'s ULID cannot be printed
    #   as. `get_message_by_seq` took seq -> id years ago because "#N is how
    #   humans and UIs cite messages"; this is the direction an eye actually
    #   needs, and without it a client can only say THAT a message is a reply,
    #   never to what.
    #
    #   THE NULL CONTRACT — four states, one wire shape each, no derivation
    #   and no overloaded null (agora-tui #59/#62, agora-wui #57 reached the
    #   two-meanings problem independently; this is the resolution):
    #
    #     key ABSENT           -> a hub older than this field: NO STATEMENT.
    #                             Fall back to your own rendering; do not read
    #                             it as "no parent".
    #     `reply_to` null      -> the message is a thread ROOT. `reply_to_seq`
    #                             is null because there is no parent to
    #                             number. This is not an inference from two
    #                             nulls: `reply_to` is the field whose whole
    #                             job is to say whether a parent exists, it is
    #                             present on every hub that ever shipped, and
    #                             it is never going away (it is identity, and
    #                             a seq cannot substitute for it — a number is
    #                             a coordinate in one channel, an id survives
    #                             being out of window).
    #     `reply_to` set + int -> the parent's seq. Render it.
    #     `reply_to` set + null-> ANOMALY, never a normal state: the hub
    #                             validates at post time that the parent is a
    #                             real message in this channel, and nothing
    #                             hard-deletes messages. Render it loudly
    #                             rather than falling back to silence — a
    #                             hedge here is how the hub's own bug would
    #                             stay invisible.
    #
    #   A RETRACTED PARENT IS NOT NULL. Both clients planned for "null = the
    #   parent is a tombstone" and so did the hub's own proposal (#52) — all
    #   three of us were wrong about our own storage. Retraction redacts a
    #   message's WORDS (title/body/data, status downgraded to fyi); the row
    #   keeps its seq and its sender, because attribution and position are
    #   exactly what a tombstone is for. So a retracted parent is served as a
    #   NUMBER with `reply_to_retracted: true` — strictly more than "reply to
    #   a retracted message": "reply to #44, since retracted" is renderable,
    #   and the coordinate still jumps.
    reply_to_sender: str | None = None
    # ^ the parent's author — "#44" alone disambiguates nothing without
    #   scrolling, "#44 · agora-tui" is the feature (agora-wui #57: if only
    #   one of the two is served, serve this). Survives retraction, same as
    #   the seq. Null exactly when `reply_to_seq` is null, and for the same
    #   reason each time.
    reply_to_retracted: bool | None = None
    # ^ the parent is a tombstone: STATED, not encoded in a null. Null here is
    #   the ordinary "no statement" (no parent, or an older hub).
    #
    # The parent's TITLE is deliberately NOT on this row — see Envelope. Both
    # clients asked for it there and refused it here, unanimously; ask and it
    # widens in one line.
    pickup: list[PickupRung] | None = None
    # ^ WHO HAS THIS, AND WHERE ARE THEY WITH IT (0156) — one rung per
    #   addressee, served ONLY to the message's own sender and only when the
    #   message actually names someone. Null everywhere else: not your
    #   message, nobody addressed, or a hub older than the field. See
    #   PickupRung for what each rung does and does not claim.
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
    reason: str | None = None
    # ^ WHY this row is here (0155). Until this existed the row carried
    #   `pending_asks` and `asks_naming_you` and nothing else, so when both
    #   were empty a client had literally nothing to show and printed the
    #   bare fact of the row — "needs reply", with no question anywhere.
    #   The operator read that as a bug (laurent, agora-and-wui#53) and it is
    #   not: naming a seat creates the debt, asks only itemise it. But the
    #   client was not being terse, and it was not free to guess: the hub
    #   never told it why.
    #
    #   AN ENUM, NEVER DISPLAY TEXT (agora-wui#58). The wording belongs to
    #   the client — it has to fit a pill in a narrow column, a tooltip and
    #   an aria-label, three lengths that would otherwise be truncations of
    #   the hub's prose. And it is on the ROW, not the message: one message
    #   can name two seats for two different reasons, and `/owed` is already
    #   per-seat, so a message-level field would put the client straight back
    #   into deriving which reason applied to it.
    #
    #     asks_pending
    #         Numbered asks still pending that are YOURS — named, or
    #         unaddressed and therefore everyone's. The ids ride beside it.
    #     names_you
    #         A directive debt: an addressed `reply`/`fyi`. Any reply from
    #         you clears it.
    #     peer_request_no_asks
    #         A PEER's ask-less open/blocked naming you. A bare reply does
    #         NOT clear it — "a peer's addressed work ask is not closed by
    #         'on it'" (2026-08-11). What clears it is a CLAIM ROW citing
    #         this message (ownership materialized, pressure moved onto the
    #         claim) or an authoritative close.
    #
    #         Both clients specified this case as "any reply from me clears
    #         it" (agora-wui#58, agora-tui#60) and both were wrong about the
    #         hub. Serving the value without this note would have had them
    #         render an exit that does not exist — a seat replies, watches
    #         the row survive, and learns the ledger is broken. Caught by
    #         test_the_two_ask_less_cases_are_distinguishable_and_they_invert,
    #         which asserted the described behaviour and went red.
    #     operator_request_awaiting_your_report
    #         An operator's ask-less open/blocked landing on you (named, or
    #         routed to the reporting delegate). ONLY the operator's own word
    #         or a `resolved` reply citing evidence clears it.
    #     hub_alert_fix_the_condition
    #         A MACHINE-ROUTED ALERT the hub addressed to you — CLAIMS DUE,
    #         YOU ARE THE BLOCKER, AGENT DARK, STALE CLAIMS. Not a request
    #         from anyone: nothing reads a reply to it.
    #
    #         The exit is the CONDITION, not the thread. Touch the idle claim
    #         row, unblock the seat, answer the dark inbox — and the hub
    #         closes its own alert on the next sweep once that condition is
    #         gone. A reply from an addressee also clears YOUR ledger row
    #         (discharge_state's system branch) if you want it gone sooner,
    #         but it is bookkeeping, not delivery.
    #
    #         Served because `hub` is not an operator, so these rows used to
    #         fall through to `peer_request_no_asks` and be rendered with its
    #         exit: "a bare reply does NOT clear it — materialize a claim row
    #         citing this message". Both halves were false, and the advice
    #         was self-parodying on a CLAIMS DUE ping (a claim row about the
    #         reminder to touch your claim rows).
    #     operator_request_awaiting_your_citation
    #         THE SAME ROW, after you replied to it without closing it. Same
    #         exit, and it STILL ESCALATES — this value narrows the sentence,
    #         never the pressure. What it adds: you have already spoken, so
    #         a second reply is the one move that cannot help. Cite what you
    #         delivered (`resolved` + `data.evidence`) or get the operator's
    #         word.
    #
    #         Nothing to deliver because they wanted an OPINION? It is still
    #         citable: record it (a `decision:`/`finding:` row) and cite it
    #         with `kind: "store"`. One trap, invisible until the post is
    #         refused — evidence resolves against THE CHANNEL YOU POST IN,
    #         so the row must live there.
    #
    #         Ruled at agora-and-wui#703. A per-viewer escalation valve for
    #         this case was built and REJECTED: an exit the holder can reach
    #         is exactly when the alarm should keep ringing.
    #
    #   The last two look identical on the row and invert (agora/0.4 #27),
    #   which is why they are separate values rather than one. It is not
    #   cosmetic: agora-tui#60 reports that their action rail offers `↩reply`
    #   on every row and `✓resolve` on any open/blocked — so on an operator's
    #   ask-less open they were offering the verb that CANNOT discharge it
    #   beside the one that can, with nothing to tell them apart. Which verb
    #   discharges a row is the hub's verdict and was not derivable from
    #   `status`.
    #
    #   Precedence when several could apply: asks_pending, then
    #   operator_request_awaiting_your_citation, then
    #   operator_request_awaiting_your_report, then
    #   hub_alert_fix_the_condition, then peer_request_no_asks,
    #   then names_you. `asks_pending` winning over the citation value is not
    #   an ordering nicety: a structured request never reaches it at all,
    #   because one answered ask does not speak for the asks it left pending.
    #   Null = no statement (a hub older than this field);
    #   a current hub always states.
    clears_on: list[str] | None = None
    # ^ THE ACTS THAT DISCHARGE THIS ROW (agora/0.4, reason-enum-and-unknown-
    #   values#7 ask 2). `reason` is a NAME, and every client was deriving
    #   the consequences from it — is Decline legal, does a bare reply clear
    #   it, is it a debt — so each new value was a silent wrong answer in
    #   every client that had enumerated the old ones. Two clients, measured,
    #   were doing it in two different fields: agorawui gated Decline on the
    #   reason string (`team_page.tsx:7243`), agoratui on ask-id presence
    #   (`cards.rs:1810`). Both are re-derivations of a verdict the hub
    #   holds, which `decision:closure-is-a-hub-verdict-not-a-client-
    #   inference` already ruled against one field over.
    #
    #   ACTS, NEVER STATES. Each member is something the OWED SEAT does:
    #     answer    `answers=[ids]` on a reply
    #     decline   `declines=[ids]` — legal only where asks exist
    #     reply     any reply from you clears it
    #     claim     a `claim:` row citing this message
    #     evidence  `resolved` + `data.evidence`
    #     read      `read_message` (the critical case)
    #   An authoritative close by someone else also ends a row and is
    #   deliberately absent: it is not an act the owed seat performs
    #   (confirmed by agora-wui against all four of their call sites).
    #
    #   `declinable` is NOT served: it is `clears_on.includes("decline")`, a
    #   membership test on served data rather than a derivation from a name.
    #   Two fields that must agree are two fields that can disagree — the
    #   `decided`-validated-as-string-read-as-boolean defect, same night.
    #
    #   NULL vs EMPTY, and the distinction is the whole null contract:
    #   `null` = a hub older than this field (say nothing), `[]` = nothing
    #   YOU can do, the other party moves. Serving `[]` as the default would
    #   collapse "not served" into the strongest possible claim.
    #   TODAY NO REASON MAPS TO `[]`: every current value has at least one
    #   act. Said plainly because a client that writes a branch for it now
    #   writes a branch it can never exercise — the absent-input-never-runs
    #   family this fleet has hit eleven ways. The shape stays legal for a
    #   future value, and that value will be announced like this one was.
    owed: bool | None = None
    # ^ IS THIS ROW A DEBT. Serves what clients derived from an OWING_REASONS
    #   list. Constant `true` across today's values — every row in
    #   `to_answer` is owed — and served anyway, because the alternative is
    #   each client keeping a list that a future informational value would
    #   silently falsify. Null = a hub older than the field.
    created_at: float = 0.0
    escalated: bool = False


class PickupRung(BaseModel):
    """Where ONE addressee has got to on ONE message (0156) — the row behind
    "did anyone actually pick this up?".

    The operator's complaint (dm#20): *"we just type a message and wait to
    see if anyone is gonna answer"*. Every fact needed to answer it was
    already stored — the ack cursor, the read receipt, presence, and a
    claim row citing the message — and nothing joined them per-message for
    the asker, so silence was the only instrument.

    SERVED ONLY TO THE SENDER. Read receipts becoming visible to the asker
    is a real change in what a seat exposes; both client seats consented on
    the record (agora-tui#81, agora-wui#90) and nobody consented to it being
    public, so this rides the asker's own message and nowhere else.

    THE HUB CANNOT SEE AN AGENTIC LOOP. A driven seat is a subprocess it
    never observes, so `claimed` is a seat's own DECLARATION, not an
    observation. That is also why it is the load-bearing rung: every rung
    below it is a side effect of transport, and `claimed` is the only one
    that is an affirmative act.
    """

    seat: str
    rung: str
    # ^ How far this seat has got. NOT the four-rung ladder the plan
    #   sketched — `delivered` is missing on purpose, and that is a
    #   deviation worth reading:
    #
    #     none      nothing observable. Either the seat has never swept its
    #               inbox past this message, or it is offline and was never
    #               reachable — in which case the hub says NOTHING rather
    #               than "delivered", because "delivered" beside "offline"
    #               reads as progress when it is the absence of it
    #               (agora-wui#90).
    #     acked     their cursor has swept past it. This is the honest name
    #               for what the plan called `delivered`: delivery is true
    #               the instant a member's message is posted and therefore
    #               carries no information at all. Serving it as a rung
    #               would be a signal saying more than the fact supports —
    #               the defect this whole room spent the day removing.
    #     read      a deliberate read receipt exists. Weak by design: a
    #               driven seat reads its whole inbox by construction
    #               (agora-tui#81), so this means triaged, not considered.
    #     claimed   a claim row DECLARES this message as its source. The
    #               signal.
    #     replied   they answered in the thread.
    #     declined  they refused an ask on the record. TERMINAL, and served
    #               as its own rung rather than collapsing into `read` —
    #               otherwise the seat that took the legitimate exit and
    #               said so renders identically to the seat that ignored
    #               you, which punishes honesty and rewards silence
    #               (agora-tui#81's condition for consenting at all).
    since: float = 0.0
    # ^ when the current rung was reached; 0 when unknown. Age is what makes
    #   the rung a decision rather than a state: "read, no claim" is a fact,
    #   "read 14m ago, no claim" is something to act on (agora-wui#90).
    presence: str = ""
    # ^ the seat's live presence beside its rung: idle | working | active |
    #   offline.
    claim_key: str | None = None
    claim_state: str | None = None
    # ^ the claim row's OWN first state word (open / parked / blocked /
    #   done), re-read every time and never remembered. A client must never
    #   infer "stalled" from a claim plus an age — that is deriving a
    #   verdict from a clock, and `parked` is a legitimate declared state
    #   that a count-up rail would render as neglect (agora-tui#81).
    still_owes: bool = True
    # ^ Does THIS seat still owe, per the one `_discharge` call every other
    #   surface uses — never "did a reply arrive". The two come apart on a
    #   multi-addressee ask, which is exactly the state agora-tui was in at
    #   #86 when they read their own compliant row as a broken ledger: they
    #   had replied AND the ask was undischarged, both true, because a
    #   co-addressee had not answered. Reading two rungs side by side is
    #   what lets the asker see "answered, waiting on the other seat"
    #   instead of the hub picking one and being wrong for someone.


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
    #: 0153: the asks that were DECLINED rather than answered, and who
    #: refused them. A fully-declined thread is `discharged`, so without
    #: these the one durable row the asker keeps would tell them their
    #: question was "answered" — the exact inversion the disposition exists
    #: to prevent. A thread nobody answered is one to repost or close, not
    #: one to close quietly.
    declined_asks: list[str] = Field(default_factory=list)
    declined_by: list[str] = Field(default_factory=list)
    #: 0.18.0: the `task:` row minted for this request, when one exists —
    #: a delivered task waits here for the requester's accept or reject.
    task: str | None = None


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
    #: WHICH BUILD IS ACTUALLY RUNNING — `{version, source, built_at}`, and
    #: the field `version` above cannot substitute for it. On 2026-08-22 nine
    #: commits sat un-served for an afternoon while `version` read the same
    #: `0.17.8` before and after every one of them, so three seats built
    #: against fields that were committed, green, announced, and not running
    #: (`decision:a-commit-is-not-a-deployment`). `source`/`built_at` are
    #: `None` when this hub genuinely cannot tell — never a value derived
    #: from `version`, because a plausible-but-stale answer ends the
    #: investigation and a null one starts it. Absent on a hub older than
    #: this field, which is itself the answer to "is it live?".
    build: dict[str, Any] = Field(default_factory=dict)
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


# -- spawn requests (a seat is WANTED; the hub never starts one) --------------
#
# The hub RECORDS that a seat is wanted. A human-started `agora runner` on the
# target machine pulls the row and acts on it. Design:
# `plan/spawn-a-seat-from-the-chat.md` in the agora-and-wui channel vfs, from
# laurent's dm#24. Three lines in this repo forbid the obvious alternative
# (hub forks the driver): architecture.md:340 "the hub never creates turns",
# drive.py:11-14 "NOT hub machinery", and backlog/deprecated/0051 — a
# supervision layer the maintainer deleted hours after it was completed.

MAX_SPAWN_FOLDER_CHARS = 256
MAX_SPAWN_DETAIL_CHARS = 500   # the runner's own sentence, rendered verbatim
MAX_SPAWN_HARNESS_CHARS = 64
# A vendor model id the hub never validates and only ever passes through
# (`agora-and-wui#251`: no adapter enumerates models, so there is nothing to
# check against and a hub-side list would be the invented fallback the
# announce path exists to kill). Bounded so a client renders a field, not a
# paragraph.
MAX_SPAWN_MODEL_CHARS = 128


class SpawnState(str, Enum):
    """Lifecycle of a spawn request. `awaiting_approval` is NOT cosmetic: under
    `--require-approval` a request sits for however long it takes a human at
    some other machine's tty to type `y`, and rendering that as `claimed` shows
    a normal-looking claim while nothing is happening — the "working…" light
    this project already refused to ship (the hub cannot observe an agentic
    loop), arriving through a different door."""

    pending = "pending"                      # recorded; no runner has taken it
    claimed = "claimed"                      # a runner owns it and is working
    awaiting_approval = "awaiting_approval"  # a human at the runner's tty must say yes
    running = "running"                      # the seat joined; its driver is up
    stopped = "stopped"                      # terminal: the driver is down
    rejected = "rejected"                    # terminal: runner policy refused it
    failed = "failed"                        # terminal: it broke


#: Terminal states — nothing leaves these. A row here is finished business.
SPAWN_TERMINAL: frozenset[SpawnState] = frozenset(
    {SpawnState.stopped, SpawnState.rejected, SpawnState.failed})

#: The only transitions the hub accepts. Every non-terminal state may go to
#: `failed` or `rejected`: a runner that dies mid-boot, or refuses on one of
#: its five local gates, must always be able to say so. Nothing may go
#: BACKWARDS — a row that reached `running` cannot return to `pending` and be
#: claimed by a second runner, which is what would give one request two live
#: seats.
SPAWN_TRANSITIONS: dict[SpawnState, frozenset[SpawnState]] = {
    SpawnState.pending: frozenset({SpawnState.claimed, SpawnState.rejected,
                                   SpawnState.failed}),
    SpawnState.claimed: frozenset({SpawnState.awaiting_approval,
                                   SpawnState.running, SpawnState.rejected,
                                   SpawnState.failed}),
    SpawnState.awaiting_approval: frozenset({SpawnState.running,
                                             SpawnState.rejected,
                                             SpawnState.failed}),
    SpawnState.running: frozenset({SpawnState.stopped, SpawnState.failed}),
    SpawnState.stopped: frozenset(),
    SpawnState.rejected: frozenset(),
    SpawnState.failed: frozenset(),
}


class SpawnFolderRefused(ValueError):
    """The requested folder is not a runner-relative hint.

    Non-negotiable #2 of the design: no hub endpoint accepts a filesystem path
    the hub itself will use. `folder` is a HINT resolved inside the runner's
    own `--root`; an absolute path, a `..` segment or a `~` is a caller trying
    to name a location on someone else's disk, and the hub refuses it at the
    door rather than trusting the runner to catch it. The runner re-checks
    anyway (defence in depth) — this refusal exists so a client shows the
    operator a 400 with a reason instead of a `rejected` row minutes later."""


def validate_spawn_folder(folder: str) -> str:
    """Return the normalised folder hint, or raise SpawnFolderRefused."""
    hint = folder.strip()
    if not hint:
        return ""
    if len(hint) > MAX_SPAWN_FOLDER_CHARS:
        raise SpawnFolderRefused(
            f"folder hint is longer than {MAX_SPAWN_FOLDER_CHARS} characters")
    if hint.startswith("/") or hint.startswith("~") or "\\" in hint:
        raise SpawnFolderRefused(
            "folder is a hint relative to the runner's own root, never an "
            f"absolute path: drop the leading '{hint[0]}'")
    if re.match(r"^[A-Za-z]:", hint):
        raise SpawnFolderRefused(
            "folder is a hint relative to the runner's own root, never a "
            "drive-absolute path")
    if ".." in hint.split("/"):
        raise SpawnFolderRefused(
            "folder may not contain '..' — it is resolved inside the runner's "
            "root and may not escape it")
    return hint.strip("/")


class SpawnRequest(BaseModel):
    """One recorded intent to start a seat, and everything the runner needs.

    `machine` is required and defaults to "local" from day one even though v1
    has exactly one runner. It is the v2 seam: adding a routing dimension later
    means changing every caller, while adding a registry behind a field that
    already exists changes nobody. Per ADR-0001 the routing target is the
    RUNNER'S OWN SEAT ID, never a hostname — the hub still never parses `@`.
    """

    id: str
    machine: str = "local"
    seat_id: str
    mission: str = ""
    harness: str = ""
    #: Relative to the runner's root; "" means `<root>/<seat_id>`. Validated by
    #: `validate_spawn_folder` at the door — the hub never resolves it.
    folder: str = ""
    channels: list[str] = Field(default_factory=list)
    #: Runner-side knobs (permission floor, about, …). Free-form on purpose:
    #: the runner owns policy and a hub-side enum would make every new knob a
    #: hub release.
    options: dict[str, Any] = Field(default_factory=dict)
    #: The vendor model this seat drives with; "" means the harness resolves
    #: its own. FIRST-CLASS rather than an `options` key (agora-wui #249,
    #: #275): a free-form dict made each client hardcode the key name, and
    #: worse, `options` reaches the runner and the runner reads only
    #: `permissions` out of it — so a model passed that way was accepted,
    #: stored, echoed back on this row, and never given to `agora drive`.
    #: A confirmation that lies is worse than an omission that is silent.
    model: str = ""
    #: The reasoning effort, validated AT THE DOOR against the vocabulary the
    #: runner announced for this harness — see `capabilities` on /machines.
    #: The hub owns no vocabulary of its own here: an unknown level is refused
    #: because the MACHINE said it cannot express it, never because the hub
    #: holds a list.
    reasoning: str = ""
    state: SpawnState = SpawnState.pending
    #: The runner's OWN sentence about this row's state, rendered verbatim by
    #: every client — so a refusal names itself instead of the operator having
    #: to go and read a log on another machine.
    detail: str = ""
    requested_by: str = ""
    claimed_by: str = ""
    #: The PUBLIC id of the single-use join token minted for this request. The
    #: hub never holds the secret (the runner receives it once, at claim), and
    #: the public id is what makes revoke-on-failure possible.
    join_token_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    claimed_at: float | None = None
    #: An operator asked for this seat to stop. It is a REQUEST, not a state:
    #: only the runner can end a process, so the hub records the intent and the
    #: runner moves the row to `stopped`. A row that never reaches `stopped`
    #: with this set is a runner that is not listening — which is visible,
    #: where a hub-side lie about it would not be.
    stop_requested_at: float | None = None


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
