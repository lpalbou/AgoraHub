"""Obligation discharge and closure: is an open/blocked message settled yet?

Two DISCHARGE modes, chosen by the message itself:

- **binary** (legacy / no structured asks): any reply from someone other than
  the asker discharges the obligation. This is the original behavior and is
  preserved exactly for messages that carry no `asks`.
- **asks** (structured): the message carries numbered `asks` (stored in
  `data.asks`); a reply discharges specific ones by listing their ids in its
  `data.answers`. The obligation is discharged only when EVERY ask has a
  matching answer from a non-sender reply — so a reply that answers 1 of 3
  no longer silently clears the whole message (the partial-answer rot the
  file protocol suffered). This is the agents' unanimous top request, made
  mechanical: importance follows unanswered asks, not a sender's say-so.

CLOSURE (backlog 0062, ADR-0003) is the second, orthogonal way a thread
settles: a `resolved`-status reply closes the obligation on EVERY surface
(inbox stickiness, escalation, digest) when its author has the authority to
close — the ASKER (closing your own question is loud, attributed and
in-thread, unlike the silent self-answering the non-sender rule exists to
prevent), an OPERATOR, or ANY member whose resolved reply carries a
`settled_by` pointer naming the message that settled the question (the
audited supersession path for rulings that landed outside the thread — the
c713/c726 incident class). A third party's bare resolved reply deliberately
does NOT close: closure by strangers needs the pointer's audit trail.

Pure functions over already-loaded messages, so they are trivially testable and
carry no transport or storage concerns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Message


@dataclass
class DischargeState:
    mode: str = "binary"                       # "binary" | "asks"
    pending: list[str] = field(default_factory=list)   # unanswered ask ids
    answered: list[str] = field(default_factory=list)  # answered ask ids
    discharged: bool = False                   # obligation fully satisfied?
    closed: bool = False                       # discharged OR authoritatively resolved
    has_resolved_reply: bool = False           # any resolved reply exists (reader signal)

    @property
    def total(self) -> int:
        return len(self.pending) + len(self.answered)

    @property
    def progress(self) -> str:
        """Human/agent-scannable 'answered/total', e.g. '1/3'. Empty in binary
        mode (no structured asks to count)."""
        return f"{len(self.answered)}/{self.total}" if self.mode == "asks" else ""


def asks_of(message: Message) -> list[dict]:
    """The structured asks declared on a message (empty if none/malformed)."""
    asks = (message.data or {}).get("asks")
    if not isinstance(asks, list):
        return []
    return [a for a in asks if isinstance(a, dict) and a.get("id") is not None]


def ask_addressees(message: Message) -> set[str]:
    """Every seat named by a per-ask `to` (0077) or `assignee`. Naming a seat
    inside an ask must flag that seat mechanically — the lurker incident's
    miss B was asks naming seats only in prose, which flags nobody (70
    occurrences in 48h). `assignee` counts too (storm review, 2026-07-28):
    it already creates owed debt, so leaving it out of addressing produced
    messages that obliged one seat while waking the whole room."""
    out: set[str] = set()
    for a in asks_of(message):
        out.update(str(x) for x in (a.get("to") or []))
        if a.get("assignee"):
            out.add(str(a["assignee"]))
    return out


def pending_addressees(message: Message, pending: list[str]) -> set[str]:
    """Seats named by an ask that is still UNANSWERED — the per-ask pin scope:
    a seat whose canvass row was answered stops being pinned even while other
    rows stay open."""
    pend = set(pending)
    out: set[str] = set()
    for a in asks_of(message):
        if str(a.get("id")) in pend:
            out.update(str(x) for x in (a.get("to") or []))
            if a.get("assignee"):
                out.add(str(a["assignee"]))
    return out


def _answers_of(message: Message) -> list[str]:
    ans = (message.data or {}).get("answers")
    return [str(a) for a in ans] if isinstance(ans, list) else []


def _closes(parent: Message, reply: Message, operators: frozenset[str]) -> bool:
    """Does this resolved reply carry closure AUTHORITY (ADR-0003)?
    Asker: always (their own question, closed in the open). Operator: always.
    Anyone else: only with a `settled_by` supersession pointer (validated at
    post time to name a real message in the channel)."""
    if reply.status.value != "resolved":
        return False
    if reply.sender == parent.sender or reply.sender in operators:
        return True
    return bool((reply.data or {}).get("settled_by"))


def closed_authoritatively(parent: Message, replies: list[Message],
                           operators: frozenset[str] = frozenset()) -> bool:
    """True when someone with closure authority resolved the thread (ADR-0003)
    — distinct from mere discharge: a fully-answered question whose asker
    stays silent is discharged but NOT authoritatively closed, and that gap
    is exactly where the asker's consumption debt (0078) lives."""
    return any(_closes(parent, r, operators) for r in replies)


def is_operator_broadcast(parent: Message,
                          operators: frozenset[str]) -> bool:
    """An operator message that names NOBODY — the shape whose binary
    discharge had to be tightened (see discharge_state)."""
    return bool(parent.sender in operators and not parent.to)


def discharge_state(parent: Message, replies: list[Message],
                    operators: frozenset[str] = frozenset(),
                    delegates: frozenset[str] = frozenset()) -> DischargeState:
    """Compute whether `parent`'s obligation is discharged and/or closed.

    A reply from the asker itself never DISCHARGES the asker's own obligation
    (you cannot quietly answer your own question to silence it) — but the
    asker's `resolved` reply CLOSES it: closure is a loud, attributed,
    in-thread act, re-openable by anyone posting a new ask. `operators` is
    the set of operator agent ids (their resolved replies also close);
    `delegates` is the set of reporting delegates (see below).

    THE 75-SECOND DISCHARGE (live, 2026-08-01). An unstructured open is
    "binary": historically ANY non-sender reply discharged and closed it.
    That is right for a peer's one-question thread and catastrophic for a
    human's broadcast: the operator posted at-test#382 carrying FIVE
    requirements, one seat replied to part of it 75 seconds later, and the
    thread was closed — no pending asks, nothing escalating, four
    requirements silently abandoned. An operator broadcast is therefore
    discharged only by the OPERATOR (any reply of theirs — they are the one
    who can say "that is what I meant") or by a reporting DELEGATE posting
    `resolved`, which is the delegate asserting end-to-end completion under
    the 2026-08-01 ruling. A bystander's partial answer no longer speaks for
    the human. Structured asks are unaffected: they already discharge
    per-ask, which is the shape the operator should prefer anyway."""
    non_sender = [r for r in replies if r.sender != parent.sender]
    has_resolved = any(r.status.value == "resolved" for r in replies)
    closed_by_resolve = any(_closes(parent, r, operators) for r in replies)
    asks = asks_of(parent)
    if not asks:
        if is_operator_broadcast(parent, operators):
            discharged = any(
                r.sender in operators
                or (r.sender in delegates and r.status.value == "resolved")
                for r in replies)
        else:
            discharged = bool(non_sender)
        return DischargeState(mode="binary", discharged=discharged,
                              closed=discharged or closed_by_resolve,
                              has_resolved_reply=has_resolved)
    answered_ids: set[str] = set()
    for r in non_sender:
        answered_ids.update(_answers_of(r))
    ids = [str(a["id"]) for a in asks]
    pending = [i for i in ids if i not in answered_ids]
    answered = [i for i in ids if i in answered_ids]
    return DischargeState(mode="asks", pending=pending, answered=answered,
                          discharged=not pending,
                          closed=not pending or closed_by_resolve,
                          has_resolved_reply=has_resolved)
