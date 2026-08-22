"""Stage 0 - find source videos worth clipping.

Uses only free data: the YouTube Data API for what is performing, and yt-dlp's
heatmap for where inside each video the good bit is. Nothing here touches a paid
scraper.

Quota discipline matters: a search costs 100 units of a 10,000/day allowance, so
a run does a handful of keyword searches and then leans on the 1-unit batch stats
call for everything else.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from core import youtube
from core.config import settings
from core.db import session_scope
from core.heatmap import fetch_metadata
from core.models import Niche, Source, TrackedSnapshot, TrackedVideo
from core.scoring import hot_segments as compute_hot_segments
from core.scoring import like_rate, peak_heat, score_video, views_per_hour
from worker.queue import enqueue

log = logging.getLogger(__name__)

# Fetching a heatmap is a full page load per video, so only the promising ones.
HEATMAP_BUDGET = 10


def _niche_id(session, niche_name: str | None) -> int | None:
    if not niche_name:
        return None
    niche = session.execute(select(Niche).where(Niche.name == niche_name)).scalar_one_or_none()
    if niche is None:
        niche = Niche(name=niche_name, config={})
        session.add(niche)
        session.flush()
    return niche.id


def scout(
    keywords: list[str] | None = None,
    niche_name: str | None = None,
    limit: int | None = None,
) -> list[int]:
    """Search, score and store. Returns the tracked_video ids touched."""
    keywords = keywords or settings.keywords
    if not keywords:
        log.warning("no SCOUT_KEYWORDS configured - nothing to look for")
        return []

    niche_name = niche_name or settings.default_niche
    limit = limit or settings.scout_track_limit
    now = datetime.now(UTC)

    video_ids: list[str] = []
    for keyword in keywords[: settings.scout_max_keywords]:
        try:
            found = youtube.search(
                keyword,
                max_results=max(10, limit // max(1, len(keywords[: settings.scout_max_keywords]))),
                region_code=settings.scout_region,
                video_duration=settings.scout_video_duration,
            )
        except youtube.QuotaExceeded as exc:
            log.warning("stopping scout: %s", exc)
            break
        except Exception as exc:  # noqa: BLE001 - one keyword must not kill the run
            log.warning("search failed for %r: %s", keyword, exc)
            continue
        log.info("keyword %r -> %d videos", keyword, len(found))
        video_ids.extend(found)

    # Preserve discovery order while removing duplicates across keywords.
    video_ids = list(dict.fromkeys(video_ids))[:limit]
    if not video_ids:
        return []

    rows = youtube.videos(video_ids)
    log.info("fetched stats for %d videos", len(rows))

    touched: list[int] = []
    with session_scope() as session:
        niche_id = _niche_id(session, niche_name)

        for row in rows:
            video = session.execute(
                select(TrackedVideo).where(
                    TrackedVideo.platform == "youtube",
                    TrackedVideo.external_id == row["external_id"],
                )
            ).scalar_one_or_none()

            previous_views = video.views if video else None
            previous_at = video.last_checked_at if video else None

            if video is None:
                video = TrackedVideo(
                    platform="youtube", external_id=row["external_id"], url=row["url"]
                )
                session.add(video)

            video.niche_id = niche_id
            video.title = row["title"]
            video.channel_id = row["channel_id"]
            video.channel_title = row["channel_title"]
            video.thumbnail_url = row["thumbnail_url"]
            video.published_at = row["published_at"]
            video.duration_s = row["duration_s"]
            video.views = row["views"]
            video.likes = row["likes"]
            video.comments = row["comments"]
            video.velocity_vph = views_per_hour(
                row["views"], row["published_at"], previous_views, previous_at, now
            )
            video.like_rate = like_rate(row["likes"], row["views"])
            video.score = score_video(
                video.velocity_vph,
                video.like_rate,
                row["published_at"],
                peak_heat(video.heatmap),
                now,
            )
            video.last_checked_at = now

            session.flush()
            session.add(
                TrackedSnapshot(
                    tracked_video_id=video.id,
                    views=row["views"],
                    likes=row["likes"],
                    comments=row["comments"],
                )
            )
            touched.append(video.id)

    enrich_heatmaps()
    return touched


def enrich_heatmaps(budget: int = HEATMAP_BUDGET) -> int:
    """Pull the most-replayed curve for the best videos that lack one.

    Free, but slow (a page fetch each), so it runs on a budget and picks the
    highest scoring videos first.
    """
    with session_scope() as session:
        pending = (
            session.execute(
                select(TrackedVideo)
                .where(TrackedVideo.heatmap.is_(None), TrackedVideo.status != "ignored")
                .order_by(TrackedVideo.score.desc().nullslast())
                .limit(budget)
            )
            .scalars()
            .all()
        )
        targets = [(v.id, v.url) for v in pending]

    enriched = 0
    for video_id, url in targets:
        meta = fetch_metadata(url)
        heat = meta.get("heatmap")
        if not heat:
            continue
        segments = compute_hot_segments(
            heat, min_len_s=settings.min_clip_s, max_len_s=settings.max_clip_s
        )
        with session_scope() as session:
            video = session.get(TrackedVideo, video_id)
            if video is None:
                continue
            video.heatmap = heat
            video.hot_segments = segments
            video.score = score_video(
                video.velocity_vph, video.like_rate, video.published_at, peak_heat(heat)
            )
        enriched += 1
        log.info("heatmap for %s -> %d hot segments", url, len(segments))

    return enriched


def refresh_tracked(limit: int = 50) -> int:
    """Re-read counters for videos already tracked. 1 quota unit per 50."""
    with session_scope() as session:
        rows = (
            session.execute(
                select(TrackedVideo)
                .where(TrackedVideo.status != "ignored")
                .order_by(TrackedVideo.last_checked_at.asc().nullsfirst())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        targets = {v.external_id: (v.id, v.views, v.last_checked_at) for v in rows}

    if not targets:
        return 0

    stats = youtube.video_stats(list(targets))
    now = datetime.now(UTC)

    with session_scope() as session:
        for external_id, (video_id, previous_views, previous_at) in targets.items():
            reading = stats.get(external_id)
            if not reading:
                continue
            video = session.get(TrackedVideo, video_id)
            if video is None:
                continue
            video.velocity_vph = views_per_hour(
                reading["views"], video.published_at, previous_views, previous_at, now
            )
            video.views = reading["views"]
            video.likes = reading["likes"]
            video.comments = reading["comments"]
            video.like_rate = like_rate(reading["likes"], reading["views"])
            video.score = score_video(
                video.velocity_vph,
                video.like_rate,
                video.published_at,
                peak_heat(video.heatmap),
                now,
            )
            video.last_checked_at = now
            session.add(
                TrackedSnapshot(
                    tracked_video_id=video.id,
                    views=reading["views"],
                    likes=reading["likes"],
                    comments=reading["comments"],
                )
            )
    return len(targets)


def send_to_pipeline(tracked_id: int, license_tag: str = "campaign") -> int:
    """Turn a tracked video into a source and start the clip pipeline.

    The license tag is required and is not defaulted to something permissive:
    only send videos you have the right to clip.
    """
    from worker.tasks.ingest import check_license
    from worker.tasks.ingest import run as ingest_run

    check_license(license_tag)

    with session_scope() as session:
        video = session.get(TrackedVideo, tracked_id)
        if video is None:
            raise ValueError(f"no tracked video {tracked_id}")
        source = Source(
            niche_id=video.niche_id,
            url=video.url,
            kind="youtube",
            license=license_tag,
            title=video.title,
            duration_s=video.duration_s,
        )
        session.add(source)
        session.flush()
        source_id = source.id
        video.status = "queued"

    enqueue("ingest", ingest_run, source_id)
    log.info("tracked video %s -> source %s", tracked_id, source_id)
    return source_id


def run() -> int:
    """Scheduler entrypoint."""
    touched = scout()
    refreshed = refresh_tracked()
    log.info("scout run: %d discovered, %d refreshed", len(touched), refreshed)
    return len(touched)
