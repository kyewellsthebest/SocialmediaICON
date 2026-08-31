"""Does it find the interesting second in a nightclub as well as in a bedroom?

That question is the whole design. A real IRL stream from a club measured 0.118
average motion and a man sitting at a desk measured 0.009 - thirteen times
apart - so any absolute motion threshold either fires on every frame of the
club or never fires on the desk. Everything here is a ratio against the
stream's own recent past, and these tests are mostly the same event staged
twice to prove it.
"""

from __future__ import annotations

import pytest
import synth_video as clips

from core import watching


@pytest.fixture(scope="module")
def club():
    return watching.watch(clips.nightclub())


@pytest.fixture(scope="module")
def room():
    return watching.watch(clips.still_room())


class TestTheBaselineIsPerStream:
    def test_a_nightclub_moves_far_more_than_a_still_room(self, club, room):
        """If this ever stops being true the rest of the file proves nothing."""
        assert club.average_motion > room.average_motion * 5

    def test_neither_produces_a_surge_on_its_own(self, club, room):
        """Constant motion is not an event, however much of it there is."""
        assert club.surges == []
        assert room.surges == []

    def test_the_same_event_is_found_in_a_still_room(self):
        found = watching.watch(clips.calm_then_chaos(at=20.0))
        assert any(19.0 <= t <= 22.0 for t, _ in found.surges)

    def test_and_in_a_nightclub_that_was_already_moving(self):
        """The one that matters. An absolute threshold cannot do this."""
        found = watching.watch(clips.club_then_surge(at=20.0))
        assert found.average_motion > 0.05, "this stream is busy to begin with"
        assert any(19.0 <= t <= 22.0 for t, _ in found.surges)

    def test_it_says_how_far_above_normal_the_surge_was(self):
        found = watching.watch(clips.calm_then_chaos(at=20.0))
        at, size = found.surges[0]
        assert size > 2.2


class TestCutsAndFlashes:
    def test_a_hard_cut_is_found_at_the_cut(self):
        found = watching.watch(clips.hard_cut(at=15.0))
        assert any(14.5 <= t <= 15.5 for t in found.cuts)

    def test_a_cut_is_not_reported_as_sustained_motion(self):
        """One changed frame is a cut. A surge is a stretch of them."""
        assert watching.watch(clips.hard_cut(at=15.0)).surges == []

    def test_the_lights_coming_up_is_a_flash(self):
        found = watching.watch(clips.lights_up(at=12.0))
        assert any(11.5 <= t <= 12.5 for t in found.flashes)

    def test_a_steady_scene_has_no_flashes(self, room):
        assert room.flashes == []


class TestStillness:
    def test_a_locked_off_shot_reads_as_still(self, room):
        assert sum(b - a for a, b in room.stillness) > 20.0

    def test_a_moving_one_does_not(self, club):
        assert club.stillness == []


class TestTheCostOfLooking:
    def test_half_a_minute_reads_in_under_two_seconds(self):
        """Three streams, on a timer, forever."""
        import time

        start = time.time()
        watching.watch(clips.nightclub())
        assert time.time() - start < 2.0

    def test_nothing_to_watch_says_so(self, tmp_path):
        empty = tmp_path / "empty.mp4"
        empty.write_bytes(b"")
        with pytest.raises(watching.WatchingError):
            watching.watch(empty)

    def test_the_first_frame_has_nothing_to_differ_from(self, room):
        assert room.motion[0] == 0.0

    def test_where_in_the_frame_is_kept(self, club):
        assert len(club.columns[0]) == watching.WIDTH

    def test_the_summary_is_json_shaped(self, room):
        import json

        json.dumps(room.as_dict())
        assert set(room.as_dict()) >= {"average_motion", "surges", "cuts", "flashes"}


class TestACutIsAFrameNotARate:
    """The gap this file had. Every other test asked "is the event found";
    none asked "is a busy stream free of events it never had", and when motion
    moved from per-frame to per-second units the cut threshold came along
    unchanged. A nightclub reported fifty cuts in thirty seconds and a real
    stream fifty-four in thirty-two, which put a scene_cuts score on a channel
    that never cut once."""

    def test_a_busy_stream_with_no_cuts_reports_no_cuts(self, club):
        assert club.cuts == [], f"{len(club.cuts)} cuts invented from constant motion"

    def test_a_still_room_reports_no_cuts(self, room):
        assert room.cuts == []

    def test_the_one_real_cut_is_still_found(self):
        found = watching.watch(clips.hard_cut(at=15.0))
        assert len(found.cuts) == 1
        assert found.cuts[0] == pytest.approx(15.0, abs=0.5)

    def test_the_test_is_per_frame_so_the_rate_cannot_move_it(self):
        """Read the same clip at two rates. A cut is one frame differing from
        the one before it, so the answer must not depend on how often the
        frames are sampled - which is exactly what went wrong."""
        src = clips.hard_cut(at=15.0)
        slow = watching.watch(src, fps=20.0).cuts
        fast = watching.watch(src, fps=None).cuts
        assert len(slow) == len(fast) == 1
        assert slow[0] == pytest.approx(fast[0], abs=0.5)


class TestItReadsEveryFrame:
    """The default is the source's own rate, and the reading has to mean the
    same thing whatever that rate turns out to be."""

    def test_the_default_is_the_rate_the_source_actually_runs_at(self):
        src = clips.nightclub()
        assert watching.watch(src).fps == watching.source_fps(src) > 0

    def test_an_absurd_or_missing_rate_falls_back(self, monkeypatch):
        src = clips.nightclub()
        monkeypatch.setattr(watching, "probe", lambda _: _Declares(0.0))
        assert watching.source_fps(src) == watching.FPS
        monkeypatch.setattr(watching, "probe", lambda _: _Declares(1000.0))
        assert watching.source_fps(src) == watching.MAX_FPS

    def test_a_file_that_will_not_probe_falls_back_rather_than_raising(self, tmp_path):
        assert watching.source_fps(tmp_path / "nothing.mp4") == watching.FPS

    def test_reading_more_frames_does_not_make_a_stream_look_busier(self):
        """Motion is per second, not per frame. Consecutive frames of a fast
        source differ by less, so a per-frame number would say a 60fps stream
        is calmer than the same room at 20 - and the floors that decide
        'still' and 'too quiet to be a surge' would move with it."""
        for clip in (clips.nightclub(), clips.still_room()):
            slow = watching.watch(clip, fps=20.0).average_motion
            fast = watching.watch(clip, fps=None).average_motion
            assert abs(fast - slow) / max(slow, 1e-6) < 0.25, clip

    def test_a_still_room_stays_under_the_stillness_floor_at_any_rate(self):
        room = clips.still_room()
        for rate in (20.0, None):
            found = watching.watch(room, fps=rate)
            assert found.average_motion < 0.08
            assert sum(b - a for a, b in found.stillness) > 20.0


class _Declares:
    def __init__(self, fps: float) -> None:
        self.fps = fps
