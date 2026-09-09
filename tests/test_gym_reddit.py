"""Finding short gym video and taking it whole.

Nothing here is edited, so the failures are not "the clip is cut wrong" - they
are "the same video went out twice", "it went out silent", and "somebody asked
us to take it down and we could not tell which post it was".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import reddit, reddit_routes
from core.config import settings
from worker.tasks import gym_reddit


def a_post(pid="abc", ups=1000, **over):
    base = dict(
        external_id=pid, title="squat PR", url=f"https://www.reddit.com/r/GYM/comments/{pid}/x/",
        video_url=f"https://www.reddit.com/r/GYM/comments/{pid}/x/", subreddit="GYM",
        author="lifter", duration_s=20.0, ups=ups, upvote_ratio=0.96,
        num_comments=30, created_utc=1_700_000_000.0, over_18=False,
    )
    return reddit.Post(**(base | over))


class TestOneVideoIsFoundOnce:
    """A post sits in several rooms and matches several terms, so the same
    video comes back repeatedly. Downloading it twice is wasted bandwidth;
    posting it twice is the page looking broken."""

    def test_the_same_post_across_rooms_is_kept_once(self, monkeypatch):
        monkeypatch.setattr(settings, "reddit_subreddits", "GYM,Weightlifting")
        monkeypatch.setattr(reddit_routes, "listing",
                            lambda *a, **k: ([a_post("dup"), a_post("dup")], "json"))
        found = gym_reddit.find()
        assert [p.external_id for p in found] == ["dup"]

    def test_the_strongest_comes_first(self, monkeypatch):
        monkeypatch.setattr(settings, "reddit_subreddits", "GYM")
        monkeypatch.setattr(reddit_routes, "listing", lambda *a, **k: ([
            a_post("small", ups=600), a_post("big", ups=9000),
            a_post("mid", ups=2000)], "json"))
        assert [p.external_id for p in gym_reddit.find()] == ["big", "mid", "small"]

    def test_an_unpostable_one_never_reaches_the_list(self, monkeypatch):
        monkeypatch.setattr(settings, "reddit_subreddits", "GYM")
        monkeypatch.setattr(reddit_routes, "listing", lambda *a, **k: ([
            a_post("fine"), a_post("adult", over_18=True),
            a_post("long", duration_s=400.0)], "json"))
        assert [p.external_id for p in gym_reddit.find()] == ["fine"]

    def test_a_dead_subreddit_does_not_end_the_run(self, monkeypatch):
        """A typo in the config is a typo, not an outage - and by the time
        `listing` gives up it has already tried every way in, so one room
        failing really does say nothing about the next."""
        monkeypatch.setattr(settings, "reddit_subreddits", "GYM,notarealsub")

        def flaky(room, *a, **k):
            if room == "notarealsub":
                raise reddit.RedditError("no route to Reddit worked - json: 404")
            return [a_post("good")], "json"

        monkeypatch.setattr(reddit_routes, "listing", flaky)
        assert [p.external_id for p in gym_reddit.find()] == ["good"]

    def test_with_no_rooms_named_it_searches_the_whole_site(self, monkeypatch):
        """Which only the JSON routes can do - there is no listing to ask for,
        so this is the one shape of the job the fallback chain cannot save."""
        monkeypatch.setattr(settings, "reddit_subreddits", "")
        monkeypatch.setattr(reddit, "search", lambda *a, **k: [a_post("found")])
        assert [p.external_id for p in gym_reddit.find(terms=["fail"])] == ["found"]

    def test_a_room_is_asked_for_its_listing_rather_than_searched(self, monkeypatch):
        """Search exists on the JSON routes alone. Building discovery on it
        means the day those are refused, five working routes find nothing."""
        monkeypatch.setattr(settings, "reddit_subreddits", "GYM")
        monkeypatch.setattr(reddit, "search", _never_called)
        monkeypatch.setattr(reddit_routes, "listing",
                            lambda *a, **k: ([a_post("good")], "rss"))
        assert [p.external_id for p in gym_reddit.find()] == ["good"]


def _never_called(*a, **k):
    raise AssertionError("a named room should be listed, not searched")


class TestAttributionSurvivesTheFilename:
    """The page offers to take a video down on request. That promise needs an
    answer to "which post was this?" months later, and a filename cannot carry
    one."""

    def test_the_caption_is_the_authors_own_words_unchanged(self, tmp_path):
        post = a_post(title="he really said 'watch this' first")
        video = tmp_path / "abc.mp4"
        video.write_bytes(b"x")
        beside = json.loads(gym_reddit.credit(post, video).read_text())
        assert beside["caption"] == "he really said 'watch this' first"

    def test_it_records_where_it_came_from_and_who_made_it(self, tmp_path):
        video = tmp_path / "abc.mp4"
        video.write_bytes(b"x")
        beside = json.loads(gym_reddit.credit(a_post(), video).read_text())
        assert beside["source"].startswith("https://www.reddit.com/r/GYM/")
        assert beside["author"] == "u/lifter"
        assert beside["subreddit"] == "r/GYM"
        assert beside["taken_at"]

    def test_it_sits_beside_the_video_it_describes(self, tmp_path):
        video = tmp_path / "abc.mp4"
        video.write_bytes(b"x")
        assert gym_reddit.credit(a_post(), video) == tmp_path / "abc.json"


class TestTheVideoHasToHaveSound:
    """Reddit serves video and audio as separate DASH files. Ask for the video
    track alone and you get a clip that downloads, plays, and is silent - which
    nobody notices until it is on the page."""

    def test_it_asks_for_video_and_audio_merged(self):
        import inspect
        asked = inspect.getsource(gym_reddit.fetch)
        assert "bestaudio" in asked
        assert "merge_output_format" in asked

    def test_it_downloads_from_the_permalink_not_the_video_file(self):
        """The fallback_url is the silent half."""
        import inspect
        assert "post.video_url" in inspect.getsource(gym_reddit.fetch)
        assert reddit.NATIVE_DOMAIN not in inspect.getsource(gym_reddit.fetch)
