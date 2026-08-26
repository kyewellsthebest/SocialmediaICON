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


class TestKeywordRotation:
    """Every keyword must get searched eventually, not just the first few."""

    def _window(self, keywords, size, slot):
        from datetime import UTC, datetime

        from core.config import settings
        from worker.tasks.scout import keywords_for_run

        # One slot per scout interval, counted from the epoch.
        when = datetime.fromtimestamp(slot * settings.scout_interval_minutes * 60, tz=UTC)
        return keywords_for_run(keywords, size, when)

    def test_a_short_list_is_searched_whole(self):
        words = ["a", "b", "c"]
        assert self._window(words, 4, 0) == words

    def test_the_window_walks_along_the_list(self):
        words = list("abcdefgh")
        assert self._window(words, 4, 0) == ["a", "b", "c", "d"]
        assert self._window(words, 4, 1) == ["e", "f", "g", "h"]
        assert self._window(words, 4, 2) == ["a", "b", "c", "d"]

    def test_every_keyword_is_reached_when_the_list_does_not_divide_evenly(self):
        words = list("abcdefg")
        seen = set()
        for slot in range(20):
            seen.update(self._window(words, 3, slot))
        assert seen == set(words)

    def test_the_window_wraps_rather_than_running_short(self):
        words = list("abcde")
        window = self._window(words, 3, 1)
        assert len(window) == 3
        assert window == ["d", "e", "a"]


class TestCommentRate:
    """Liking costs one tap; commenting means the video provoked something."""

    def test_comments_per_view(self):
        from core.scoring import comment_rate

        assert comment_rate(500, 100_000) == pytest.approx(0.005)

    def test_no_views_means_no_rate_rather_than_a_crash(self):
        from core.scoring import comment_rate

        assert comment_rate(10, 0) is None
        assert comment_rate(10, None) is None
        assert comment_rate(None, 1000) is None

    def test_a_talked_about_video_outscores_a_merely_liked_one(self):
        from datetime import UTC, datetime

        from core.scoring import score_video

        now = datetime(2026, 8, 26, tzinfo=UTC)
        published = datetime(2026, 8, 24, tzinfo=UTC)
        common = dict(velocity_vph=500.0, like_ratio=0.05, published_at=published, now=now)

        talked_about = score_video(**common, comment_ratio=0.01)
        merely_liked = score_video(**common, comment_ratio=0.0002)

        assert talked_about > merely_liked


class TestQualityGate:
    """Copying a video nobody watched copies noise."""

    def _row(self, **kw):
        base = {"views": 500_000, "language": "en"}
        base.update(kw)
        return base

    def test_a_video_below_the_view_floor_is_dropped(self, monkeypatch):
        from core.config import settings
        from worker.tasks.scout import wanted

        monkeypatch.setattr(settings, "scout_min_views", 100_000, raising=False)

        assert wanted(self._row(views=500_000))
        assert not wanted(self._row(views=3_000))

    def test_another_language_is_dropped(self, monkeypatch):
        from core.config import settings
        from worker.tasks.scout import wanted

        monkeypatch.setattr(settings, "scout_language", "en", raising=False)

        assert wanted(self._row(language="en-GB"))
        assert not wanted(self._row(language="ru"))
        assert not wanted(self._row(language="pl"))

    def test_an_unlabelled_video_is_kept(self, monkeypatch):
        """YouTube leaves this field unset constantly; rejecting on absence
        would throw away most of the good material."""
        from core.config import settings
        from worker.tasks.scout import wanted

        monkeypatch.setattr(settings, "scout_language", "en", raising=False)

        assert wanted(self._row(language=None))
        assert wanted(self._row(language=""))

    def test_a_blank_language_setting_accepts_everything(self, monkeypatch):
        from core.config import settings
        from worker.tasks.scout import wanted

        monkeypatch.setattr(settings, "scout_language", "", raising=False)

        assert wanted(self._row(language="ru"))

    def test_a_missing_view_count_is_not_treated_as_zero(self, monkeypatch):
        from core.config import settings
        from worker.tasks.scout import wanted

        monkeypatch.setattr(settings, "scout_min_views", 100_000, raising=False)

        assert wanted(self._row(views=None))


class TestMomentum:
    """Raw views-per-hour cannot be compared between videos of different ages."""

    def _at(self, days_old):
        from datetime import UTC, datetime, timedelta

        now = datetime(2026, 8, 26, tzinfo=UTC)
        return now - timedelta(days=days_old), now

    def test_steady_viewing_scores_about_one(self):
        from core.scoring import momentum

        published, now = self._at(30)
        # 100k views over 720 hours is ~139/hour; watching at that rate now.
        assert momentum(138.9, 100_000, published, now) == pytest.approx(1.0, abs=0.01)

    def test_a_video_picking_up_again_scores_above_one(self):
        from core.scoring import momentum

        published, now = self._at(180)
        # 500k over ~4320h is ~116/hour lifetime; currently doing 400.
        assert momentum(400.0, 500_000, published, now) > 3

    def test_a_faded_video_scores_below_one(self):
        from core.scoring import momentum

        published, now = self._at(7)
        assert momentum(50.0, 100_000, published, now) < 0.2

    def test_an_old_slow_video_and_a_new_fast_one_are_comparable(self):
        """The point of the metric: same number, same meaning, any age."""
        from core.scoring import momentum

        old_published, now = self._at(365)
        new_published, _ = self._at(2)

        old = momentum(200.0, 1_000_000, old_published, now)
        new = momentum(2000.0, 100_000, new_published, now)

        assert old == pytest.approx(1.75, abs=0.05)
        assert new == pytest.approx(0.96, abs=0.05)
        assert old > new  # the year-old video is the one accelerating

    def test_missing_pieces_give_no_answer_rather_than_a_wrong_one(self):
        from core.scoring import momentum

        published, now = self._at(30)
        assert momentum(None, 100_000, published, now) is None
        assert momentum(100.0, None, published, now) is None
        assert momentum(100.0, 100_000, None, now) is None
