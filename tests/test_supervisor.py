"""The loop, and the three rules it must not break.

Nothing is posted. The caps hold across a restart. One dead channel does not
stop the others. Those are the properties worth defending; the rest of the
supervisor is glue over modules that already have their own tests.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from core import chat as chatlib
from core import roster
from core.config import settings
from core.supervisor import (
    DORMANT_READINGS,
    DORMANT_REST_S,
    Found,
    Supervisor,
    Watched,
)


@dataclass
class FakeBuffer:
    channel: str = "x"
    running: bool = True
    stopped: bool = False
    extracted: list = None

    def __post_init__(self):
        self.extracted = []

    def discard(self):
        self.stopped = True

    def failure(self):
        return "" if self.running else "ffmpeg exited"

    def status(self):
        return {"running": self.running, "held_s": 120.0, "megabytes": 90.0, "segments": 30}

    def extract(self, dest, **kwargs):
        self.extracted.append(kwargs)
        return dest


class FakeChat:
    def __init__(self, log):
        self.log = log
        self.stopped = False
        self.messages_seen = len(log.recent())

    def stop(self):
        self.stopped = True

    def status(self):
        return {"channel": "x", "connected": True, "messages_seen": self.messages_seen,
                "held": self.log.status(), "error": ""}


class Heard:
    """A hearing.Hearing with only the fields the scorer reads."""

    def __init__(self, laughs=(), shouts=(), drops=()):
        self.laughs, self.shouts, self.drops = list(laughs), list(shouts), list(drops)

    def as_dict(self):
        return {"laughs": self.laughs, "shouts": self.shouts, "drops": self.drops}


class Seen:
    """Likewise for watching.Watching."""

    def __init__(self, surges=(), cuts=(), flashes=(), stillness=()):
        self.surges, self.cuts = list(surges), list(cuts)
        self.flashes, self.stillness = list(flashes), list(stillness)

    def as_dict(self):
        return {"surges": self.surges, "cuts": self.cuts}


#: Chat offsets are measured from when the buffer opened, so "the live edge is
#: at offset 300" is the fiction every test here runs under: the stream has
#: been up five minutes and the senses have just read the last thirty seconds
#: of it. An event planted at chat offset 285 is fifteen seconds into that
#: window and five seconds before now.
LIVE_EDGE = 300.0
WINDOW_S = 30.0


def _watched(channel="x", messages=None, *, heard=None, seen=None) -> Watched:
    log = chatlib.LiveLog(window_s=300.0)
    log.extend(messages or [])
    found = Watched(
        channel=channel, buffer=FakeBuffer(channel), chat=FakeChat(log), started_at=0.0
    )
    found.heard, found.seen = heard, seen
    found.sense_window_s = WINDOW_S
    found.senses_at = LIVE_EDGE
    found.senses = {
        "heard": heard.as_dict() if heard else None,
        "seen": seen.as_dict() if seen else None,
    }
    return found


def _laughing_at(chat_s: float, *, length: float = 3.0):
    """Something the bot heard, at a chat offset, so the two line up."""
    at = chat_s - (LIVE_EDGE - WINDOW_S)
    return Heard(laughs=[(at, at + length, 0.9)])


def _chatter(seconds: int = 300, burst_at: float | None = None):
    msgs = [chatlib.Message(float(t), "hi", f"u{t % 30}") for t in range(seconds)]
    if burst_at is not None:
        msgs += [chatlib.Message(burst_at, "KEKW", f"r{i}") for i in range(60)]
        msgs += [chatlib.Message(burst_at + 2, "clip that", f"r{i}") for i in range(4)]
    return msgs


class TestNothingIsPosted:
    def test_posting_is_off_by_default(self):
        assert settings.live_posting_enabled is False

    def test_a_catch_is_stored_unapproved(self, monkeypatch):
        """A clip has to be looked at by a person before it goes anywhere."""
        stored = {}
        sup = Supervisor()
        monkeypatch.setattr(sup, "store", lambda record: stored.update(record))
        monkeypatch.setattr("core.reframe.to_portrait", lambda src, dest, **k: dest)

        watched = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        record = sup.catch(
            watched,
            found=Found(score=9.0, why={"chat_burst": 9.0}, at_s=15.0,
                        ago_s=15.0, chat_s=285.0),
            now=time.time(),
        )
        assert record["channel"] == "x"
        # The row the API renders carries approved=False until someone acts.
        assert "approved" not in record or record.get("approved") is not True


class TestTheCaps:
    def _rows(self, count, newest_minutes_ago=999):
        now = datetime.now(UTC)
        return [
            type("Row", (), {"created_at": now - timedelta(minutes=newest_minutes_ago + i)})()
            for i in range(count)
        ]

    def test_the_daily_cap_stops_further_catches(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(
            sup, "recent_catches", lambda since: self._rows(settings.live_clips_per_day)
        )
        assert sup.allowed() is False

    def test_under_the_cap_and_past_the_gap_is_allowed(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(sup, "recent_catches", lambda since: self._rows(3))
        assert sup.allowed() is True

    def test_the_hourly_gap_is_enforced(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(sup, "recent_catches", lambda since: self._rows(1, 10))
        assert sup.allowed() is False, "a clip ten minutes ago must block the next"

    def test_the_gap_opens_once_it_has_passed(self, monkeypatch):
        sup = Supervisor()
        gap = settings.live_min_gap_minutes + 5
        monkeypatch.setattr(sup, "recent_catches", lambda since: self._rows(1, gap))
        assert sup.allowed() is True

    def test_the_cap_is_counted_from_storage_not_memory(self, monkeypatch):
        """A restart must not hand back a fresh allowance."""
        sup = Supervisor()
        calls = []
        monkeypatch.setattr(
            sup, "recent_catches", lambda since: calls.append(since) or self._rows(0)
        )
        sup.allowed()
        assert calls, "allowed() must ask the database, not an in-memory counter"

    def test_a_full_day_blocks_the_tick_from_cutting(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(
            sup, "recent_catches", lambda since: self._rows(settings.live_clips_per_day)
        )
        cut = []
        monkeypatch.setattr(sup, "catch", lambda *a, **k: cut.append(1))
        sup.watching["x"] = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        sup.tick()
        assert not cut


class TestOneBadChannelDoesNotStopTheOthers:
    def test_a_dead_buffer_is_released_not_retried(self, monkeypatch):
        sup = Supervisor()
        dead = _watched("dead")
        dead.buffer.running = False
        alive = _watched("alive", messages=_chatter())
        sup.watching["dead"], sup.watching["alive"] = dead, alive
        monkeypatch.setattr(sup, "allowed", lambda **k: True)

        sup.tick()
        assert "dead" not in sup.watching
        assert "alive" in sup.watching
        assert dead.buffer.stopped and dead.chat.stopped

    def test_a_cut_that_throws_is_noted_and_the_loop_continues(self, monkeypatch):
        sup = Supervisor()
        sup.watching["x"] = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        sup.watching["y"] = _watched(
            "y", messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0)
        )
        monkeypatch.setattr(sup, "allowed", lambda **k: True)

        def explode(*_a, **_k):
            raise RuntimeError("ffmpeg said no")

        monkeypatch.setattr(sup, "catch", explode)
        sup.tick(now=LIVE_EDGE)  # must not raise
        assert any("ffmpeg said no" in e for e in sup.errors)
        assert set(sup.watching) == {"x", "y"}

    def test_a_channel_that_will_not_resolve_is_skipped(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(
            sup, "playback_url", lambda ch: (_ for _ in ()).throw(RuntimeError("403"))
        )
        assert sup.attach("nope") is None
        assert "nope" not in sup.watching
        assert any("could not resolve" in e for e in sup.errors)


class TestScoring:
    def test_a_quiet_channel_scores_nothing(self):
        sup = Supervisor()
        found = sup.score(_watched(messages=_chatter()), now=LIVE_EDGE)
        assert not found

    def test_a_burst_scores_and_names_its_reason(self):
        sup = Supervisor()
        found = sup.score(
            _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0)),
            now=LIVE_EDGE,
        )
        assert found.score > 0 and found.why
        assert found.chat_s == pytest.approx(285.0, abs=8.0)
        assert found.ago_s == pytest.approx(15.0, abs=8.0)

    def test_a_channel_too_new_to_have_a_window_scores_nothing(self):
        """Attaching and immediately cutting would produce a clip of nothing."""
        sup = Supervisor()
        short = _watched(messages=[chatlib.Message(float(t), "hi") for t in range(5)])
        assert not sup.score(short, now=LIVE_EDGE)

    def test_the_cooldown_stops_one_moment_being_cut_repeatedly(self, monkeypatch):
        sup = Supervisor()
        watched = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        watched.last_catch_at = time.time()
        sup.watching["x"] = watched
        monkeypatch.setattr(sup, "allowed", lambda **k: True)
        cut = []
        monkeypatch.setattr(sup, "catch", lambda *a, **k: cut.append(1))
        sup.tick()
        assert not cut, "chat keeps talking about a moment long after it happened"


class TestWhatTheDashboardSees:
    def test_signals_carry_chat_mood_and_buffer_state(self):
        watched = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        found = watched.signals()
        assert found["channel"] == "x"
        assert found["buffer"]["running"] is True
        assert "mood" in found["chat"]
        assert found["chat"]["recent"], "the Live view shows what chat is saying"
        assert found["chat"]["per_minute"] > 0

    def test_signals_do_not_decode_video(self, monkeypatch):
        """This runs every few seconds; it must stay arithmetic."""
        monkeypatch.setattr(
            "subprocess.run", lambda *a, **k: pytest.fail("signals() decoded video")
        )
        _watched(messages=_chatter()).signals()


class TestADeadDatabaseDoesNotStopTheWatch:
    """The bug that killed the first live run.

    allowed() runs on every tick and hit the database. The 'catches' table did
    not exist yet, so the very first tick threw and took the whole supervisor
    down - three buffers, three chat sockets, everything - about a second after
    Start was pressed. From the dashboard it looked like a dead button.
    """

    def _broken(self, sup, monkeypatch):
        def explode(**_kwargs):
            raise RuntimeError('relation "catches" does not exist')

        monkeypatch.setattr(sup, "recent_catches", explode)

    def test_allowed_refuses_rather_than_raising(self, monkeypatch):
        sup = Supervisor()
        self._broken(sup, monkeypatch)
        assert sup.allowed() is False, "cutting without knowing the count breaks the caps"

    def test_the_reason_is_recorded_where_the_page_can_show_it(self, monkeypatch):
        sup = Supervisor()
        self._broken(sup, monkeypatch)
        sup.allowed()
        assert any("daily cap" in e for e in sup.errors)
        assert any("catches" in e for e in sup.errors)

    def test_a_tick_survives_it_and_keeps_the_streams(self, monkeypatch):
        sup = Supervisor()
        self._broken(sup, monkeypatch)
        sup.watching["x"] = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        sup.tick()  # must not raise
        assert "x" in sup.watching, "a database fault must not drop the buffers"

    def test_status_never_throws_over_it(self, monkeypatch):
        sup = Supervisor()
        self._broken(sup, monkeypatch)
        found = sup.status()
        assert found["caps"]["allowed_now"] is False

    def test_a_catch_with_no_timestamps_does_not_crash_the_gap_check(self, monkeypatch):
        """max() over an empty generator is a ValueError, not a cap decision."""
        sup = Supervisor()
        monkeypatch.setattr(
            sup, "recent_catches", lambda **k: [type("Row", (), {"created_at": None})()]
        )
        assert sup.allowed() is True


class TestTheScoreCarriesItsReasons:
    """A total with no breakdown is the one thing this project promised not to do."""

    def test_signals_include_the_per_signal_breakdown(self):
        watched = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        watched.last_score = 22.3
        watched.last_why = {"chat_voices": 20.0, "chat_burst": 2.3}
        found = watched.signals()
        assert found["score"] == 22.3
        assert found["why"]["chat_voices"] == 20.0, (
            "the page showed 22.3 total and 'nothing is standing out'"
        )

    def test_the_breakdown_is_ordered_strongest_first(self):
        watched = _watched()
        watched.last_why = {"a": 1.0, "b": 9.0, "c": 5.0}
        assert list(watched.signals()["why"]) == ["b", "c", "a"]

    def test_a_tick_records_the_breakdown_it_scored_with(self, monkeypatch):
        sup = Supervisor()
        watched = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        sup.watching["x"] = watched
        monkeypatch.setattr(sup, "allowed", lambda **k: False)
        sup.tick(now=LIVE_EDGE)
        assert watched.last_why, "tick scored but threw the reasons away"
        assert watched.last_reason in watched.last_why


class TestASleepingStreamerLosesTheSlot:
    """The LosPollosTV case.

    Second on the list, thirteen thousand people watching, and asleep on
    camera. Chat carries on talking regardless, viewers say nothing, and the
    slot produces no clips for hours while fourth place goes unwatched.
    """

    def _asleep(self, motion=0.0005, mean_db=-57.9, peak_db=-40.2):
        watched = _watched(messages=_chatter())
        watched.audio = {"ok": True, "mean_db": mean_db, "peak_db": peak_db}
        watched.read_motion = lambda: motion
        return watched

    def test_silence_and_stillness_together_read_as_asleep(self):
        assert self._asleep().read_activity()["asleep_now"] is True

    def test_a_quiet_room_someone_is_moving_in_is_not_asleep(self):
        """Reading, drawing, playing something quiet - all still a stream."""
        assert self._asleep(motion=0.02).read_activity()["asleep_now"] is False

    def test_a_still_shot_with_someone_talking_over_it_is_not_asleep(self):
        watched = self._asleep(mean_db=-38.0, peak_db=-9.0)
        assert watched.read_activity()["asleep_now"] is False

    def test_one_quiet_reading_is_a_pause_not_a_bedtime(self):
        watched = self._asleep()
        watched.asleep_readings = 1
        assert watched.dormant is False

    def test_it_takes_a_run_of_readings(self):
        watched = self._asleep()
        watched.asleep_readings = DORMANT_READINGS
        assert watched.dormant is True

    def test_a_single_word_resets_the_count(self, monkeypatch):
        sup = Supervisor()
        watched = self._asleep(mean_db=-20.0, peak_db=-5.0)
        watched.asleep_readings = 2
        watched.audio_at = 0.0
        monkeypatch.setattr(watched, "read_audio", lambda *a, **k: watched.audio)
        sup.watching["x"] = watched
        monkeypatch.setattr(sup, "allowed", lambda **k: False)
        sup.tick()
        assert watched.asleep_readings == 0

    def test_a_dormant_stream_is_not_scored(self, monkeypatch):
        sup = Supervisor()
        watched = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        watched.asleep_readings = DORMANT_READINGS
        watched.audio_at = time.time()
        sup.watching["x"] = watched
        monkeypatch.setattr(sup, "allowed", lambda **k: True)
        cut = []
        monkeypatch.setattr(sup, "catch", lambda *a, **k: cut.append(1))
        sup.tick()
        assert not cut
        assert watched.last_reason == "asleep"

    def test_the_dashboard_is_told_which_stream_is_asleep(self):
        watched = self._asleep()
        watched.asleep_readings = DORMANT_READINGS
        watched.activity = watched.read_activity()
        found = watched.signals()
        assert found["dormant"] is True
        assert found["activity"]["motion"] is not None


class TestTheSlotGoesToTheNextStreamDown:
    """First, third and fourth - until second wakes up."""

    def _sup(self, monkeypatch, listing):
        sup = Supervisor()
        monkeypatch.setattr("core.roster.fetch_kick_live", lambda **k: listing)
        # No chat sockets in a unit test: the ranking falls back to viewers,
        # which is what an unmeasured stream gets anyway.
        monkeypatch.setattr(sup, "measure_chat", lambda listing, **k: listing)

        def attach(channel, *, entry=None, viewers=0):
            sup.watching[channel] = _watched(channel)
            return sup.watching[channel]

        monkeypatch.setattr(sup, "attach", attach)
        monkeypatch.setattr(sup, "release", lambda ch: sup.watching.pop(ch, None))
        return sup

    def _listing(self, *channels):
        return [
            roster.Live(channel=c, viewers=10000 - i * 100) for i, c in enumerate(channels)
        ]

    def test_a_sleeping_stream_is_swapped_for_the_next_one(self, monkeypatch):
        listing = self._listing("one", "two", "three", "four", "five")
        sup = self._sup(monkeypatch, listing)
        sup.poll_roster()
        assert set(sup.watching) == {"one", "two", "three"}

        sup.watching["two"].asleep_readings = DORMANT_READINGS
        sup.poll_roster()
        assert set(sup.watching) == {"one", "three", "four"}

    def test_the_roster_does_not_hand_the_sleeper_straight_back(self, monkeypatch):
        listing = self._listing("one", "two", "three", "four", "five")
        sup = self._sup(monkeypatch, listing)
        sup.poll_roster()
        sup.watching["two"].asleep_readings = DORMANT_READINGS
        sup.poll_roster()
        for _ in range(3):
            sup.poll_roster()
        assert "two" not in sup.watching, "second place is still asleep"
        assert len(sup.watching) == settings.live_slots

    def test_it_is_picked_up_again_once_the_rest_has_passed(self, monkeypatch):
        listing = self._listing("one", "two", "three", "four", "five")
        sup = self._sup(monkeypatch, listing)
        sup.poll_roster()
        sup.watching["two"].asleep_readings = DORMANT_READINGS
        sup.poll_roster()
        assert "two" not in sup.watching

        # Ranks are unchanged, so once the rest expires second place displaces
        # fourth exactly as it would have in the first place.
        later = time.time() + DORMANT_REST_S + 1
        sup.poll_roster(now=later)
        assert "two" in sup.watching

    def test_the_swap_back_settles_instead_of_thrashing(self, monkeypatch):
        listing = self._listing("one", "two", "three", "four", "five")
        sup = self._sup(monkeypatch, listing)
        sup.poll_roster()
        sup.watching["two"].asleep_readings = DORMANT_READINGS
        sup.poll_roster()
        later = time.time() + DORMANT_REST_S + 1
        sup.poll_roster(now=later)
        settled = set(sup.watching)
        for i in range(4):
            sup.poll_roster(now=later + i)
        assert set(sup.watching) == settled == {"one", "two", "three"}

    def test_it_never_holds_more_slots_than_it_is_allowed(self, monkeypatch):
        listing = self._listing(*[f"c{i}" for i in range(10)])
        sup = self._sup(monkeypatch, listing)
        for _ in range(4):
            sup.poll_roster()
            assert len(sup.watching) <= settings.live_slots


class TestTheClipRunsAsLongAsTheMomentDoes:
    def _cut(self, sup, watched, monkeypatch, *, at: float = 285.0,
             ago: float = 90.0, **kwargs):
        """`ago` is how long before the live edge the peak was.

        Generous by default, because the tail cannot be longer than the
        footage that exists after the peak - the clip is clamped to it, which
        is a rule worth testing on its own rather than tripping over here.
        """
        monkeypatch.setattr(sup, "store", lambda record: record)
        monkeypatch.setattr("core.reframe.to_portrait", lambda src, dest, **k: dest)
        return sup.catch(
            watched,
            found=Found(score=9.0, why={"laughter": 9.0}, at_s=15.0,
                        ago_s=ago, chat_s=at),
            now=time.time(), **kwargs,
        )

    def test_a_burst_that_dies_quickly_gives_a_short_clip(self, monkeypatch):
        sup = Supervisor()
        msgs = _chatter(300)
        msgs += [chatlib.Message(120.0 + (i % 20) * 0.1, "KEKW", f"r{i}") for i in range(60)]
        watched = _watched(messages=msgs)
        record = self._cut(sup, watched, monkeypatch, at=120.0, ago=180.0)
        assert record["duration_s"] < settings.live_max_clip_s

    def test_a_moment_that_keeps_going_is_capped_not_truncated_at_thirty(self, monkeypatch):
        sup = Supervisor()
        msgs = _chatter(300)
        msgs += [
            chatlib.Message(120.0 + t * 0.05, "KEKW", f"r{t}")
            for t in range(3000)
        ]
        watched = _watched(messages=msgs)
        record = self._cut(sup, watched, monkeypatch, at=120.0, ago=180.0)
        assert record["duration_s"] == pytest.approx(settings.live_max_clip_s, abs=0.51)

    def test_no_clip_ever_exceeds_the_cap(self, monkeypatch):
        sup = Supervisor()
        msgs = _chatter(300) + [
            chatlib.Message(120.0 + t * 0.02, "OMG", f"r{t}") for t in range(9000)
        ]
        watched = _watched(messages=msgs)
        record = self._cut(sup, watched, monkeypatch, at=120.0, ago=180.0)
        assert record["duration_s"] <= settings.live_max_clip_s

    def test_the_tail_never_runs_past_the_live_edge(self, monkeypatch):
        """A moment ten seconds ago cannot have thirty seconds of tail."""
        sup = Supervisor()
        msgs = _chatter(300) + [
            chatlib.Message(120.0 + t * 0.02, "OMG", f"r{t}") for t in range(9000)
        ]
        watched = _watched(messages=msgs)
        record = self._cut(sup, watched, monkeypatch, at=120.0, ago=6.0)
        asked = watched.buffer.extracted[-1]
        assert asked["trail_s"] <= 6.0
        assert record["duration_s"] == pytest.approx(settings.live_lead_s + 6.0, abs=0.05)

    def test_the_extract_asks_the_buffer_for_the_same_length(self, monkeypatch):
        sup = Supervisor()
        watched = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        record = self._cut(sup, watched, monkeypatch, at=285.0, ago=90.0)
        asked = watched.buffer.extracted[-1]
        assert asked["lead_s"] == settings.live_lead_s
        assert record["duration_s"] == pytest.approx(asked["lead_s"] + asked["trail_s"], abs=0.05)


class TestWhyItIsNotCutting:
    """"No" has four meanings and a bare False tells them apart for nobody.

    Waiting out the hour looks exactly like a worker that cannot reach the
    database, and only one of those is something to go and fix.
    """

    def _rows(self, count, newest_minutes_ago=999):
        now = datetime.now(UTC)
        return [
            type("Row", (), {"created_at": now - timedelta(minutes=newest_minutes_ago + i)})()
            for i in range(count)
        ]

    def test_a_dead_database_says_so(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(
            sup, "recent_catches",
            lambda **k: (_ for _ in ()).throw(RuntimeError('relation "catches" does not exist')),
        )
        found = sup.cap_state()
        assert found["allowed"] is False
        assert found["reason"] == "no database"
        assert "catches" in found["detail"]

    def test_the_hourly_gap_says_how_long_is_left(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(sup, "recent_catches", lambda **k: self._rows(1, 10))
        found = sup.cap_state()
        assert found["reason"] == "hourly gap"
        assert found["wait_minutes"] == settings.live_min_gap_minutes - 10

    def test_the_daily_cap_says_so_separately(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(
            sup, "recent_catches", lambda **k: self._rows(settings.live_clips_per_day)
        )
        found = sup.cap_state()
        assert found["reason"] == "daily cap"
        assert found["cut_today"] == settings.live_clips_per_day

    def test_clear_reports_how_many_are_gone(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(sup, "recent_catches", lambda **k: self._rows(3))
        found = sup.cap_state()
        assert found["allowed"] is True
        assert found["reason"] == "clear"
        assert found["cut_today"] == 3

    def test_allowed_still_answers_the_same_way(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(sup, "recent_catches", lambda **k: self._rows(3))
        assert sup.allowed() is True
        monkeypatch.setattr(sup, "recent_catches", lambda **k: self._rows(1, 10))
        assert sup.allowed() is False

    def test_the_reason_reaches_the_dashboard(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(
            sup, "recent_catches",
            lambda **k: (_ for _ in ()).throw(RuntimeError("could not connect")),
        )
        caps = sup.status()["caps"]
        assert caps["allowed_now"] is False
        assert caps["cap_reason"] == "no database"
        assert "could not connect" in caps["cap_detail"]


class TestItRefusesToClipNothing:
    """The clip that should never have existed.

    A streamer typing bet amounts into a gambling site, music over the top,
    nobody saying anything. Scored 18.0, entirely on chat_voices, and was cut
    because the only bar in front of a cut was the daily cap. Something always
    scores highest; that is not the same as something being worth watching.
    """

    def _busy_but_dull(self, seconds: int = 300):
        """A channel doing eight messages a second and reacting to nothing."""
        rng = random.Random(4)
        spam = ["deenthegreatWdeenthegreatW", "deenthegreatDigg", "CaptFail", "W"]
        return [
            chatlib.Message(float(t), rng.choice(spam), f"u{rng.randint(0, 400)}")
            for t in range(seconds)
            for _ in range(rng.randint(6, 10))
        ]

    def _ticked(self, monkeypatch, messages, *, heard=None):
        sup = Supervisor()
        watched = _watched(messages=messages, heard=heard)
        sup.watching["x"] = watched
        monkeypatch.setattr(sup, "allowed", lambda **k: True)
        cut = []
        monkeypatch.setattr(sup, "catch", lambda *a, **k: cut.append(1))
        sup.tick(now=LIVE_EDGE)
        return watched, cut

    def test_a_busy_channel_reacting_to_nothing_is_not_cut(self, monkeypatch):
        _, cut = self._ticked(monkeypatch, self._busy_but_dull())
        assert not cut

    def test_and_the_page_is_told_it_was_refused_rather_than_missed(self, monkeypatch):
        watched, _ = self._ticked(monkeypatch, self._busy_but_dull())
        assert watched.last_reason in ("too weak", "nothing happened", "")

    def test_a_real_reaction_on_the_same_channel_is_still_cut(self, monkeypatch):
        """The bar must reject nothing without also rejecting everything."""
        messages = self._busy_but_dull()
        messages += [chatlib.Message(283.0 + (i % 25) * 0.1, "KEKW", f"r{i}") for i in range(200)]
        messages += [chatlib.Message(286.0, "CLIP THAT", f"q{i}") for i in range(6)]
        _, cut = self._ticked(monkeypatch, messages, heard=_laughing_at(284.0, length=4.0))
        assert cut, "a crowd reacting on a busy channel is the thing we are here for"

    def test_a_score_under_the_floor_is_refused(self, monkeypatch):
        monkeypatch.setattr(settings, "live_min_score", 999.0)
        _, cut = self._ticked(monkeypatch, _chatter(burst_at=285.0))
        assert not cut

    def test_a_score_made_only_of_levels_is_refused(self, monkeypatch):
        """Even a huge total means nothing if none of it is an event."""
        sup = Supervisor()
        watched = _watched()

        monkeypatch.setattr(
            sup, "score",
            lambda w, **k: Found(score=500.0, why={"chat_voices": 500.0}, chat_s=285.0),
        )
        monkeypatch.setattr(sup, "allowed", lambda **k: True)
        sup.watching["x"] = watched
        cut = []
        monkeypatch.setattr(sup, "catch", lambda *a, **k: cut.append(1))
        sup.tick(now=LIVE_EDGE)
        assert not cut
        assert watched.last_reason == "nothing happened"

    def test_the_event_share_is_what_is_counted(self):
        why = {"chat_burst": 9.0, "chat_voices": 40.0, "audio_energy": 5.0}
        assert Supervisor.event_score(why) == 9.0

    def test_a_level_can_lift_a_real_moment_but_not_carry_one(self, monkeypatch):
        sup = Supervisor()
        watched = _watched()
        why = {"laughter": 16.0, "chat_voices": 30.0}
        monkeypatch.setattr(
            sup, "score", lambda w, **k: Found(score=46.0, why=why, chat_s=285.0)
        )
        monkeypatch.setattr(sup, "allowed", lambda **k: True)
        sup.watching["x"] = watched
        cut = []
        monkeypatch.setattr(sup, "catch", lambda *a, **k: cut.append(1))
        sup.tick(now=LIVE_EDGE)
        assert cut


class TestTheSensesLeadAndChatAgrees:
    """What the bot heard and saw decides. Chat only gets to agree.

    Everything before this was scored on chat, and chat on a big Kick channel
    is four hundred messages a minute of the channel's own emote whatever is
    on screen. A clip cut because a lot of people typed is a clip of people
    typing.
    """

    def _bed(self):
        """Five minutes of ordinary chatter, reacting to nothing."""
        rng = random.Random(5)
        return [
            chatlib.Message(
                float(t), rng.choice(["W", "KEKW", "deenDigg"]), f"u{rng.randint(0, 300)}"
            )
            for t in range(300)
            for _ in range(6)
        ]

    def _reacting(self, at: float = 285.0):
        msgs = [chatlib.Message(at + (i % 20) * 0.1, "KEKW", f"r{i}") for i in range(150)]
        msgs += [chatlib.Message(at + 3.0, "CLIP THAT", f"q{i}") for i in range(8)]
        return msgs

    def test_chat_erupting_over_nothing_scores_nothing(self):
        """No laugh, no surge, no shout - nothing was heard or seen."""
        sup = Supervisor()
        watched = _watched(messages=self._bed() + self._reacting())
        assert not sup.score(watched, now=LIVE_EDGE)

    def test_a_laugh_the_bot_heard_scores_on_its_own(self):
        """...and chat is not required for it."""
        sup = Supervisor()
        watched = _watched(messages=self._bed(), heard=_laughing_at(285.0, length=4.0))
        found = sup.score(watched, now=LIVE_EDGE)
        assert found and found.event_score > 0
        assert found.top_reason == "laughter"

    def test_chat_agreeing_raises_it_but_does_not_lead_it(self):
        sup = Supervisor()
        alone = sup.score(
            _watched(messages=self._bed(), heard=_laughing_at(285.0, length=4.0)),
            now=LIVE_EDGE,
        )
        agreed = sup.score(
            _watched(messages=self._bed() + self._reacting(),
                     heard=_laughing_at(285.0, length=4.0)),
            now=LIVE_EDGE,
        )
        assert agreed.score > alone.score, "chat agreeing has to count for something"
        assert agreed.top_reason == "laughter", "...but not for more than being there"

    def test_a_surge_in_the_picture_scores_on_its_own(self):
        sup = Supervisor()
        watched = _watched(messages=self._bed(), seen=Seen(surges=[(15.0, 4.0)]))
        found = sup.score(watched, now=LIVE_EDGE)
        assert found and found.top_reason == "motion_surge"

    def test_a_shout_scores(self):
        sup = Supervisor()
        watched = _watched(messages=self._bed(), heard=Heard(shouts=[(15.0, 16.0)]))
        assert sup.score(watched, now=LIVE_EDGE).event_score > 0

    def test_a_stream_with_no_senses_yet_scores_nothing(self):
        """The first twenty seconds, before anything has been listened to."""
        sup = Supervisor()
        watched = _watched(messages=self._bed())
        watched.sense_window_s = 0.0
        assert not sup.score(watched, now=LIVE_EDGE)


class TestTheThreeClocks:
    """Senses run 0..30 and end at senses_at. Chat counts from the buffer
    opening. The buffer only answers 'how long ago'. Confusing them puts the
    clip somewhere else entirely."""

    def test_the_end_of_the_sense_window_is_the_live_edge(self):
        watched = _watched()
        assert watched.chat_offset(WINDOW_S) == pytest.approx(LIVE_EDGE)
        assert watched.seconds_ago(WINDOW_S, now=LIVE_EDGE) == pytest.approx(0.0)

    def test_the_start_of_it_is_a_window_earlier(self):
        watched = _watched()
        assert watched.chat_offset(0.0) == pytest.approx(LIVE_EDGE - WINDOW_S)
        assert watched.seconds_ago(0.0, now=LIVE_EDGE) == pytest.approx(WINDOW_S)

    def test_the_mapping_round_trips(self):
        watched = _watched()
        for position in (0.0, 7.5, 15.0, 29.0):
            assert watched.window_position(
                watched.chat_offset(position)
            ) == pytest.approx(position)

    def test_reading_late_still_places_the_moment_correctly(self):
        """The senses run on a twenty second timer, so 'now' is always after them."""
        watched = _watched()
        later = LIVE_EDGE + 18.0
        assert watched.seconds_ago(WINDOW_S, now=later) == pytest.approx(18.0)
        assert watched.chat_offset(WINDOW_S) == pytest.approx(LIVE_EDGE), (
            "reading late must not move where the moment was"
        )

    def test_a_moment_is_quoted_from_where_it_happened(self, monkeypatch):
        sup = Supervisor()
        msgs = [chatlib.Message(float(t), f"m{t}", f"u{t}") for t in range(300)]
        watched = _watched(messages=msgs, heard=_laughing_at(285.0))
        found = sup.score(watched, now=LIVE_EDGE)
        monkeypatch.setattr(sup, "store", lambda record: record)
        monkeypatch.setattr("core.reframe.to_portrait", lambda src, dest, **k: dest)
        record = sup.catch(watched, found=found, now=LIVE_EDGE)
        said = {q["text"] for q in record["quotes"]}
        assert said & {f"m{t}" for t in range(280, 295)}, (
            f"quoted the wrong minute: {said}"
        )
