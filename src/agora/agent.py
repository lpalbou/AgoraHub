"""AgentRunner — turn ANY callable into an agora-triggered agent.

This is agora's answer to "how do you trigger an agent?" for agents you own
(a plain Python loop, a LangChain/LangGraph agent, an abstractcore agent, a
custom function). Triggering is not a magic push into a sleeping process; it
is a **long-lived subscriber** that binds agora's two delivery primitives
(WebSocket push + durable cursor catch-up) to your handler. You write:

    async def handle(msg, ctx):
        if "?" in msg.title:
            await ctx.reply("here's my answer", status=Status.reply)

    run_agent(handle, url="http://127.0.0.1:8765", api_key="agora_...",
              channels=["design"])

...and the runner owns the rest: connect, subscribe, presence (working while
your handler runs, idle otherwise), per-message dispatch, ack, reconnect, and
the safety rails that keep two agents from triggering each other forever.

The SAME contract underlies the other adapters: the attaché is this runner
for headless CLIs (it wakes a harness instead of calling a function), and a
Gateway bridge is this runner delivering into abstractflow's on_agent_message
node. See docs/orchestrating_agents.md.

Honest limit: this process must stay alive to trigger its agent. If it can't
(serverless/on-demand), put it under a supervisor (systemd/cron) or use a
runtime whose own server owns wake (LangGraph Platform, AbstractGateway).
"""

from __future__ import annotations

import asyncio
import inspect
import signal
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .client import AgoraClient
from .models import Envelope, Message, Status, Urgency


class _TurnBudget:
    """Sliding-window cap on handler invocations — the throttle that bounds
    cost and, with the per-peer cap, arrests runaway reply loops."""

    def __init__(self, max_per_minute: int) -> None:
        self.max = max_per_minute
        self._events: deque[float] = deque()

    def allow(self) -> bool:
        now = time.time()
        while self._events and now - self._events[0] > 60.0:
            self._events.popleft()
        if len(self._events) >= self.max:
            return False
        self._events.append(now)
        return True


class _PeerExchangeCap:
    """Bounds how many times we auto-reply to the SAME peer within a window.
    Two runner-agents ping-ponging (`ok`/`thanks`) trips this and goes quiet,
    the last line of defense against reply loops even if etiquette fails."""

    def __init__(self, max_replies: int, window_s: float = 120.0) -> None:
        self.max = max_replies
        self.window = window_s
        self._by_peer: dict[str, deque[float]] = {}

    def allow(self, peer: str) -> bool:
        now = time.time()
        dq = self._by_peer.setdefault(peer, deque())
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= self.max:
            return False
        dq.append(now)
        return True


@dataclass
class Context:
    """Handed to your handler: the tools to act on one message."""

    runner: "AgentRunner"
    envelope: Envelope
    agent_id: str

    @property
    def channel(self) -> str:
        return self.envelope.channel

    @property
    def client(self) -> AgoraClient:
        return self.runner.client

    async def body(self) -> str:
        """The message body. Envelopes elide large bodies; this fetches on
        demand (and returns the reply-chain-aware text)."""
        if self.envelope.body is not None:
            return self.envelope.body
        chain = await self.client.read(self.channel, self.envelope.id)
        return "\n\n".join(m.body for m in chain)

    async def reply(self, body: str, *, status: Status = Status.reply,
                    urgency: Urgency = Urgency.inbox, data: dict | None = None,
                    force: bool = False) -> Message | None:
        """Reply in-thread to the triggering message. By default we refuse to
        reply to `fyi`/`resolved` (loop hygiene) and to exchanges that exceed
        the per-peer cap — pass force=True to override deliberately."""
        if not force and self.envelope.status in (Status.fyi, Status.resolved):
            return None
        if not force and not self.runner._peer_cap.allow(self.envelope.sender):
            self.runner._log(f"peer-exchange cap hit for {self.envelope.sender}; "
                             "not replying (possible loop)")
            return None
        return await self.client.post(self.channel, body, status=status,
                                      urgency=urgency, data=data,
                                      reply_to=self.envelope.id)

    async def post(self, channel: str, body: str, **kw) -> Message:
        return await self.client.post(channel, body, **kw)

    async def store_get(self, key: str) -> dict[str, Any]:
        return await self.client.store_get(self.channel, key)

    async def store_set(self, key: str, value: Any, expect_version: int | None = None):
        return await self.client.store_set(self.channel, key, value, expect_version)

    async def note(self, subject: str, text: str) -> None:
        await self.client.set_note(subject, text)


Handler = Callable[[Envelope, Context], Awaitable[None] | None]


class AgentRunner:
    """A long-lived subscriber that triggers `handler` on incoming messages.

    Dispatch is SERIAL (one handler at a time): LLM turns are expensive and
    ordering matters; messages that arrive mid-handler queue in the client
    inbox and are drained next. Delivery is effectively-once: duplicates from
    a reconnect are dropped by a bounded seen-set, and each message is acked
    only AFTER its handler returns.
    """

    def __init__(self, handler: Handler, *, url: str, api_key: str,
                 channels: list[str], invoke_on_fyi: bool = False,
                 max_turns_per_minute: int = 30, max_replies_per_peer: int = 8,
                 should_invoke: Callable[[Envelope], bool] | None = None,
                 verbose: bool = True) -> None:
        self.handler = handler
        self.client = AgoraClient(url, api_key)
        self.channels = channels
        self.invoke_on_fyi = invoke_on_fyi
        self._budget = _TurnBudget(max_turns_per_minute)
        self._peer_cap = _PeerExchangeCap(max_replies_per_peer)
        self._should_invoke = should_invoke
        self.verbose = verbose
        self._seen: deque[str] = deque(maxlen=4096)
        self._seen_set: set[str] = set()
        self._stop = asyncio.Event()
        self.agent_id: str = ""

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[agent-runner {self.agent_id or '?'}] {msg}", flush=True)

    def _default_should_invoke(self, e: Envelope) -> bool:
        """Respect the attention model: always act on things owed to you or
        forced (obligations, addressing, critical, escalated). Skip pure fyi
        broadcasts unless configured otherwise — that's the anti-noise default."""
        if e.critical or e.escalated or e.to_me or e.reply_to_me:
            return True
        if e.status in (Status.open, Status.blocked):
            return True
        return self.invoke_on_fyi

    async def start(self) -> None:
        info = await self.client.whoami()
        self.agent_id = info["id"]
        await self.client.connect(self.channels)
        await self.client.set_presence("idle")
        self._log(f"watching {self.channels}")
        try:
            while not self._stop.is_set():
                envelopes = await self.client.inbox.wait(timeout=30.0)
                for env in envelopes:
                    if self._stop.is_set():
                        break
                    await self._dispatch(env)
        finally:
            await self.client.set_presence("idle")
            await self.client.close()

    async def _dispatch(self, env: Envelope) -> None:
        if env.id in self._seen_set:  # effectively-once across reconnects
            return
        decide = self._should_invoke or self._default_should_invoke
        if not decide(env):
            self._ack(env)
            return
        if not self._budget.allow():
            self._log("turn budget exhausted — skipping (halts runaway loops)")
            self._ack(env)
            return
        ctx = Context(self, env, self.agent_id)
        await self.client.set_presence("working")
        try:
            result = self.handler(env, ctx)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # a poison message must not kill the runner
            self._log(f"handler error on {env.id} ({env.channel}#{env.seq}): {exc!r}")
        finally:
            await self.client.set_presence("idle")
            self._ack(env)  # ack after handling: a crash before this re-delivers

    def _ack(self, env: Envelope) -> None:
        self._seen_set.add(env.id)
        self._seen.append(env.id)
        if len(self._seen_set) > self._seen.maxlen:
            self._seen_set.discard(self._seen.popleft())
        # Per-message ack (explicit cursor) — never a blanket "ack everything".
        asyncio.create_task(self.client.ack({env.channel: env.seq}))

    def stop(self) -> None:
        self._stop.set()


def run_agent(handler: Handler, *, url: str, api_key: str, channels: list[str],
              **kwargs) -> None:
    """Blocking convenience: run a handler as an agora agent until Ctrl-C."""
    runner = AgentRunner(handler, url=url, api_key=api_key, channels=channels, **kwargs)

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, runner.stop)
            except NotImplementedError:
                pass  # e.g. Windows
        await runner.start()

    asyncio.run(_main())
