"""The fifteen slots, and what happens when a sixteenth video turns up.

The queue is a leaderboard, not a pipeline, and the whole behaviour is one
rule: the fifteen best unposted videos anyone has seen are the fifteen in the
queue. A video found on Tuesday can still be beaten on Thursday and never go
out, and one that was beaten is never picked up again.

These run against a real SQLite database rather than mocks, because the rule
lives in an ORDER BY and a slice - and a mock of a slice proves nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db
from core.config import settings
from core.models import Base, Reel
from core.reddit import Post
from worker.tasks import harvest


@pytest.fixture
def database(monkeypatch, tmp_path):
    """A real database, thrown away after each test."""
    engine = create_engine(f"sqlite:///{tmp_path/'q.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    from contextlib import contextmanager

    @contextmanager
    def scope():
        session = maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(db, "session_scope", scope)
    monkeypatch.setattr(harvest, "session_scope", scope)
    return maker


def a_post(pid, ups, **over):
    base = dict(
        external_id=pid, title=f"lift {pid}",
        url=f"https://www.reddit.com/r/GYM/comments/{pid}/x/",
        video_url=f"https://www.reddit.com/r/GYM/comments/{pid}/x/",
        subreddit="GYM", author="lifter", duration_s=20.0, ups=ups,
        upvote_ratio=0.96, num_comments=10, created_utc=1_700_000_000.0,
        over_18=False,
    )
    return Post(**(base | over))


def places(database):
    """The queue as the dashboard would show it: best first."""
    with database() as session:
        return [
            (r.external_id, r.ups)
            for r in session.execute(
                select(Reel).where(Reel.state.in_(("found", "ready")))
                .order_by(Reel.ups.desc(), Reel.id.asc())
            ).scalars()
        ]


class TestFifteenSlots:
    def test_a_run_that_finds_more_keeps_only_the_best(self, database, monkeypatch):
        monkeypatch.setattr(settings, "queue_size", 15)
        harvest.admit([a_post(f"p{i:02d}", ups=100 * i) for i in range(1, 21)])
        harvest.trim()

        held = places(database)
        assert len(held) == 15
        assert held[0] == ("p20", 2000)
        assert held[-1] == ("p06", 600)

    def test_a_better_video_pushes_the_weakest_out(self, database, monkeypatch):
        """The rule as stated: something better than the last one in takes
        its slot, everything below it shifts down one, and the weakest goes."""
        monkeypatch.setattr(settings, "queue_size", 15)
        harvest.admit([a_post(f"p{i:02d}", ups=100 * i) for i in range(1, 16)])
        harvest.trim()
        assert places(database)[-1] == ("p01", 100)

        harvest.admit([a_post("newcomer", ups=1450)])
        harvest.trim()

        held = places(database)
        assert len(held) == 15
        assert ("newcomer", 1450) in held
        # p01 was the weakest, so p01 is the one that leaves.
        assert "p01" not in [pid for pid, _ in held]
        # ...and the newcomer sits exactly where its score puts it: below the
        # 1500 and above the 1400, not at the top or the bottom.
        assert held[1] == ("newcomer", 1450)

    def test_one_that_is_not_good_enough_does_not_get_in(self, database, monkeypatch):
        monkeypatch.setattr(settings, "queue_size", 15)
        harvest.admit([a_post(f"p{i:02d}", ups=1000 + i) for i in range(1, 16)])
        harvest.trim()
        harvest.admit([a_post("weak", ups=3)])
        harvest.trim()

        held = [pid for pid, _ in places(database)]
        assert "weak" not in held and len(held) == 15

    def test_a_beaten_video_is_kept_rather_than_deleted(self, database, monkeypatch):
        """Otherwise the next run finds it again, admits it again, and drops
        it again - forever."""
        monkeypatch.setattr(settings, "queue_size", 2)
        harvest.admit([a_post("a", 300), a_post("b", 200), a_post("c", 100)])
        harvest.trim()

        with database() as session:
            beaten = session.execute(
                select(Reel).where(Reel.external_id == "c")
            ).scalar_one()
            assert beaten.state == "dropped"
            assert "did not make the top 2" in (beaten.note or "")

        assert "c" in harvest.known_ids()

    def test_the_same_video_is_only_admitted_once(self, database):
        harvest.admit([a_post("dup", 500)])
        assert harvest.admit([a_post("dup", 900)]) == 0
        assert len(places(database)) == 1


class TestWhatGoesOutNext:
    def test_the_strongest_is_first_in_line(self, database, monkeypatch):
        monkeypatch.setattr(settings, "queue_size", 15)
        harvest.admit([a_post("mid", 500), a_post("big", 9000), a_post("small", 50)])
        harvest.trim()
        assert [r.external_id for r in harvest.queued()] == ["big", "mid", "small"]

    def test_a_run_sends_out_only_its_allowance(self, database, monkeypatch):
        monkeypatch.setattr(settings, "queue_size", 15)
        monkeypatch.setattr(settings, "post_per_run", 8)
        harvest.admit([a_post(f"p{i:02d}", ups=100 * i) for i in range(1, 16)])
        harvest.trim()

        going = harvest.queued(limit=settings.post_per_run)
        assert len(going) == 8
        assert going[0].external_id == "p15"
        # ...and seven are still waiting for the next run, which is the
        # arithmetic in the brief: fifteen found, eight posted, seven saved.
        assert len(harvest.queued()) - len(going) == 7

    def test_a_posted_video_leaves_the_queue_but_not_the_record(
            self, database, monkeypatch):
        harvest.admit([a_post("gone", 900)])
        with database() as session:
            reel = session.execute(
                select(Reel).where(Reel.external_id == "gone")).scalar_one()
            reel.state = "posted"
            session.commit()

        assert harvest.queued() == []
        assert "gone" in harvest.known_ids()


class TestNothingIsLookedUpTwice:
    def test_what_is_already_known_is_not_asked_about_again(
            self, database, monkeypatch):
        """The feed routes cost a round trip per candidate. A queue that has
        been running a week recognises most of a feed, and re-reading those is
        the difference between a one-minute run and a ten-minute one."""
        harvest.admit([a_post("seen", 400)])
        asked = {}

        def listing(room, sort, window, limit, skip):
            asked["skip"] = skip
            return [a_post("fresh", 800)], "rss"

        monkeypatch.setattr(settings, "reddit_subreddits", "GYM")
        monkeypatch.setattr(harvest.reddit_routes, "listing", listing)
        harvest.discover()
        assert "seen" in asked["skip"]

    def test_one_dead_room_does_not_end_the_run(self, database, monkeypatch):
        from core.reddit import RedditError

        def listing(room, sort, window, limit, skip):
            if room == "notarealsub":
                raise RedditError("no route to Reddit worked")
            return [a_post("good", 700)], "rss"

        monkeypatch.setattr(settings, "reddit_subreddits", "GYM,notarealsub")
        monkeypatch.setattr(harvest.reddit_routes, "listing", listing)
        assert [p.external_id for p in harvest.discover()] == ["good"]

    def test_an_unpostable_one_never_reaches_the_queue(self, database, monkeypatch):
        def listing(room, sort, window, limit, skip):
            return [a_post("fine", 700),
                    a_post("adult", 9000, over_18=True),
                    a_post("long", 9000, duration_s=400.0)], "rss"

        monkeypatch.setattr(settings, "reddit_subreddits", "GYM")
        monkeypatch.setattr(harvest.reddit_routes, "listing", listing)
        assert [p.external_id for p in harvest.discover()] == ["fine"]
