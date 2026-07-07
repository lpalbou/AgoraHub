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
    service.presence.update(agent.id, "idle")

    async def pump_outgoing() -> None:
        while True:
            payload = await queue.get()
            if payload.get("type") == "message":
                # Fan-out carries the raw message; the envelope is computed
                # here because it is viewer-specific (to_me, inlining, ...).
                message = Message(**payload["message"])
                if message.sender == agent.id:
                    continue
                envelope = service.envelope_for(agent.id, message)
                payload = {"type": "envelope", "envelope": envelope.model_dump()}
            await websocket.send_text(json.dumps(payload))

    pump = asyncio.create_task(pump_outgoing())
    try:
        while True:
            frame = json.loads(await websocket.receive_text())
            await _handle_frame(service, agent, frame, queue)
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        service.unsubscribe(queue)
        service.presence.update(agent.id, "offline")


async def _handle_frame(service: HubService, agent, frame: dict, queue: asyncio.Queue) -> None:
    kind = frame.get("type")
    try:
        if kind == "subscribe":
            backlog = service.subscribe(
                agent, frame.get("channels", []), queue, frame.get("since"),
            )
            queue.put_nowait({"type": "subscribed", "channels": frame.get("channels", [])})
            for message in backlog:
                queue.put_nowait({"type": "message", "message": message.model_dump()})
                # (converted to a viewer-specific envelope by the outgoing pump)
        elif kind == "post":
            payload = PostMessage(**{
                k: v for k, v in frame.items()
                if k in PostMessage.model_fields
            })
            message = service.post_message(agent, frame["channel"], payload)
            queue.put_nowait({"type": "posted", "id": message.id, "seq": message.seq})
        elif kind == "presence":
            service.presence.update(agent.id, frame.get("state", "idle"))
        elif kind == "ack":
            service.ack_inbox(agent, frame.get("cursors", {}))
        elif kind == "ping":
            queue.put_nowait({"type": "pong"})
        else:
            queue.put_nowait({"type": "error", "detail": f"unknown frame type '{kind}'"})
    except HubError as e:
        queue.put_nowait({"type": "error", "detail": e.detail, "status": e.status_code})
    except (KeyError, ValueError) as e:
        queue.put_nowait({"type": "error", "detail": f"malformed frame: {e}"})
