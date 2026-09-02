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
from pathlib import Path

import pytest

from core import chat as chatlib
from core import moments, roster
from core.config import settings
from core.supervisor import (
    DORMANT_READINGS,
    DORMANT_REST_S,
    SENSE_EVERY_S,
    SENSE_PARALLEL,
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
        # A real extract leaves a file behind, and the shortlist drops any
        # candidate whose file has gone - so a fake that writes nothing would
        # quietly test the wrong thing.
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"not really a clip")
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
        return {
            "laughs": [
                {"start_s": a, "end_s": b, "confidence": c} for a, b, c in self.laughs
            ],
            "shouts": [{"at_s": t, "rise_db": d} for t, d in self.shouts],
            "drops": [{"start_s": a, "end_s": b} for a, b in self.drops],
            "speech_share": 0.5,
            "music_share": 0.05,
        }


class Seen:
    """Likewise for watching.Watching."""

    def __init__(self, surges=(), cuts=(), flashes=(), stillness=()):
        self.surges, self.cuts = list(surges), list(cuts)
        self.flashes, self.stillness = list(flashes), list(stillness)

    def as_dict(self):
        return {
            "surges": [{"at_s": t, "size": v} for t, v in self.surges],
            "cuts": list(self.cuts),
            "still_s": 0.0,
            "duration_s": 30.0,
        }


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


def _catch(sup, watched, found, now):
    """The two halves as one call. Most tests care about the outcome, not the
    fact that cutting and deciding are now separated by up to an hour."""
    candidate = sup.cut(watched, found=found, now=now)
    return None if candidate is None else sup.finish(candidate, now=now)


def _approves(sup, monkeypatch, **kwargs):
    """Stand in for the model watching the clip. The gate has its own tests."""
    from core.verdict import Verdict

    found = Verdict(watched=True, worth_it=True, confidence=0.9,
                    happening="something happens", kind="funny", **kwargs)
    monkeypatch.setattr(sup, "consider", lambda *a, **k: found)
    return found


def _refuses(sup, monkeypatch, **kwargs):
    from core.verdict import Verdict

    found = Verdict(watched=True, worth_it=False, confidence=0.9,
                    happening="a man reads a menu", why="nothing happens", **kwargs)
    monkeypatch.setattr(sup, "consider", lambda *a, **k: found)
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
        _approves(sup, monkeypatch)
        monkeypatch.setattr(sup, "store", lambda record: stored.update(record))
        monkeypatch.setattr("core.reframe.to_portrait", lambda src, dest, **k: dest)

        watched = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        record = _catch(
            sup, watched,
            Found(score=9.0, why={"chat_burst": 9.0}, at_s=15.0, ago_s=15.0, chat_s=285.0),
            time.time(),
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

    def test_the_days_number_does_not_block_the_next_clip(self, monkeypatch):
        """It is a target for how many are kept, not a gate on cutting.

        Blocking was the old rule and it meant the day's allowance went to
        whatever happened first, which on a live stream is not the same thing
        as whatever was best. The weakest are trimmed as they are stored.
        """
        sup = Supervisor()
        monkeypatch.setattr(
            sup, "recent_catches", lambda since: self._rows(settings.live_clips_per_day)
        )
        assert sup.allowed() is True

    def test_but_something_has_gone_wrong_well_past_it(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(
            sup, "recent_catches",
            lambda since: self._rows(settings.live_clips_per_day * 3 + 1),
        )
        assert sup.allowed() is False

    def test_under_the_cap_and_past_the_gap_is_allowed(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(sup, "recent_catches", lambda since: self._rows(3))
        assert sup.allowed() is True

    def test_there_is_no_gap_between_clips_any_more(self, monkeypatch):
        """The single worst rule in the system.

        The buffer remembers five minutes and the gap was an hour, so a moment
        that was not cut immediately was gone before permission arrived - and
        the bot clipped whatever happened to be happening when the hour turned.
        """
        sup = Supervisor()
        monkeypatch.setattr(sup, "recent_catches", lambda since: self._rows(1, 1))
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

    def test_a_full_day_keeps_nothing_but_still_holds_the_moment(self, monkeypatch):
        """The cap stops the *keep*, not the cut.

        The buffer remembers five minutes and the gap is an hour, so refusing
        to cut until permission arrives means the moment is gone before it is
        granted. It is cut and held; only the keep waits.
        """
        sup = Supervisor()
        monkeypatch.setattr(
            sup, "recent_catches", lambda since: self._rows(settings.live_clips_per_day)
        )
        sup.watching["x"] = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        caught = sup.tick(now=LIVE_EDGE)
        assert caught == [], "the day's allowance is spent"
        assert len(sup.shortlist) == 1, "...but the moment is not thrown away"

    def test_nothing_is_kept_while_the_cap_is_full(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(
            sup, "recent_catches", lambda since: self._rows(settings.live_clips_per_day)
        )
        finished = []
        monkeypatch.setattr(sup, "finish", lambda *a, **k: finished.append(1))
        sup.watching["x"] = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        sup.tick(now=LIVE_EDGE)
        assert not finished


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

        monkeypatch.setattr(sup, "cut", explode)
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
        watched.last_catch_at = LIVE_EDGE
        sup.watching["x"] = watched
        monkeypatch.setattr(sup, "allowed", lambda **k: True)
        cut = []
        monkeypatch.setattr(sup, "cut", lambda *a, **k: cut.append(1) or None)
        sup.tick(now=LIVE_EDGE)
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
        sup.tick(now=LIVE_EDGE)  # must not raise
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

    def _asleep(self, motion=0.019, mean_db=-57.9, peak_db=-40.2, speech=0.0):
        watched = _watched(messages=_chatter())
        watched.audio = {"ok": True, "mean_db": mean_db, "peak_db": peak_db}
        watched.read_motion = lambda: motion
        watched.heard = Heard()
        watched.heard.speech_share = speech
        # These tests are about read_activity, not about sensing: a real sense
        # pass would replace `heard` with a failed read of a fake buffer.
        watched.read_senses = lambda *a, **k: watched.senses
        return watched

    def test_silence_and_stillness_together_read_as_asleep(self):
        assert self._asleep().read_activity()["asleep_now"] is True

    def test_a_quiet_room_someone_is_moving_in_is_not_asleep(self):
        """Reading, drawing, playing something quiet - all still a stream."""
        assert self._asleep(motion=0.4).read_activity()["asleep_now"] is False

    def test_a_still_shot_with_someone_talking_over_it_is_not_asleep(self):
        """A podcast on a locked-off camera is a stream, not an empty room."""
        watched = self._asleep(mean_db=-38.0, peak_db=-9.0, speech=0.55)
        assert watched.read_activity()["asleep_now"] is False

    def test_a_still_shot_with_music_over_it_is_asleep(self):
        """The case this exists for: asleep with a game or a playlist running.

        Requiring silence meant the count never started, so the slot was never
        freed, so the bot watched a sleeping man all night.
        """
        watched = self._asleep(mean_db=-22.0, peak_db=-4.0, speech=0.01)
        assert watched.read_activity()["asleep_now"] is True

    def test_a_silent_still_room_is_believed_faster_than_a_noisy_one(self):
        assert (
            self._asleep().read_activity()["weight"]
            > self._asleep(mean_db=-22.0, peak_db=-4.0).read_activity()["weight"]
        )

    def test_one_quiet_reading_is_a_pause_not_a_bedtime(self):
        watched = self._asleep()
        watched.asleep_readings = 1
        assert watched.dormant is False

    def test_it_takes_a_run_of_readings(self):
        watched = self._asleep()
        watched.asleep_readings = DORMANT_READINGS
        assert watched.dormant is True

    def test_speech_alone_keeps_a_motionless_stream_watched(self):
        watched = self._asleep(speech=0.5)
        assert watched.read_activity()["weight"] == 0

    def test_a_single_word_resets_the_count(self, monkeypatch):
        sup = Supervisor()
        watched = self._asleep(mean_db=-20.0, peak_db=-5.0, speech=0.6)
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
        monkeypatch.setattr(sup, "cut", lambda *a, **k: cut.append(1) or None)
        sup.tick(now=LIVE_EDGE)
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
        # Pinned, because what is under test is the swap and not the slot
        # count: with five channels and ten slots there is nothing to swap.
        monkeypatch.setattr(settings, "live_slots", 3)
        monkeypatch.setattr(settings, "live_drop_rank", 6)
        sup = Supervisor()
        monkeypatch.setattr("core.roster.fetch_kick_live", lambda **k: listing)
        # No chat sockets in a unit test: the ranking falls back to viewers,
        # which is what an unmeasured stream gets anyway.
        monkeypatch.setattr(sup, "measure_chat", lambda listing, **k: listing)
        # No research calls in a unit test: the gate has its own tests.
        monkeypatch.setattr(sup, "wanted", lambda listing, **k: listing)

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
        _approves(sup, monkeypatch)
        monkeypatch.setattr(sup, "store", lambda record: record)
        monkeypatch.setattr("core.reframe.to_portrait", lambda src, dest, **k: dest)
        return _catch(
            sup, watched,
            Found(score=9.0, why={"laughter": 9.0}, at_s=15.0, ago_s=ago, chat_s=at),
            time.time(), **kwargs,
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

    def test_a_clip_a_minute_ago_is_no_longer_a_reason(self, monkeypatch):
        """The hourly gap is gone; it was the worst rule in the system."""
        sup = Supervisor()
        monkeypatch.setattr(sup, "recent_catches", lambda **k: self._rows(1, 1))
        assert sup.cap_state()["reason"] == "clear"

    def test_far_past_the_days_number_says_so(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(
            sup, "recent_catches",
            lambda **k: self._rows(settings.live_clips_per_day * 3 + 1),
        )
        found = sup.cap_state()
        assert "past the day" in found["reason"]

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
        monkeypatch.setattr(
            sup, "recent_catches",
            lambda **k: self._rows(settings.live_clips_per_day * 3 + 1),
        )
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
        monkeypatch.setattr(sup, "cut", lambda *a, **k: cut.append(1) or None)
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
        monkeypatch.setattr(sup, "cut", lambda *a, **k: cut.append(1) or None)
        sup.tick(now=LIVE_EDGE)
        assert not cut
        assert watched.last_reason == "nothing happened"

    def test_the_event_share_is_what_the_gate_counts(self):
        """Only what was heard or seen. A chat burst is agreement about a
        moment, never evidence that there was one - so a window carried
        entirely by typing has an event share of nothing and is refused.

        This used to assert the opposite, against a Supervisor.event_score
        that counted chat as an event. It had no caller: the gate reads
        Found.event_score, which is sensed-only, so the method existed solely
        to make this test pass while contradicting the rule beside it."""
        found = Found(score=54.0, why={"chat_burst": 9.0, "chat_voices": 40.0,
                                       "audio_energy": 5.0})
        assert found.event_score == 0.0
        assert found.crowd_score == 9.0
        from core.supervisor import gate

        passed, _, reason = gate(found)
        assert not passed and reason == "nothing happened"

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
        monkeypatch.setattr(sup, "cut", lambda *a, **k: cut.append(1) or None)
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
        _approves(sup, monkeypatch)
        monkeypatch.setattr(sup, "store", lambda record: record)
        monkeypatch.setattr("core.reframe.to_portrait", lambda src, dest, **k: dest)
        record = _catch(sup, watched, found, LIVE_EDGE)
        said = {q["text"] for q in record["quotes"]}
        assert said & {f"m{t}" for t in range(280, 295)}, (
            f"quoted the wrong minute: {said}"
        )


class TestNothingIsCutThatNothingHasWatched:
    """Once posting stops going past a person, this is the whole safety net."""

    def _ready(self, monkeypatch):
        sup = Supervisor()
        watched = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        monkeypatch.setattr(sup, "store", lambda record: record)
        monkeypatch.setattr("core.reframe.to_portrait", lambda src, dest, **k: dest)
        found = Found(score=40.0, why={"laughter": 40.0}, at_s=15.0, ago_s=90.0, chat_s=285.0)
        return sup, watched, found

    def test_a_refusal_throws_the_clip_away(self, monkeypatch):
        sup, watched, found = self._ready(monkeypatch)
        _refuses(sup, monkeypatch)
        assert _catch(sup, watched, found, time.time()) is None

    def test_and_says_what_it_refused_and_why(self, monkeypatch):
        sup, watched, found = self._ready(monkeypatch)
        _refuses(sup, monkeypatch)
        _catch(sup, watched, found, time.time())
        assert sup.declined
        assert sup.declined[-1]["happening"] == "a man reads a menu"
        assert sup.declined[-1]["why"] == "nothing happens"

    def test_a_candidate_nothing_could_watch_is_kept_anyway(self, monkeypatch):
        """This used to delete it, and that cost a day of clips.

        The harvest loop spends a look per candidate it tries and a declined
        one produces nothing, so the day's budget went early - and every
        candidate after that arrived here unwatched and was binned. One clip
        in twenty-four hours, with the rest cut, held and thrown away.
        """
        from core.verdict import Verdict

        sup, watched, found = self._ready(monkeypatch)
        monkeypatch.setattr(sup, "consider", lambda *a, **k: Verdict(
            problems=["the daily look budget of 30 is spent"]))
        record = _catch(sup, watched, found, time.time())
        assert record is not None, "a clip nothing watched is worth less, not nothing"
        assert record["verdict"]["watched"] is False

    def test_an_unwatched_clip_ranks_below_a_watched_one(self, monkeypatch):
        """Which is what makes keeping it safe: it sorts to the bottom of the
        review queue rather than pretending to be as good as the rest."""
        from core import ranking
        from core.verdict import Verdict

        sup, watched, found = self._ready(monkeypatch)
        monkeypatch.setattr(sup, "consider", lambda *a, **k: Verdict(problems=["spent"]))
        unwatched = _catch(sup, watched, found, time.time())

        sup2, watched2, found2 = self._ready(monkeypatch)
        _approves(sup2, monkeypatch)
        seen = _catch(sup2, watched2, found2, time.time())

        assert ranking.rank(unwatched).score < ranking.rank(seen).score

    def test_an_approval_is_recorded_with_the_clip(self, monkeypatch):
        sup, watched, found = self._ready(monkeypatch)
        _approves(sup, monkeypatch)
        record = _catch(sup, watched, found, time.time())
        assert record["verdict"]["worth_it"] is True
        assert record["verdict"]["happening"] == "something happens"

    def test_a_refused_candidate_still_costs_the_cooldown(self, monkeypatch):
        """Otherwise the same moment is cut, transcribed and judged every tick."""
        sup = Supervisor()
        watched = _watched(messages=_chatter(burst_at=285.0), heard=_laughing_at(285.0))
        sup.watching["x"] = watched
        monkeypatch.setattr(sup, "allowed", lambda **k: True)
        monkeypatch.setattr(sup, "cut", lambda *a, **k: None)
        sup.tick(now=LIVE_EDGE)
        assert watched.last_catch_at == LIVE_EDGE

    def test_the_page_is_told_how_much_of_the_budget_is_spent(self):
        from core.verdict import Verdict

        sup = Supervisor()
        sup.record_look(Verdict(watched=True, cost_usd=0.008))
        sup.record_look(Verdict(watched=True, cost_usd=0.008))
        found = sup.status()
        assert found["looked_today"] == 2
        assert found["look_budget_usd"] == settings.verdict_daily_usd


class TestItChoosesRatherThanTakingTheFirst:
    """The gap this class exists for.

    With one clip an hour, cutting the first moment that clears the bar is
    picking at random from everything that hour held. The buffer remembers five
    minutes and the gap is sixty, so waiting for permission is not an option
    either - by the time the slot opens the moment is long gone. So everything
    that qualifies is cut immediately and held, and the slot is spent on the
    best of them.
    """

    def _sup(self, monkeypatch, *, allowed=True):
        sup = Supervisor()
        monkeypatch.setattr(sup, "allowed", lambda **k: allowed)
        monkeypatch.setattr(sup, "store", lambda record: record)
        monkeypatch.setattr("core.reframe.to_portrait", lambda src, dest, **k: dest)
        _approves(sup, monkeypatch)
        return sup

    def _hold(self, sup, channel, score, *, at=None):
        """Held, and old enough to be used.

        Candidates wait live_review_s to be compared before a slot is spent on
        one - see harvest(). These tests are about *which* moment is chosen,
        so they start past that; the waiting itself is tested below."""
        watched = _watched(channel, messages=_chatter())
        found = Found(score=score, why={"laughter": score}, at_s=15.0,
                      ago_s=90.0, chat_s=285.0)
        ripe = time.time() - settings.live_review_s - 1.0
        candidate = sup.cut(watched, found=found, now=at if at is not None else ripe)
        sup.shortlist_add(candidate)
        return candidate

    def test_a_moment_is_not_used_before_it_has_met_its_competition(self, monkeypatch):
        """The bug this delay exists for. tick() adds a candidate and calls
        harvest() in the same pass, so without a wait every moment was cut and
        spent the instant it cleared the bar - it never met a competitor, and
        "keep the best five" only ever held one."""
        sup = self._sup(monkeypatch)
        now = time.time()
        self._hold(sup, "just-now", 71.0, at=now)
        assert sup.harvest(now=now) == [], "spent a slot on an unjudged moment"
        assert len(sup.shortlist) == 1, "and it is still held"

    def test_it_is_used_once_the_review_period_has_passed(self, monkeypatch):
        sup = self._sup(monkeypatch)
        now = time.time()
        self._hold(sup, "waited", 71.0, at=now - settings.live_review_s - 1.0)
        assert [r["channel"] for r in sup.harvest(now=now)] == ["waited"]

    def test_a_better_moment_arriving_late_displaces_the_weakest(self, monkeypatch):
        """Where the choosing actually happens: on insert, not on harvest."""
        monkeypatch.setattr(settings, "live_shortlist_max", 2)
        sup = self._sup(monkeypatch)
        now = time.time()
        self._hold(sup, "weak", 22.0, at=now)
        self._hold(sup, "middling", 40.0, at=now)
        self._hold(sup, "strong", 80.0, at=now + 60.0)
        assert sorted(c.channel for c in sup.shortlist) == ["middling", "strong"]

    def test_the_best_held_moment_is_the_one_that_is_used(self, monkeypatch):
        sup = self._sup(monkeypatch)
        self._hold(sup, "first", 22.0)
        self._hold(sup, "best", 71.0)
        self._hold(sup, "last", 30.0)
        made = sup.harvest(now=time.time())
        assert [r["channel"] for r in made] == ["best"]

    def test_it_chooses_across_streams_not_within_one(self, monkeypatch):
        """Three streams, one slot: the best moment of the hour, wherever it was."""
        sup = self._sup(monkeypatch)
        self._hold(sup, "n3on", 25.0)
        self._hold(sup, "oblivionsw", 64.0)
        self._hold(sup, "deenthegreat", 41.0)
        assert sup.harvest(now=time.time())[0]["channel"] == "oblivionsw"

    def test_only_one_slot_is_spent_at_a_time(self, monkeypatch):
        sup = self._sup(monkeypatch)
        self._hold(sup, "a", 50.0)
        self._hold(sup, "b", 40.0)
        assert len(sup.harvest(now=time.time())) == 1
        assert len(sup.shortlist) == 1, "the runner-up is still held for next time"

    def test_a_refused_favourite_hands_the_slot_to_the_next(self, monkeypatch):
        """A refusal says this moment is not worth posting, not that the hour was empty."""
        sup = self._sup(monkeypatch)
        self._hold(sup, "loud-but-empty", 80.0)
        self._hold(sup, "actually-good", 40.0)

        from core.verdict import Verdict

        seen = []

        def judge(candidate, **kwargs):
            seen.append(candidate.channel)
            worth = candidate.channel == "actually-good"
            return Verdict(watched=True, worth_it=worth, confidence=0.9,
                           happening="x", why="y", kind="funny")

        monkeypatch.setattr(sup, "consider", judge)
        made = sup.harvest(now=time.time())
        assert seen == ["loud-but-empty", "actually-good"]
        assert [r["channel"] for r in made] == ["actually-good"]

    def test_nothing_is_used_while_the_slot_is_shut(self, monkeypatch):
        sup = self._sup(monkeypatch, allowed=False)
        self._hold(sup, "a", 50.0)
        assert sup.harvest(now=time.time()) == []
        assert len(sup.shortlist) == 1

    def test_the_shortlist_has_a_ceiling(self, monkeypatch):
        """Held clips are files on a disk that is also holding three buffers."""
        sup = self._sup(monkeypatch, allowed=False)
        for i in range(settings.live_shortlist_max + 4):
            self._hold(sup, f"c{i}", float(i))
        assert len(sup.shortlist) == settings.live_shortlist_max

    def test_and_it_is_the_weakest_that_is_dropped(self, monkeypatch):
        sup = self._sup(monkeypatch, allowed=False)
        weakest = self._hold(sup, "weak", 1.0)
        for i in range(settings.live_shortlist_max):
            self._hold(sup, f"strong{i}", 90.0 + i)
        assert weakest not in sup.shortlist
        assert not weakest.raw.exists(), "the file goes with the candidate"

    def test_a_moment_held_too_long_is_let_go(self, monkeypatch):
        sup = self._sup(monkeypatch, allowed=False)
        now = time.time()
        stale = self._hold(sup, "stale", 90.0, at=now - settings.live_hold_max_s - 60)
        fresh = self._hold(sup, "fresh", 10.0, at=now)
        sup.prune_shortlist(now=now)
        assert sup.shortlist == [fresh]
        assert not stale.raw.exists()

    def test_a_held_clip_whose_file_vanished_is_forgotten(self, monkeypatch):
        sup = self._sup(monkeypatch, allowed=False)
        gone = self._hold(sup, "gone", 50.0)
        gone.raw.unlink()
        sup.prune_shortlist(now=time.time())
        assert sup.shortlist == []

    def test_the_context_travels_with_the_clip(self, monkeypatch):
        """By the time it is used, chat has forgotten the whole thing."""
        sup = self._sup(monkeypatch)
        msgs = [chatlib.Message(float(t), f"m{t}", f"u{t}") for t in range(300)]
        watched = _watched(messages=msgs, heard=_laughing_at(285.0))
        found = Found(score=50.0, why={"laughter": 50.0}, at_s=15.0, ago_s=90.0, chat_s=285.0)
        candidate = sup.cut(watched, found=found, now=time.time())

        # The stream is gone from the roster and its chat log with it.
        assert candidate.quotes, "quotes have to be captured at the cut"
        assert candidate.mood, "so does the mood"
        record = sup.finish(candidate, now=time.time() + 2000)
        assert record["quotes"]
        assert record["peak_viewers"] == candidate.viewers

    def test_the_record_is_dated_when_it_happened_not_when_it_was_used(self, monkeypatch):
        sup = self._sup(monkeypatch)
        cut_at = time.time() - 1800
        candidate = self._hold(sup, "x", 50.0, at=cut_at)
        record = sup.finish(candidate, now=time.time())
        assert record["caught_at"].startswith(
            datetime.fromtimestamp(cut_at, UTC).isoformat()[:16]
        )
        assert record["held_s"] == pytest.approx(1800, abs=5)

    def test_finishing_a_stream_that_has_since_been_dropped_still_works(self, monkeypatch):
        """Forty minutes is long enough for a channel to go offline."""
        sup = self._sup(monkeypatch)
        candidate = self._hold(sup, "gone-offline", 50.0)
        sup.watching.clear()
        assert sup.finish(candidate, now=time.time()) is not None

    def test_stopping_lets_go_of_everything_held(self, monkeypatch):
        sup = self._sup(monkeypatch, allowed=False)
        held = [self._hold(sup, f"c{i}", float(i + 1)) for i in range(3)]
        sup.stop()
        assert sup.shortlist == []
        assert not any(c.raw.exists() for c in held)

    def test_the_page_can_see_what_is_being_held(self, monkeypatch):
        sup = self._sup(monkeypatch, allowed=False)
        self._hold(sup, "a", 50.0)
        rows = sup.status()["shortlist"]
        assert rows[0]["channel"] == "a"
        assert rows[0]["score"] == 50.0


class TestTheRosterPollSurvivesARealListing:
    """`Live` is frozen. Every test above stubbed `measure_chat` and `wanted`
    out, so the two places that assigned to a field of one were never once
    executed - and in production both raised FrozenInstanceError, took down
    the roster poll every five seconds, and left the bot watching nothing all
    night. These run the real functions on real records."""

    class _Log:
        def __init__(self, count):
            self._held = [object()] * count

        def recent(self):
            return self._held

    class _Probe:
        def __init__(self, count, origin):
            self.log = TestTheRosterPollSurvivesARealListing._Log(count)
            self.origin = origin

        def stop(self):
            pass

    def test_chat_rate_is_measured_without_mutating_a_frozen_record(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr("core.livechat.LiveChat", lambda **k: self._Probe(0, 0.0))
        now = 1_000.0
        # A probe that has been listening two minutes and heard 240 messages.
        sup.probes["one"] = self._Probe(240, now - 120.0)
        listing = [roster.Live(channel="one", viewers=9000)]

        ranked = sup.measure_chat(listing, now=now)

        assert ranked[0].messages_per_min == 120.0
        assert listing[0].messages_per_min == 0.0, "the original must be untouched"

    def test_a_probe_that_has_not_settled_yet_is_left_in_the_listing(self, monkeypatch):
        """Dropping it would hide the stream from the roster entirely."""
        sup = Supervisor()
        monkeypatch.setattr("core.livechat.LiveChat", lambda **k: self._Probe(0, 0.0))
        now = 1_000.0
        sup.probes["one"] = self._Probe(99, now - 5.0)
        ranked = sup.measure_chat([roster.Live(channel="one", viewers=9000)], now=now)
        assert [live.channel for live in ranked] == ["one"]
        assert ranked[0].messages_per_min == 0.0

    def test_a_stream_with_no_probe_at_all_still_comes_back(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr("core.livechat.LiveChat", lambda **k: (_ for _ in ()).throw(
            RuntimeError("no socket")))
        ranked = sup.measure_chat([roster.Live(channel="one", viewers=9000)], now=1_000.0)
        assert [live.channel for live in ranked] == ["one"]

    def test_an_eligible_stream_carries_its_research_without_mutating_it(self, monkeypatch):
        sup = Supervisor()
        from core import profile as profiles

        monkeypatch.setattr(profiles, "decide", lambda channel, **k: profiles.Profile(
            channel=channel, eligible=True, about="A man who talks to a camera.",
            confidence=0.9,
        ))
        listing = [roster.Live(channel="one", viewers=9000)]
        kept = sup.wanted(listing, now=1_000.0)

        assert [live.channel for live in kept] == ["one"]
        assert kept[0].about, "the research has to reach the verdict prompt"
        assert listing[0].about == "", "the original must be untouched"

    def test_a_full_poll_runs_end_to_end_with_nothing_stubbed_out(self, monkeypatch):
        """The one test that would have caught it: no measure_chat stub, no
        wanted stub, real frozen Live records all the way through."""
        from core import profile as profiles

        listing = [roster.Live(channel=f"c{i}", viewers=9000 - i * 100) for i in range(6)]
        sup = Supervisor()
        monkeypatch.setattr("core.roster.fetch_kick_live", lambda **k: listing)
        monkeypatch.setattr("core.livechat.LiveChat", lambda **k: self._Probe(0, 0.0))
        monkeypatch.setattr(profiles, "decide", lambda channel, **k: profiles.Profile(
            channel=channel, eligible=True, about="talks to a camera", confidence=0.9,
        ))
        monkeypatch.setattr(sup, "attach", lambda channel, **k: sup.watching.setdefault(
            channel, _watched(channel)))
        monkeypatch.setattr(sup, "release", lambda ch: sup.watching.pop(ch, None))

        sup.poll_roster()

        assert sup.watching, "a poll that watches nothing is the bug"
        assert not [n for n in sup.errors if "failed" in n], sup.errors


class TestItSaysWhenItIsBroken:
    """Watching nothing looks exactly like starting up for the first minute
    and exactly like a fatal bug after the first hour. The only thing that
    told them apart was a repeating line in a log nobody was reading, and it
    cost a night of clipping."""

    def test_a_fresh_start_reads_as_starting_up(self):
        sup = Supervisor()
        found = sup.health()
        assert found["ok"] is True
        assert found["state"] == "starting"

    def test_watching_nothing_an_hour_in_is_a_fault(self):
        sup = Supervisor()
        sup.began_at = time.time() - 3600
        sup.errors.append("20:27:21 roster poll failed (cannot assign to field)")
        found = sup.health()
        assert found["ok"] is False
        assert "no roster poll has ever succeeded" in found["detail"]
        assert "60 min" in found["detail"]
        assert "cannot assign" in found["last_error"]

    def test_a_poll_that_works_but_refuses_everyone_says_so(self):
        sup = Supervisor()
        sup.began_at = time.time() - 3600
        sup.last_good_poll = time.time()
        sup.skipped = {"a": "is a game", "b": "is a game"}
        found = sup.health()
        assert found["ok"] is False
        assert "every stream was refused (2 skipped)" in found["detail"]

    def test_watching_anything_at_all_is_healthy(self):
        sup = Supervisor()
        sup.began_at = time.time() - 3600
        sup.watching["one"] = _watched("one")
        sup.watching["one"].senses_at = time.time()
        found = sup.health()
        assert found["ok"] is True
        assert found["state"] == "watching"

    def test_watching_but_reading_them_too_slowly_is_a_fault(self):
        """Ten streams on a box that can read four is not an outage - the page
        looks identical and the scores quietly describe a stream as it was a
        minute ago. Nothing else would ever say so."""
        sup = Supervisor()
        sup.began_at = time.time() - 3600
        sup.watching["one"] = _watched("one")
        sup.watching["one"].senses_at = time.time() - 300
        found = sup.health()
        assert found["ok"] is False
        assert found["state"] == "falling behind"
        assert "Fewer slots" in found["detail"]

    def test_the_status_the_page_reads_carries_it(self):
        assert "health" in Supervisor().status()


class TestHowManyStreamsItTakes:
    """Ten slots exist to answer a question - how many streams does it take
    to reach ten clips a day - and "eleven clips" does not answer it."""

    def _sup(self, monkeypatch, rows, watching=("a", "b", "c")):
        sup = Supervisor()
        for channel in watching:
            sup.watching[channel] = _watched(channel)
        monkeypatch.setattr(Supervisor, "recent_catches", lambda self, **k: rows)
        return sup

    def _catch(self, channel):
        return type("Row", (), {"channel": channel, "created_at": None})()

    def test_it_says_which_streams_the_clips_came_from(self, monkeypatch):
        rows = [self._catch("a"), self._catch("a"), self._catch("b")]
        found = self._sup(monkeypatch, rows).yield_report()
        assert found["clips_24h"] == 3
        assert found["per_stream"][0] == {"channel": "a", "clips": 2}

    def test_a_stream_that_produced_nothing_is_still_a_row(self, monkeypatch):
        """A zero is the most useful row in the table - it is the one that
        says the slot could be given back."""
        found = self._sup(monkeypatch, [self._catch("a")]).yield_report()
        assert {"channel": "c", "clips": 0} in found["per_stream"]
        assert found["streams_earning"] == 1
        assert found["streams_watched"] == 3

    def test_it_extrapolates_how_many_streams_the_target_needs(self, monkeypatch):
        """Three streams, three clips, target ten: it takes ten streams."""
        monkeypatch.setattr(settings, "live_clips_per_day", 10)
        rows = [self._catch("a"), self._catch("b"), self._catch("c")]
        found = self._sup(monkeypatch, rows).yield_report()
        assert found["streams_for_target"] == 10.0

    def test_no_clips_yet_is_not_a_division_by_zero(self, monkeypatch):
        found = self._sup(monkeypatch, []).yield_report()
        assert found["streams_for_target"] is None

    def test_a_dead_database_does_not_take_the_status_down(self, monkeypatch):
        def boom(self, **k):
            raise RuntimeError("no postgres")

        monkeypatch.setattr(Supervisor, "recent_catches", boom)
        found = Supervisor().status()
        assert found["yield"]["known"] is False

    def test_the_status_the_page_reads_carries_it(self, monkeypatch):
        monkeypatch.setattr(Supervisor, "recent_catches", lambda self, **k: [])
        assert "yield" in Supervisor().status()


class TestTheChatCurveReachesTheChart:
    """A number for "messages a minute" cannot show a room going quiet and
    then all talking at once, which is the shape every clip has."""

    def _curve(self, counts, voices=None):
        from core.chat import Curve

        return Curve(bucket_s=1.0, duration_s=float(len(counts)),
                     counts=list(counts), voices=list(voices or []))

    def test_it_downsamples_to_something_a_chart_can_draw(self):
        from core.supervisor import _trace

        found = _trace(self._curve(list(range(900))), points=90)
        assert len(found["counts"]) <= 91
        assert found["bucket_s"] == 10.0, "the axis has to say what a point covers"

    def test_it_keeps_the_peak_of_each_fold_not_the_mean(self):
        """A burst two seconds long inside a ten-second bucket is the whole
        point of the chart; averaging it away leaves a flat line."""
        from core.supervisor import _trace

        counts = [1] * 40
        counts[17] = 90
        found = _trace(self._curve(counts), points=4)
        assert max(found["counts"]) == 90

    def test_voices_travel_beside_counts(self):
        """One person sending forty messages and forty people sending one are
        the same count and a completely different moment."""
        from core.supervisor import _trace

        found = _trace(self._curve([10] * 20, [1] * 20), points=20)
        assert found["voices"] and len(found["voices"]) == len(found["counts"])

    def test_a_mismatched_voices_series_is_dropped_rather_than_drawn_wrong(self):
        from core.supervisor import _trace

        found = _trace(self._curve([10] * 20, [1] * 3), points=20)
        assert found["voices"] == []

    def test_an_empty_curve_is_not_a_crash(self):
        from core.supervisor import _trace

        assert _trace(self._curve([]))["counts"] == []


class TestTheSensesAreReadTogether:
    """Serially this is what broke ten streams: seven seconds of ffmpeg each,
    nothing published until the whole pass ended, and a snapshot that expired
    inside one normal pass. The page said "RESTARTING" for two thirds of every
    minute and a stream page 404ed because its channel was in a snapshot that
    had gone."""

    def _sup(self, channels, *, stale=999.0):
        sup = Supervisor()
        now = time.time()
        for i, c in enumerate(channels):
            w = _watched(c)
            w.senses_at = now - stale - i  # earlier index = fresher
            sup.watching[c] = w
        return sup

    def _record(self, sup, monkeypatch):
        import threading
        seen, lock = [], threading.Lock()

        def read(self, out_dir, *, now=None):
            with lock:
                seen.append(self.channel)
            self.senses_at = now or time.time()

        monkeypatch.setattr(Watched, "read_senses", read)
        return seen

    def test_it_reads_a_batch_at_once_not_one_at_a_time(self, monkeypatch):
        sup = self._sup([f"c{i}" for i in range(10)])
        seen = self._record(sup, monkeypatch)
        sup.read_all_senses(now=time.time())
        assert len(seen) == SENSE_PARALLEL, "a pass must be bounded, not all ten"

    def test_the_stalest_go_first(self, monkeypatch):
        """A box that cannot finish a pass has to starve the streams read most
        recently, not always the same ones at the end of the dict."""
        sup = self._sup(["fresh", "old", "oldest"])
        now = time.time()
        sup.watching["fresh"].senses_at = now - 30
        sup.watching["old"].senses_at = now - 300
        sup.watching["oldest"].senses_at = now - 900
        seen = self._record(sup, monkeypatch)
        sup.read_all_senses(now=now)
        assert seen[:2] == ["oldest", "old"] or set(seen[:2]) == {"oldest", "old"}

    def test_a_stream_read_recently_is_left_alone(self, monkeypatch):
        sup = self._sup(["a"], stale=0.0)
        seen = self._record(sup, monkeypatch)
        sup.read_all_senses(now=time.time())
        assert seen == []

    def test_one_stream_throwing_does_not_stop_the_others(self, monkeypatch):
        sup = self._sup(["bad", "good"])
        done = []

        def read(self, out_dir, *, now=None):
            if self.channel == "bad":
                raise RuntimeError("ffmpeg fell over")
            done.append(self.channel)
            self.senses_at = now or time.time()

        monkeypatch.setattr(Watched, "read_senses", read)
        sup.read_all_senses(now=time.time())
        assert done == ["good"]
        assert "ffmpeg fell over" in str(sup.watching["bad"].senses.get("problems"))

    def test_a_deaf_read_still_moves_the_clock(self, monkeypatch):
        """Otherwise it is due again next tick, forever, and starves the rest."""
        sup = self._sup(["bad"])

        def boom(self, out_dir, *, now=None):
            raise RuntimeError("no")

        monkeypatch.setattr(Watched, "read_senses", boom)
        now = time.time()
        sup.read_all_senses(now=now)
        assert sup.watching["bad"].senses_at == now


class TestItSaysWhenItCannotKeepUp:
    def test_the_lag_travels_to_the_page(self):
        sup = Supervisor()
        sup.watching["a"] = _watched("a")
        sup.watching["a"].senses_at = time.time() - 12
        found = sup.status()["lag"]
        assert found["known"] and found["keeping_up"] is True
        assert found["target_s"] == SENSE_EVERY_S

    def test_a_minute_old_reading_is_not_keeping_up(self):
        sup = Supervisor()
        sup.watching["a"] = _watched("a")
        sup.watching["a"].senses_at = time.time() - 90
        assert sup.sense_lag()["keeping_up"] is False

    def test_nothing_read_yet_is_not_a_crash(self):
        assert Supervisor().sense_lag() == {"known": False}


class TestTheStillnessThresholdIsAnchoredToRealFootage:
    """The numbers in the dormancy tests above were invented, and invented
    numbers move with whatever units the code happens to use. When motion
    became a rate per second, DORMANT_MOTION stayed a per-frame value: an
    empty room measured 0.019 against a threshold of 0.004, nothing was ever
    judged still, and sleep detection was dead with every test still green.

    This one measures the fixtures, so the threshold has to sit between a room
    with nobody in it and a room with something happening, whatever units
    either side is written in."""

    def _motion(self, src):
        from core import watching

        return watching.watch(src).average_motion

    def test_an_empty_room_falls_under_the_threshold(self):
        import synth_video as clips

        from core.supervisor import DORMANT_MOTION

        still = self._motion(clips.still_room())
        assert still <= DORMANT_MOTION, (
            f"an empty room reads {still:.4f} against a threshold of "
            f"{DORMANT_MOTION} - nothing will ever be judged asleep"
        )

    def test_a_room_with_something_happening_does_not(self):
        import synth_video as clips

        from core.supervisor import DORMANT_MOTION

        busy = self._motion(clips.nightclub())
        assert busy > DORMANT_MOTION * 3, (
            f"a nightclub reads {busy:.4f} against {DORMANT_MOTION} - too close "
            "to call a stream asleep on"
        )

    def test_the_threshold_agrees_with_the_one_in_the_eye(self):
        """core.watching asks the same question in its own stillness floor,
        and two answers to "is anything moving" is one too many."""
        import inspect

        from core import watching
        from core.supervisor import DORMANT_MOTION

        floor = inspect.signature(watching._find_stillness).parameters["below"].default
        assert floor == DORMANT_MOTION


class TestItSaysWhyItIsHoldingThisMany:
    """Three slots of ten filled is either "there were only three streams
    worth watching" or "seven would not attach", and those look identical on
    a page that only prints the number."""

    def _sup(self, monkeypatch, listing, *, eligible):
        from core import profile as profiles

        sup = Supervisor()
        keep = set(eligible)
        monkeypatch.setattr(profiles, "decide", lambda channel, **k: profiles.Profile(
            channel=channel, eligible=channel in keep, confidence=0.9,
            reason="an event" if channel not in keep else "a person",
        ))
        return sup

    def test_the_arithmetic_reaches_the_page(self, monkeypatch):
        listing = [roster.Live(channel=f"c{i}", viewers=9000 - i) for i in range(8)]
        sup = self._sup(monkeypatch, listing, eligible=["c0", "c1", "c2"])
        sup.wanted(listing, now=1000.0)
        found = sup.status()["roster"]
        assert found["considered"] == 8
        assert found["eligible"] == 3
        assert found["refused"] == 5
        assert found["slots"] == settings.live_slots

    def test_a_stream_that_would_not_attach_is_named(self, monkeypatch):
        """The one case that is a fault rather than an empty directory."""
        sup = Supervisor()
        monkeypatch.setattr(Supervisor, "playback_url", lambda self, ch: 1 / 0)
        assert sup.attach("gone") is None
        found = sup.status()["roster"]["attach_failed"]
        assert found and found[0]["channel"] == "gone"
        assert "playback" in found[0]["why"]

    def test_attaching_clears_an_earlier_failure(self, monkeypatch):
        sup = Supervisor()
        sup.attach_failed["gone"] = "playback: RuntimeError"
        monkeypatch.setattr(Supervisor, "playback_url", lambda self, ch: 1 / 0)
        sup.watching["gone"] = _watched("gone")
        sup.attach("gone")
        assert "gone" not in sup.attach_failed

    def test_the_page_can_tell_whether_a_watchdog_is_running(self):
        """"Is this watching 24/7 or only while I have the dashboard open" is
        a question the dashboard should be able to answer."""
        from core import livestate

        livestate._fallback.clear()
        assert Supervisor().status()["roster"]["watchdog_last_s"] is None
        livestate.watchdog_ran()
        assert Supervisor().status()["roster"]["watchdog_last_s"] is not None
        livestate._fallback.clear()


class TestWhereTheMomentsWent:
    """"Is it too harsh, or is it missing things" is not answerable from a
    count of clips. A day that scored four thousand windows and rejected all
    but one as too weak, and a day that scored six windows in total, produce
    the same one clip and need opposite fixes."""

    def test_it_counts_every_stage(self, monkeypatch):
        monkeypatch.setattr(settings, "live_min_score", 20.0)
        sup = Supervisor()
        for score in (2.0, 5.0, 18.0, 40.0):
            sup._tally("scored", score)
            if score < 20.0:
                sup._tally("too weak", score)
        found = sup.funnel_report()
        assert found["scored"] == 4
        assert {"stage": "too weak", "n": 3} in found["stages"]
        assert found["bar"] == 20.0

    def test_a_near_miss_is_kept_and_a_hopeless_one_is_not(self, monkeypatch):
        """A hundred windows scoring 18 against a bar of 20 says the bar is
        wrong. A hundred scoring 3 says it is not. The difference is the whole
        question."""
        monkeypatch.setattr(settings, "live_min_score", 20.0)
        sup = Supervisor()
        sup._tally("too weak", 18.5)
        sup._tally("too weak", 3.0)
        found = sup.funnel_report()
        assert found["near_misses"] == 1
        assert found["near_best"] == 18.5

    def test_near_misses_do_not_grow_without_bound(self, monkeypatch):
        monkeypatch.setattr(settings, "live_min_score", 20.0)
        sup = Supervisor()
        for _ in range(500):
            sup._tally("too weak", 19.0)
        assert len(sup.funnel["near_misses"]) <= 40

    def test_a_new_day_starts_again(self, monkeypatch):
        sup = Supervisor()
        sup._tally("scored", 5.0)
        sup.funnel["day"] = "1999-01-01"
        sup._tally("scored", 5.0)
        assert sup.funnel_report()["scored"] == 1

    def test_the_bill_is_reported_in_dollars(self, monkeypatch):
        """A count of looks is a guess at a bill dressed up as a limit: thirty
        Opus looks was $2.20 a day and thirty Haiku looks is 25 cents."""
        from core.verdict import Verdict

        monkeypatch.setattr(settings, "verdict_daily_usd", 2.50)
        sup = Supervisor()
        sup.record_look(Verdict(watched=True, cost_usd=0.008))
        sup.record_look(Verdict(watched=True, cost_usd=0.008))
        found = sup.funnel_report()
        assert found["looks_spent"] == 2
        assert found["spent_usd"] == 0.02
        assert found["budget_usd"] == 2.50

    def test_it_reaches_the_page(self):
        assert "funnel" in Supervisor().status()


class TestTheLookBudgetLastsTheDay:
    """A cap alone is spent in the first hour: the harvest loop offers the
    strongest held moment on every tick, a refusal costs money and produces
    nothing, and by mid-morning there is nothing left for the evening - which
    on these channels is when things happen."""

    def _sup(self, monkeypatch):
        monkeypatch.setattr(settings, "verdict_enabled", True)
        monkeypatch.setattr(settings, "verdict_daily_usd", 2.40)
        return Supervisor()

    def _candidate(self):
        return type("C", (), {
            "raw": Path("/nonexistent.mp4"), "senses": {}, "about": "", "said": None,
            "faces_at": [], "quotes": [], "channel": "x",
        })()

    def _spend(self, sup, usd):
        sup.spent_today()          # roll the day over
        sup.spend["usd"] = usd

    def test_spending_the_whole_day_stops_it(self, monkeypatch):
        sup = self._sup(monkeypatch)
        self._spend(sup, 2.40)
        found = sup.consider(self._candidate())
        assert found.watched is False
        assert "spent" in " ".join(found.problems)

    def test_running_ahead_of_the_clock_is_paced(self, monkeypatch):
        """Two dollars of a $2.40 day, spent by breakfast, is the shape that
        left the evening judged entirely on arithmetic.

        The clock is pinned, because the thing under test is a comparison
        against the time of day and reading the real one makes the test a
        reading of when it was run. Unpinned, this passed all morning and
        failed after 20:10 UTC, when a whole day's allowance has accrued and
        $2.00 is no longer ahead of anything."""
        sup = self._sup(monkeypatch)
        self._spend(sup, 2.00)
        # A quarter of the way through the day: $0.60 of $2.40 has accrued.
        monkeypatch.setattr(time, "time", lambda: 86400.0 * 10 + 86400.0 * 0.25)
        found = sup.consider(self._candidate())
        assert found.watched is False
        assert "pacing" in " ".join(found.problems)

    def test_spending_behind_the_clock_is_allowed(self, monkeypatch):
        sup = self._sup(monkeypatch)
        self._spend(sup, 0.0)
        monkeypatch.setattr("core.verdict.look", lambda *a, **k: "looked")
        assert sup.consider(self._candidate()) == "looked"

    def test_a_quiet_morning_banks_its_share_for_a_busy_night(self, monkeypatch):
        """The pace is against the clock, not a fixed gap, so an hour with
        nothing in it leaves more for the hour that has everything."""
        sup = self._sup(monkeypatch)
        # Most of the way through the day, a third of the budget spent.
        self._spend(sup, 0.80)
        monkeypatch.setattr("core.verdict.look", lambda *a, **k: "looked")
        monkeypatch.setattr(time, "time", lambda: 86400.0 * 10 + 86400.0 * 0.8)
        assert sup.consider(self._candidate()) == "looked"

    def test_what_a_look_cost_is_added_to_the_day(self, monkeypatch):
        from core.verdict import Verdict

        sup = self._sup(monkeypatch)
        sup.record_look(Verdict(watched=True, cost_usd=0.0082))
        sup.record_look(Verdict(watched=True, cost_usd=0.0082))
        assert sup.spent_today() == pytest.approx(0.0164)
        assert sup.spend["looks"] == 2

    def test_a_new_day_starts_the_budget_again(self, monkeypatch):
        from core.verdict import Verdict

        sup = self._sup(monkeypatch)
        sup.record_look(Verdict(watched=True, cost_usd=1.0))
        sup.spend["day"] = "1999-01-01"
        assert sup.spent_today() == 0.0


class TestHowManyClipsToExpect:
    """"How many should I expect a day" is not answerable from a total
    partway through one. Six clips an hour into a day is 144 a day."""

    def _sup(self, monkeypatch, *, hour, cut, judged):
        sup = Supervisor()
        monkeypatch.setattr(Supervisor, "recent_catches", lambda self, **k: [])
        monkeypatch.setattr(time, "time", lambda: 86400.0 * 100 + 3600.0 * hour)
        for _ in range(cut):
            sup._tally("cut", 30.0)
        sup.spent_today()
        sup.spend["looks"] = judged
        return sup

    def test_it_projects_a_day_from_an_hour(self, monkeypatch):
        found = self._sup(monkeypatch, hour=1, cut=6, judged=6).funnel_report()
        assert found["cut_today"] == 6
        assert found["cut_per_day"] == 144

    def test_it_says_how_many_were_cut_and_never_looked_at(self, monkeypatch):
        """The number a bigger budget buys, and the only one that says the
        money is what is limiting the count."""
        found = self._sup(monkeypatch, hour=6, cut=40, judged=12).funnel_report()
        assert found["unjudged_today"] == 28

    def test_judging_everything_leaves_nothing_unjudged(self, monkeypatch):
        found = self._sup(monkeypatch, hour=6, cut=12, judged=12).funnel_report()
        assert found["unjudged_today"] == 0

    def test_more_looks_than_cuts_is_not_a_negative_number(self, monkeypatch):
        """A look is spent on a refusal too, so judged can exceed cut."""
        found = self._sup(monkeypatch, hour=6, cut=5, judged=9).funnel_report()
        assert found["unjudged_today"] == 0

    def test_the_first_seconds_of_a_day_do_not_divide_by_zero(self, monkeypatch):
        found = self._sup(monkeypatch, hour=0, cut=1, judged=1).funnel_report()
        assert found["cut_per_day"] > 0

    def test_a_dead_database_still_gives_the_projection(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(Supervisor, "recent_catches",
                            lambda self, **k: 1 / 0)
        sup._tally("cut", 30.0)
        found = sup.funnel_report()
        assert found["kept_24h"] is None
        assert "cut_per_day" in found


class TestALoneSignalHasToBeEnormous:
    """The reading that made this necessary: motion 40.2, flash 3.0, chat 1.1,
    total 44.3, on a man walking down a street with a phone. It cleared the
    score bar of 20 and the event bar of 15 and would have been cut, and the
    panel called it "2 families of evidence contributing" because chat was
    three percent of motion."""

    def _ready(self, monkeypatch, why):
        monkeypatch.setattr(settings, "live_min_score", 20.0)
        monkeypatch.setattr(settings, "live_min_event_score", 15.0)
        monkeypatch.setattr(settings, "live_lone_signal_score", 55.0)
        sup = Supervisor()
        watched = _watched("x")
        sup.watching["x"] = watched
        found = Found(score=sum(why.values()), why=why, at_s=10.0, chat_s=10.0)
        monkeypatch.setattr(sup, "score", lambda w, **k: found)
        cut = []
        monkeypatch.setattr(sup, "cut", lambda w, **k: cut.append(1) or None)
        monkeypatch.setattr(sup, "harvest", lambda **k: [])
        monkeypatch.setattr(sup, "still_wanted", lambda w: True)
        return sup, watched, cut

    def test_the_walking_camera_is_not_cut(self, monkeypatch):
        sup, watched, cut = self._ready(
            monkeypatch, {"motion_surge": 40.2, "flash": 3.0, "chat_voices": 1.1})
        sup.tick(now=time.time())
        assert cut == []
        assert "motion" in watched.last_reason

    def test_the_same_motion_with_a_shout_is_cut(self, monkeypatch):
        """Two kinds of evidence landing together is the whole difference."""
        sup, watched, cut = self._ready(
            monkeypatch, {"motion_surge": 40.2, "shout": 12.0})
        sup.tick(now=time.time())
        assert cut == [1]

    def test_an_enormous_lone_signal_is_still_cut(self, monkeypatch):
        """Not a ban on one signal - a price. A man falling over with the
        sound muted is still a clip."""
        sup, watched, cut = self._ready(monkeypatch, {"motion_surge": 62.0})
        sup.tick(now=time.time())
        assert cut == [1]

    def test_the_refusal_is_counted_where_it_can_be_seen(self, monkeypatch):
        sup, watched, cut = self._ready(monkeypatch, {"motion_surge": 40.2})
        sup.tick(now=time.time())
        stages = {r["stage"]: r["n"] for r in sup.funnel_report()["stages"]}
        assert stages.get("one signal only") == 1


class TestBothBarsAreWatchedForBeingWrong:
    """The lone-signal bar of 55 was picked from a single screenshot. A bar
    picked that way being too high looks exactly like the streams being quiet,
    so it has to be watched the same way the score bar is."""

    def _sup(self, monkeypatch):
        monkeypatch.setattr(settings, "live_min_score", 20.0)
        monkeypatch.setattr(settings, "live_lone_signal_score", 55.0)
        return Supervisor()

    def test_a_near_miss_on_the_lone_signal_bar_is_kept(self, monkeypatch):
        sup = self._sup(monkeypatch)
        sup._tally("one signal only", 52.0)
        rows = {r["stage"]: r for r in sup.funnel_report()["near_by_bar"]}
        assert rows["one signal only"]["n"] == 1
        assert rows["one signal only"]["best"] == 52.0
        assert rows["one signal only"]["bar"] == 55.0

    def test_a_hopeless_lone_signal_is_not_a_near_miss(self, monkeypatch):
        """Motion of 20 against a bar of 55 says nothing about the bar."""
        sup = self._sup(monkeypatch)
        sup._tally("one signal only", 20.0)
        assert sup.funnel_report()["near_by_bar"] == []

    def test_the_two_bars_are_counted_apart(self, monkeypatch):
        """Near the score bar means the streams were quiet; near the lone
        bar means that number is wrong. Summing them hides both."""
        sup = self._sup(monkeypatch)
        sup._tally("too weak", 18.0)
        sup._tally("one signal only", 52.0)
        rows = {r["stage"]: r["n"] for r in sup.funnel_report()["near_by_bar"]}
        assert rows == {"too weak": 1, "one signal only": 1}

    def test_neither_grows_without_bound(self, monkeypatch):
        sup = self._sup(monkeypatch)
        for _ in range(300):
            sup._tally("one signal only", 52.0)
        assert len(sup.funnel["near_misses"]["one signal only"]) <= 40


class TestAClipTooWeakToKeepIsNotKept:
    """Six hours of watching filled the page with clips ranked 17 to 25.

    Every one had been cut legitimately. The number that cuts and the number
    that orders are different things on different scales - live_min_score is a
    threshold on a moment, measured out of the raw evidence before anything is
    cut, and the rank is out of 100 against every other clip - and nothing
    anywhere compared the finished clip against the bar a person would set for
    it. A moment scoring 46 became a clip ranked 19 and went in the queue.
    """

    def _sup(self, monkeypatch, *, verdict=None):
        from core.verdict import Verdict

        sup = Supervisor()
        monkeypatch.setattr(sup, "allowed", lambda **k: True)
        monkeypatch.setattr(sup, "store", lambda record: record)
        monkeypatch.setattr(sup, "publish_clip", lambda path: "key")
        monkeypatch.setattr("core.reframe.to_portrait", lambda src, dest, **k: dest)
        monkeypatch.setattr(settings, "live_keep_rank", 20.0)
        # Nothing looked at it, which is the case that produced the page.
        monkeypatch.setattr(
            sup, "consider",
            lambda *a, **k: verdict or Verdict(problems=["the day's looking is spent"]),
        )
        return sup

    def _finish(self, sup, *, heard=None, seen=None, messages=None):
        watched = _watched("x", messages=messages if messages is not None else _chatter(),
                           heard=heard, seen=seen)
        found = Found(score=46.0, why={"motion_surge": 46.0}, at_s=15.0,
                      ago_s=90.0, chat_s=285.0)
        candidate = sup.cut(watched, found=found, now=time.time())
        return candidate, sup.finish(candidate, now=time.time())

    def test_a_clip_ranked_under_the_floor_never_reaches_the_queue(self, monkeypatch):
        sup = self._sup(monkeypatch)
        candidate, record = self._finish(sup, seen=Seen(surges=[(15.0, 1.4)]),
                                         messages=[])
        assert record is None, "this is the clip the page was full of"
        assert not candidate.raw.exists()

    def test_it_says_so_where_the_funnel_can_see_it(self, monkeypatch):
        sup = self._sup(monkeypatch)
        self._finish(sup, seen=Seen(surges=[(15.0, 1.4)]), messages=[])
        stages = sup.funnel_report()["stages"]
        assert any(row["stage"] == "ranked too low" for row in stages), stages

    def test_a_clip_over_the_floor_is_kept(self, monkeypatch):
        # Chat bursting at the moment, not just ticking over. A clip with a
        # laugh, a surge and a completely flat chat is rejected outright now,
        # and rightly - see TestAClipNobodyRespondedToIsNotAClip.
        sup = self._sup(monkeypatch)
        _, record = self._finish(
            sup,
            heard=_laughing_at(285.0),
            seen=Seen(surges=[(15.0, 4.2)], cuts=[14.0, 16.0]),
            messages=_chatter(burst_at=285.0),
        )
        assert record is not None
        assert record["rank_score"] >= settings.live_keep_rank

    def test_a_clip_the_model_approved_is_kept_however_it_ranks(self, monkeypatch):
        """The verdict is the only judgement here formed by something that
        saw the video, and arithmetic does not get to overrule it."""
        from core.verdict import Verdict

        sup = self._sup(monkeypatch, verdict=Verdict(
            watched=True, worth_it=True, confidence=0.9, kind="funny"))
        _, record = self._finish(sup, seen=Seen(surges=[(15.0, 1.4)]), messages=[])
        assert record is not None

    def test_nothing_is_uploaded_for_a_clip_that_is_dropped(self, monkeypatch):
        """A clip nobody will ever see should not cost a transfer as well as
        an encode."""
        sup = self._sup(monkeypatch)
        sent: list = []
        monkeypatch.setattr(sup, "publish_clip", lambda path: sent.append(path))
        self._finish(sup, seen=Seen(surges=[(15.0, 1.4)]), messages=[])
        assert sent == []


class TestThePageShowsTheBarThatActuallyApplies:
    """"What is this scoring system?" has been asked of this page three times.

    Part of the answer was that the meter drew the wrong bar. There are two,
    and which applies depends on the evidence rather than on the settings: a
    reading with two families agreeing has to clear live_min_score, and one
    carried by a single family has to clear live_lone_signal_score, which is
    nearly three times higher. The page drew the low one in both cases, so a
    stream reading 46 off a lone motion surge appeared to be well past the
    line and about to be clipped, when it was never going to be cut at all.
    """

    def test_one_family_is_measured_against_the_high_bar(self):
        watched = _watched()
        watched.last_why = {"motion_surge": 45.6, "chat_voices": 0.8}
        assert watched.bar_now() == settings.live_lone_signal_score
        assert watched.signals()["cut_at"] == settings.live_lone_signal_score

    def test_families_agreeing_are_measured_against_the_low_one(self):
        watched = _watched()
        watched.last_why = {"motion_surge": 40.0, "laughter": 30.0}
        assert watched.bar_now() == settings.live_min_score

    def test_nothing_at_all_is_not_the_easy_bar(self):
        """An empty reading has no second family either."""
        watched = _watched()
        watched.last_why = {}
        assert watched.bar_now() == settings.live_lone_signal_score

    def test_the_bar_matches_the_gate_that_will_judge_it(self):
        """The whole point: the number drawn beside the score has to be the
        number the cut actually uses, or the meter is decoration."""
        for why in ({"motion_surge": 45.6, "chat_voices": 0.8},
                    {"motion_surge": 40.0, "laughter": 30.0},
                    {"laughter": 12.0}):
            watched = _watched()
            watched.last_why = why
            agreed = moments.agreeing(why)
            gate = (settings.live_min_score if len(agreed) >= 2
                    else settings.live_lone_signal_score)
            assert watched.bar_now() == gate, why


class TestItSaysWhyNothingIsWatchingClips:
    """Every clip in a six-hour run came out UNWATCHED and the page could only
    say so, not say why - and "why" has four completely different answers, one
    of which is a missing environment variable on one of two Railway services
    and is invisible from everywhere else.

    A clip nothing watched loses the only judgement here formed by something
    that saw the video, so this is not a detail."""

    def _sup(self, monkeypatch, **cfg):
        monkeypatch.setattr(settings, "verdict_enabled", True)
        monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
        monkeypatch.setattr(settings, "verdict_daily_usd", 2.40)
        for name, value in cfg.items():
            monkeypatch.setattr(settings, name, value)
        return Supervisor()

    def test_a_missing_key_is_named(self, monkeypatch):
        found = self._sup(monkeypatch, anthropic_api_key=None).looking()
        assert found["can"] is False
        assert "ANTHROPIC_API_KEY" in found["why"]

    def test_being_switched_off_is_not_the_same_as_being_broken(self, monkeypatch):
        found = self._sup(monkeypatch, verdict_enabled=False).looking()
        assert found["can"] is False
        assert "switched off" in found["why"]

    def test_a_spent_budget_says_so(self, monkeypatch):
        sup = self._sup(monkeypatch)
        sup.spent_today()
        sup.spend["usd"] = 2.40
        found = sup.looking()
        assert found["can"] is False
        assert "spent" in found["why"]

    def test_nothing_wrong_reads_as_nothing_wrong(self, monkeypatch):
        sup = self._sup(monkeypatch)
        sup.spent_today()
        sup.spend["usd"] = 0.0
        found = sup.looking()
        assert found["can"] is True
        assert found["why"] == ""

    def test_it_reaches_the_page(self, monkeypatch):
        sup = self._sup(monkeypatch, anthropic_api_key=None)
        assert sup.status()["looking"]["can"] is False

    def test_the_reason_matches_what_consider_would_do(self, monkeypatch):
        """A page that explains a refusal the code would not make is worse
        than one that says nothing."""
        sup = self._sup(monkeypatch)
        sup.spent_today()
        sup.spend["usd"] = 2.40
        assert sup.looking()["can"] is False
        candidate = type("C", (), {
            "raw": Path("/nonexistent.mp4"), "senses": {}, "about": "", "said": None,
            "faces_at": [], "quotes": [], "channel": "x",
        })()
        assert sup.consider(candidate).watched is False


class TestItSaysWhatTheMoneyBuys:
    """The budget is set in dollars rather than in looks precisely so this can
    be answered from real usage. An estimate is what let a cap of thirty looks
    survive a redesign meant to clip everything, because nobody could see the
    bill."""

    def _sup(self, monkeypatch):
        monkeypatch.setattr(settings, "verdict_enabled", True)
        monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
        monkeypatch.setattr(settings, "verdict_daily_usd", 2.50)
        return Supervisor()

    def test_it_prices_a_look_from_what_looks_have_cost(self, monkeypatch):
        from core.verdict import Verdict

        sup = self._sup(monkeypatch)
        for _ in range(4):
            sup.record_look(Verdict(watched=True, cost_usd=0.005))
        found = sup.looking()
        assert found["per_look_usd"] == pytest.approx(0.005, abs=1e-4)
        assert found["looks_a_day"] == 500

    def test_before_the_first_look_it_says_nothing_rather_than_zero(self, monkeypatch):
        """A made-up number here is worse than none: it is the number the
        budget would be set from."""
        found = self._sup(monkeypatch).looking()
        assert found["per_look_usd"] is None
        assert found["looks_a_day"] is None

    def test_a_free_look_does_not_divide_by_zero(self, monkeypatch):
        from core.verdict import Verdict

        sup = self._sup(monkeypatch)
        sup.record_look(Verdict(watched=True, cost_usd=0.0))
        found = sup.looking()
        assert found["looks_a_day"] is None


class TestADroppedClipCostsOnlyALook:
    """A clip below the floor used to be tightened, reframed to portrait and
    uploaded, and only then deleted. On a day when the floor was catching most
    of them - which is a day that happens, because the floor is a number
    somebody picked - that is most of the machine's work thrown away.

    Ranking needs the evidence, the verdict and the length, all of which exist
    before any of the pixels are touched. So the floor is checked first."""

    def _sup(self, monkeypatch, *, floor=20.0):
        from core.verdict import Verdict

        sup = Supervisor()
        monkeypatch.setattr(sup, "allowed", lambda **k: True)
        monkeypatch.setattr(sup, "store", lambda record: record)
        monkeypatch.setattr(settings, "live_keep_rank", floor)
        monkeypatch.setattr(
            sup, "consider", lambda *a, **k: Verdict(problems=["the day's looking is spent"]))
        return sup

    def _run(self, sup, monkeypatch, **kw):
        framed: list = []
        monkeypatch.setattr("core.reframe.to_portrait",
                            lambda src, dest, **k: framed.append(dest) or dest)
        sent: list = []
        monkeypatch.setattr(sup, "publish_clip", lambda path: sent.append(path) or "key")
        watched = _watched("x", messages=kw.pop("messages", _chatter()), **kw)
        found = Found(score=46.0, why={"motion_surge": 46.0}, at_s=15.0,
                      ago_s=90.0, chat_s=285.0)
        candidate = sup.cut(watched, found=found, now=time.time())
        record = sup.finish(candidate, now=time.time())
        return record, framed, sent

    def test_a_clip_under_the_floor_is_never_reframed(self, monkeypatch):
        sup = self._sup(monkeypatch)
        record, framed, sent = self._run(
            sup, monkeypatch, seen=Seen(surges=[(15.0, 1.4)]), messages=[])
        assert record is None
        assert framed == [], "it was cropped to portrait before being deleted"
        assert sent == []

    def test_a_clip_over_it_still_is(self, monkeypatch):
        sup = self._sup(monkeypatch)
        record, framed, sent = self._run(
            sup, monkeypatch,
            heard=_laughing_at(285.0),
            seen=Seen(surges=[(15.0, 4.2)], cuts=[14.0, 16.0]),
            messages=_chatter(burst_at=285.0),
        )
        assert record is not None
        assert len(framed) == 1
        assert len(sent) == 1

    def test_how_it_was_framed_still_reaches_the_record(self, monkeypatch):
        """The framing report is filled in during the reframe, which now
        happens after the record is built - so it has to be put back."""
        sup = self._sup(monkeypatch)

        def frame(src, dest, **kw):
            (kw.get("report") if kw.get("report") is not None else {}).update(
                {"layout": "stacked", "webcam": {"seen": 0.9}})
            return dest

        monkeypatch.setattr("core.reframe.to_portrait", frame)
        monkeypatch.setattr(sup, "publish_clip", lambda path: "key")
        watched = _watched("x", messages=_chatter(burst_at=285.0),
                           heard=_laughing_at(285.0),
                           seen=Seen(surges=[(15.0, 4.2)], cuts=[14.0, 16.0]))
        found = Found(score=46.0, why={"motion_surge": 46.0}, at_s=15.0,
                      ago_s=90.0, chat_s=285.0)
        record = sup.finish(sup.cut(watched, found=found, now=time.time()),
                            now=time.time())
        assert record["framing"]["layout"] == "stacked"


class TestTheDeclineCountIsADayNotAWindow:
    """The Clips tally showed "3 it turned down" off len(self.declined) - a
    list trimmed to the last eight. It is right until the ninth decline of the
    day and then stops moving, which reads like a model that stopped being
    fussy at exactly the moment it got fussier."""

    def _sup(self, monkeypatch):
        sup = Supervisor()
        monkeypatch.setattr(sup, "allowed", lambda **k: True)
        monkeypatch.setattr(sup, "store", lambda record: record)
        monkeypatch.setattr(sup, "publish_clip", lambda path: "key")
        monkeypatch.setattr("core.reframe.to_portrait", lambda src, dest, **k: dest)
        _refuses(sup, monkeypatch)
        return sup

    def test_it_keeps_counting_past_the_recent_list(self, monkeypatch):
        sup = self._sup(monkeypatch)
        for i in range(12):
            watched = _watched(f"c{i}", messages=_chatter(burst_at=285.0),
                               heard=_laughing_at(285.0))
            found = Found(score=50.0, why={"laughter": 50.0}, at_s=15.0,
                          ago_s=90.0, chat_s=285.0)
            sup.finish(sup.cut(watched, found=found, now=time.time()), now=time.time())

        assert len(sup.declined) == 8, "the recent list is still trimmed"
        assert sup.funnel_report()["declined"] == 12, "but the count is the day's"

    def test_a_declined_clip_is_still_not_kept(self, monkeypatch):
        sup = self._sup(monkeypatch)
        watched = _watched("x", messages=_chatter(burst_at=285.0),
                           heard=_laughing_at(285.0))
        found = Found(score=50.0, why={"laughter": 50.0}, at_s=15.0,
                      ago_s=90.0, chat_s=285.0)
        assert sup.finish(sup.cut(watched, found=found, now=time.time()),
                          now=time.time()) is None


class TestWhatShipsIsAClipNotTheWindow:
    """The buffer holds `live_lead_s` before the peak and a tail after it -
    22 seconds and 8 - because a moment nominated by chat happened before chat
    reacted to it. That correction belongs to the *trigger*, not to the edges
    of the clip, and the two were conflated: every clip shipped with
    twenty-two seconds of preamble in front of it.

    core.clipping finds the real edges from the audio. It was written for the
    offline reel tool first, and until this was wired in it ran nowhere near
    the live path, so the bot kept shipping the window.
    """

    @staticmethod
    def _held_window(tmp_path):
        """The shape of what the buffer hands over: talking, one loud moment
        at live_lead_s, talking after it."""
        import subprocess

        import synth_audio

        sound = synth_audio.join(
            synth_audio.speech(22.0, level=0.05),
            synth_audio.laughter(4.0, level=0.45),
            synth_audio.speech(14.0, level=0.05),
        )
        wav = synth_audio.write("held-window", sound, out=tmp_path)
        held = tmp_path / "held.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=c=gray:s=320x180:r=15:d=40",
             "-i", str(wav), "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(held)],
            check=True, capture_output=True,
        )
        return held

    def test_the_window_is_trimmed_to_the_edges_the_audio_gives(self, tmp_path):
        from core import clipping
        from core import supervisor as sup
        from core.ffmpeg_ops import probe

        src = self._held_window(tmp_path)
        was = probe(src).duration_s
        out = sup.Supervisor._tighten(src, _Judged(None, None), tmp_path)

        assert out != src, "it shipped the window untrimmed"
        length = probe(out).duration_s
        assert length < was - 4.0, "nothing was trimmed off the window"
        assert clipping.MIN_CLIP_S - 2.0 <= length <= clipping.MAX_CLIP_S

    def test_it_opens_near_the_moment_not_twenty_two_seconds_before_it(
            self, tmp_path):
        """The whole point. The moment is at 22s in the window; the clip has
        to open in the seconds before it, not at the top of the window."""
        from core import clipping
        from core import supervisor as sup
        from core import hearing
        from core.ffmpeg_ops import probe

        src = self._held_window(tmp_path)
        bounds = clipping.find(hearing.listen(src), 22.0, span_s=probe(src).duration_s)
        assert bounds.start_s > 6.0, bounds.as_dict()
        assert bounds.start_s <= 22.0 <= bounds.end_s

    def test_a_clip_it_cannot_listen_to_is_still_shipped(self, tmp_path):
        """Never lose a clip to this. A file with no audio, an ffmpeg that
        fails - the untrimmed window is worse than the clip and much better
        than nothing."""
        from core import supervisor as sup

        missing = tmp_path / "not-a-video.mp4"
        missing.write_bytes(b"")
        judged = _Judged(best_start_s=None, best_end_s=None)
        assert sup.Supervisor._tighten(missing, judged, tmp_path) == missing


@dataclass
class _Judged:
    best_start_s: float | None
    best_end_s: float | None


class TestTheChannelsYouChose:
    """The roster picks by viewers and chat rate, and the profile refuses the
    formats that cannot produce a clip. Neither is a substitute for naming the
    streamers you want: a watch party, a solo grind and a person being funny
    with their friends are the same listing row, and the difference between
    them is the whole business.
    """

    def _entry(self, name):
        return roster.Live(channel=name, viewers=9000)

    def test_with_no_list_every_channel_is_still_considered(self, monkeypatch):
        """The behaviour this had before, and the default."""
        monkeypatch.setattr(settings, "live_only_channels", "")
        monkeypatch.setattr(settings, "live_never_channels", "")
        assert settings.may_watch("anyone") == (True, "")

    def test_only_the_named_channels_are_watched(self, monkeypatch):
        monkeypatch.setattr(settings, "live_only_channels", "deenthegreat, n3on")
        assert settings.may_watch("n3on")[0]
        allowed, why = settings.may_watch("somebodyelse")
        assert not allowed and "watch list" in why

    def test_the_name_is_matched_however_it_is_typed(self, monkeypatch):
        monkeypatch.setattr(settings, "live_only_channels", " DeenTheGreat , N3on ")
        assert settings.may_watch("deenthegreat")[0]
        assert settings.may_watch("N3ON")[0]

    def test_never_beats_only(self, monkeypatch):
        """So one name can suspend a stream without editing the list."""
        monkeypatch.setattr(settings, "live_only_channels", "deenthegreat,n3on")
        monkeypatch.setattr(settings, "live_never_channels", "n3on")
        assert not settings.may_watch("n3on")[0]
        assert settings.may_watch("deenthegreat")[0]

    def test_a_refused_channel_never_reaches_the_research(self, monkeypatch):
        """Which is the other half of the point: a narrow list also stops the
        bot paying a model to decide about forty channels it will not watch."""
        asked = []

        def decide(channel, **kwargs):
            asked.append(channel)
            raise AssertionError("should not have been researched")

        monkeypatch.setattr("core.profile.decide", decide)
        monkeypatch.setattr(settings, "live_only_channels", "keepme")
        monkeypatch.setattr(settings, "live_never_channels", "")
        sup = Supervisor()
        kept = sup.wanted([self._entry("dropme")], now=1_000.0)

        assert kept == []
        assert asked == []
        assert "watch list" in sup.skipped["dropme"]

    def test_it_says_why_a_channel_was_skipped(self, monkeypatch):
        monkeypatch.setattr(settings, "live_only_channels", "")
        monkeypatch.setattr(settings, "live_never_channels", "noisy")
        sup = Supervisor()
        sup.wanted([self._entry("noisy")], now=1_000.0)
        assert sup.skipped["noisy"] == "on the never-watch list"


class TestWatchingOneCategory:
    """Ten slots on IRL. The directory mixes every category by viewer count,
    so filtering one page of forty can leave three eligible streams and seven
    empty slots - and nothing anywhere saying why."""

    def _rows(self, *pairs):
        return [roster.Live(channel=c, viewers=9000, category=k) for c, k in pairs]

    def test_only_the_named_category_is_watched(self, monkeypatch):
        monkeypatch.setattr(settings, "live_only_channels", "")
        monkeypatch.setattr(settings, "live_only_categories", "irl")
        assert settings.may_watch("n3on", "IRL")[0]
        allowed, why = settings.may_watch("someone", "Slots")
        assert not allowed and why == "not in irl"

    def test_the_category_is_matched_however_kick_capitalises_it(self, monkeypatch):
        monkeypatch.setattr(settings, "live_only_channels", "")
        monkeypatch.setattr(settings, "live_only_categories", " IRL , Just Chatting ")
        assert settings.may_watch("a", "irl")[0]
        assert settings.may_watch("b", "just chatting")[0]

    def test_a_named_channel_beats_the_category(self, monkeypatch):
        """If you asked for somebody by name you want them whatever they have
        loaded - a streamer's category changes through an evening without
        them becoming a different person."""
        monkeypatch.setattr(settings, "live_only_channels", "deenthegreat")
        monkeypatch.setattr(settings, "live_only_categories", "irl")
        assert settings.may_watch("deenthegreat", "Grand Theft Auto V")[0]

    def test_a_stream_in_the_wrong_category_never_reaches_the_research(
            self, monkeypatch):
        def decide(channel, **kwargs):
            raise AssertionError("paid to research a category we do not watch")

        monkeypatch.setattr("core.profile.decide", decide)
        monkeypatch.setattr(settings, "live_only_channels", "")
        monkeypatch.setattr(settings, "live_only_categories", "irl")
        sup = Supervisor()
        assert sup.wanted(self._rows(("slotsguy", "Slots")), now=1_000.0) == []
        assert sup.skipped["slotsguy"] == "not in irl"

    def test_it_walks_the_directory_until_the_slots_can_be_filled(
            self, monkeypatch):
        """One page of forty holding three IRL streams must not leave seven
        slots empty."""
        monkeypatch.setattr(settings, "live_only_categories", "irl")
        monkeypatch.setattr(settings, "live_slots", 2)
        pages = {
            1: self._rows(("a", "IRL"), ("b", "Slots"), ("c", "Slots")),
            2: self._rows(("d", "IRL"), ("e", "IRL"), ("f", "Slots")),
            3: self._rows(("g", "IRL"), ("h", "IRL")),
        }
        asked = []

        def fetch(limit=40, *, language="en", page=1):
            asked.append(page)
            return pages.get(page, [])

        monkeypatch.setattr("core.roster.fetch_kick_live", fetch)
        got = Supervisor.listing()
        assert asked[0] == 1 and len(asked) > 1, "stopped at one page"
        assert sum(1 for r in got if r.category == "IRL") >= 4

    def test_it_stops_when_the_directory_runs_out(self, monkeypatch):
        """A directory that repeats itself or returns nothing must not loop."""
        monkeypatch.setattr(settings, "live_only_categories", "irl")
        monkeypatch.setattr(settings, "live_slots", 10)
        monkeypatch.setattr("core.roster.fetch_kick_live",
                            lambda limit=40, *, language="en", page=1:
                            self._rows(("same", "Slots")))
        got = Supervisor.listing()
        assert [r.channel for r in got] == ["same"], "paged over a repeat"

    def test_a_page_that_fails_does_not_lose_the_pages_before_it(
            self, monkeypatch):
        monkeypatch.setattr(settings, "live_only_categories", "irl")
        monkeypatch.setattr(settings, "live_slots", 10)

        def fetch(limit=40, *, language="en", page=1):
            if page == 1:
                return self._rows(("a", "IRL"))
            raise RuntimeError("the directory stopped answering")

        monkeypatch.setattr("core.roster.fetch_kick_live", fetch)
        assert [r.channel for r in Supervisor.listing()] == ["a"]

    def test_with_no_category_named_it_still_asks_for_one_page(self, monkeypatch):
        monkeypatch.setattr(settings, "live_only_categories", "")
        asked = []

        def fetch(limit=40, *, language="en", page=1):
            asked.append(page)
            return self._rows(("a", "Slots"))

        monkeypatch.setattr("core.roster.fetch_kick_live", fetch)
        Supervisor.listing()
        assert asked == [1]
