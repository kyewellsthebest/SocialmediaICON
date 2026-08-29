"""What the bot is watching, and what it has caught.

Two views onto the same run: `/live` is what it can see right now - the
streams, their chat, every signal it is scoring - and `/catches` is what it
decided was worth keeping.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core.config import settings
from core.db import get_db
from core.models import Catch
from worker.queue import enqueue

log = logging.getLogger(__name__)

router = APIRouter(prefix="/live", tags=["live"])


@router.get("")
def status() -> dict[str, Any]:
    """Everything the watcher can see, for the Live view."""
    from worker.tasks.live_watch import current

    supervisor = current()
    if supervisor is None or not supervisor.running:
        return {
            "running": False,
            "enabled": settings.live_enabled,
            "slots": settings.live_slots,
            "posting_enabled": settings.live_posting_enabled,
            "caps": {
                "per_day": settings.live_clips_per_day,
                "min_gap_minutes": settings.live_min_gap_minutes,
            },
            "streams": [],
            "errors": [],
            "hint": (
                "Set LIVE_ENABLED=true and press Start."
                if not settings.live_enabled
                else "Not running - press Start."
            ),
        }
    return supervisor.status()


@router.post("/start")
def start() -> dict[str, Any]:
    if not settings.live_enabled:
        raise HTTPException(400, "LIVE_ENABLED is not set on this service")
    enqueue("worker.tasks.live_watch.run")
    return {"ok": True, "queued": True}


@router.post("/stop")
def stop() -> dict[str, Any]:
    from worker.tasks.live_watch import stop as stop_watching

    return stop_watching()


@router.get("/catches")
def catches(
    limit: int = Query(30, ge=1, le=200), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    """What has been caught, newest first."""
    rows = db.query(Catch).order_by(Catch.id.desc()).limit(limit).all()
    return [_row(c) for c in rows]


@router.get("/catches/{catch_id}/video")
def video(catch_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """The clip itself, so it can be watched in the dashboard."""
    row = db.get(Catch, catch_id)
    if row is None or not row.storage_key:
        raise HTTPException(404, "no such clip")
    path = Path(row.storage_key)
    if not path.exists():
        raise HTTPException(410, "the clip file is gone from this service's disk")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.post("/catches/{catch_id}/keep")
def keep(catch_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(Catch, catch_id)
    if row is None:
        raise HTTPException(404, "no such clip")
    row.approved = True
    row.status = "kept"
    db.flush()
    return _row(row)


@router.delete("/catches/{catch_id}")
def discard(catch_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(Catch, catch_id)
    if row is None:
        raise HTTPException(404, "no such clip")
    # Delete the file too: a rejected clip is the one thing here with no
    # reason to occupy disk.
    if row.storage_key:
        Path(row.storage_key).unlink(missing_ok=True)
    db.delete(row)
    return {"ok": True}


def _row(c: Catch) -> dict[str, Any]:
    return {
        "id": c.id,
        "channel": c.channel,
        "source_url": c.source_url,
        "at_s": c.at_s,
        "duration_s": c.duration_s,
        "score": c.score,
        "why": c.why or {},
        "mood": c.mood or {},
        "quotes": c.quotes or [],
        "peak_viewers": c.peak_viewers,
        "status": c.status,
        "approved": c.approved,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "has_video": bool(c.storage_key and Path(c.storage_key).exists()),
    }
