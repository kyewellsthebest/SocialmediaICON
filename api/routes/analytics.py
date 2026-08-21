"""Analytics (Phase 3+).

The endpoints exist so the dashboard has a stable shape to build against; they
report emptiness rather than pretending, until publishing is live.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.db import get_db
from core.models import Clip, MetricSnapshot, Post

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary")
def summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    clips_by_status = dict(
        db.query(Clip.status, func.count(Clip.id)).group_by(Clip.status).all()
    )
    return {
        "clips_by_status": clips_by_status,
        "posts": db.query(func.count(Post.id)).scalar() or 0,
        "metric_snapshots": db.query(func.count(MetricSnapshot.id)).scalar() or 0,
        "note": "Per-post metrics arrive in Phase 3, once publishing is approved.",
    }


@router.get("/posts")
def posts(limit: int = 50, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.query(Post).order_by(Post.id.desc()).limit(limit).all()
    return [
        {
            "id": post.id,
            "clip_id": post.clip_id,
            "platform": post.platform,
            "platform_post_id": post.platform_post_id,
            "posted_at": post.posted_at.isoformat() if post.posted_at else None,
            "status": post.status,
            "snapshots": len(post.snapshots),
        }
        for post in rows
    ]
