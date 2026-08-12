"""Obligation discharge and closure: is an open/blocked message settled yet?

Two DISCHARGE modes, chosen by the message itself:

- **binary** (legacy / no structured asks): an UNaddressed peer thread keeps
  the cheap original rule — any reply from someone other than the asker
  discharges it. Addressed work asks and operator asks are tighter: "on it"
  is not completion.
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


def _cites_evidence(reply: Message) -> bool:
    """Does this completion report POINT at what it delivered?

    The hub stamped and resolved these refs at post time
    (`HubService._validate_evidence`), so their presence means every citation
    exists in this channel — never that the prose above them is true. This is
    the difference between a report a reader can check and one they cannot:
    on 2026-08-04 a delegate closed a novel commission with "5.1MB, 3
    embedded images ... Channel filesystem: /path/to/novel", where the
    channel filesystem held no such file, and nothing could tell that from a
    real delivery.

    Deliberately NOT checked: whether the cited artifact is the RIGHT one. A
    delegate can cite a real but irrelevant version, and judging relevance is
    the mind-reading gate the operator principle forbids. The bounded win is
    that the claim becomes attributable, hub-timestamped and cheap to
    falsify."""
    refs = (reply.data or {}).get("evidence")
    if not isinstance(refs, list) or not refs:
        return False
    # AT LEAST ONE CITATION THE HUB COULD ACTUALLY RESOLVE (2026-08-06).
    # `_validate_evidence` goes to real trouble to refuse to claim it saw
    # bytes it cannot see: an `external` ref is shape-checked and stamped
    # `verified: false`. This gate then threw that distinction away, so a
    # fabricated sha256 over a path that does not exist discharged an
    # operator commission. A report whose every citation is unverifiable is
    # exactly the report a reader cannot check — the thing this function
    # exists to require.
    #
    # Still deliberately NOT checked: whether the artifact is the RIGHT one.
    # That is the mind-reading gate the operator principle forbids.
    return any(not isinstance(r, dict) or r.get("verified") is not False
               for r in refs)


def is_operator_ask(parent: Message,
                    operators: frozenset[str]) -> bool:
    """Any operator-authored message — the shape whose binary discharge is
    tightened (see discharge_state). Until 2026-08-04 this required
    `not parent.to`: ADDRESSING a seat forfeited the protection, so the one
    message most worth protecting — a commission naming its delegate — was
    closed hub-wide by the first bystander reply (scifi-novel#40, 67s)."""
    return parent.sender in operators


def discharge_state(parent: Message, replies: list[Message],
                    operators: frozenset[str] = frozenset(),
                    delegates: frozenset[str] = frozenset(),
                    operator_rule_epoch: float = 0.0,
                    operator_asks_rule_epoch: float = 0.0,
                    canvass_rule_epoch: float = 0.0,
                    peer_addressed_rule_epoch: float | None = None) -> DischargeState:
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
    per-ask, which is the shape the operator should prefer anyway.

    ADDRESSED IS NOT WEAKER (2026-08-04). The tightening above originally
    keyed on operator messages that named NOBODY, so an operator commission
    that DID name its delegate fell back to any-reply discharge: a
    bystander's reply 67 seconds in closed the novel commission
    (scifi-novel#40) on every ledger, and the delegate owed nothing for the
    17.5h stall that followed. The operator rule now covers every ask-less
    operator message, addressed or not."""
    # SEMANTICS CHANGES MUST NOT REWRITE THE PAST (the `_directive_epoch`
    # discipline, applied here 2026-08-04). Tightening the operator rule
    # re-opened 132 ask-less operator messages that were discharged under
    # the old rule — every one instantly SLA-breached, on 23 seats, some 19
    # days old. A message settled under the rule in force when it was
    # written stays settled; only messages posted after the epoch are judged
    # by the new rule.
    pre_epoch = bool(operator_rule_epoch) and parent.created_at < operator_rule_epoch
    # Its OWN epoch. Reusing `operator_rule_epoch` would re-judge every
    # ask-carrying operator message settled since 2026-08-04 under a rule
    # that did not exist when it was written — the 132-message storm these
    # guards were built to prevent.
    pre_asks_epoch = (bool(operator_asks_rule_epoch)
                      and parent.created_at < operator_asks_rule_epoch)
    pre_canvass_epoch = (bool(canvass_rule_epoch)
                         and parent.created_at < canvass_rule_epoch)
    pre_peer_addressed_epoch = (
        peer_addressed_rule_epoch is not None
        and parent.created_at < peer_addressed_rule_epoch
    )
    non_sender = [r for r in replies if r.sender != parent.sender]
    has_resolved = any(r.status.value == "resolved" for r in replies)
    closed_by_resolve = any(_closes(parent, r, operators) for r in replies)
    def _operator_settled() -> bool:
        """Only the operator's own word, or the reporting delegate's cited
        completion report, settles an operator's request."""
        return any(
            r.sender in operators
            or (r.sender in delegates and r.status.value == "resolved"
                and _cites_evidence(r))
            for r in replies)

    asks = asks_of(parent)
    if not asks:
        if is_operator_ask(parent, operators) and not pre_epoch:
            discharged = _operator_settled()
        elif parent.sender not in operators and parent.to and not pre_peer_addressed_epoch:
            # A PEER'S ADDRESSED WORK ASK IS NOT CLOSED BY "ON IT"
            # (2026-08-11). The delivery contract the fleet is taught is:
            # keep the ask open until the seat either reports completion
            # (`resolved`) or materializes ownership with a linked claim row.
            # Discharge cannot see claim rows, so its bounded job is only to
            # refuse the old lie that a bare non-sender reply means the work
            # is done. The addressee's own /owed row may still clear once a
            # linked claim exists; the thread itself stays open until an
            # authoritative close.
            discharged = False
        else:
            discharged = bool(non_sender)
        return DischargeState(mode="binary", discharged=discharged,
                              closed=discharged or closed_by_resolve,
                              has_resolved_reply=has_resolved)
    # A ROLL CALL IS NOT ANSWERED BY THE FIRST VOTER (2026-08-06).
    #
    # This loop had no addressee check: an ask carrying `to=[a,b,c]` was
    # fully answered the instant ANYONE replied with that id — including a
    # seat it never named. The silent addressees were then unpinned, dropped
    # from /owed, erased from the asker's `waiting_on`, and their envelope
    # `to_me` flipped back to false. Another seat's reply did what the
    # addressee's own bare read is explicitly forbidden to do.
    #
    # Measured on this hub: 9 of 28 multi-addressee asks discharged with at
    # least one named seat silent. `owed()` already implements the correct
    # per-addressee rule twice for directive debts ("another addressee's
    # reply never clears YOUR debt"); `waiting_on` already tracks per
    # addressee. Only discharge disagreed — and discharge gates `closed`,
    # so discharge won.
    #
    # The hub authors nothing here: the addressee list is data the ASKER
    # typed. Want one answer from a group? Name one seat. An ask with no
    # `to` keeps the any-non-sender rule exactly as before.
    answered_ids: set[str] = set()
    by_sender: dict[str, set[str]] = {}
    for r in non_sender:
        got = _answers_of(r)
        answered_ids.update(got)
        if got:
            by_sender.setdefault(r.sender, set()).update(got)
    ids = [str(a["id"]) for a in asks]

    def _ask_answered(ask: dict) -> bool:
        aid = str(ask["id"])
        if aid not in answered_ids:
            return False
        named = [str(x) for x in (ask.get("to") or [])]
        if not named or pre_canvass_epoch:
            return True
        return all(aid in by_sender.get(seat, set()) for seat in named)

    pending = [str(a["id"]) for a in asks if not _ask_answered(a)]
    answered = [i for i in ids if i not in pending]
    # ANSWERING THE QUESTIONS IS NOT DOING THE WORK (2026-08-06).
    #
    # `rtype-open#10` was an operator commission: a build brief worth hours
    # in the BODY, plus three structured asks — "who takes what?", "what
    # could make this fail?", "what will you show me as proof?". Three seats
    # answered one ask each inside 20 minutes. `discharged = not pending`
    # then read the WHOLE commission as settled: zero /owed rows, zero
    # doctor rows, board empty, digest "decided". The game was never built.
    #
    # The perverse part: the ask-less operator rule above is strict, so
    # ADDING structure to a commission — the shape this module recommends —
    # strictly WEAKENED it. Doctrine and mechanism pointed opposite ways.
    #
    # So the asks branch answers a narrower question than it used to.
    # `pending`/`answered` keep their exact meaning, which is what releases
    # a seat's pin once it has answered its own row — nobody is re-nagged
    # for a canvass they completed. What changes is that on an OPERATOR's
    # message, clearing the asks no longer clears the instruction. That
    # still takes the operator's word or the delegate's cited report.
    asks_settled = not pending
    if asks_settled and is_operator_ask(parent, operators) and not pre_asks_epoch:
        asks_settled = _operator_settled()
    return DischargeState(mode="asks", pending=pending, answered=answered,
                          discharged=asks_settled,
                          closed=asks_settled or closed_by_resolve,
                          has_resolved_reply=has_resolved)
