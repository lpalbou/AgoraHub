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
    answered: list[str] = field(default_factory=list)  # discharged ask ids
    declined: list[str] = field(default_factory=list)  # discharged by REFUSAL:
    #                                                    a subset of `answered`
    #                                                    that nobody actually
    #                                                    answered (0153)
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


def declines_of(message: Message) -> list[str]:
    """The ask ids this reply DECLINED — refused rather than answered (0153).

    Always a subset of `answers`: the hub folds a decline into the discharge
    set at post time, so a refusal clears its row exactly as an answer does.
    What this field buys is that the record can say which of the two happened
    — a digest crediting a refuser under `decided` records the opposite of
    what occurred."""
    dec = (message.data or {}).get("declines")
    return [str(d) for d in dec] if isinstance(dec, list) else []


def substantive_answers_of(message: Message) -> list[str]:
    """The ask ids this reply actually ANSWERED — its discharges minus its
    refusals. The set every credit- and consumption-side surface wants: a
    decline discharges, but there is nothing in it to adopt or reject."""
    declined = set(declines_of(message))
    return [a for a in _answers_of(message) if a not in declined]


def _closes(parent: Message, reply: Message, operators: frozenset[str],
            delegates: frozenset[str] = frozenset(),
            closure_rule_epoch: float = 0.0,
            rulers: frozenset[str] = frozenset()) -> bool:
    """Does this resolved reply carry closure AUTHORITY (ADR-0003)?

    THE ASKER, ALWAYS — their own question, closed in the open. This is also
    how every hub self-closer works (`_steward_sweep`, `_phase_sweep`, the
    fleet and silence closers): the hub closes rows it authored itself, so
    `hub` needs no standing in `operators` and must not be given any.

    THE OPERATOR, ALWAYS.

    A RULING DELEGATE, always, in the channels its grant reaches — a seat the
    operator appointed to sign off in scope closes what it signs off on. The
    caller supplies this set already scoped, so an unscoped grant reaches
    nothing and a grant scoped elsewhere does not leak here.

    THE REPORTING DELEGATE, on an OPERATOR's request, with a `settled_by`
    pointer AND cited evidence — the 2026-08-01 ruling that something must be
    able to settle a commission whose operator has gone quiet, at the price of
    a completion report.

    NOBODY ELSE (operator ruling, 2026-08-22). `settled_by` used to be an
    unconditional third-party master key: any member could retire any thread
    by pointing a `resolved` reply at another message. It was already refused
    on an operator's request (2026-08-06); this extends the same rule to
    peers, because a question is the asker's to close and a bystander cannot
    know whether it was answered.

    ...but SEMANTICS CHANGES MUST NOT REWRITE THE PAST. Threads closed under
    the old rule stay closed: `closure_rule_epoch` judges only replies posted
    after it. Without that guard, tightening this reopens every third-party
    closure in the hub's history AT ONCE, each one instantly SLA-breached —
    the 132-message storm the sibling epochs in this file were built for."""
    if reply.status.value != "resolved":
        return False
    if reply.sender == parent.sender or reply.sender in operators:
        return True
    if reply.sender in rulers:
        return True
    if not (reply.data or {}).get("settled_by"):
        return False
    if closure_rule_epoch and reply.created_at < closure_rule_epoch:
        return True         # closed under the old rule; it stays closed
    return (reply.sender in delegates
            and parent.sender in operators
            and _cites_evidence(reply))


def closed_authoritatively(parent: Message, replies: list[Message],
                           operators: frozenset[str] = frozenset(),
                           delegates: frozenset[str] = frozenset(),
                           closure_rule_epoch: float = 0.0,
                           rulers: frozenset[str] = frozenset()) -> bool:
    """True when someone with closure authority resolved the thread (ADR-0003)
    — distinct from mere discharge: a fully-answered question whose asker
    stays silent is discharged but NOT authoritatively closed, and that gap
    is exactly where the asker's consumption debt (0078) lives."""
    return any(_closes(parent, r, operators, delegates, closure_rule_epoch,
                       rulers)
               for r in replies)


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
                    peer_addressed_rule_epoch: float | None = None,
                    closure_rule_epoch: float = 0.0,
                    rulers: frozenset[str] = frozenset()) -> DischargeState:
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
    closed_by_resolve = any(
        _closes(parent, r, operators, delegates, closure_rule_epoch, rulers)
        for r in replies)
    named = set(parent.to) | ask_addressees(parent)

    def _operator_settled() -> bool:
        """The operator's own word, or a CITED COMPLETION REPORT from a seat
        the request is actually on: the reporting delegate, or a seat the
        operator NAMED.

        The addressee's door was missing, and the hole it left was reported
        by three seats independently: an operator posts `open`, the named
        seat answers in-thread, and the row stays and escalates against the
        one seat that did the work. Every exit was shut — `answers=[...]`
        needs ask ids that an ask-less commission does not have, and
        `settled_by` is refused on an operator's request. The hub's own
        advice was "Answer it", and answering was exactly what did not clear
        it, so a seat that delivered was indistinguishable from a seat that
        ignored the human.

        The 2026-08-04 lesson is kept whole: a bare "on it" still settles
        nothing. What clears the row is a `resolved` reply that CITES what
        was delivered — the same price the delegate pays, asked of the same
        kind of claim. An UNADDRESSED commission still has no addressee to
        pay it, so it stays the operator's to close, which is the case that
        rule was written for."""
        return any(
            r.sender in operators
            or (r.status.value == "resolved" and _cites_evidence(r)
                and (r.sender in delegates or r.sender in named))
            for r in replies)

    asks = asks_of(parent)
    if not asks:
        if is_operator_ask(parent, operators) and not pre_epoch:
            discharged = _operator_settled()
        elif parent.kind.value == "system" and parent.to:
            # THE HUB CANNOT SPEAK FOR ITSELF (2026-08-13). A hub-authored
            # alert — "YOU ARE THE BLOCKER", "CLAIMS DUE", "AGENT DARK",
            # "STALE CLAIMS" — is an ask-less ADDRESSED open whose sender is
            # `hub`, so the peer-addressed rule below pinned it FOREVER: the
            # only remaining exit is an authoritative close, and over a
            # `hub` message only `hub` itself or an operator holds that
            # authority. The addressee doing exactly what the alert demands
            # moved nothing.
            #
            # Measured (rtype-v3, 2026-08-12): rtype-build#72/#76/#86 drew
            # twelve replies between them — including two `resolved` from
            # the named seat and one from the delegate — and all three stood
            # open on their addressees to the end of the run; on
            # hub-alerts#7 the delegate posted the same answer twice
            # ("evidently my reply did not clear it"). Answering the hub
            # twice is pure waste: nothing reads it, and the hub already
            # closes its own alerts when the underlying condition ends.
            # That bookkeeping (`_standing_hub_alerts`,
            # `_standing_claim_pings`) keys on CLOSURE, not discharge, so
            # clearing the seat's ledger here cannot double-announce an
            # alert or break its supersession.
            #
            # An ADDRESSEE must be the one who speaks: a bystander's reply
            # does not discharge a machine-routed alert naming someone else.
            # KNOWN GAP: discharge is global, so on a multi-addressee alert
            # (`_steward_sweep`/`_phase_sweep` post to=stewards) one
            # steward's reply clears the row for the others. Per-addressee
            # release for system parents belongs in `owed()`, where the
            # directive path already does it.
            discharged = any(r.sender in set(parent.to) for r in non_sender)
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
    # Substance, tracked alongside discharge (0153). A decline discharges its
    # ask — that is deliberate and the WUI depends on it — so it belongs in
    # `answered_ids`; but an ask NOBODY answered substantively is a different
    # fact from an ask that was answered, and the surfaces that credit or
    # consume need to tell them apart.
    substantive_ids: set[str] = set()
    for r in non_sender:
        got = _answers_of(r)
        answered_ids.update(got)
        substantive_ids.update(substantive_answers_of(r))
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
    # An ask counts as declined only when NO reply answered it substantively:
    # on a multi-addressee canvass where one seat answers and another
    # declines, the ask was answered.
    declined = [i for i in answered if i not in substantive_ids]
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
                          declined=declined, discharged=asks_settled,
                          closed=asks_settled or closed_by_resolve,
                          has_resolved_reply=has_resolved)
