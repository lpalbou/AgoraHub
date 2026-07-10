"""WebSocket surface: live push for connected clients.

Frames (JSON objects, `type` discriminated):

  client -> hub:
    {"type": "subscribe", "channels": [...], "since": {"chan": seq, ...}}
    {"type": "post", "channel": "...", "body": "...", ...PostMessage fields}
    {"type": "presence", "state": "idle" | "working"}
    {"type": "ack", "cursors": {"chan": seq}}
    {"type": "ping"}

  hub -> client:
    {"type": "envelope", "envelope": {...}}    # live or backlog delivery
    {"type": "posted", "id": "...", "seq": n}  # confirmation of own post
    {"type": "subscribed", "channels": [...]}
    {"type": "pong"}
    {"type": "error", "detail": "..."}

Since v0.2, delivery is ENVELOPES, not raw messages: the hub computes a
viewer-specific headline (to_me / reply_to_me / escalation) and inlines the
body only where the attention policy allows (small, addressed, or critical).
Bodies are fetched deliberately via GET /channels/{c}/messages/{id}.

Delivery is at-least-once: a reconnecting client passes its cursors in
`since` and receives the backlog before live traffic; dedup by message id.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..models import Message, PostMessage
from .service import HubError, HubService

router = APIRouter()

_QUEUE_SIZE = 1000


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    service: HubService = websocket.app.state.service
    token = websocket.query_params.get("token", "")
    if not token:
        auth = websocket.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
    try:
        agent = service.authenticate(token)
    except HubError:
        await websocket.close(code=4401, reason="invalid api key")
        return

    await websocket.accept()
    service.bind_loop(asyncio.get_running_loop())  # fan-out wakes us thread-safely
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
    # Connection-derived presence: holding this socket IS reachability. The
    # connect is balanced by the disconnect in `finally` below — everything
    # between them lives inside the try, so no exception path can leak the
    # refcount (a leaked count = zombie "idle" until hub restart; audit bug).
    service.presence.connect(agent.id)
    pump = None

    async def pump_outgoing() -> None:
        # Per-connection high-water: the same queue is subscribed under both
        # the channel key and the agent/<id> key, so each message arrives
        # twice — dedup here so the wire carries one envelope per message
        # (audit M1). Seq is per-channel monotonic and puts are loop-ordered.
        sent: dict[str, int] = {}
        while True:
            payload = await queue.get()
            if payload.get("type") == "message":
                # Fan-out carries the raw message; the envelope is computed
                # here because it is viewer-specific (to_me, inlining, ...).
                message = Message(**payload["message"])
                if message.sender == agent.id:
                    continue
                if message.seq <= sent.get(message.channel, 0):
                    continue
                # Membership at DELIVERY time: channel subscriptions are only
                # checked at subscribe time, so without this an agent that
                # left a channel would keep receiving its live pushes for the
                # life of the socket (audit H2 — membership is THE isolation
                # boundary).
                if not service.db.is_member(message.channel, agent.id):
                    continue
                sent[message.channel] = message.seq
                envelope = service.envelope_for(agent.id, message)
                payload = {"type": "envelope", "envelope": envelope.model_dump()}
            await websocket.send_text(json.dumps(payload))

    try:
        # Identity-keyed fan-out: every message in a channel this agent BELONGS
        # to reaches this connection, even for channels born after connect (a
        # fresh DM was previously undeliverable until the watcher restarted).
        # Channel subscriptions still matter for backlog catch-up; the client
        # dedups any double delivery by per-channel seq.
        service.fanout.subscribe(f"agent/{agent.id}", queue)
        pump = asyncio.create_task(pump_outgoing())
        # If the pump dies on an exception the socket would stay open but
        # deliver nothing — silent deafness the client cannot detect. Closing
        # the socket converts it into a disconnect, which the client's
        # reconnect + catch-up machinery already handles (review F4).
        pump.add_done_callback(
            lambda t: asyncio.ensure_future(websocket.close(code=1011))
            if not t.cancelled() and t.exception() is not None else None)
        while True:
            raw = await websocket.receive_text()
            try:
                frame = json.loads(raw)
            except ValueError:
                # Malformed text must get an error frame, not a server
                # traceback that tears the connection down (audit L5).
                queue.put_nowait({"type": "error", "detail": "malformed frame: not valid JSON"})
                continue
            await _handle_frame(service, agent, frame, queue)
    except WebSocketDisconnect:
        pass
    finally:
        if pump is not None:
            pump.cancel()
        service.unsubscribe(queue)
        service.presence.disconnect(agent.id)


async def _handle_frame(service: HubService, agent, frame: dict, queue: asyncio.Queue) -> None:
    kind = frame.get("type")
    try:
        if kind == "subscribe":
            backlog = service.subscribe(
                agent, frame.get("channels", []), queue, frame.get("since"),
            )
            # `await put` for control frames too: put_nowait on a queue filled
            # by a large backlog raises QueueFull past the except clauses below
            # and tears the connection down (review F6).
            await queue.put({"type": "subscribed", "channels": frame.get("channels", [])})
            for message in backlog:
                # `await put`, not put_nowait: a reconnect backlog larger than
                # the queue would raise QueueFull, escape the except below,
                # and tear the connection down — the client would then loop
                # subscribe -> overflow -> disconnect forever. Awaiting gives
                # backpressure: the pump drains while we feed.
                await queue.put({"type": "message", "message": message.model_dump()})
                # (converted to a viewer-specific envelope by the outgoing pump)
        elif kind == "post":
            payload = PostMessage(**{
                k: v for k, v in frame.items()
                if k in PostMessage.model_fields
            })
            message = service.post_message(agent, frame["channel"], payload)
            await queue.put({"type": "posted", "id": message.id, "seq": message.seq})
        elif kind == "presence":
            service.presence.update(agent.id, frame.get("state", "idle"))
        elif kind == "ack":
            service.ack_inbox(agent, frame.get("cursors", {}))
        elif kind == "ping":
            await queue.put({"type": "pong"})
        else:
            await queue.put({"type": "error", "detail": f"unknown frame type '{kind}'"})
    except HubError as e:
        await queue.put({"type": "error", "detail": e.detail, "status": e.status_code})
    except (KeyError, ValueError) as e:
        await queue.put({"type": "error", "detail": f"malformed frame: {e}"})
