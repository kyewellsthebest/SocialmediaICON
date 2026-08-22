"""YouTube Data API v3 — the one platform that will tell you what is winning.

Free up to 10,000 quota units a day, which is plenty *if* you respect the cost
table: a search costs 100 units, a stats lookup costs 1 for up to 50 videos.
Every call goes through `_spend`, which refuses to start a call that would blow
the daily budget rather than discovering it mid-run.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.config import settings
from core.db import session_scope
from core.models import ApiQuota
from core.scoring import parse_iso8601_duration

log = logging.getLogger(__name__)

BASE = "https://www.googleapis.com/youtube/v3"

# Cost per call, from the API's own quota table.
COST_SEARCH = 100
COST_VIDEOS = 1
COST_UPLOAD = 100  # cut from ~1600 in Dec 2025 — verify before relying on it

DAILY_QUOTA = 10_000


class QuotaExceeded(RuntimeError):
    pass


class YouTubeError(RuntimeError):
    pass


def units_used_today(service: str = "youtube") -> int:
    if not settings.has_db:
        return 0
    with session_scope() as session:
        row = session.execute(
            select(ApiQuota).where(ApiQuota.day == date.today(), ApiQuota.service == service)
        ).scalar_one_or_none()
        return row.units if row else 0


def _spend(units: int, service: str = "youtube") -> None:
    """Record quota spend, refusing to go over the daily allowance."""
    if not settings.has_db:
        return
    with session_scope() as session:
        stmt = (
            pg_insert(ApiQuota)
            .values(day=date.today(), service=service, units=units)
            .on_conflict_do_update(
                index_elements=["day", "service"],
                set_={"units": ApiQuota.__table__.c.units + units},
            )
            .returning(ApiQuota.units)
        )
        total = session.execute(stmt).scalar_one()
    if total > DAILY_QUOTA:
        raise QuotaExceeded(
            f"YouTube quota for today would reach {total}/{DAILY_QUOTA} units. "
            "Scouting is paused until midnight Pacific, when the quota resets."
        )


def _get(path: str, params: dict[str, Any], cost: int) -> dict[str, Any]:
    if not settings.youtube_api_key:
        raise YouTubeError("YOUTUBE_API_KEY is not set")
    _spend(cost)
    params = {**params, "key": settings.youtube_api_key}
    with httpx.Client(timeout=30.0) as client:
        response = client.get(f"{BASE}/{path}", params=params)
    if response.status_code == 403:
        raise QuotaExceeded(f"YouTube API refused the call: {response.text[:300]}")
    response.raise_for_status()
    return response.json()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def search(
    query: str,
    published_within_days: int = 30,
    max_results: int = 25,
    order: str = "viewCount",
    region_code: str | None = None,
    video_duration: str = "medium",
) -> list[str]:
    """Return video ids matching a niche keyword.

    `video_duration=medium` is 4–20 minutes; use "long" for podcasts and streams.
    Costs 100 units — the expensive call, so scout runs batch keywords carefully.
    """
    published_after = (
        datetime.now(UTC) - timedelta(days=published_within_days)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")

    params: dict[str, Any] = {
        "part": "id",
        "q": query,
        "type": "video",
        "order": order,
        "maxResults": min(50, max_results),
        "publishedAfter": published_after,
        "videoDuration": video_duration,
    }
    if region_code:
        params["regionCode"] = region_code

    payload = _get("search", params, COST_SEARCH)
    return [
        item["id"]["videoId"]
        for item in payload.get("items", [])
        if item.get("id", {}).get("videoId")
    ]


def videos(video_ids: list[str]) -> list[dict[str, Any]]:
    """Stats + snippet for up to 50 ids at a time. 1 unit per call."""
    results: list[dict[str, Any]] = []
    for start in range(0, len(video_ids), 50):
        batch = video_ids[start : start + 50]
        payload = _get(
            "videos",
            {"part": "snippet,statistics,contentDetails", "id": ",".join(batch)},
            COST_VIDEOS,
        )
        for item in payload.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            results.append(
                {
                    "external_id": item["id"],
                    "url": f"https://www.youtube.com/watch?v={item['id']}",
                    "title": snippet.get("title"),
                    "channel_id": snippet.get("channelId"),
                    "channel_title": snippet.get("channelTitle"),
                    "thumbnail_url": (
                        snippet.get("thumbnails", {}).get("high", {}).get("url")
                        or snippet.get("thumbnails", {}).get("default", {}).get("url")
                    ),
                    "published_at": _parse_dt(snippet.get("publishedAt")),
                    "duration_s": parse_iso8601_duration(
                        item.get("contentDetails", {}).get("duration")
                    ),
                    "views": int(stats["viewCount"]) if "viewCount" in stats else None,
                    "likes": int(stats["likeCount"]) if "likeCount" in stats else None,
                    "comments": int(stats["commentCount"]) if "commentCount" in stats else None,
                }
            )
    return results


def video_stats(video_ids: list[str]) -> dict[str, dict[str, int | None]]:
    """Just the counters — used for metric snapshots of your own posts."""
    return {
        row["external_id"]: {
            "views": row["views"],
            "likes": row["likes"],
            "comments": row["comments"],
        }
        for row in videos(video_ids)
    }
