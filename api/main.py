"""FastAPI app.

Phase 0 acceptance is `/health` returning 200 on Railway; the routers below are
the surface the Phase 2 dashboard talks to.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from core.config import settings
from core.db import session_scope
from core.storage import get_storage

from .routes import analytics, clips, review, sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="clip-engine", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dashboard is the only client; tighten before Phase 3
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sources.router)
app.include_router(clips.router)
app.include_router(review.router)
app.include_router(analytics.router)


@app.exception_handler(RuntimeError)
def missing_infrastructure(request: Request, exc: RuntimeError) -> JSONResponse:
    """A route that needs Postgres or Redis should say so, not 500 blankly."""
    message = str(exc)
    if "DATABASE_URL" in message or "REDIS_URL" in message:
        return JSONResponse(status_code=503, content={"detail": message})
    raise exc


@app.get("/health")
def health() -> dict[str, object]:
    """Liveness plus a readout of which subsystems are actually wired up."""
    db_ok = False
    if settings.has_db:
        try:
            with session_scope() as session:
                session.execute(text("select 1"))
            db_ok = True
        except Exception:  # noqa: BLE001 - health must not raise
            db_ok = False

    return {
        "status": "ok",
        "env": settings.env,
        "db": db_ok,
        "db_configured": settings.has_db,
        "redis_configured": settings.has_redis,
        "storage": get_storage().kind,
    }
