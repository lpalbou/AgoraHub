"""Demo: two agents working together with mid-work interleaving.

Runs a hub in-process, registers two agents ("runtime" and "memory"), and
shows the core capability that motivated this project: `memory` sends a
correction WHILE `runtime` is mid-task, and `runtime` folds it into its next
loop iteration without abandoning its work — the agent-to-agent equivalent
of steering Codex mid-run.

Run:  uv run python examples/two_agents_interleaving.py
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import uvicorn

from agora.client import AgoraClient
from agora.hub.app import create_app
from agora.models import Status, Urgency

HUB = "http://127.0.0.1:8901"
ADMIN_KEY = "demo-admin-key"


async def start_hub() -> uvicorn.Server:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8901,
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
        response = await http.post(
            HUB + "/agents", json={"id": agent_id},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
        return response.json()["api_key"]


async def runtime_agent(api_key: str, invite_handoff: asyncio.Future) -> None:
    """Works through a multi-step task, draining its inbox at each boundary."""
    client = AgoraClient(HUB, api_key)
    await client.create_channel("seam-design")
    # Invites travel out-of-band by design (a DM, a CLI, a human). Here the
    # handoff is an in-process future standing in for that side channel.
    invite_handoff.set_result(await client.create_invite("seam-design", "memory"))

    await client.connect(channels=["seam-design"])
    await client.set_presence("working")
    plan = ["survey the API", "draft the seam interface", "write the adapter",
            "wire the tests", "post the summary"]
    api_version = "v1"

    for step, work in enumerate(plan, start=1):
        await asyncio.sleep(0.3)  # ... doing the actual work ...
        print(f"  runtime | step {step}: {work} (against API {api_version})")

        # THE INTERLEAVING POINT: fold in whatever arrived while working.
        for message in client.inbox.drain():
            print(f"  runtime |   folded in mid-work message from {message.sender}: "
                  f"{message.body!r} (urgency={message.urgency.value})")
            if message.urgency in (Urgency.next_turn, Urgency.interrupt) and message.data:
                api_version = message.data.get("api_version", api_version)
                await client.post(
                    "seam-design",
                    f"Acknowledged mid-run: switching to API {api_version} from step {step + 1} "
                    "without restarting the task.",
                    status=Status.reply, reply_to=message.id,
                )
        await client.ack()

    await client.post("seam-design", f"Task complete on API {api_version}.",
                      title="done", status=Status.resolved)
    await client.set_presence("idle")
    await client.close()


async def memory_agent(api_key: str, invite_handoff: asyncio.Future) -> None:
    """Watches the channel and steers the runtime agent mid-task."""
    client = AgoraClient(HUB, api_key)
    await client.join_channel("seam-design", await invite_handoff)
    await client.connect(channels=["seam-design"])

    await asyncio.sleep(0.8)  # runtime is mid-task by now
    print("  memory  | steering runtime mid-task (urgency=next_turn)...")
    await client.post(
        "seam-design",
        "Heads-up: the memory API changed — target v2, the v1 write path is frozen.",
        title="interface change", status=Status.open, urgency=Urgency.next_turn,
        data={"api_version": "v2"},
    )
    for message in await client.inbox.wait(timeout=10.0):
        if message.reply_to:
            print(f"  memory  | runtime replied: {message.body!r}")
    await client.ack()
    await client.close()


async def main() -> None:
    server = await start_hub()
    runtime_key = await register("runtime")
    memory_key = await register("memory")
    invite_handoff: asyncio.Future = asyncio.get_running_loop().create_future()

    print("— two agents, one channel, interleaved steering —")
    await asyncio.gather(
        runtime_agent(runtime_key, invite_handoff),
        memory_agent(memory_key, invite_handoff),
    )
    print("— demo complete —")
    server.should_exit = True
    await asyncio.sleep(0.1)


if __name__ == "__main__":
    asyncio.run(main())
