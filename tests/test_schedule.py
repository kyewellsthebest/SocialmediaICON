"""Five a day, an hour apart, from seven in the morning.

Every one of these drives a clock reading through a pure function rather than
waiting for a real one. That is the only way a bug at 06:59 gets found: a
scheduler that reads the clock itself can only be tested by being there at
06:59, which nobody ever is.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import schedule

BRISBANE = schedule.zone("Australia/Brisbane")  # UTC+10, no daylight saving
WINDOW = schedule.Window(start_hour=7, per_day=5, every_minutes=60)


def local(hour: int, minute: int = 0, day: int = 14) -> datetime:
    """A moment in Brisbane, as the UTC instant the code would actually see."""
    return datetime(2026, 9, day, hour, minute, tzinfo=BRISBANE).astimezone(UTC)


def due(now, posted=0, last=None, window=WINDOW):
    return schedule.due(now, posted, last, window, BRISBANE)


class TestTheDayOpensAtSeven:
    @pytest.mark.parametrize("hour", [0, 3, 6])
    def test_nothing_goes_out_before_it(self, hour):
        ready, why = due(local(hour))
        assert not ready
        assert "opens at 07:00" in why

    def test_the_minute_before_is_still_too_early(self):
        """The boundary is the only part of a schedule that is ever wrong."""
        ready, _ = due(local(6, 59))
        assert not ready

    def test_seven_exactly_is_not_too_early(self):
        ready, why = due(local(7, 0))
        assert ready, why

    def test_it_is_seven_where_the_audience_is_not_where_the_server_is(self):
        """Railway runs in UTC. 07:00 UTC is five in the afternoon in
        Brisbane, so reading the schedule against the server's clock would
        post the whole day into the evening."""
        seven_utc = datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
        ready, why = due(seven_utc)
        assert not ready, "07:00 UTC is 17:00 in Brisbane - the day is over"
        assert "closed" in why


class TestFiveAndNoMore:
    def test_the_fifth_is_still_allowed(self):
        ready, why = due(local(11), posted=4, last=local(10))
        assert ready, why

    def test_the_sixth_is_not(self):
        ready, why = due(local(12), posted=5, last=local(11))
        assert not ready
        assert "all 5 have gone out today" in why

    def test_tomorrow_starts_again(self):
        """The tally is per local day, so the count resets rather than the
        page going quiet forever."""
        ready, why = due(local(7, 0, day=15), posted=0, last=local(11, 0, day=14))
        assert ready, why


class TestAnHourApart:
    def test_two_in_the_same_minute_is_refused(self):
        ready, why = due(local(9), posted=1, last=local(9))
        assert not ready
        assert "until the next slot" in why

    def test_fifty_nine_minutes_is_not_an_hour(self):
        ready, _ = due(local(9, 59), posted=1, last=local(9, 0))
        assert not ready

    def test_an_hour_later_is(self):
        ready, why = due(local(10, 0), posted=1, last=local(9, 0))
        assert ready, why

    def test_a_long_gap_does_not_release_the_whole_backlog(self):
        """Coming back at 10:30 having posted once sends one reel, not three.
        Catching up by dumping the backlog is the burst this schedule exists
        to avoid, so a quiet morning means fewer posts rather than four
        arriving in four minutes."""
        ready, _ = due(local(10, 30), posted=1, last=local(7))
        assert ready
        # One goes out; the next decision is made against that one.
        ready_again, why = due(local(10, 31), posted=2, last=local(10, 30))
        assert not ready_again, why
        assert "until the next slot" in why


class TestADayThatIsShapedDifferently:
    def test_two_a_day_every_six_hours(self):
        window = schedule.Window(start_hour=9, per_day=2, every_minutes=360)
        assert due(local(9), 0, None, window)[0]
        assert not due(local(12), 1, local(9), window)[0]
        assert due(local(15), 1, local(9), window)[0]

    def test_the_day_closes_one_gap_after_the_last_slot(self):
        """07:00 plus five hourly slots means the window shuts at noon, not
        at eleven - the eleven o'clock reel gets its hour like the rest."""
        assert not due(local(12, 0), 4, local(10))[0]
        assert due(local(11, 30), 4, local(10))[0]

    def test_the_last_slot_is_worked_out_rather_than_guessed(self):
        assert WINDOW.end_hour == 11
        assert schedule.Window(start_hour=8, per_day=4, every_minutes=90).end_hour == 12.5


class TestWhenTheNextOneLands:
    def test_before_the_day_opens_it_is_the_opening(self):
        nxt = schedule.next_slot(local(5), 0, None, WINDOW, BRISBANE)
        assert nxt.hour == 7 and nxt.day == 14

    def test_mid_day_it_is_an_hour_after_the_last(self):
        nxt = schedule.next_slot(local(9, 10), 2, local(9), WINDOW, BRISBANE)
        assert nxt.hour == 10 and nxt.minute == 0

    def test_once_the_day_is_spent_it_is_tomorrow_morning(self):
        nxt = schedule.next_slot(local(12), 5, local(11), WINDOW, BRISBANE)
        assert nxt.day == 15 and nxt.hour == 7

    def test_after_the_window_shuts_it_is_tomorrow_even_with_slots_unused(self):
        nxt = schedule.next_slot(local(19), 2, local(9), WINDOW, BRISBANE)
        assert nxt.day == 15 and nxt.hour == 7


class TestTheLocalDay:
    def test_today_means_the_viewers_today(self):
        """Counting against a UTC day would roll over at ten in the morning
        in Brisbane, and put ten reels out on one afternoon."""
        midnight = schedule.day_of(local(14, 30), BRISBANE)
        assert midnight.hour == 0 and midnight.day == 14

    def test_late_evening_still_belongs_to_the_same_day(self):
        """23:00 Brisbane is 13:00 UTC the same day - but 09:00 Brisbane is
        23:00 UTC the day *before*, which is where a naive count goes wrong."""
        assert schedule.day_of(local(23, 30), BRISBANE).day == 14
        assert schedule.day_of(local(0, 30), BRISBANE).day == 14


class TestABadTimezoneDoesNotStopThePage:
    def test_a_typo_falls_back_to_utc(self):
        """A wrong hour is a bad schedule. A crash is no schedule at all."""
        assert schedule.zone("Austraila/Brisbane").key == "UTC"
        assert schedule.zone("").key == "UTC"

    def test_a_real_one_is_used(self):
        assert schedule.zone("Australia/Brisbane").key == "Australia/Brisbane"
        assert schedule.zone("America/New_York").key == "America/New_York"

    def test_a_zone_with_daylight_saving_still_works(self):
        """Brisbane has none, which is why it is the default - but somewhere
        that does must not break the boundary arithmetic."""
        sydney = schedule.zone("Australia/Sydney")
        summer = datetime(2026, 1, 14, 7, 0, tzinfo=sydney).astimezone(UTC)
        ready, why = schedule.due(summer, 0, None, WINDOW, sydney)
        assert ready, why


class TestWhatItTellsYouWhenNothingPosts:
    """"Why has nothing gone out?" has four different answers and each one is
    a different thing to go and look at."""

    @pytest.mark.parametrize("now,posted,last,expected", [
        (local(5), 0, None, "opens at 07:00"),
        (local(12), 5, local(11), "gone out today"),
        (local(9, 30), 1, local(9), "until the next slot"),
        (local(18), 2, local(9), "window closed"),
        (local(7, 30), 1, None, "has not come round yet"),
    ])
    def test_each_refusal_says_which_one_it_is(self, now, posted, last, expected):
        ready, why = due(now, posted, last)
        assert not ready and expected in why

    def test_a_yes_has_nothing_to_explain(self):
        ready, why = due(local(7))
        assert ready and why == ""


class TestTheDefaultsMatchTheBrief:
    def test_five_an_hour_apart_from_seven(self):
        from core.config import Settings

        config = Settings()
        assert config.post_start_hour == 7
        assert config.post_per_day == 5
        assert config.post_every_minutes == 60

    def test_the_last_one_lands_at_eleven(self):
        from core.config import Settings

        config = Settings()
        window = schedule.Window(config.post_start_hour, config.post_per_day,
                                 config.post_every_minutes)
        assert window.end_hour == 11

    def test_the_timezone_is_named_rather_than_assumed(self):
        from core.config import Settings

        assert schedule.zone(Settings().post_timezone).key != "UTC", (
            "an unnamed timezone means the server's, and the server is in UTC")


class TestCountingWhatHasGoneOut:
    def test_only_today_counts_and_only_posted_ones(self, tmp_path, monkeypatch):
        from contextlib import contextmanager

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from core.models import Base, Reel
        from worker.tasks import publish

        engine = create_engine(f"sqlite:///{tmp_path/'p.db'}")
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

        monkeypatch.setattr(publish, "session_scope", scope)

        now = datetime.now(UTC)
        with scope() as session:
            for n, (state, when) in enumerate([
                ("posted", now - timedelta(minutes=30)),
                ("posted", now - timedelta(hours=2)),
                ("posted", now - timedelta(days=3)),   # not today
                ("found", None),                        # never went out
            ]):
                session.add(Reel(
                    external_id=f"r{n}", permalink="u", caption="c",
                    state=state, posted_at=when, ups=1))

        count, last = publish.posted_today(BRISBANE)
        assert count == 2, "the old one and the unposted one must not count"
        assert last is not None and (now - last) < timedelta(hours=1)
