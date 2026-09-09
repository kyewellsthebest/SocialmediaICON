"""What may be reposted, and what may not.

Nothing on this path gets edited: what is found is what is published, under
the original title. So the filter is the whole of the editorial judgement, and
the two rules that matter are the two a person would be embarrassed by - an
adult video on a gym page, and a nine-minute video posted as a Reel.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import reddit
from core.config import settings


def a_post(**over):
    """A native Reddit video that passes everything unless told otherwise."""
    base = dict(
        external_id="abc123", title="PR attempt goes wrong",
        url="https://www.reddit.com/r/GYM/comments/abc123/x/",
        video_url="https://www.reddit.com/r/GYM/comments/abc123/x/",
        subreddit="GYM", author="someone", duration_s=22.0, ups=1500,
        upvote_ratio=0.95, num_comments=80, created_utc=0.0, over_18=False,
    )
    return reddit.Post(**(base | over))


class TestAdultContentNeverPasses:
    """The rule that has to hold even when something else is broken."""

    def test_an_adult_post_is_refused(self):
        ok, why = reddit.postable(a_post(over_18=True))
        assert not ok and why == "adult"

    def test_it_is_refused_even_when_it_is_otherwise_perfect(self):
        """Short, popular, well-liked and still not going on the page."""
        ok, _ = reddit.postable(
            a_post(over_18=True, duration_s=15.0, ups=90_000, upvote_ratio=0.99))
        assert not ok

    def test_the_check_lives_where_every_caller_passes(self):
        """include_over_18=false on the search is a preference on a listing,
        not a guarantee - it does not cover a crosspost out of a quarantined
        room. A caller that forgets the check publishes the result, so the
        refusal belongs in the one function they all go through."""
        import inspect
        assert "over_18" in inspect.getsource(reddit.postable)


class TestItHasToBeShortFormAlready:
    def test_a_video_over_a_minute_is_refused(self):
        ok, why = reddit.postable(a_post(duration_s=95.0))
        assert not ok and "longer than short-form" in why

    def test_exactly_at_the_limit_is_allowed(self):
        assert reddit.postable(a_post(duration_s=60.0))[0]

    def test_a_two_second_clip_is_not_a_post(self):
        ok, why = reddit.postable(a_post(duration_s=2.0))
        assert not ok and "not a post" in why

    def test_a_payload_with_no_duration_is_refused_rather_than_guessed(self):
        """v.redd.it always reports one. Missing means the payload was not
        what it claimed, and finding out by downloading costs a download."""
        ok, why = reddit.postable(a_post(duration_s=None))
        assert not ok and why == "no duration"

    def test_the_bounds_are_configurable(self, monkeypatch):
        monkeypatch.setattr(settings, "reddit_max_duration_s", 30.0)
        assert not reddit.postable(a_post(duration_s=45.0))[0]
        assert reddit.postable(a_post(duration_s=25.0))[0]


class TestItHasToHaveEarnedItsPlace:
    def test_a_post_nobody_upvoted_is_refused(self, monkeypatch):
        monkeypatch.setattr(settings, "reddit_min_upvotes", 500)
        ok, why = reddit.postable(a_post(ups=12))
        assert not ok and "upvotes" in why

    def test_an_ordinary_good_post_passes(self):
        ok, why = reddit.postable(a_post())
        assert ok and why == ""


class TestOnlyVideoRedditHostsItself:
    """A link to YouTube or Streamable is not something this can download or
    has any business reposting."""

    def test_a_link_post_is_not_a_video(self):
        assert reddit._post_from({"domain": "youtube.com", "title": "x"}) is None

    def test_a_text_post_is_not_a_video(self):
        assert reddit._post_from({"is_self": True, "domain": "v.redd.it"}) is None

    def test_a_pinned_post_is_skipped(self):
        """Every gym subreddit pins its rules and its weekly thread."""
        assert reddit._post_from({"stickied": True, "domain": "v.redd.it"}) is None

    def test_a_native_video_is_read_with_its_duration_and_flag(self):
        found = reddit._post_from({
            "id": "xyz", "title": "  squat fail  ", "permalink": "/r/GYM/comments/xyz/s/",
            "domain": "v.redd.it", "subreddit": "GYM", "author": "lifter",
            "ups": 900, "num_comments": 40, "created_utc": 1.0, "over_18": False,
            "secure_media": {"reddit_video": {"fallback_url": "https://v.redd.it/x/DASH_720.mp4",
                                              "duration": 18}},
        })
        assert found is not None
        assert found.duration_s == 18.0
        assert found.title == "squat fail", "the caption is copied verbatim, so it is trimmed"
        assert found.over_18 is False

    def test_yt_dlp_is_given_the_permalink_not_the_fallback(self):
        """Reddit serves video and audio as separate DASH files. Hand over the
        fallback_url and you get a silent video; hand over the permalink and
        yt-dlp pairs the two."""
        found = reddit._post_from({
            "id": "xyz", "title": "t", "permalink": "/r/GYM/comments/xyz/s/",
            "domain": "v.redd.it",
            "secure_media": {"reddit_video": {"fallback_url": "https://v.redd.it/x/DASH_720.mp4",
                                              "duration": 10}},
        })
        assert found.video_url.startswith("https://www.reddit.com/r/")
        assert "v.redd.it" not in found.video_url
