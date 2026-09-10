"""Six ways in to Reddit, and what happens when five of them are shut.

The API is not hard to get - a script app is free and instant - but it is one
endpoint, and a page that stops posting because one endpoint started refusing
is not worth running. So there are six ways in, and the thing worth testing is
not any one of them: it is that a failure moves to the next one, that a total
failure says which six things were tried, and that the thin routes cannot
smuggle an adult video onto the page just because their feed forgot to mention
it was one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import reddit, reddit_routes
from core.config import settings

# --------------------------------------------------------------------------
# fixtures


def a_child(**over):
    """One t3 entry, shaped the way a real listing shapes it."""
    base = {
        "id": "abc123", "title": "PR attempt goes wrong", "domain": "v.redd.it",
        "permalink": "/r/GYM/comments/abc123/x/", "subreddit": "GYM",
        "author": "someone", "ups": 4200, "upvote_ratio": 0.96,
        "num_comments": 80, "created_utc": 1_700_000_000.0, "over_18": False,
        "secure_media": {"reddit_video": {
            "fallback_url": "https://v.redd.it/abc123/DASH_720.mp4",
            "duration": 24,
        }},
    }
    return base | over


def a_listing(*children):
    return {"kind": "Listing",
            "data": {"children": [{"kind": "t3", "data": c} for c in children]}}


REDDIT_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <author><name>/u/someone</name></author>
    <title>PR attempt goes wrong</title>
    <link href="https://www.reddit.com/r/GYM/comments/abc123/pr_attempt/" />
    <id>t3_abc123</id>
  </entry>
  <entry>
    <author><name>/u/other</name></author>
    <title>Form check</title>
    <link href="https://www.reddit.com/r/GYM/comments/def456/form_check/" />
    <id>t3_def456</id>
  </entry>
</feed>
"""

# A Redlib instance serves the same feed with its own hostname on every link.
MIRROR_FEED = REDDIT_FEED.replace("https://www.reddit.com", "https://safereddit.com")


@pytest.fixture(autouse=True)
def _forget_the_last_working_route():
    """Stickiness is deliberate and would otherwise leak between tests."""
    reddit_routes._preferred = None
    yield
    reddit_routes._preferred = None


@pytest.fixture
def answering(monkeypatch):
    """Point every httpx client in the process at a handler.

    The routes build their own clients - a proxy is the entire subject of one
    of them, so it cannot be injected from outside - which leaves patching the
    constructor as the way to get a fake in.
    """
    real = httpx.Client

    def install(handler):
        def make(**kwargs):
            kwargs.pop("proxy", None)
            return real(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, "Client", make)

    return install


# --------------------------------------------------------------------------


class TestOneRouteFailingIsNotRedditFailing:
    def test_it_moves_on_to_the_next_way_in(self, answering, monkeypatch):
        """The whole reason there are six."""
        monkeypatch.setattr(settings, "reddit_routes", "json,rss")

        def handler(request):
            if request.url.path.endswith(".json"):
                return httpx.Response(403)
            return httpx.Response(200, text=REDDIT_FEED)

        answering(handler)
        monkeypatch.setattr(
            reddit_routes, "hydrate",
            lambda entries, limit, skip=None: [_a_post(e.external_id) for e in entries])

        posts, route = reddit_routes.listing("GYM")
        assert route == "rss"
        assert [p.external_id for p in posts] == ["abc123", "def456"]

    def test_a_total_failure_names_every_route_and_its_reason(
            self, answering, monkeypatch):
        """'Reddit failed' with one reason sends you to fix the wrong thing."""
        monkeypatch.setattr(settings, "reddit_routes", "json,reader,rss")
        answering(lambda request: httpx.Response(403))

        with pytest.raises(reddit.RedditError) as raised:
            reddit_routes.listing("GYM")

        said = str(raised.value)
        assert "json:" in said and "reader:" in said and "rss:" in said

    def test_the_route_that_worked_is_tried_first_next_time(
            self, answering, monkeypatch):
        """Four failures per search, forever, is a working system that is
        also unusable."""
        monkeypatch.setattr(settings, "reddit_routes", "json,rss")
        tried: list[str] = []

        def handler(request):
            tried.append(request.url.path)
            if request.url.path.endswith(".json"):
                return httpx.Response(403)
            return httpx.Response(200, text=REDDIT_FEED)

        answering(handler)
        monkeypatch.setattr(reddit_routes, "hydrate",
                            lambda entries, limit, skip=None: [_a_post("abc123")])

        reddit_routes.listing("GYM")
        tried.clear()
        reddit_routes.listing("GYM")
        assert tried and tried[0].endswith(".rss")

    def test_an_answered_but_empty_listing_is_not_a_failed_route(
            self, answering, monkeypatch):
        """A room with no Reddit-hosted video that day is a bounds problem.
        Trying five more ways to be told the same thing then reports it as a
        reach problem, which is the wrong thing to go and fix."""
        monkeypatch.setattr(settings, "reddit_routes", "json,rss")
        answering(lambda request: httpx.Response(
            200, json=a_listing(a_child(domain="youtube.com"))))

        posts, route = reddit_routes.listing("GYM")
        assert route == "json" and posts == []


def _a_post(external_id, **over):
    base = dict(
        external_id=external_id, title="x", url="u", video_url="u",
        subreddit="GYM", author="someone", duration_s=20.0, ups=0,
        upvote_ratio=None, num_comments=0, created_utc=0.0, over_18=False,
        ups_known=False,
    )
    return reddit.Post(**(base | over))


class TestTheRoutesThatCannotSeeVotes:
    """A feed gives a title and a link. Everything else has to be looked up,
    and the difference between 'nobody voted' and 'nobody said' decides
    whether these routes find anything at all."""

    def test_an_unknown_score_does_not_read_as_a_score_of_zero(self, monkeypatch):
        monkeypatch.setattr(settings, "reddit_min_upvotes", 500)
        ok, why = reddit.postable(_a_post("abc123"))
        assert ok, f"refused for {why!r} - a feed cannot report votes"

    def test_a_reported_score_below_the_bar_is_still_refused(self, monkeypatch):
        monkeypatch.setattr(settings, "reddit_min_upvotes", 500)
        ok, why = reddit.postable(_a_post("abc123", ups=12, ups_known=True))
        assert not ok and "12 upvotes" in why

    def test_the_adult_check_is_never_the_one_that_gets_skipped(self):
        """The vote bar is a preference. This one is not, and a route with
        thin metadata is exactly where it would quietly go missing."""
        ok, why = reddit.postable(_a_post("abc123", over_18=True))
        assert not ok and why == "adult"

    def test_a_missing_duration_is_refused_rather_than_assumed(self):
        ok, why = reddit.postable(_a_post("abc123", duration_s=None))
        assert not ok and why == "no duration"


class TestReadingAFeed:
    def test_it_takes_the_id_out_of_the_permalink(self):
        entries = reddit_routes.parse_atom(REDDIT_FEED)
        assert [e.external_id for e in entries] == ["abc123", "def456"]

    def test_a_mirrors_links_are_pointed_back_at_reddit(self):
        """yt-dlp has an extractor for reddit.com and none for a Redlib
        instance, so a mirror URL carried through downloads nothing."""
        entries = reddit_routes.parse_atom(MIRROR_FEED)
        assert all(e.permalink.startswith("https://www.reddit.com/r/GYM/comments/")
                   for e in entries)

    def test_the_author_loses_its_prefix(self):
        assert reddit_routes.parse_atom(REDDIT_FEED)[0].author == "someone"

    def test_a_block_page_is_a_failed_route_not_a_crash(self):
        with pytest.raises(reddit_routes.RouteFailed):
            reddit_routes.parse_atom("<html><body>Blocked</body></html>")


class TestAskingForTheRightListing:
    def test_top_carries_the_window_and_hot_does_not(self):
        path, params = reddit_routes._listing_path("GYM", "top", "day", 25)
        assert path == "/r/GYM/top" and params["t"] == "day"
        _, hot = reddit_routes._listing_path("GYM", "hot", "day", 25)
        assert "t" not in hot

    def test_an_unknown_sort_falls_back_to_top(self):
        """Which is the sort the thin routes depend on: they cannot filter by
        score, so the window sort has to be doing it."""
        path, params = reddit_routes._listing_path("GYM", "nonsense", "day", 25)
        assert path.endswith("/top") and params["t"] == "day"

    def test_no_room_means_the_whole_site(self):
        path, _ = reddit_routes._listing_path(None, "top", "day", 25)
        assert path == "/top"


class TestTheJsonRoute:
    def test_a_403_says_it_is_about_the_address(self, answering, monkeypatch):
        """Because it is, and the fix is a proxy rather than a key."""
        answering(lambda request: httpx.Response(403))
        with pytest.raises(reddit_routes.RouteFailed) as raised:
            reddit_routes.by_json("GYM", "top", "day", 25)
        assert "address" in str(raised.value)

    def test_an_html_block_page_is_reported_as_one(self, answering):
        """Reddit answers a refusal with a page, and parsing that as JSON
        raises something that says nothing about what happened."""
        answering(lambda request: httpx.Response(200, text="<html>nope</html>"))
        with pytest.raises(reddit_routes.RouteFailed) as raised:
            reddit_routes.by_json("GYM", "top", "day", 25)
        assert "block page" in str(raised.value)

    def test_it_flattens_a_real_listing(self, answering):
        answering(lambda request: httpx.Response(200, json=a_listing(a_child())))
        posts = reddit_routes.by_json("GYM", "top", "day", 25)
        assert len(posts) == 1
        assert posts[0].duration_s == 24 and posts[0].ups == 4200
        assert posts[0].ups_known


class TestTheReaderRoute:
    def test_it_asks_the_service_for_the_reddit_url(self, answering, monkeypatch):
        monkeypatch.setattr(settings, "reddit_reader", "https://r.example")
        asked = []

        def handler(request):
            asked.append(str(request.url))
            return httpx.Response(200, json=a_listing(a_child()))

        answering(handler)
        reddit_routes.by_reader("GYM", "top", "day", 25)
        assert asked[0].startswith("https://r.example/https://www.reddit.com/r/GYM/top.json")

    def test_json_wrapped_in_markdown_is_still_json(self):
        """These services return prose about a page by default, and the
        wrapping is not worth a failed route."""
        found = reddit_routes._json_inside(
            "Here is the content:\n\n```json\n" +
            '{"data": {"children": []}}' + "\n```\n")
        assert found == {"data": {"children": []}}

    def test_a_page_with_no_json_in_it_is_a_failed_route(self):
        with pytest.raises(reddit_routes.RouteFailed):
            reddit_routes._json_inside("Access denied.")

    def test_with_no_service_configured_it_declines_rather_than_guesses(
            self, monkeypatch):
        monkeypatch.setattr(settings, "reddit_reader", "")
        with pytest.raises(reddit_routes.RouteFailed):
            reddit_routes.by_reader("GYM", "top", "day", 25)


class TestTheMirrorRoute:
    def test_a_dead_instance_moves_to_the_next_one(self, answering, monkeypatch):
        """Public instances come and go. Sitting on the first one in the list
        is how a list of five behaves like a list of one."""
        monkeypatch.setattr(settings, "reddit_mirrors",
                            "https://dead.example,https://alive.example")
        seen = []

        def handler(request):
            seen.append(request.url.host)
            if request.url.host == "dead.example":
                return httpx.Response(502)
            return httpx.Response(200, text=MIRROR_FEED)

        answering(handler)
        monkeypatch.setattr(reddit_routes, "hydrate",
                            lambda entries, limit, skip=None:
                            [_a_post(e.external_id) for e in entries])
        posts = reddit_routes.by_mirror("GYM", "top", "day", 25)
        # Both URL shapes are tried on the dead one before moving on: RSS is
        # opt-in on a Redlib instance and lives on two different routes
        # depending on version, so a 404 is not proof the instance is gone.
        assert seen[0] == "dead.example" and seen[-1] == "alive.example"
        assert "alive.example" in seen and len(posts) == 2

    def test_it_cannot_serve_the_whole_site(self):
        with pytest.raises(reddit_routes.RouteFailed):
            reddit_routes.by_mirror(None, "top", "day", 25)


class TestChoosingWhichRoutesToTry:
    def test_an_explicit_list_is_an_ordering_as_well_as_a_filter(self, monkeypatch):
        monkeypatch.setattr(settings, "reddit_routes", "rss,json")
        assert [r.name for r in reddit_routes.routes()] == ["rss", "json"]

    def test_blank_means_all_of_them_in_the_default_order(self, monkeypatch):
        monkeypatch.setattr(settings, "reddit_routes", "")
        assert [r.name for r in reddit_routes.routes()] == \
            [r.name for r in reddit_routes.ROUTES]


class TestAskingTheDeploymentItself:
    """The measurement has to happen on the host that will do the work, and
    that host has no terminal on it - so it answers over HTTP instead."""

    def _app(self, monkeypatch):
        from fastapi.testclient import TestClient

        import api.main
        monkeypatch.setattr(settings, "dashboard_token", None)
        return TestClient(api.main.app)

    def test_it_reports_every_route_not_just_the_first_that_worked(
            self, monkeypatch):
        """A chain that stops at the first success is right in production and
        useless as a diagnostic: the point is to see all six."""
        monkeypatch.setattr(
            "api.routes.reddit._try",
            lambda route, *a, **k: {"route": route.name, "ok": route.name == "rss",
                               "seconds": 0.1, "reason": "403", "video": 1,
                               "postable": 1, "sees_scores": False, "sample": []})
        body = self._app(monkeypatch).get("/api/reddit/ways-in").text
        for route in reddit_routes.ROUTES:
            assert route.name in body

    def test_a_total_failure_says_what_the_three_causes_are(self, monkeypatch):
        monkeypatch.setattr(
            "api.routes.reddit._try",
            lambda route, *a, **k: {"route": route.name, "ok": False,
                               "seconds": 0.1, "reason": "403 Forbidden"})
        body = self._app(monkeypatch).get("/api/reddit/ways-in").text
        assert "Nothing got through" in body and "egress policy" in body

    def test_it_ends_with_the_line_to_paste_into_the_environment(
            self, monkeypatch):
        monkeypatch.setattr(
            "api.routes.reddit._try",
            lambda route, *a, **k: {"route": route.name, "ok": route.name in ("rss", "json"),
                               "seconds": 1.0 if route.name == "json" else 2.0,
                               "reason": "403", "video": 2, "postable": 2,
                               "sees_scores": route.name == "json", "sample": []})
        body = self._app(monkeypatch).get("/api/reddit/ways-in").text
        assert "REDDIT_ROUTES=json,rss" in body

    def test_it_is_behind_the_dashboard_token(self, monkeypatch):
        from fastapi.testclient import TestClient

        import api.main
        monkeypatch.setattr(settings, "dashboard_token", "secret")
        client = TestClient(api.main.app)
        assert client.get("/api/reddit/ways-in").status_code == 401


class TestTheSilentClipCheck:
    """Reddit serves video and audio as two separate files. A download that
    fetched only the video half plays perfectly, opens in any player, and is
    silent - and there is no editor downstream to notice before it is posted.
    So the check is a real download and a real probe, not a format string that
    looked right."""

    def test_a_silent_file_is_reported_as_a_failure(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        import api.main
        import api.routes.reddit as route
        monkeypatch.setattr(settings, "dashboard_token", None)
        monkeypatch.setattr(reddit_routes, "listing",
                            lambda *a, **k: ([_a_post("abc123", ups=900,
                                                      ups_known=True)], "rss"))
        video = tmp_path / "abc123.mp4"
        video.write_bytes(b"x" * 1000)
        monkeypatch.setattr(route, "_fetch_once", lambda p: video)
        monkeypatch.setattr(route, "_streams", lambda p: {
            "probed": True, "has_audio": False, "has_video": True,
            "size": "720x1280", "duration_s": 20.0})

        body = TestClient(api.main.app).get("/api/reddit/try-one").text
        assert "SILENT" in body

    def test_a_good_file_says_the_path_works(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        import api.main
        import api.routes.reddit as route
        monkeypatch.setattr(settings, "dashboard_token", None)
        monkeypatch.setattr(reddit_routes, "listing",
                            lambda *a, **k: ([_a_post("abc123")], "rss"))
        video = tmp_path / "abc123.mp4"
        video.write_bytes(b"x" * 1000)
        monkeypatch.setattr(route, "_fetch_once", lambda p: video)
        monkeypatch.setattr(route, "_streams", lambda p: {
            "probed": True, "has_audio": True, "has_video": True,
            "size": "720x1280", "duration_s": 20.0})

        answer = TestClient(api.main.app).get("/api/reddit/try-one?format=json")
        assert answer.json()["ok"] is True
        assert "has an audio track" in answer.json()["report"]

    def test_nothing_postable_is_not_reported_as_a_broken_download(
            self, monkeypatch):
        """A quiet day in one room is a bounds problem. Reporting it as a
        download fault sends you to read the wrong code."""
        from fastapi.testclient import TestClient

        import api.main
        monkeypatch.setattr(settings, "dashboard_token", None)
        monkeypatch.setattr(reddit_routes, "listing",
                            lambda *a, **k: ([_a_post("x", over_18=True)], "rss"))
        body = TestClient(api.main.app).get("/api/reddit/try-one").text
        assert "not necessarily a fault" in body

    def test_it_records_nothing_so_it_can_be_run_again(self):
        """The harvest remembers what it took, on purpose. A diagnostic that
        did the same could be run exactly once per post."""
        import inspect

        import api.routes.reddit as route
        source = inspect.getsource(route.try_one)
        assert "remember" not in source


class TestAttributionIsNotGuessed:
    """The sidecar written beside every download is what answers "which post
    was this?" when somebody asks for their video to be taken down. A field in
    it being confidently wrong is worse than it being absent."""

    def test_the_room_comes_from_the_permalink(self):
        """yt-dlp leaves `channel` unset on Reddit, and falling through to
        `uploader` filed a post from r/GYM under r/<whoever posted it>. The
        permalink carries the room and cannot disagree with itself."""
        entries = reddit_routes.parse_atom(REDDIT_FEED)
        assert all(e.subreddit == "GYM" for e in entries)

    def test_a_mirrors_links_still_carry_the_room(self):
        entries = reddit_routes.parse_atom(MIRROR_FEED)
        assert all(e.subreddit == "GYM" for e in entries)

    def test_the_author_and_the_room_are_not_the_same_field(self):
        """The bug that made this test exist: both read u/imkiyoko."""
        entry = reddit_routes.parse_atom(REDDIT_FEED)[0]
        assert entry.author == "someone" and entry.subreddit == "GYM"
