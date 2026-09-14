"""What a post actually costs Meta, counted on this side of the wire.

The question this exists to answer: Instagram refused everything with
"Application request limit reached" on a day four things had been posted.
Four is not twenty-five, and nothing could say where the gap came from,
because the only number available was Meta's - reported after the fact and
never itemised.

Posts and requests are not the same number. A reel is a container and a
publish. A carousel is three containers and a publish, because each slide is
fetched separately and the pair is then tied together. And a failed attempt
costs its containers whether or not anything is published, so a carousel
retried four times has asked Meta to hold twelve files for nothing. No cap on
*posts* would have caught that, because the posts were never the problem.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import budget
from core.config import settings
from core.models import Base, GraphCall


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'b.db'}")
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

    monkeypatch.setattr(budget, "session_scope", scope)
    monkeypatch.setattr(settings, "post_per_day", 5)
    monkeypatch.setattr(settings, "carousel_enabled", True)
    monkeypatch.setattr(settings, "carousel_per_day", 3)
    monkeypatch.setattr(settings, "publish_headroom", 4)
    monkeypatch.setattr(settings, "instagram_daily_attempts", 0)
    return scope


def a_call(session, kind, platform="instagram", hours_ago=0.0, ok=True):
    session.add(GraphCall(
        at=datetime.now(UTC) - timedelta(hours=hours_ago),
        platform=platform, kind=kind, ok=ok,
    ))


class TestWhatAPostCosts:
    def test_a_carousel_is_four_requests_not_one(self, ledger, meta_for_carousel):
        """Three containers - a slide each, then the pair tied together - and
        a publish. This is the arithmetic that turned four posts into a spent
        allowance, and it is invisible from the word "post"."""
        publisher, handler_calls = meta_for_carousel
        publisher.publish_carousel("c.jpg", "v.mp4", "a lift")

        spent = budget.spent("instagram")
        assert spent["container"] == 3
        assert spent["publish"] == 1

    def test_only_the_publish_counts_as_an_attempt(self, ledger):
        """One per post flow, successful or not - which is what makes the cap
        a cap on posting rather than on polling."""
        with ledger() as session:
            for _ in range(9):
                a_call(session, "container")
            for _ in range(30):
                a_call(session, "poll")
            a_call(session, "publish")

        assert budget.attempts_today() == 1


class TestTheCapIsCheckedBeforeAnythingIsSent:
    def test_the_schedule_sets_it(self, ledger):
        """Five reels and three carousels, plus headroom for a genuine retry.
        Derived rather than set beside the schedule, so raising POST_PER_DAY
        does not leave the cap behind and stop posting in the afternoon."""
        assert budget.cap() == 5 + 3 + 4

    def test_it_follows_the_schedule_up(self, ledger, monkeypatch):
        monkeypatch.setattr(settings, "post_per_day", 8)
        assert budget.cap() == 8 + 3 + 4

    def test_carousels_cannot_outnumber_the_reels_they_follow(self, ledger, monkeypatch):
        """Each one trails a reel by half an hour, so asking for more of them
        than there are reels buys allowance nothing can spend."""
        monkeypatch.setattr(settings, "post_per_day", 2)
        monkeypatch.setattr(settings, "carousel_per_day", 9)
        assert budget.cap() == 2 + 2 + 4

    def test_without_carousels_it_is_half(self, ledger, monkeypatch):
        monkeypatch.setattr(settings, "carousel_enabled", False)
        assert budget.cap() == 5 + 4

    def test_an_explicit_number_wins(self, ledger, monkeypatch):
        monkeypatch.setattr(settings, "instagram_daily_attempts", 3)
        assert budget.cap() == 3

    def test_room_while_under(self, ledger):
        with ledger() as session:
            for _ in range(9):
                a_call(session, "publish")
        assert budget.allowed("instagram")[0]

    def test_refused_at_the_cap_with_the_numbers_in_the_reason(self, ledger):
        with ledger() as session:
            for _ in range(12):
                a_call(session, "publish")
        ok, why = budget.allowed("instagram")
        assert not ok
        assert "12 of 12" in why
        assert "INSTAGRAM_DAILY_ATTEMPTS" in why

    def test_a_failed_attempt_still_counts(self, ledger):
        """Retrying past the cap is precisely how the cap got spent."""
        with ledger() as session:
            for _ in range(12):
                a_call(session, "publish", ok=False)
        assert not budget.allowed("instagram")[0]

    def test_it_refills_as_the_oldest_age_out(self, ledger):
        with ledger() as session:
            for _ in range(12):
                a_call(session, "publish", hours_ago=25)
        assert budget.allowed("instagram")[0]
        assert budget.attempts_today() == 0

    def test_the_carousel_shares_the_reels_allowance(self, ledger):
        """Same account, same limit. Counting them separately is how you
        arrive at twice the cap you set."""
        with ledger() as session:
            for _ in range(6):
                a_call(session, "publish", platform="instagram")
            for _ in range(6):
                a_call(session, "publish", platform="instagram_carousel")
        assert not budget.allowed("instagram")[0]
        assert not budget.allowed("instagram_carousel")[0]

    def test_facebook_is_not_capped_by_instagrams_number(self, ledger):
        with ledger() as session:
            for _ in range(40):
                a_call(session, "publish", platform="facebook")
        assert budget.allowed("facebook")[0]
        assert budget.attempts_today("instagram") == 0


class TestTheCapStopsTheQueueRatherThanMeta:
    def test_a_slot_does_not_open_once_the_cap_is_spent(self, ledger, monkeypatch):
        """Refused here, before three containers go out to find out."""
        from worker.tasks import publish

        monkeypatch.setattr(settings, "autopost_enabled", True)
        monkeypatch.setattr(publish, "destinations", lambda: ["instagram"])
        with ledger() as session:
            for _ in range(12):
                a_call(session, "publish")

        ready, why = publish.next_due()
        assert not ready
        assert "daily cap is spent" in why

    def test_carousels_stop_too(self, ledger, monkeypatch):
        from worker.tasks import publish

        monkeypatch.setattr(settings, "autopost_enabled", True)
        monkeypatch.setattr(settings, "carousel_enabled", True)
        monkeypatch.setattr(publish, "destinations", lambda: ["instagram"])
        with ledger() as session:
            for _ in range(12):
                a_call(session, "publish")

        assert "daily cap is spent" in publish.post_carousels_due()["skipped"]

    def test_retries_stop_too(self, ledger, monkeypatch):
        """A retry is a post flow like any other and costs the same
        containers."""
        from worker.tasks import publish

        monkeypatch.setattr(settings, "autopost_enabled", True)
        monkeypatch.setattr(settings, "retry_rate_limited", True)
        with ledger() as session:
            for _ in range(12):
                a_call(session, "publish")

        assert "daily cap is spent" in publish.retry_rate_limited()["skipped"]


class TestTheLedgerSurvivesABadDatabase:
    """Counting is not the job. A ledger that raises breaks the thing it is
    supposed to be measuring."""

    def test_recording_does_not_raise(self, monkeypatch):
        @contextmanager
        def broken():
            raise RuntimeError("no database")
            yield

        monkeypatch.setattr(budget, "session_scope", broken)
        budget.record("instagram", "publish")

    def test_reading_comes_back_empty_rather_than_raising(self, monkeypatch):
        @contextmanager
        def broken():
            raise RuntimeError("no database")
            yield

        monkeypatch.setattr(budget, "session_scope", broken)
        assert budget.spent("instagram")["publish"] == 0
        assert budget.allowed("instagram")[0]


class TestPollingIsNotFreeEither:
    def test_the_gaps_widen_rather_than_staying_flat(self):
        """A four-second clip is usually ready on the first look, and one that
        is not is rarely ready a moment later. Asking every five seconds for
        two minutes is twenty-four requests to learn one thing."""
        from core.publishers.meta import _poll_wait

        waits = [_poll_wait(n) for n in range(8)]
        assert waits == sorted(waits)
        assert waits[0] < waits[-1]
        # Two minutes of waiting cost twenty-four requests at a flat five
        # seconds. It is nine now, and the ceiling is what is being pinned -
        # anything under half the flat count is the point.
        total, looks = 0.0, 0
        while total < 120:
            total += _poll_wait(looks)
            looks += 1
        assert looks <= 12


class TestTheQuotaReadoutIsARequestToo:
    def test_it_is_cached_so_a_dashboard_refresh_is_not_a_call(self, monkeypatch):
        """A dashboard left open on a phone would otherwise spend the
        allowance asking how much of the allowance is left."""
        import core.publishers.meta as meta

        monkeypatch.setattr(meta, "_quota_cache", None)
        monkeypatch.setattr(settings, "quota_cache_minutes", 10)
        monkeypatch.setattr(settings, "instagram_user_id", "1784", raising=False)
        monkeypatch.setattr(settings, "meta_access_token", "EAA", raising=False)
        monkeypatch.setattr(settings, "instagram_access_token", None, raising=False)

        asked = {"n": 0}

        def handler(request):
            asked["n"] += 1
            return httpx.Response(200, json={"data": [
                {"quota_usage": 4, "config": {"quota_total": 25, "quota_duration": 86400}}
            ]})

        for _ in range(5):
            client = httpx.Client(transport=httpx.MockTransport(handler))
            out = meta.publishing_quota(client=client)
            client.close()

        assert asked["n"] == 1
        assert out["used"] == 4


@pytest.fixture
def meta_for_carousel(monkeypatch, ledger):
    """A publisher wired to a fake Graph API that says yes to everything."""
    from core.publishers.meta import MetaPublisher

    monkeypatch.setattr(settings, "meta_graph_version", "v23.0", raising=False)
    monkeypatch.setattr(settings, "instagram_user_id", "1784", raising=False)
    monkeypatch.setattr(settings, "meta_access_token", "EAA", raising=False)
    monkeypatch.setattr(settings, "instagram_access_token", None, raising=False)
    monkeypatch.setattr(settings, "meta_publish_timeout_s", 30, raising=False)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(f"{request.method} {path}")
        if request.method == "POST" and path.endswith("/media"):
            body = dict(httpx.QueryParams(request.content.decode()))
            if body.get("media_type") == "CAROUSEL":
                return httpx.Response(200, json={"id": "parent"})
            return httpx.Response(200, json={
                "id": "cover" if "image_url" in body else "video"})
        if path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "post-1"})
        return httpx.Response(200, json={"status_code": "FINISHED"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return MetaPublisher(client=client, sleep=lambda _s: None), seen


def test_the_ledger_reads_back_what_it_recorded(ledger):
    budget.record("instagram", "container")
    budget.record("instagram", "publish")
    budget.record("instagram", "publish", ok=False, detail="nope")

    out = budget.ledger("instagram")
    assert out["attempts"] == 2
    assert out["posted"] == 1
    assert out["calls"]["container"] == 1
    with ledger() as session:
        rows = list(session.execute(select(GraphCall)).scalars())
    assert {r.kind for r in rows} == {"container", "publish"}
