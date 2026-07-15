"""Application factory: wire the service, HTTP API and WebSocket together."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from .. import PROTOCOL_VERSION, __version__
from ..db import Database
from . import http_api, ws
from .service import HubService


def create_app(db_path: str = "agora.db", admin_key: str = "",
               rate_per_minute: float = 60.0,
               notify_dir: str | None = None,
               notify_rotate_mb: float = 8.0,
               dark_watch_seconds: float = 300.0,
               max_attachment_bytes: int | None = None,
               max_channel_attachment_bytes: int | None = None) -> FastAPI:
    if not admin_key:
        raise ValueError("an admin key is required (set AGORA_ADMIN_KEY)")
    sink = None
    if notify_dir:
        from .notify_sink import NotifySink
        sink = NotifySink(notify_dir, rotate_mb=notify_rotate_mb)
    extra: dict[str, Any] = {}
    if max_attachment_bytes:
        extra["max_attachment_bytes"] = max_attachment_bytes
    if max_channel_attachment_bytes:
        extra["max_channel_attachment_bytes"] = max_channel_attachment_bytes
    service = HubService(Database(db_path), rate_per_minute=rate_per_minute,
                         notify_sink=sink, **extra)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Bind the serving loop up front so a REST post that arrives before any
        # WebSocket connects still marshals fan-out wake-ups onto this loop
        # (rather than running inline on a worker thread).
        service.bind_loop(asyncio.get_running_loop())
        # Dark-episode watchdog (0067): one operator alert per (agent, episode)
        # when a seat is offline holding SLA-breached obligations. 0 disables.
        watchdog = (asyncio.create_task(service.dark_watchdog(dark_watch_seconds))
                    if dark_watch_seconds > 0 else None)
        try:
            yield
        finally:
            if watchdog is not None:
                watchdog.cancel()
            # Graceful shutdown: checkpoint the WAL and close SQLite so a long-
            # lived remote hub restarts cleanly and backups are complete.
            service.db.close()

    app = FastAPI(title="agora hub", version=__version__, lifespan=lifespan)
    app.state.service = service
    app.state.admin_key = admin_key
    app.include_router(http_api.router)
    app.include_router(ws.router)

    @app.get("/")
    def root() -> dict[str, str]:
        return {"service": "agora", "version": __version__, "protocol": PROTOCOL_VERSION}

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        # Liveness + DB reachability, for a supervisor/proxy to probe a remote
        # hub. `paused` rides here unauthenticated so a supervisor can tell a
        # stood-down hub from a dead one; `protocol` so an unauthenticated
        # probe can also tell WHAT the hub speaks (docs/protocol.md, Scope).
        return {"ok": service.db.ping(), "version": __version__,
                "protocol": PROTOCOL_VERSION,
                "paused": service.hub_paused() is not None}

    return app
