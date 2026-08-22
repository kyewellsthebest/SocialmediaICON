"""Stage 8 - pull performance back in.

Only official insights APIs. YouTube is implemented because its Data API returns
public counters for free; the reseller platforms need their own analytics
endpoints, which is why `PROVIDER_UNSUPPORTED` is explicit rather than silent.

Snapshots are append-only on the schedule below - never overwrite a reading, the
time series is the whole point.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from core.db import session_scope
from core.models import MetricSnapshot, Post
from core.scoring import hours_since

log = logging.getLogger(__name__)

SNAPSHOT_SCHEDULE_S = (
    5 * 60,
    15 * 60,
    30 * 60,
    60 * 60,
    3 * 60 * 60,
    6 * 60 * 60,
    12 * 60 * 60,
    24 * 60 * 60,
    48 * 60 * 60,
)

PROVIDER_UNSUPPORTED = (
    "no public insights API wired up for this platform yet - "
    "YouTube works today, the rest need their own analytics endpoints"
)


def run(post_id: int) -> int | None:
    """Take one reading for one post. Returns the snapshot id, or None."""
    from core import youtube

    with session_scope() as session:
        post = session.get(Post, post_id)
        if post is None:
            raise ValueError(f"no post {post_id}")
        platform, external_id = post.platform, post.platform_post_id

    if platform != "youtube":
        log.info("skipping metrics for post %s on %s: %s", post_id, platform, PROVIDER_UNSUPPORTED)
        return None
    if not external_id:
        return None

    stats = youtube.video_stats([external_id]).get(external_id)
    if not stats:
        return None

    with session_scope() as session:
        snapshot = MetricSnapshot(
            post_id=post_id,
            views=stats["views"],
            likes=stats["likes"],
            comments=stats["comments"],
        )
        session.add(snapshot)
        session.flush()
        snapshot_id = snapshot.id

    log.info("post %s: %s views", post_id, stats["views"])
    return snapshot_id


def due_posts(now: datetime | None = None) -> list[int]:
    """Posts whose next scheduled reading has come due.

    A post is due when more of the schedule's checkpoints have elapsed since
    posting than we have snapshots for it.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=SNAPSHOT_SCHEDULE_S[-1])
    due: list[int] = []

    with session_scope() as session:
        rows = session.execute(
            select(Post.id, Post.posted_at, func.count(MetricSnapshot.id))
            .outerjoin(MetricSnapshot, MetricSnapshot.post_id == Post.id)
            .where(Post.status == "posted", Post.posted_at.is_not(None), Post.posted_at >= cutoff)
            .group_by(Post.id, Post.posted_at)
        ).all()

    for post_id, posted_at, snapshot_count in rows:
        elapsed_s = (hours_since(posted_at, now) or 0) * 3600
        checkpoints_passed = sum(1 for mark in SNAPSHOT_SCHEDULE_S if elapsed_s >= mark)
        if checkpoints_passed > snapshot_count:
            due.append(post_id)

    return due


def collect_due(now: datetime | None = None) -> int:
    """Scheduler entrypoint: take every reading that is currently due."""
    taken = 0
    for post_id in due_posts(now):
        try:
            if run(post_id) is not None:
                taken += 1
        except Exception as exc:  # noqa: BLE001 - one post must not stop the sweep
            log.warning("metrics failed for post %s: %s", post_id, exc)
    return taken
