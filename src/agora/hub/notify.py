"""In-process wake-up primitives for push delivery.

Two complementary mechanisms:

- `FanOut`: per-channel subscriber queues for live WebSocket connections
  (lowest latency; a connected client receives every message as it lands).
- `Notifier`: a global "something happened" event for long-pollers (the
  `/inbox?wait=` endpoint). Waiters grab the current event, re-check their
  filter, and sleep on it; every post replaces and fires the event. At
  local-first scale a global event with re-check is simpler and strictly
  correct (no lost wake-ups) compared to per-channel condition juggling.

THREAD SAFETY (v0.3.1 fix): Starlette runs sync route handlers in worker
threads, but `asyncio.Event`/`asyncio.Queue` are bound to the event loop and
are not thread-safe. Both primitives therefore hold a reference to the
serving loop and marshal every mutation onto it via `call_soon_threadsafe`.
When no loop is bound (pure synchronous unit tests, no async waiter exists),
the mutation runs inline — safe because there is no cross-thread waiter.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class LoopBinder:
    """Shared holder for the event loop that async consumers run on.

    Async entry points (the WebSocket endpoint, the long-poll waiter) call
    `bind` with their running loop; synchronous producers (REST handlers on
    worker threads) then schedule wake-ups onto it thread-safely.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def run(self, fn) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(fn)
                return
            except RuntimeError:
                pass  # loop closed underneath us; fall back to inline
        fn()


class Notifier:
    def __init__(self, binder: LoopBinder | None = None) -> None:
        self._event = asyncio.Event()
        self._binder = binder or LoopBinder()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._binder.bind(loop)

    def snapshot(self) -> asyncio.Event:
        """Grab the current event BEFORE checking state, to avoid lost wake-ups."""
        return self._event

    def notify(self) -> None:
        # Swap in a fresh event and fire the old one, on the serving loop so
        # that waiters (which live on that loop) are woken deterministically.
        def _flip() -> None:
            event, self._event = self._event, asyncio.Event()
            event.set()

        self._binder.run(_flip)

    @staticmethod
    async def wait(event: asyncio.Event, timeout: float) -> bool:
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


class FanOut:
    """Registry of live subscriber queues, keyed by channel."""

    def __init__(self, binder: LoopBinder | None = None) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._binder = binder or LoopBinder()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._binder.bind(loop)

    def subscribe(self, channel: str, queue: asyncio.Queue) -> None:
        self._subscribers[channel].add(queue)

    def unsubscribe_all(self, queue: asyncio.Queue) -> None:
        for queues in self._subscribers.values():
            queues.discard(queue)

    def publish(self, channel: str, payload: dict[str, Any]) -> None:
        # Snapshot the subscriber set: it may be mutated concurrently by a
        # disconnecting client on the loop thread (else "set changed size").
        subscribers = list(self._subscribers.get(channel, ()))
        if not subscribers:
            return

        def _deliver() -> None:
            for queue in subscribers:
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    # Slow consumer: it recovers missed messages via its
                    # cursor on reconnect (at-least-once via catch-up).
                    pass

        self._binder.run(_deliver)
