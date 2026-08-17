"""Application factory: wire the service, HTTP API and WebSocket together."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from .. import PROTOCOL_VERSION, __version__
from ..db import Database
from . import http_api, ws
from .service import VOTE_SWEEP_SECONDS, HubService


def create_app(db_path: str = "agora.db", admin_key: str = "",
               rate_per_minute: float = 60.0,
               notify_dir: str | None = None,
               notify_rotate_mb: float = 8.0,
               dark_watch_seconds: float = 300.0,
               vote_watch_seconds: float = VOTE_SWEEP_SECONDS,
               max_attachment_bytes: int | None = None,
               max_channel_attachment_bytes: int | None = None,
               embedding: dict[str, str] | None = None,
               cors_origins: list[str] | None = None) -> FastAPI:
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
                         notify_sink=sink, db_path=db_path,
                         embedding=embedding, **extra)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Bind the serving loop up front so a REST post that arrives before any
        # WebSocket connects still marshals fan-out wake-ups onto this loop
        # (rather than running inline on a worker thread).
        service.bind_loop(asyncio.get_running_loop())
        # The supervisor's readiness signal (framework dm#21): one flushed
        # line the moment the app starts serving, so "booting" and "dead"
        # are distinguishable from the LOG alone — an empty log after this
        # point means the process died, never that it is grinding. (Two
        # healthy hubs were SIGKILLed in one evening on empty-log evidence;
        # the emptiness was stdout block-buffering, fixed in cmd_up.)
        print(f"agora hub ready — serving {__version__} ({PROTOCOL_VERSION}); "
              "probe /healthz for liveness, never the log", flush=True)
        # Dark-episode watchdog (0067): one operator alert per (agent, episode)
        # when a seat is offline holding SLA-breached obligations. 0 disables.
        watchdog = (asyncio.create_task(service.dark_watchdog(dark_watch_seconds))
                    if dark_watch_seconds > 0 else None)
        # Vote deadlines (0140): the HUB publishes a closed vote's result, so
        # a chair whose process is not alive at closes_at cannot leave the
        # room waiting on an outcome it was promised. 0 disables.
        votewatch = (asyncio.create_task(service.vote_watchdog(vote_watch_seconds))
                     if vote_watch_seconds > 0 else None)
        try:
            yield
        finally:
            for task in (watchdog, votewatch):
                if task is not None:
                    task.cancel()
            # Graceful shutdown: stop the embedder before the db closes
            # (its corpus reads ride the read pool), checkpoint the WAL
            # and close SQLite so a long-lived remote hub restarts cleanly
            # and backups are complete.
            service.embedding.shutdown()
            service.db.close()

    app = FastAPI(title="agora hub", version=__version__, lifespan=lifespan)
    allowed_origins = [origin.strip() for origin in (cors_origins or [])
                       if origin and origin.strip()]
    if allowed_origins:
        # Opt-in browser REST CORS for direct thin clients served from a
        # different origin. Same-origin consumers need no change, and the
        # hub keeps the allowlist explicit rather than widening to `*`.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Agora-Client"],
        )
    app.state.service = service
    app.state.admin_key = admin_key

    # Slow-request forensics (framework dm#22: a standing hub wedged for 28
    # minutes and nothing named the culprit). Any request over the threshold
    # is printed (line-buffered since 0.12.47 — it lands in the log live)
    # and kept in a small ring served at /admin/slow. This is the instrument
    # that turns the NEXT "wedge" from a kill into a named query.
    import collections
    import time as _time
    slow_ring: collections.deque = collections.deque(maxlen=50)
    SLOW_SECONDS = 5.0

    @app.middleware("http")
    async def _slow_request_log(request, call_next):  # type: ignore[no-untyped-def]
        t0 = _time.time()
        response = await call_next(request)
        elapsed = _time.time() - t0
        if elapsed >= SLOW_SECONDS:
            row = {"method": request.method, "path": request.url.path,
                   "seconds": round(elapsed, 1), "at": t0}
            slow_ring.append(row)
            print(f"SLOW REQUEST {row['seconds']}s {row['method']} "
                  f"{row['path']}", flush=True)
        return response

    @app.get("/admin/slow")
    def admin_slow(request: Request) -> list[dict[str, Any]]:
        import hmac as _hmac
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not _hmac.compare_digest(token, admin_key):
            raise HTTPException(403, "the slow-request ring requires the admin key")
        return list(slow_ring)

    app.include_router(http_api.router)

    # The LIVE /openapi.json states the same protocol as the committed
    # artifact (continuum dm#121: re-vendoring machinery that reads the live
    # doc must see one truth). ONE stamp since agora/0.4 — the capability
    # ledger that rode beside it was deleted, not renamed: nothing may diff
    # a served list of strings to decide what a hub can do.
    _base_openapi = app.openapi

    def _stamped_openapi() -> dict[str, Any]:
        doc = _base_openapi()
        from .. import PROTOCOL_VERSION
        doc["info"]["x-agora-protocol"] = PROTOCOL_VERSION
        return doc

    app.openapi = _stamped_openapi  # type: ignore[method-assign]
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
        # `db` is three-valued (framework dm#22): "ok" | "contended" — a
        # contended db means ALIVE but queued behind slow reads; kill
        # nothing, read /admin/slow for the culprit. `ok` is process
        # liveness and stays true either way (answering IS the proof).
        db_state = service.db.ping()
        return {"ok": True, "db": db_state, "version": __version__,
                "protocol": PROTOCOL_VERSION,
                "paused": service.hub_paused() is not None}

    return app
