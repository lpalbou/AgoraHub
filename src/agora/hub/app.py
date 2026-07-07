"""Application factory: wire the service, HTTP API and WebSocket together."""

from __future__ import annotations

from fastapi import FastAPI

from .. import PROTOCOL_VERSION, __version__
from ..db import Database
from . import http_api, ws
from .service import HubService


def create_app(db_path: str = "agora.db", admin_key: str = "",
               rate_per_minute: float = 60.0) -> FastAPI:
    if not admin_key:
        raise ValueError("an admin key is required (set AGORA_ADMIN_KEY)")
    app = FastAPI(title="agora hub", version=__version__)
    app.state.service = HubService(Database(db_path), rate_per_minute=rate_per_minute)
    app.state.admin_key = admin_key
    app.include_router(http_api.router)
    app.include_router(ws.router)

    @app.get("/")
    def root() -> dict[str, str]:
        return {"service": "agora-hub", "version": __version__, "protocol": PROTOCOL_VERSION}

    return app
