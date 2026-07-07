"""Inbox: the interleaving primitive on the receiving side.

This is deliberately the actor-model "selective receive" pattern (Erlang's
50-year-old answer, and how Codex-style mid-run steering works internally):
the agent is never interrupted; envelopes accumulate here, and the agent's
loop *chooses* its receive points — typically once per iteration, between
tool calls. `drain()` is non-blocking (the mid-work check); `wait()` blocks
(the idle stance).

Since v0.2 the inbox holds ENVELOPES (headlines, with bodies inlined only
when small/addressed/critical): the agent triages by headline and fetches
bodies deliberately via `AgoraClient.read()` — genuine communication
without force-fed noise.
"""

from __future__ import annotations

import asyncio

from ..models import Envelope, Urgency


class Inbox:
    def __init__(self, maxsize: int = 1000) -> None:
        self._queue: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=maxsize)
        self._interrupt_flag = asyncio.Event()

    def deliver(self, envelope: Envelope) -> None:
        if envelope.critical or envelope.effective_urgency == Urgency.interrupt:
            self._interrupt_flag.set()
        try:
            self._queue.put_nowait(envelope)
        except asyncio.QueueFull:
            pass  # backlog recoverable via cursors on the hub

    def drain(self) -> list[Envelope]:
        """Non-blocking: everything received so far. Call at loop boundaries."""
        items: list[Envelope] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                self._interrupt_flag.clear()
                return items

    async def wait(self, timeout: float | None = None) -> list[Envelope]:
        """Blocking: wait for at least one envelope, then drain the rest."""
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return []
        return [first, *self.drain()]

    @property
    def has_interrupt(self) -> bool:
        """Cheap mid-work check: did anything interrupt-worthy arrive?"""
        return self._interrupt_flag.is_set()

    def __len__(self) -> int:
        return self._queue.qsize()
