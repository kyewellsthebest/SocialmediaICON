"""YouTube's most-replayed curve, straight from yt-dlp — no vendor, no key.

yt-dlp exposes a `heatmap` field: ~100 markers of about 2.5s each with a
normalised 0–1 intensity. It is the closest thing to retention data available
for a video you do not own, and it is free.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def fetch_metadata(url: str, with_heatmap: bool = True) -> dict[str, Any]:
    """Public metadata for a video, including the heatmap when YouTube shows one.

    Returns `{}` on failure: a missing heatmap must never take down a scout run.
    """
    import yt_dlp

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises a wide range
        log.warning("metadata fetch failed for %s: %s", url, exc)
        return {}

    if not info:
        return {}

    result: dict[str, Any] = {
        "title": info.get("title"),
        "duration_s": float(info["duration"]) if info.get("duration") else None,
        "views": info.get("view_count"),
        "likes": info.get("like_count"),
        "comments": info.get("comment_count"),
        "channel_title": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id"),
        "thumbnail_url": info.get("thumbnail"),
    }

    if with_heatmap:
        result["heatmap"] = normalise_heatmap(info.get("heatmap"))
    return result


def normalise_heatmap(raw: Any) -> list[dict[str, float]] | None:
    """yt-dlp gives start_time / end_time / value; store a compact version."""
    if not raw:
        return None
    out: list[dict[str, float]] = []
    for marker in raw:
        try:
            out.append(
                {
                    "start": round(float(marker["start_time"]), 2),
                    "end": round(float(marker["end_time"]), 2),
                    "value": round(float(marker["value"]), 4),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out or None
