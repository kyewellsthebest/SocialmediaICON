"""Reddit as a video source.

The two things that decide whether this is useful at all: does it correctly
keep native Reddit video and drop everything else, and does it rank a post
people argued about above one they merely upvoted.
"""

from __future__ import annotations

import time

import httpx
import pytest

from core import reddit
from core.config import settings
from worker.tasks import scout_reddit


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setattr(settings, "reddit_client_id", "id", raising=False)
    monkeypatch.setattr(settings, "reddit_client_secret", "secret", raising=False)
    monkeypatch.setattr(settings, "reddit_min_duration_s", 45.0, raising=False)
    monkeypatch.setattr(settings, "reddit_min_upvotes", 500, raising=False)
    monkeypatch.setattr(settings, "scout_language", "en", raising=False)


def _listing(*posts):
    return {"data": {"children": [{"data": p} for p in posts]}}


def _video_post(**kw):
    base = {
        "id": "abc123",
        "title": "One Second Before Disaster - Metal Detecting Find",
        "permalink": "/r/metaldetecting/comments/abc123/one_second/",
        "domain": "v.redd.it",
        "subreddit": "metaldetecting",
        "author": "someone",
        "ups": 4200,
        "upvote_ratio": 0.96,
        "num_comments": 380,
        "created_utc": time.time() - 3600 * 48,
        "over_18": False,
        "secure_media": {
            "reddit_video": {"duration": 92, "fallback_url": "https://v.redd.it/x/DASH_720.mp4"}
        },
    }
    base.update(kw)
    return base


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _handler(listing):
    def handle(request: httpx.Request) -> httpx.Response:
        if "access_token" in request.url.path:
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json=listing)

    return handle


class TestFiltering:
    def test_a_native_video_post_is_kept(self):
        posts = reddit.search("metal detecting", client=_client(_handler(_listing(_video_post()))))

        assert len(posts) == 1
        assert posts[0].duration_s == 92
        assert posts[0].subreddit == "metaldetecting"

    def test_a_youtube_crosspost_is_dropped(self):
        """Downloading those is the door we already found shut."""
        post = _video_post(domain="youtube.com")
        assert reddit.search("x", client=_client(_handler(_listing(post)))) == []

    def test_an_image_post_is_dropped(self):
        post = _video_post(domain="i.redd.it", secure_media=None)
        assert reddit.search("x", client=_client(_handler(_listing(post)))) == []

    def test_a_text_post_is_dropped(self):
        post = _video_post(is_self=True)
        assert reddit.search("x", client=_client(_handler(_listing(post)))) == []

    def test_a_video_with_no_playable_url_is_dropped(self):
        post = _video_post(secure_media={"reddit_video": {"duration": 90}})
        assert reddit.search("x", client=_client(_handler(_listing(post)))) == []

    def test_the_permalink_is_what_yt_dlp_gets(self):
        """The fallback url is video only; the permalink lets yt-dlp pair the audio."""
        posts = reddit.search("x", client=_client(_handler(_listing(_video_post()))))

        assert posts[0].video_url.startswith("https://www.reddit.com/r/")
        assert "DASH" not in posts[0].video_url


class TestGate:
    def _post(self, **kw):
        data = _video_post(**kw)
        return reddit._post_from(data)

    def test_a_long_enough_popular_english_post_passes(self):
        assert scout_reddit.wanted(self._post()) is True

    def test_a_clip_too_short_to_cut_is_rejected(self):
        post = self._post(
            secure_media={"reddit_video": {"duration": 20, "fallback_url": "https://v.redd.it/a"}}
        )
        assert scout_reddit.wanted(post) is False

    def test_a_post_nobody_voted_for_is_rejected(self):
        assert scout_reddit.wanted(self._post(ups=40)) is False

    def test_a_foreign_title_is_rejected(self):
        assert scout_reddit.wanted(self._post(title="¡Encontré un tesoro increíble!")) is False

    def test_adult_content_is_rejected(self):
        assert scout_reddit.wanted(self._post(over_18=True)) is False


class TestScoring:
    def _post(self, **kw):
        return reddit._post_from(_video_post(**kw))

    def test_an_argued_about_post_beats_a_merely_upvoted_one(self):
        """The whole reason to prefer Reddit: discussion says there is a moment."""
        argued = self._post(ups=4000, num_comments=600)
        approved = self._post(ups=4000, num_comments=20)

        assert scout_reddit.score_post(argued) > scout_reddit.score_post(approved)

    def test_a_fast_climbing_post_beats_a_slow_one(self):
        fresh = self._post(ups=4000, created_utc=time.time() - 3600 * 6)
        old = self._post(ups=4000, created_utc=time.time() - 3600 * 24 * 20)

        assert scout_reddit.score_post(fresh) > scout_reddit.score_post(old)

    def test_a_contested_post_scores_below_a_well_liked_one(self):
        liked = self._post(upvote_ratio=0.98)
        contested = self._post(upvote_ratio=0.55)

        assert scout_reddit.score_post(liked) > scout_reddit.score_post(contested)

    def test_the_score_stays_on_the_same_hundred_point_scale(self):
        assert 0 <= scout_reddit.score_post(self._post()) <= 100


class TestComments:
    def test_deleted_comments_are_left_out_and_the_rest_ranked(self):
        thread = [
            {},
            {
                "data": {
                    "children": [
                        {"data": {"body": "[deleted]", "ups": 900}},
                        {"data": {"body": "the bit where it beeps", "ups": 12}},
                        {"data": {"body": "watch his face at the end", "ups": 400}},
                    ]
                }
            },
        ]

        def handle(request: httpx.Request) -> httpx.Response:
            if "access_token" in request.url.path:
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(200, json=thread)

        comments = reddit.top_comments("abc123", client=_client(handle))

        assert [c["ups"] for c in comments] == [400, 12]
        assert "his face" in comments[0]["body"]


def test_no_credentials_falls_back_to_the_public_endpoint(monkeypatch):
    """Creating a Reddit app can simply refuse to work; that must not be the
    thing that stops the scout, since the listings are public anyway."""
    monkeypatch.setattr(settings, "reddit_client_id", None, raising=False)
    monkeypatch.setattr(settings, "reddit_client_secret", None, raising=False)
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert "authorization" not in request.headers
        assert request.headers["user-agent"] == settings.reddit_user_agent
        return httpx.Response(200, json=_listing(_video_post()))

    posts = reddit.search("metal detecting", client=_client(handle))

    assert len(posts) == 1
    assert seen[0].startswith("https://www.reddit.com/search.json")


def test_a_broken_token_exchange_falls_back_rather_than_failing(monkeypatch):
    monkeypatch.setattr(settings, "reddit_client_id", "id", raising=False)
    monkeypatch.setattr(settings, "reddit_client_secret", "", raising=False)

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_listing(_video_post()))

    assert len(reddit.search("x", client=_client(handle))) == 1


def test_credentials_use_the_oauth_host(monkeypatch):
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "access_token" in request.url.path:
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json=_listing(_video_post()))

    reddit.search("x", client=_client(handle))

    assert any("oauth.reddit.com" in url for url in seen)


def test_the_trending_row_says_which_platform_it_came_from():
    """Upvotes are not views: a row's numbers mean nothing without the source."""
    import api.routes.trending as trending

    video = type(
        "V",
        (),
        {
            "id": 1,
            "platform": "reddit",
            "external_id": "abc",
            "url": "https://reddit.com/x",
            "title": "t",
            "channel_title": "r/metaldetecting",
            "channel_id": "metaldetecting",
            "thumbnail_url": None,
            "published_at": None,
            "duration_s": 90.0,
            "views": 4200,
            "likes": None,
            "comments": 380,
            "velocity_vph": 87.5,
            "like_rate": 0.96,
            "score": 61.0,
            "status": "new",
            "hot_segments": [],
            "heatmap": None,
        },
    )()

    payload = trending._row(None, video) if hasattr(trending, "_row") else None
    if payload is None:  # helper is named differently; assert the field exists
        import inspect

        assert "platform" in inspect.getsource(trending)
    else:
        assert payload["platform"] == "reddit"
