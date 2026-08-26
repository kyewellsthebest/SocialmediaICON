"""Dashboard overview - the numbers that answer "is it working".""" 

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.config import settings
from core.db import get_db
from core.models import Clip, MetricSnapshot, Post, Source, TrackedVideo
from core.youtube import DAILY_QUOTA, units_used_today

router = APIRouter(prefix="/overview", tags=["overview"])

# The spend figure is an estimate from COST_FIXED_MONTHLY and COST_PER_SOURCE,
# not a bill from anyone. Fixed is what you pay whatever happens - host,
# storage, proxies - and per-source is the transcription and model calls one
# video costs to process. Both are settings, because guessing them in code and
# then showing the guess as a number is how a dashboard starts lying.


@router.get("")
def overview(db: Session = Depends(get_db)) -> dict[str, Any]:
    now = datetime.now(UTC)
    day_ago = now - timedelta(days=1)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    clips_by_status = dict(db.query(Clip.status, func.count(Clip.id)).group_by(Clip.status).all())
    sources_by_status = dict(
        db.query(Source.status, func.count(Source.id)).group_by(Source.status).all()
    )

    posts_total = db.query(func.count(Post.id)).filter(Post.status == "posted").scalar() or 0
    posts_24h = (
        db.query(func.count(Post.id))
        .filter(Post.status == "posted", Post.posted_at >= day_ago)
        .scalar()
        or 0
    )
    posts_failed = db.query(func.count(Post.id)).filter(Post.status == "failed").scalar() or 0

    # Latest snapshot per post, summed - the closest thing to "total views".
    latest = (
        select(
            MetricSnapshot.post_id.label("post_id"),
            func.max(MetricSnapshot.captured_at).label("captured_at"),
        )
        .group_by(MetricSnapshot.post_id)
        .subquery()
    )
    total_views = (
        db.query(func.coalesce(func.sum(MetricSnapshot.views), 0))
        .join(
            latest,
            (MetricSnapshot.post_id == latest.c.post_id)
            & (MetricSnapshot.captured_at == latest.c.captured_at),
        )
        .scalar()
        or 0
    )

    sources_this_month = (
        db.query(func.count(Source.id)).filter(Source.created_at >= month_start).scalar() or 0
    )

    tracked = db.query(func.count(TrackedVideo.id)).scalar() or 0
    tracked_new = (
        db.query(func.count(TrackedVideo.id)).filter(TrackedVideo.status == "new").scalar() or 0
    )

    quota_used = units_used_today()

    return {
        "clips": {
            "total": sum(clips_by_status.values()),
            "by_status": clips_by_status,
            "awaiting_review": clips_by_status.get("queued", 0)
            + clips_by_status.get("rendered", 0),
            "approved": clips_by_status.get("approved", 0),
        },
        "sources": {"by_status": sources_by_status, "this_month": sources_this_month},
        "posts": {
            "total": posts_total,
            "last_24h": posts_24h,
            "failed": posts_failed,
            "total_views": int(total_views),
        },
        "tracking": {"total": tracked, "new": tracked_new},
        "quota": {
            "youtube_used": quota_used,
            "youtube_limit": DAILY_QUOTA,
            "pct": round(100 * quota_used / DAILY_QUOTA, 1),
        },
        "spend": {
            "estimate_month": round(
                settings.cost_fixed_monthly + sources_this_month * settings.cost_per_source, 2
            ),
            "budget": settings.monthly_budget,
            "fixed": settings.cost_fixed_monthly,
            "per_source": settings.cost_per_source,
            "sources_this_month": sources_this_month,
            "note": (
                f"estimate, not a bill: ${settings.cost_fixed_monthly:.0f} fixed + "
                f"{sources_this_month} sources x ${settings.cost_per_source:.2f}"
            ),
        },
        "config": {
            "env": settings.env,
            "publisher": settings.publisher,
            "autopost": settings.autopost_enabled,
            "scout": settings.scout_enabled and settings.has_youtube_read,
            "scout_every_min": settings.scout_interval_minutes,
            "storage": "r2" if settings.has_r2 else "local",
        },
    }


@router.get("/activity")
def activity(limit: int = 12, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """A merged, newest-first feed of what the machine has been doing."""
    events: list[dict[str, Any]] = []

    for source in db.query(Source).order_by(Source.id.desc()).limit(limit).all():
        events.append(
            {
                "at": source.created_at.isoformat(),
                "kind": "source",
                "status": source.status,
                "text": source.title or source.url,
                "id": source.id,
            }
        )
    for clip in db.query(Clip).order_by(Clip.id.desc()).limit(limit).all():
        events.append(
            {
                "at": clip.created_at.isoformat(),
                "kind": "clip",
                "status": clip.status,
                "text": clip.title or f"clip {clip.id}",
                "id": clip.id,
            }
        )
    for post in db.query(Post).order_by(Post.id.desc()).limit(limit).all():
        events.append(
            {
                "at": (post.posted_at or post.created_at).isoformat(),
                "kind": "post",
                "status": post.status,
                "text": f"{post.platform}: {post.platform_post_id or post.error or 'pending'}",
                "id": post.id,
            }
        )

    events.sort(key=lambda e: e["at"], reverse=True)
    return events[:limit]
