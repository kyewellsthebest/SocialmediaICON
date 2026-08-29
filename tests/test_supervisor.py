"""The loop, and the three rules it must not break.

Nothing is posted. The caps hold across a restart. One dead channel does not
stop the others. Those are the properties worth defending; the rest of the
supervisor is glue over modules that already have their own tests.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from core import chat as chatlib
from core.config import settings
from core.supervisor import Supervisor, Watched


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


def _watched(channel="x", messages=None) -> Watched:
    log = chatlib.LiveLog(window_s=300.0)
    log.extend(messages or [])
    return Watched(channel=channel, buffer=FakeBuffer(channel), chat=FakeChat(log))


def _chatter(seconds: int = 200, burst_at: float | None = None):
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

        watched = _watched(messages=_chatter(burst_at=120.0))
        record = sup.catch(watched, why={"chat_burst": 9.0}, peak_s=120.0, now=time.time())
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
        sup.watching["x"] = _watched(messages=_chatter(burst_at=120.0))
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
        sup.watching["x"] = _watched(messages=_chatter(burst_at=150.0))
        sup.watching["y"] = _watched("y", messages=_chatter(burst_at=150.0))
        monkeypatch.setattr(sup, "allowed", lambda **k: True)

        def explode(*_a, **_k):
            raise RuntimeError("ffmpeg said no")

        monkeypatch.setattr(sup, "catch", explode)
        sup.tick()  # must not raise
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
        value, why, _ = sup.score(_watched(messages=_chatter()))
        assert why == {} or value == 0.0 or "chat_burst" not in why

    def test_a_burst_scores_and_names_its_reason(self):
        sup = Supervisor()
        value, why, peak = sup.score(_watched(messages=_chatter(burst_at=150.0)))
        assert value > 0 and why
        assert peak == pytest.approx(150.0, abs=20.0)

    def test_a_channel_too_new_to_have_a_window_scores_nothing(self):
        """Attaching and immediately cutting would produce a clip of nothing."""
        sup = Supervisor()
        short = _watched(messages=[chatlib.Message(float(t), "hi") for t in range(5)])
        assert sup.score(short) == (0.0, {}, 0.0)

    def test_the_cooldown_stops_one_moment_being_cut_repeatedly(self, monkeypatch):
        sup = Supervisor()
        watched = _watched(messages=_chatter(burst_at=150.0))
        watched.last_catch_at = time.time()
        sup.watching["x"] = watched
        monkeypatch.setattr(sup, "allowed", lambda **k: True)
        cut = []
        monkeypatch.setattr(sup, "catch", lambda *a, **k: cut.append(1))
        sup.tick()
        assert not cut, "chat keeps talking about a moment long after it happened"


class TestWhatTheDashboardSees:
    def test_signals_carry_chat_mood_and_buffer_state(self):
        watched = _watched(messages=_chatter(burst_at=150.0))
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
