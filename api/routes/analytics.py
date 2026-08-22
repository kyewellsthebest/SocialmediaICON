"""Per-post performance, built from metric snapshots."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.db import get_db
from core.models import Clip, MetricSnapshot, Post

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _series(db: Session, post_id: int) -> list[dict[str, Any]]:
    rows = (
        db.execute(
            select(MetricSnapshot)
            .where(MetricSnapshot.post_id == post_id)
            .order_by(MetricSnapshot.captured_at.asc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "t": row.captured_at.isoformat(),
            "views": row.views or 0,
            "likes": row.likes or 0,
            "comments": row.comments or 0,
        }
        for row in rows
    ]


@router.get("/posts")
def posts(limit: int = 50, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.query(Post).order_by(Post.id.desc()).limit(limit).all()
    payload: list[dict[str, Any]] = []

    for post in rows:
        clip = db.get(Clip, post.clip_id)
        series = _series(db, post.id)
        latest = series[-1] if series else {"views": 0, "likes": 0, "comments": 0}
        views = latest["views"] or 0
        payload.append(
            {
                "id": post.id,
                "clip_id": post.clip_id,
                "title": clip.title if clip else None,
                "platform": post.platform,
                "platform_post_id": post.platform_post_id,
                "url": post.platform_url,
                "posted_at": post.posted_at.isoformat() if post.posted_at else None,
                "status": post.status,
                "error": post.error,
                "views": views,
                "likes": latest["likes"] or 0,
                "comments": latest["comments"] or 0,
                "like_rate": round(latest["likes"] / views, 4) if views else None,
                "series": series,
            }
        )
    return payload


@router.get("/platforms")
def platforms(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Totals per platform, using each post's most recent snapshot."""
    latest = (
        select(
            MetricSnapshot.post_id.label("post_id"),
            func.max(MetricSnapshot.captured_at).label("captured_at"),
        )
        .group_by(MetricSnapshot.post_id)
        .subquery()
    )

    rows = (
        db.query(
            Post.platform,
            func.count(func.distinct(Post.id)),
            func.coalesce(func.sum(MetricSnapshot.views), 0),
            func.coalesce(func.sum(MetricSnapshot.likes), 0),
        )
        .outerjoin(
            latest,
            latest.c.post_id == Post.id,
        )
        .outerjoin(
            MetricSnapshot,
            (MetricSnapshot.post_id == latest.c.post_id)
            & (MetricSnapshot.captured_at == latest.c.captured_at),
        )
        .filter(Post.status == "posted")
        .group_by(Post.platform)
        .all()
    )

    return [
        {
            "platform": platform,
            "posts": int(count),
            "views": int(views),
            "likes": int(likes),
            "avg_views": round(int(views) / count, 1) if count else 0,
        }
        for platform, count, views, likes in rows
    ]
