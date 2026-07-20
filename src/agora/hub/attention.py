"""Attention policy: what gets delivered, inlined, escalated, and downgraded.

Distilled from the v0.2 adversarial design review (docs/KnowledgeBase.md):

- Importance is DERIVED, never sender-declared: obligations (status),
  addressing (to_me/reply_to_me, hub-computed), authority (critical).
  A free-form priority field was rejected — sender-declared severity decays
  to noise between LLMs (severity inflation) and doubles the spoof surface.
- Body inlining follows the token economics: an envelope-then-fetch round
  trip costs more than a small body, so small bodies are always inlined;
  only large, low-urgency, non-addressed bodies are envelope-only.
- Obligations must not rot: unanswered open/blocked messages older than the
  channel SLA are escalated by the hub (a disinterested party raising
  urgency by obligation AGE — the anti-inflation mechanism).
- Interrupts cost budget: over-budget interrupts are downgraded to
  next_turn and visibly marked, so crying wolf has a price.
"""

from __future__ import annotations

import time

from ..models import (
    ADDRESSED_INLINE_BYTES,
    INLINE_BODY_BYTES,
    Envelope,
    Message,
    Status,
    Urgency,
)
from .obligations import asks_of, pending_addressees

DEFAULT_RESPONSE_SLA_MINUTES = 60.0


class SlidingWindowBudget:
    """Per-agent cap on expensive signals (interrupts, criticals) per hour."""

    def __init__(self, max_per_hour: int, window_seconds: float = 3600.0) -> None:
        self.max_per_hour = max_per_hour
        self._window = window_seconds
        self._events: dict[str, list[float]] = {}

    def allow(self, agent_id: str) -> bool:
        now = time.time()
        events = [t for t in self._events.get(agent_id, []) if now - t < self._window]
        if len(events) >= self.max_per_hour:
            self._events[agent_id] = events
            return False
        events.append(now)
        self._events[agent_id] = events
        return True


class AttentionPolicy:
    """Computes viewer-specific envelopes from stored messages."""

    def envelope_for(self, viewer_id: str, message: Message, *,
                     parent_sender: str | None, has_reply: bool,
                     pending_asks: list[str] | None = None, ask_total: int = 0,
                     has_resolved_reply: bool = False,
                     sla_minutes: float = DEFAULT_RESPONSE_SLA_MINUTES,
                     paused_seconds: float = 0.0,
                     already_read: bool = False,
                     owes_reply: bool = False) -> Envelope:
        # `has_reply` here means "obligation CLOSED" (discharged, or an
        # authoritative resolved reply — ADR-0003): for a structured-asks
        # message a partial answer keeps it escalating/pinned, while a proper
        # closure stops escalation on the spot. `has_resolved_reply` is the
        # reader's context signal ("this thread carries a resolved reply —
        # check before answering"), independent of whether it closed.
        pending = pending_asks or []
        # to_me: message-level `to` is the sender's word and stays for the
        # message's life; the ask-derived half (0077) is scoped to asks STILL
        # PENDING — the nine-seat debrief showed a to-you flag that outlives
        # the viewer's own discharged ask lies ("you owe" vs "others owe"),
        # and seats re-verified their own discharges for hours because of it.
        yours = pending_addressees(message, pending)
        to_me = viewer_id in message.to or viewer_id in yours
        reply_to_me = parent_sender == viewer_id if parent_sender else False
        body_bytes = len(message.body.encode())
        # Re-delivery suppression (debrief friction 1, unanimous): a pinned
        # obligation the viewer already READ re-surfaces headline-only —
        # full bodies were re-sent whole dozens of times per night per seat.
        # read_message re-fetches on demand; nothing is lost but repetition.
        inline = (not already_read
                  and self._should_inline(message, to_me, reply_to_me, body_bytes))
        effective, escalated = self._effective_urgency(
            message, viewer_id, has_reply, sla_minutes, paused_seconds,
            owes_reply)
        answered = max(ask_total - len(pending), 0)
        return Envelope(
            id=message.id, channel=message.channel, seq=message.seq,
            sender=message.sender, kind=message.kind, status=message.status,
            urgency=message.urgency, effective_urgency=effective, escalated=escalated,
            downgraded=message.downgraded, critical=message.critical,
            to_me=to_me, reply_to_me=reply_to_me, title=message.title,
            body_bytes=body_bytes,
            body=message.body if inline else None,
            data=message.data if inline else None,
            reply_to=message.reply_to,
            retracted=message.retracted,
            pending_asks=pending,
            # Attachment REFS always ride the envelope, inlined body or not —
            # a recipient must learn files exist from the headline (0091);
            # the bytes stay behind the membership-gated fetch.
            attachments=list((message.data or {}).get("attachments") or []),
            your_pending_asks=sorted(
                str(a["id"]) for a in asks_of(message)
                if viewer_id in (a.get("to") or []) and str(a["id"]) in set(pending)),
            ask_progress=f"{answered}/{ask_total}" if ask_total else "",
            has_resolved_reply=has_resolved_reply,
            redelivery=already_read,
            # Reserved authorship shape (present on every envelope so consumers
            # can bind to it now); echo the sender's token, attest nothing yet.
            signature=(message.data or {}).get("signature"),
            verified_by=None,
            created_at=message.created_at,
        )

    @staticmethod
    def _should_inline(message: Message, to_me: bool, reply_to_me: bool,
                       body_bytes: int) -> bool:
        if message.critical:
            return True  # forced attention includes the content, always
        if (to_me or reply_to_me) and body_bytes <= ADDRESSED_INLINE_BYTES:
            return True  # addressed to you: the read decision is near-certain
        return body_bytes <= INLINE_BODY_BYTES  # small body: fetch would cost more

    @staticmethod
    def _effective_urgency(message: Message, viewer_id: str, has_reply: bool,
                           sla_minutes: float,
                           paused_seconds: float = 0.0,
                           owes_reply: bool = False) -> tuple[Urgency, bool]:
        if message.critical:
            return Urgency.interrupt, False
        # `paused_seconds` excludes operator-pause time from the obligation's
        # age: the SLA clock measures time the fleet could actually respond.
        # `owes_reply` (0102) is the service's viewer-specific verdict that
        # this reply/fyi is an addressed directive debt the viewer has not
        # engaged — it rots on the same clock as an unanswered question.
        rotting = (
            owes_reply
            or (message.status in (Status.open, Status.blocked)
                and message.sender != viewer_id
                and not has_reply)
        ) and (time.time() - message.created_at - paused_seconds) > sla_minutes * 60.0
        if rotting and message.urgency != Urgency.interrupt:
            return Urgency.interrupt, True
        return message.urgency, False
