"""Pure maths for ranking what to clip. No network, no DB — easy to test.

Two different jobs live here:
  * scoring a *source* video (is this worth clipping at all)
  * finding the hot moments inside it (which 30 seconds)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def parse_iso8601_duration(value: str | None) -> float | None:
    """YouTube returns durations like PT1H2M10S."""
    if not value:
        return None
    match = ISO_DURATION.match(value.strip())
    if not match:
        return None
    parts = {k: int(v) if v else 0 for k, v in match.groupdict().items()}
    total = parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]
    return float(total)


def hours_since(moment: datetime | None, now: datetime | None = None) -> float | None:
    if moment is None:
        return None
    now = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    delta = (now - moment).total_seconds() / 3600
    return max(delta, 0.01)


def views_per_hour(
    views: int | None,
    published_at: datetime | None,
    previous_views: int | None = None,
    previous_at: datetime | None = None,
    now: datetime | None = None,
) -> float | None:
    """Velocity.

    With two readings this is the *recent* rate, which is what actually says
    "this is taking off right now". With only one it falls back to the lifetime
    average, which flatters old videos — hence the recency penalty in `score`.
    """
    if views is None:
        return None
    if previous_views is not None and previous_at is not None:
        elapsed = hours_since(previous_at, now)
        if elapsed and elapsed > 0.05:
            delta = views - previous_views
            return max(delta / elapsed, 0.0)
    lifetime = hours_since(published_at, now)
    if not lifetime:
        return None
    return views / lifetime


def like_rate(likes: int | None, views: int | None) -> float | None:
    """Likes per view. Healthy short-form usually lands around 0.05–0.10."""
    if not views or likes is None:
        return None
    return likes / views


def score_video(
    velocity_vph: float | None,
    like_ratio: float | None,
    published_at: datetime | None,
    heat_peak: float | None = None,
    now: datetime | None = None,
) -> float:
    """Composite 0–100 used to order the trending table.

    Velocity says people are watching it now, like-rate says they liked what
    they watched, the heat peak says there is a specific moment worth cutting,
    and the age penalty stops a three-year-old megahit sitting at the top
    forever.
    """
    # Velocity is heavily skewed, so compress it: 10k views/hour ~ full marks.
    velocity_points = 0.0
    if velocity_vph:
        velocity_points = min(1.0, (velocity_vph / 10_000) ** 0.5)

    # 10% like-rate is excellent, 2% is ordinary.
    like_points = min(1.0, (like_ratio or 0) / 0.10)

    heat_points = min(1.0, max(0.0, heat_peak or 0.0))

    age_h = hours_since(published_at, now) or 1.0
    # Full marks under a week old, tailing off after that.
    recency = 1.0 if age_h <= 168 else max(0.25, (168 / age_h) ** 0.4)

    raw = 0.45 * velocity_points + 0.25 * like_points + 0.30 * heat_points
    return round(100 * raw * recency, 1)


def hot_segments(
    heatmap: list[dict[str, Any]] | None,
    min_len_s: float = 15.0,
    max_len_s: float = 60.0,
    threshold: float = 0.55,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Turn YouTube's most-replayed curve into clippable windows.

    The curve is ~100 markers with a 0–1 intensity. Contiguous runs above the
    threshold are the bits people rewind to; each run is padded out to at least
    `min_len_s` (a 5-second spike is a moment, not a clip) and capped at
    `max_len_s`.
    """
    if not heatmap:
        return []

    markers = sorted(
        (
            {
                "start": float(m["start_time"] if "start_time" in m else m["start"]),
                "end": float(m["end_time"] if "end_time" in m else m["end"]),
                "value": float(m.get("value") or 0.0),
            }
            for m in heatmap
            if m
        ),
        key=lambda m: m["start"],
    )
    if not markers:
        return []

    total_end = markers[-1]["end"]
    runs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for marker in markers:
        if marker["value"] >= threshold:
            if current is None:
                current = {
                    "start_s": marker["start"],
                    "end_s": marker["end"],
                    "value": marker["value"],
                }
            else:
                current["end_s"] = marker["end"]
                current["value"] = max(current["value"], marker["value"])
        elif current is not None:
            runs.append(current)
            current = None
    if current is not None:
        runs.append(current)

    windows: list[dict[str, Any]] = []
    for run in runs:
        start, end = run["start_s"], run["end_s"]
        if end - start > max_len_s:
            end = start + max_len_s
        if end - start < min_len_s:
            # Grow around the peak, keeping a little more lead-in than tail:
            # the hook needs the run-up to make sense.
            shortfall = min_len_s - (end - start)
            start = max(0.0, start - shortfall * 0.6)
            end = min(total_end, end + shortfall * 0.4)
            if end - start < min_len_s:
                start = max(0.0, min(start, total_end - min_len_s))
                end = min(total_end, start + min_len_s)
        if end - start >= min_len_s * 0.8:
            windows.append(
                {
                    "start_s": round(start, 2),
                    "end_s": round(end, 2),
                    "value": round(run["value"], 3),
                }
            )

    windows.sort(key=lambda w: w["value"], reverse=True)
    return windows[:top_k]


def peak_heat(heatmap: list[dict[str, Any]] | None) -> float | None:
    if not heatmap:
        return None
    values = [float(m.get("value") or 0.0) for m in heatmap if m]
    return max(values) if values else None
