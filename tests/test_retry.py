"""Asking again on a platform that said "not now".

The failure this exists for: Instagram's publishing limit is 25 posts per
account per rolling 24 hours, and it is spent by *asking*, not only by
succeeding. When it ran out, every reel's Instagram attempt failed - and
because Facebook took the same reel, the reel was marked posted and Instagram
was never asked again. A limit that clears by itself in an hour cost a whole
day of Instagram posts permanently.

The distinction that has to hold: a rate limit is a queue and a bad token is a
fault. Retrying the first is the fix; retrying the second is what spent the
limit in the first place.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import settings
from core.models import Base, Reel, ReelPost

LIMIT = "Application request limit reached (code 4/1349210)"
DEAD = "Invalid OAuth access token (code 190)"


@pytest.fixture
def database(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'r.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session = maker()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    from worker.tasks import publish

    monkeypatch.setattr(publish, "session_scope", scope)
    monkeypatch.setattr(settings, "autopost_enabled", True)
    monkeypatch.setattr(settings, "retry_rate_limited", True)
    return scope


def a_refusal(session, pid, error, *, tried_minutes_ago=90, posted_hours_ago=1,
              platform="instagram"):
    reel = Reel(
        external_id=pid, permalink=f"https://reddit.com/{pid}", caption="lift",
        state="posted", ups=100,
        posted_at=datetime.now(UTC) - timedelta(hours=posted_hours_ago),
    )
    session.add(reel)
    session.flush()
    session.add(ReelPost(
        reel_id=reel.id, platform=platform, status="failed", error=error,
        tried_at=datetime.now(UTC) - timedelta(minutes=tried_minutes_ago),
    ))
    return reel.id


class TestWhatIsWorthAskingAgain:
    def test_a_spent_limit_is_retried(self, database, monkeypatch):
        from worker.tasks import publish

        with database() as session:
            reel_id = a_refusal(session, "aaa", LIMIT)

        asked: list[tuple[int, list[str]]] = []
        monkeypatch.setattr(publish, "publish_one",
                            lambda rid, only=None: asked.append((rid, only)) or [])

        publish.retry_rate_limited()
        assert asked == [(reel_id, ["instagram"])]

    def test_a_dead_token_is_not(self, database, monkeypatch):
        """It fails the same way forever, and asking again is exactly what
        spends the limit that broke the other reels."""
        from worker.tasks import publish

        with database() as session:
            a_refusal(session, "bbb", DEAD)

        asked = []
        monkeypatch.setattr(publish, "publish_one",
                            lambda rid, only=None: asked.append(rid) or [])

        assert publish.retry_rate_limited() == {"retried": 0, "failed": 0, "owed": 0}
        assert asked == []

    def test_one_refused_ten_minutes_ago_waits_out_the_hour(self, database, monkeypatch):
        from worker.tasks import publish

        monkeypatch.setattr(settings, "rate_limit_wait_minutes", 60)
        with database() as session:
            a_refusal(session, "ccc", LIMIT, tried_minutes_ago=10)

        assert publish.retry_rate_limited()["owed"] == 0

    def test_yesterdays_reel_is_let_go(self, database, monkeypatch):
        """Past a day it is no longer the clip of the day, and the queue has
        moved on."""
        from worker.tasks import publish

        monkeypatch.setattr(settings, "retry_within_hours", 24)
        with database() as session:
            a_refusal(session, "ddd", LIMIT, posted_hours_ago=40)

        assert publish.retry_rate_limited()["owed"] == 0

    def test_nothing_happens_while_autopost_is_off(self, database, monkeypatch):
        from worker.tasks import publish

        monkeypatch.setattr(settings, "autopost_enabled", False)
        with database() as session:
            a_refusal(session, "fff", LIMIT)

        assert publish.retry_rate_limited() == {"retried": 0}

    def test_only_the_refused_platform_is_asked_again(self, database, monkeypatch):
        """Not the whole reel. Facebook already took it, and posting it there
        twice is worse than not retrying at all."""
        from worker.tasks import publish

        with database() as session:
            reel_id = a_refusal(session, "ggg", LIMIT)
            session.add(ReelPost(
                reel_id=reel_id, platform="facebook", status="posted",
                platform_url="https://facebook.com/reel/1",
                posted_at=datetime.now(UTC)))

        asked = []
        monkeypatch.setattr(publish, "publish_one",
                            lambda rid, only=None: asked.append(only) or [])

        publish.retry_rate_limited()
        assert asked == [["instagram"]]


class TestAPostedReelCanStillBeRetried:
    """A reel counts as posted the moment *any* platform takes it, so the
    guard against double-posting would otherwise block the retry entirely."""

    def test_naming_the_platform_gets_past_the_already_posted_guard(
            self, database, monkeypatch, tmp_path):
        from worker.tasks import publish

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"not really a video")
        with database() as session:
            reel_id = a_refusal(session, "hhh", LIMIT)
            session.get(Reel, reel_id).local_path = str(clip)

        class Publisher:
            def publish(self, request):
                assert request.platforms == ["instagram"]
                from core.publishers import PublishResult
                return [PublishResult(platform="instagram", ok=True, post_id="p1",
                                      url="https://instagram.com/p/1")]

        monkeypatch.setattr(publish, "get_publisher", lambda: Publisher())
        rows = publish.publish_one(reel_id, only=["instagram"])
        assert [r.status for r in rows] == ["posted"]

    def test_without_a_platform_it_is_still_refused(self, database):
        from worker.tasks import publish

        with database() as session:
            reel_id = a_refusal(session, "iii", LIMIT)

        with pytest.raises(ValueError, match="already gone out"):
            publish.publish_one(reel_id)

    def test_a_retry_updates_the_row_rather_than_adding_one(
            self, database, monkeypatch, tmp_path):
        """Forty rows saying the same thing is a record of how often something
        was retried, not of what happened."""
        from worker.tasks import publish

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"not really a video")
        with database() as session:
            reel_id = a_refusal(session, "jjj", LIMIT)
            session.get(Reel, reel_id).local_path = str(clip)

        class Publisher:
            def publish(self, request):
                from core.publishers import PublishResult
                return [PublishResult(platform="instagram", ok=True, post_id="p2")]

        monkeypatch.setattr(publish, "get_publisher", lambda: Publisher())
        publish.publish_one(reel_id, only=["instagram"])

        with database() as session:
            rows = list(session.execute(
                select(ReelPost).where(ReelPost.reel_id == reel_id)).scalars())
        assert len(rows) == 1
        assert rows[0].status == "posted"
        assert rows[0].error is None

    def test_a_late_success_does_not_move_the_reels_posted_at(
            self, database, monkeypatch, tmp_path):
        """The day's tally counts posted_at, so moving it forward on a retry
        miscounts how many have gone out today."""
        from worker.tasks import publish

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"not really a video")
        with database() as session:
            reel_id = a_refusal(session, "kkk", LIMIT, posted_hours_ago=2)
            session.get(Reel, reel_id).local_path = str(clip)
            was = session.get(Reel, reel_id).posted_at

        class Publisher:
            def publish(self, request):
                from core.publishers import PublishResult
                return [PublishResult(platform="instagram", ok=True, post_id="p3")]

        monkeypatch.setattr(publish, "get_publisher", lambda: Publisher())
        publish.publish_one(reel_id, only=["instagram"])

        with database() as session:
            now = session.get(Reel, reel_id).posted_at
        assert now.replace(tzinfo=UTC) == was.replace(tzinfo=UTC)
