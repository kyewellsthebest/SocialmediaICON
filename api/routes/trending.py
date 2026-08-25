"""Trending source videos - what the scout found."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.db import get_db
from core.models import TrackedSnapshot, TrackedVideo

router = APIRouter(prefix="/trending", tags=["trending"])

SPARK_POINTS = 24


class ClipRequest(BaseModel):
    license: str = "none"


def _sparkline(session: Session, video_id: int) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            select(TrackedSnapshot)
            .where(TrackedSnapshot.tracked_video_id == video_id)
            .order_by(TrackedSnapshot.captured_at.desc())
            .limit(SPARK_POINTS)
        )
        .scalars()
        .all()
    )
    return [{"t": row.captured_at.isoformat(), "views": row.views or 0} for row in reversed(rows)]


def _serialise(session: Session, video: TrackedVideo, with_heatmap: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": video.id,
        "platform": video.platform,
        "external_id": video.external_id,
        "url": video.url,
        "title": video.title,
        "channel_title": video.channel_title,
        "thumbnail_url": video.thumbnail_url,
        "published_at": video.published_at.isoformat() if video.published_at else None,
        "duration_s": video.duration_s,
        "views": video.views,
        "likes": video.likes,
        "comments": video.comments,
        "velocity_vph": video.velocity_vph,
        "like_rate": video.like_rate,
        "score": video.score,
        "status": video.status,
        "hot_segments": video.hot_segments or [],
        "has_heatmap": bool(video.heatmap),
        "series": _sparkline(session, video.id),
    }
    if with_heatmap:
        payload["heatmap"] = video.heatmap or []
    return payload


@router.get("")
def list_trending(
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    query = db.query(TrackedVideo)
    if status:
        query = query.filter(TrackedVideo.status == status)
    videos = query.order_by(TrackedVideo.score.desc().nullslast()).limit(limit).all()
    return [_serialise(db, video) for video in videos]


@router.get("/{video_id}")
def get_trending(video_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    video = db.get(TrackedVideo, video_id)
    if video is None:
        raise HTTPException(404, "tracked video not found")
    return _serialise(db, video, with_heatmap=True)


@router.post("/{video_id}/clip")
def clip_trending(
    video_id: int, payload: ClipRequest, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Send a tracked video into the clip pipeline."""
    from worker.tasks.scout import send_to_pipeline

    if db.get(TrackedVideo, video_id) is None:
        raise HTTPException(404, "tracked video not found")
    source_id = send_to_pipeline(video_id, payload.license)
    return {"source_id": source_id, "status": "queued"}


@router.post("/{video_id}/ignore")
def ignore_trending(video_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    video = db.get(TrackedVideo, video_id)
    if video is None:
        raise HTTPException(404, "tracked video not found")
    video.status = "ignored"
    db.flush()
    return {"id": video_id, "status": video.status}


@router.post("/scan")
def scan_now() -> dict[str, Any]:
    """Kick a scout run by hand instead of waiting for the schedule."""
    from core.config import settings
    from worker.queue import enqueue
    from worker.tasks.scout import run as scout_run

    if not settings.has_youtube_read:
        raise HTTPException(422, "YOUTUBE_API_KEY is not set")
    job = enqueue("metrics", scout_run)
    if job is None:  # no Redis configured - run it here
        found = scout_run()
        return {"queued": False, "discovered": found}
    return {"queued": True, "job_id": job.id}
