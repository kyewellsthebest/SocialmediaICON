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


class TestTheDiskCannotFill:
    """Nothing posts these yet, so the folder only ever grows. Railway
    answers a full disk by failing the next write, not by warning, and a
    worker that cannot write is a worker that stops."""

    def _downloads(self, tmp_path, n):
        import os
        for i in range(n):
            video = tmp_path / f"vid{i:02d}.mp4"
            video.write_bytes(b"x" * 10)
            video.with_suffix(".json").write_text("{}")
            os.utime(video, (1_700_000_000 + i, 1_700_000_000 + i))
        return tmp_path

    def test_it_keeps_the_newest_and_drops_the_rest(self, tmp_path):
        self._downloads(tmp_path, 10)
        assert gym_reddit.prune(tmp_path, keep=3) == 7
        left = sorted(p.name for p in tmp_path.glob("*.mp4"))
        assert left == ["vid07.mp4", "vid08.mp4", "vid09.mp4"]

    def test_the_attribution_goes_with_its_video(self, tmp_path):
        """A sidecar for a video that is no longer there answers a question
        nobody can ask, and looks like a video we still hold."""
        self._downloads(tmp_path, 5)
        gym_reddit.prune(tmp_path, keep=2)
        assert sorted(p.stem for p in tmp_path.glob("*.json")) == ["vid03", "vid04"]

    def test_it_does_nothing_when_there_is_room(self, tmp_path):
        self._downloads(tmp_path, 3)
        assert gym_reddit.prune(tmp_path, keep=30) == 0
        assert len(list(tmp_path.glob("*.mp4"))) == 3

    def test_keeping_nothing_is_treated_as_not_configured(self, tmp_path):
        """Rather than as an instruction to delete the whole folder."""
        self._downloads(tmp_path, 3)
        assert gym_reddit.prune(tmp_path, keep=0) == 0
        assert len(list(tmp_path.glob("*.mp4"))) == 3


class TestTheHarvestIsOnTheClock:
    def test_the_scheduler_runs_it_daily(self):
        from worker.scheduler import _jobs

        job = next(j for j in _jobs() if j.name == "gym_harvest")
        assert job.every_minutes == 24 * 60 and job.enabled

    def test_no_rooms_means_the_job_does_not_run(self, monkeypatch):
        """Blank is not "search everything" any more - search lives on the
        JSON routes, and those are the ones Railway cannot use. A run with no
        rooms would find nothing, slowly."""
        from worker.scheduler import _jobs

        monkeypatch.setattr(settings, "reddit_subreddits", "")
        job = next(j for j in _jobs() if j.name == "gym_harvest")
        assert not job.enabled

    def test_it_downloads_so_it_belongs_on_the_download_queue(self):
        from worker.scheduler import _jobs

        job = next(j for j in _jobs() if j.name == "gym_harvest")
        assert job.queue == "ingest"
