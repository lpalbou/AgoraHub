"""Proof: two OWNED agents triggered purely by messages via AgentRunner.

No human relay, no harness, no polling in user code — each agent is just a
`handle(msg, ctx)` function wrapped by `AgentRunner`, which subscribes to the
hub and fires the handler when a message arrives. Demonstrates:
  - message-driven triggering (ping asks -> pong is woken and answers)
  - the interleaving/attention model (handlers see envelopes, reply in-thread)
  - loop safety (a deliberate fyi chatter storm does NOT ping-pong forever)

Run:  uv run python examples/runner_two_agents.py
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import uvicorn

from agora.agent import AgentRunner, Context
from agora.hub.app import create_app
from agora.models import Envelope, Status

HUB = "http://127.0.0.1:8903"
ADMIN = "demo-admin-key"


async def start_hub() -> uvicorn.Server:
    app = create_app(db_path=":memory:", admin_key=ADMIN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8903,
                                           log_level="warning", lifespan="off"))
    asyncio.create_task(server.serve())
    async with httpx.AsyncClient() as probe:
        while True:
            with contextlib.suppress(httpx.ConnectError):
                await probe.get(HUB + "/")
                return server
            await asyncio.sleep(0.05)


async def register(agent_id: str) -> str:
    async with httpx.AsyncClient() as http:
        r = await http.post(HUB + "/agents", json={"id": agent_id},
                            headers={"Authorization": f"Bearer {ADMIN}"})
        return r.json()["api_key"]


# --- the two agents are just handler functions -----------------------------

async def ping_handler(msg: Envelope, ctx: Context) -> None:
    body = await ctx.body()
    print(f"  ping  woke on [{msg.channel}#{msg.seq}] from {msg.sender}: {body!r}")
    if "pong" in body.lower():
        # Answer once, then resolve — no infinite ack-of-acks.
        await ctx.reply("thanks pong, resolving.", status=Status.resolved)


async def pong_handler(msg: Envelope, ctx: Context) -> None:
    body = await ctx.body()
    print(f"  pong  woke on [{msg.channel}#{msg.seq}] from {msg.sender}: {body!r}")
    if msg.status in (Status.open, Status.blocked):
        await ctx.reply("pong! here is your answer.", status=Status.reply)


async def main() -> None:
    server = await start_hub()
    ping_key = await register("ping")
    pong_key = await register("pong")

    # Owner sets up the channel + invites the peer (using the raw client).
    from agora.client import AgoraClient
    ping_client = AgoraClient(HUB, ping_key)
    await ping_client.create_channel("pingpong")
    invite = await ping_client.create_invite("pingpong", "pong")
    await AgoraClient(HUB, pong_key).join_channel("pingpong", invite)
    await ping_client.close()

    # Wrap each handler as a triggered agent.
    ping = AgentRunner(ping_handler, url=HUB, api_key=ping_key, channels=["pingpong"])
    pong = AgentRunner(pong_handler, url=HUB, api_key=pong_key, channels=["pingpong"])
    ping_task = asyncio.create_task(ping.start())
    pong_task = asyncio.create_task(pong.start())
    await asyncio.sleep(0.5)  # let both subscribe

    print("— ping asks an open question; pong is triggered and answers —")
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {ping_key}"}) as h:
        await h.post(f"{HUB}/channels/pingpong/messages",
                     json={"body": "are you there?", "title": "ping",
                           "status": "open", "to": ["pong"]})

    await asyncio.sleep(2.0)  # let the trigger chain play out

    print("\n— a low-value fyi does NOT trigger a reply storm —")
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {ping_key}"}) as h:
        await h.post(f"{HUB}/channels/pingpong/messages",
                     json={"body": "fyi: build is green", "title": "status",
                           "status": "fyi"})
    await asyncio.sleep(1.5)

    history = await AgoraClient(HUB, pong_key).history("pingpong")
    replies = [m for m in history if m.kind == "message"]
    print(f"\ntotal messages in channel: {len(replies)} "
          f"(bounded — no infinite loop)")

    ping.stop(); pong.stop()
    await asyncio.gather(ping_task, pong_task, return_exceptions=True)
    server.should_exit = True
    await asyncio.sleep(0.1)
    print("— demo complete —")


if __name__ == "__main__":
    asyncio.run(main())
