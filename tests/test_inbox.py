"""Tests of the client-side interleaving primitive (selective receive)."""

from __future__ import annotations

import time

from agora.client.inbox import Inbox
from agora.models import Envelope, Kind, Status, Urgency


def make_envelope(seq: int, urgency: Urgency = Urgency.inbox,
                  critical: bool = False) -> Envelope:
    return Envelope(id=f"m{seq}", channel="design", seq=seq, sender="alice",
                    kind=Kind.message, status=Status.fyi,
                    urgency=urgency, effective_urgency=urgency, critical=critical,
                    body=f"msg {seq}", body_bytes=6, created_at=time.time())


def test_drain_is_non_blocking_and_ordered():
    inbox = Inbox()
    assert inbox.drain() == []
    for seq in (1, 2, 3):
        inbox.deliver(make_envelope(seq))
    assert [e.seq for e in inbox.drain()] == [1, 2, 3]
    assert inbox.drain() == []


def test_interrupt_flag_lifecycle():
    inbox = Inbox()
    inbox.deliver(make_envelope(1))
    assert not inbox.has_interrupt          # ordinary envelopes don't raise the flag
    inbox.deliver(make_envelope(2, Urgency.interrupt))
    assert inbox.has_interrupt              # cheap mid-work check
    inbox.drain()
    assert not inbox.has_interrupt          # consumed together with the envelopes


def test_critical_raises_interrupt_flag():
    inbox = Inbox()
    inbox.deliver(make_envelope(1, Urgency.inbox, critical=True))
    assert inbox.has_interrupt


async def test_wait_returns_batch():
    inbox = Inbox()
    inbox.deliver(make_envelope(1))
    inbox.deliver(make_envelope(2))
    envelopes = await inbox.wait(timeout=0.5)
    assert [e.seq for e in envelopes] == [1, 2]


async def test_wait_times_out():
    inbox = Inbox()
    assert await inbox.wait(timeout=0.05) == []
