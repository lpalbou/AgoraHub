"""Demo: the v0.2 attention model — triage by envelope, not force-fed bodies.

A worker agent stays focused while a channel produces mixed traffic:
noise, a big dump, a spoofed-"URGENT" title, an addressed message, an open
question, and finally an operator's critical broadcast. The worker triages
headlines (the coded rules stand in for an LLM's judgment), fetches only
what earns it, and demonstrates that criticals stay pinned until read.

Run:  uv run python examples/attention_triage.py
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import uvicorn

from agora.client import AgoraClient
from agora.hub.app import create_app
from agora.models import Envelope, Status, Urgency

HUB = "http://127.0.0.1:8902"
ADMIN_KEY = "demo-admin-key"


async def start_hub() -> uvicorn.Server:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8902,
                                           log_level="warning", lifespan="off"))
    asyncio.create_task(server.serve())
    async with httpx.AsyncClient() as probe:
        while True:
            with contextlib.suppress(httpx.ConnectError):
                await probe.get(HUB + "/")
                return server
            await asyncio.sleep(0.05)


async def register(agent_id: str, operator: bool = False) -> str:
    async with httpx.AsyncClient() as http:
        response = await http.post(HUB + "/agents",
                                   json={"id": agent_id, "operator": operator},
                                   headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        return response.json()["api_key"]


def triage(e: Envelope) -> str:
    """The worker's read/skip policy — trusted structured signals only."""
    if e.critical or e.escalated:
        return "read"
    if e.to_me or e.reply_to_me or e.status in (Status.open, Status.blocked):
        return "read"
    return "skip"           # fyi broadcast: the headline is enough


async def main() -> None:
    server = await start_hub()
    worker_key = await register("worker")
    owner_key = await register("owner")
    operator_key = await register("operator", operator=True)

    owner = AgoraClient(HUB, owner_key)
    operator = AgoraClient(HUB, operator_key)
    worker = AgoraClient(HUB, worker_key)

    # Owner sets up the channel with metadata (the expectations contract).
    await owner.create_channel("build")
    await owner.store_set("build", "channel:meta", {
        "purpose": "coordination for the parser build",
        "norms": "asks numbered; fyi = genuinely skippable",
        "expected_traffic": ["asks", "decisions", "fyi"],
        "response_sla_minutes": 30,
    })
    for invitee, client in (("worker", worker), ("operator", operator)):
        await client.join_channel("build", await owner.create_invite("build", invitee))

    info = await worker.channel_info("build")
    print(f"channel meta: {info['meta']['purpose']!r} | norms: {info['meta']['norms']!r}\n")
    await worker.check_inbox()
    await worker.ack()  # clear system join messages

    # Mixed traffic lands while the worker is focused elsewhere.
    await owner.post("build", "refactoring the test harness today", title="status update")
    await owner.post("build", "x" * 5000, title="full build log attached")
    await owner.post("build", "URGENT!!! " + "y" * 3000, title="URGENT: read me now")
    await owner.post("build", "the parser entrypoint moved to src/parse.py",
                     title="entrypoint moved", to=["worker"])
    await owner.post("build", "1. do we keep py3.11 support?", title="python floor?",
                     status=Status.open, urgency=Urgency.next_turn)

    print("— worker triages its inbox by envelope —")
    saved_tokens = 0
    for e in await worker.check_inbox():
        decision = triage(e)
        flags = (" to-you" if e.to_me else "") + (" CRITICAL" if e.critical else "")
        inline = "inlined" if e.body is not None else "headline-only"
        print(f"  [{e.status.value:8}]{flags} {e.body_bytes:>5}B {inline:13} "
              f"title=<<<{e.title}>>>  -> {decision.upper()}")
        if decision == "read" and e.body is None:
            chain = await worker.read(e.channel, e.id)
            print(f"      fetched body ({len(chain)} message(s) incl. unread ancestors)")
        if decision == "skip" and e.body is None:
            saved_tokens += e.body_bytes
    await worker.ack()
    print(f"  => skipped {saved_tokens}B of broadcast bodies without reading them; "
          "the spoofed-URGENT title did not force a read (fyi + not addressed).\n")

    # The worker records its subjective impression — free text, private, revisable.
    await worker.set_note("owner", "titles sometimes shout ('URGENT') on skippable fyi; "
                                   "addressed messages and asks are reliable")
    [note] = await worker.get_notes("owner")
    print(f"worker's private colleague note on owner: {note['note']!r}\n")

    # Operator sends a critical: forced attention, pinned until actually read.
    await operator.post("build", "STOP: /tmp wiped nightly — move artifacts to ./out",
                        title="artifact path", critical=True)
    print("— operator posted a CRITICAL —")
    [e] = [e for e in await worker.check_inbox() if e.critical]
    print(f"  delivered with body ({e.body_bytes}B, inlined={e.body is not None})")
    await worker.ack()  # cursor ack alone does NOT clear it...
    still_pinned = [e for e in await worker.check_inbox() if e.critical]
    print(f"  after cursor ack: still pinned = {bool(still_pinned)}")
    await worker.read(e.channel, e.id)  # ...only an actual read does
    cleared = [e for e in await worker.check_inbox() if e.critical]
    print(f"  after read_message: still pinned = {bool(cleared)}")

    for client in (worker, owner, operator):
        await client.close()
    server.should_exit = True
    await asyncio.sleep(0.1)
    print("\n— demo complete —")


if __name__ == "__main__":
    asyncio.run(main())
