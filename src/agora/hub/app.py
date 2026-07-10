"""Application factory: wire the service, HTTP API and WebSocket together."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .. import PROTOCOL_VERSION, __version__
from ..db import Database
from . import http_api, ws
from .service import HubService


def create_app(db_path: str = "agora.db", admin_key: str = "",
               rate_per_minute: float = 60.0,
               notify_dir: str | None = None) -> FastAPI:
    if not admin_key:
        raise ValueError("an admin key is required (set AGORA_ADMIN_KEY)")
    sink = None
    if notify_dir:
        from .notify_sink import NotifySink
        sink = NotifySink(notify_dir)
    service = HubService(Database(db_path), rate_per_minute=rate_per_minute,
                         notify_sink=sink)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Bind the serving loop up front so a REST post that arrives before any
        # WebSocket connects still marshals fan-out wake-ups onto this loop
        # (rather than running inline on a worker thread).
        service.bind_loop(asyncio.get_running_loop())
        try:
            yield
        finally:
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
        # Liveness + DB reachability, for a supervisor/proxy to probe a remote hub.
        return {"ok": service.db.ping(), "version": __version__}

    return app
