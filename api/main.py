"""FastAPI app: JSON API under /api, dashboard at /.

One Railway service serves both - the dashboard is static files talking to the
same API, which keeps the deploy to a single web process.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from core.config import settings
from core.db import session_scope
from core.storage import get_storage

from .deps import require_token
from .routes import analytics, clips, overview, review, sources, trending
from .routes import settings as settings_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="clip-engine",
    version="0.2.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Every data route sits behind the dashboard token.
protected = [Depends(require_token)]
for router in (
    overview.router,
    trending.router,
    sources.router,
    clips.router,
    review.router,
    analytics.router,
    settings_routes.router,
):
    app.include_router(router, prefix="/api", dependencies=protected)


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
        "publisher": settings.publisher,
        "secured": bool(settings.dashboard_token),
    }


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")
