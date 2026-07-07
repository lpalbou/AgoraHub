"""Demo: triggering a running ReAct loop in realtime, against an EXTERNAL hub.

Unlike `two_agents_interleaving.py` (which embeds the hub in-process), this
script connects to an already-running hub and demonstrates the three
receive stances a native Python agent has:

  1. WORKING — the worker runs a simulated ReAct loop (Thought -> Action ->
     Observation). At each iteration boundary it calls the NON-BLOCKING
     `client.inbox.drain()` and folds new envelopes into its next Thought,
     without aborting the iteration in flight (actor-model selective receive).
  2. INTERRUPT — mid-Action, the worker cheaply polls `inbox.has_interrupt`
     (set by urgency=interrupt / critical deliveries) and breaks off the
     current Action at a safe point instead of waiting for the boundary.
  3. IDLE — the task is done; the worker blocks on `client.inbox.wait()`
     (no busy polling). A peer posts; the worker wakes and handles it.

It also shows envelope economics: a DM whose body exceeds the addressed
inline cap (4 KB) is delivered as a headline only (`body=None`), and the
worker fetches it deliberately with `client.read()`.

Prerequisites: a running hub and two registered agents. Configure via env:

    AGORA_URL          hub base url        (default http://127.0.0.1:8821)
    AGORA_WORKER_KEY   api key of agent 1  (required)
    AGORA_SENDER_KEY   api key of agent 2  (required)

Run:  AGORA_WORKER_KEY=... AGORA_SENDER_KEY=... uv run python examples/react_trigger_demo.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from agora.client import AgoraClient
from agora.models import Envelope, Status, Urgency

HUB = os.environ.get("AGORA_URL", "http://127.0.0.1:8821")
CHANNEL = f"build-{int(time.time())}"   # unique per run so the demo is rerunnable
T0 = time.monotonic()


def t() -> str:
    """Relative wall-clock stamp so the interleaving is visible in the output."""
    return f"t={time.monotonic() - T0:5.2f}s"


def headline(e: Envelope) -> str:
    flags = ("[to-me]" if e.to_me else "") + ("[INTERRUPT]" if e.effective_urgency == Urgency.interrupt else "")
    return (f"[{e.channel} #{e.seq}] from={e.sender} status={e.status.value} "
            f"urgency={e.effective_urgency.value}{flags} size={e.body_bytes}B "
            f"title=<<<{e.title}>>> body_inlined={e.body is not None}")


# --------------------------------------------------------------------------- #
# WORKER: a ReAct-style agent that stays steerable without being preemptible. #
# --------------------------------------------------------------------------- #

async def worker(api_key: str, invite_handoff: asyncio.Future, dm_ready: asyncio.Event) -> None:
    client = AgoraClient(HUB, api_key)
    await client.create_channel(CHANNEL)
    invite_handoff.set_result(await client.create_invite(CHANNEL, "sender"))
    await client.connect(channels=[CHANNEL])      # push -> client.inbox (WebSocket)
    await client.set_presence("working")          # tells any attache: don't wake me

    algorithm = "A"          # the behavior a mid-run steer will change
    notes: list[str] = []    # steers folded in, consumed by the next Thought

    for step in range(1, 7):
        # ---- Thought: incorporates anything folded in at the last boundary ----
        thought = f"step {step}/6 of the build, using algorithm {algorithm}"
        if notes:
            thought += f" | folded in: {'; '.join(notes)}"
            notes.clear()
        print(f"{t()} worker  | THOUGHT  {thought}")

        # ---- Action: simulated tool work (1.5s). NOT abortable by ordinary  ----
        # ---- messages; only `has_interrupt` breaks it, and only at the safe ----
        # ---- points we choose (every 0.25s slice).                          ----
        print(f"{t()} worker  | ACTION   run_tool(step={step}, algorithm={algorithm})")
        interrupted = False
        for _ in range(6):
            await asyncio.sleep(0.25)
            if client.inbox.has_interrupt:
                interrupted = True
                print(f"{t()} worker  |   has_interrupt set -> breaking Action at a safe point")
                break

        # ---- Observation ----
        print(f"{t()} worker  | OBSERVE  step {step} {'cut short' if interrupted else 'done'}")

        # ---- BOUNDARY: the receive point. Non-blocking drain; fold, ack. ----
        for env in client.inbox.drain():
            print(f"{t()} worker  | DRAIN    {headline(env)}")
            if env.effective_urgency == Urgency.interrupt:
                # Worth reacting to now: answer, then resume the plan.
                print(f"{t()} worker  |   handling interrupt before next step")
                await client.post(CHANNEL, f"Status: {step}/6 steps done, algorithm {algorithm}, on track.",
                                  status=Status.reply, reply_to=env.id)
            elif env.data and "algorithm" in env.data:
                # Ordinary steer: change future behavior, don't redo the past.
                algorithm = env.data["algorithm"]
                notes.append(f"switched to algorithm {algorithm} per {env.sender} (msg #{env.seq})")
            elif env.body is not None:
                notes.append(f"{env.sender} says: {env.body!r}")
        await client.ack()

    await client.post(CHANNEL, f"Build finished with algorithm {algorithm}.",
                      title="done", status=Status.resolved)
    await client.set_presence("idle")

    # ---- IDLE: block on the inbox. Zero busy polling; the WebSocket push ----
    # ---- wakes `wait()` the moment something arrives.                     ----
    print(f"{t()} worker  | IDLE     blocking on client.inbox.wait(timeout=30)")
    for env in await client.inbox.wait(timeout=30):
        print(f"{t()} worker  | WAKE     {headline(env)}")
        if env.status == Status.open:
            await client.post(CHANNEL, "Yes — archived the artifacts as requested.",
                              status=Status.reply, reply_to=env.id)

    # ---- DM + deliberate read: open the DM channel, subscribe the live   ----
    # ---- socket to it, then receive an envelope-only (>4KB) body.        ----
    await client.open_dm("sender")
    await client.subscribe(["dm:sender--worker"])
    dm_ready.set()
    for env in await client.inbox.wait(timeout=10):
        print(f"{t()} worker  | DM ENV   {headline(env)}")
        if env.body is None:  # too big to inline: fetch deliberately
            messages = await client.read(env.channel, env.id)
            body = messages[-1].body
            print(f"{t()} worker  | READ     fetched full body via client.read(): "
                  f"{len(body)} chars, starts {body[:40]!r}")
    await client.ack()
    await client.close()


# --------------------------------------------------------------------------- #
# SENDER: a peer that steers, interrupts, wakes, and DMs the worker.          #
# --------------------------------------------------------------------------- #

async def sender(api_key: str, invite_handoff: asyncio.Future, dm_ready: asyncio.Event) -> None:
    client = AgoraClient(HUB, api_key)
    await client.join_channel(CHANNEL, await invite_handoff)
    await client.connect(channels=[CHANNEL])

    # (1) Steer mid-run: lands while the worker is inside an Action; the worker
    #     picks it up at the NEXT iteration boundary (urgency=next_turn).
    await asyncio.sleep(2.5)
    print(f"{t()} sender  | POST     steer (urgency=next_turn): switch to algorithm B")
    await client.post(CHANNEL, "Benchmarks came in: algorithm B is 3x faster. Switch for remaining steps.",
                      title="switch to algorithm B", status=Status.open,
                      urgency=Urgency.next_turn, data={"algorithm": "B"})

    # (2) Interrupt: sets the worker's has_interrupt flag; the worker breaks
    #     off its current Action at a safe point instead of finishing it.
    await asyncio.sleep(3.0)
    print(f"{t()} sender  | POST     interrupt (urgency=interrupt): status check")
    await client.post(CHANNEL, "Operator asking for an immediate status report.",
                      title="status check NOW", status=Status.open, urgency=Urgency.interrupt,
                      to=["worker"])

    # (3) Wake the idle worker: wait until its 'done' post, then ask a follow-up.
    while True:
        envs = await client.inbox.wait(timeout=30)
        if any(e.status == Status.resolved and e.sender == "worker" for e in envs):
            break
    await asyncio.sleep(0.5)  # let the worker reach its idle wait() first
    print(f"{t()} sender  | POST     follow-up ask to the now-idle worker")
    await client.post(CHANNEL, "One more thing: did you archive the build artifacts?",
                      title="archive check", status=Status.open, to=["worker"])

    # (4) DM with a large body: exceeds the 4KB addressed-inline cap, so the
    #     worker receives an ENVELOPE ONLY and must client.read() the body.
    await dm_ready.wait()
    big_body = "artifact manifest line\n" * 260  # ~6 KB
    print(f"{t()} sender  | DM       sending {len(big_body)}-char DM (envelope-only for receiver)")
    await client.dm("worker", big_body, title="full artifact manifest")

    await client.ack()
    await client.close()


async def main() -> None:
    worker_key = os.environ.get("AGORA_WORKER_KEY")
    sender_key = os.environ.get("AGORA_SENDER_KEY")
    if not worker_key or not sender_key:
        sys.exit("set AGORA_WORKER_KEY and AGORA_SENDER_KEY (see module docstring)")
    invite_handoff: asyncio.Future = asyncio.get_running_loop().create_future()
    dm_ready = asyncio.Event()
    print(f"— ReAct trigger demo against {HUB} (channel {CHANNEL}) —")
    await asyncio.gather(
        worker(worker_key, invite_handoff, dm_ready),
        sender(sender_key, invite_handoff, dm_ready),
    )
    print("— demo complete —")


if __name__ == "__main__":
    asyncio.run(main())
