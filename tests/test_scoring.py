from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.scoring import (
    hot_segments,
    hours_since,
    like_rate,
    parse_iso8601_duration,
    peak_heat,
    score_video,
    views_per_hour,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def test_parse_iso8601_duration():
    assert parse_iso8601_duration("PT1H2M10S") == 3730
    assert parse_iso8601_duration("PT45S") == 45
    assert parse_iso8601_duration("PT22M") == 1320
    assert parse_iso8601_duration("P1DT2H") == 93600
    assert parse_iso8601_duration(None) is None
    assert parse_iso8601_duration("nonsense") is None


def test_hours_since_never_returns_zero():
    assert hours_since(NOW - timedelta(hours=5), NOW) == pytest.approx(5)
    # A just-published video must not divide by zero downstream.
    assert hours_since(NOW, NOW) > 0
    assert hours_since(None) is None


def test_velocity_prefers_the_recent_window_over_the_lifetime_average():
    published = NOW - timedelta(days=30)
    lifetime = views_per_hour(720_000, published, now=NOW)
    recent = views_per_hour(
        720_000, published, previous_views=700_000, previous_at=NOW - timedelta(hours=1), now=NOW
    )
    assert lifetime == pytest.approx(1000, rel=0.01)
    assert recent == pytest.approx(20_000, rel=0.01)


def test_velocity_never_goes_negative_on_a_corrected_count():
    recent = views_per_hour(
        900, None, previous_views=1000, previous_at=NOW - timedelta(hours=1), now=NOW
    )
    assert recent == 0.0


def test_like_rate():
    assert like_rate(50, 1000) == 0.05
    assert like_rate(5, 0) is None
    assert like_rate(None, 100) is None


def test_score_rewards_fast_well_liked_recent_videos():
    fresh = score_video(8000, 0.09, NOW - timedelta(hours=6), heat_peak=0.95, now=NOW)
    slow = score_video(50, 0.01, NOW - timedelta(hours=6), heat_peak=0.2, now=NOW)
    assert fresh > slow
    assert 0 <= slow < fresh <= 100


def test_score_penalises_age():
    recent = score_video(5000, 0.07, NOW - timedelta(days=2), heat_peak=0.8, now=NOW)
    ancient = score_video(5000, 0.07, NOW - timedelta(days=900), heat_peak=0.8, now=NOW)
    assert ancient < recent


def _curve(peak_at: float, total: float = 600.0, width: float = 0.02):
    """A heatmap with a single hump centred on `peak_at` (0-1 of duration)."""
    markers = []
    for i in range(100):
        position = i / 100
        value = 0.2 + 0.8 * max(0.0, 1 - abs(position - peak_at) / width)
        markers.append(
            {"start": position * total, "end": (position + 0.01) * total, "value": round(value, 3)}
        )
    return markers


def test_hot_segments_finds_the_peak_and_respects_clip_bounds():
    segments = hot_segments(_curve(0.5), min_len_s=15, max_len_s=60)
    assert segments
    top = segments[0]
    assert 15 <= top["end_s"] - top["start_s"] <= 60
    # the hump sits at the halfway mark of a 600s video
    assert 250 <= top["start_s"] <= 320


def test_hot_segments_pads_a_short_spike_up_to_the_minimum():
    segments = hot_segments(_curve(0.5, width=0.005), min_len_s=20, max_len_s=60)
    assert segments
    assert segments[0]["end_s"] - segments[0]["start_s"] >= 20


def test_hot_segments_handles_a_peak_at_the_very_start():
    segments = hot_segments(_curve(0.0, total=300), min_len_s=15, max_len_s=60)
    assert segments
    assert segments[0]["start_s"] >= 0


def test_hot_segments_returns_nothing_without_a_heatmap():
    assert hot_segments(None) == []
    assert hot_segments([]) == []
    # a flat curve never crosses the threshold
    flat = [{"start": i, "end": i + 1, "value": 0.3} for i in range(60)]
    assert hot_segments(flat) == []


def test_hot_segments_accepts_yt_dlp_field_names():
    raw = [
        {"start_time": 10.0, "end_time": 12.5, "value": 0.9},
        {"start_time": 12.5, "end_time": 15.0, "value": 0.95},
    ]
    assert hot_segments(raw, min_len_s=1, max_len_s=60)


def test_peak_heat():
    assert peak_heat([{"value": 0.2}, {"value": 0.77}]) == 0.77
    assert peak_heat(None) is None
