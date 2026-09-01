"""A clip has edges, and they are not a fixed distance from the trigger.

The code this replaces hung `live_lead_s` seconds in front of the moment and
`live_trail_s` behind it - 22 and 8. Every clip therefore opened with
twenty-two seconds of preamble and stopped eight seconds after the trigger
whether anything had resolved or not, which puts the payoff 73% of the way
through a clip nobody is still watching by then.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import clipping


class Heard:
    """Loudness over time, at 100 frames a second, and nothing else - which is
    all the boundary finder reads."""

    def __init__(self, level_db: list[float], window_s: float = 0.01) -> None:
        self.level_db = level_db
        self.window_s = window_s


def build(*parts: tuple[float, float], step: float = 0.01) -> Heard:
    """(seconds, dB) segments, laid end to end."""
    levels: list[float] = []
    for seconds, db in parts:
        levels += [db] * int(seconds / step)
    return Heard(levels, step)


class TestTheSetupIsShort:
    """"Open on the action" is the one thing every guide on clipping agrees
    about. Twenty-two seconds of lead-in is the opposite of that."""

    def test_the_lead_is_seconds_not_half_a_minute(self):
        # Talking, a gap, more talking, then the moment.
        heard = build((10.0, -20.0), (0.5, -40.0), (4.0, -20.0), (10.0, -8.0))
        found = clipping.find(heard, trigger_s=14.5, span_s=24.5)
        assert found.lead_s <= clipping.SETUP_MAX_S
        assert found.lead_s >= clipping.SETUP_MIN_S

    def test_it_opens_on_the_pause_before_the_moment(self):
        """So the clip starts at the beginning of the line that sets it up,
        not halfway through a word."""
        heard = build((10.0, -20.0), (0.5, -40.0), (4.0, -20.0), (10.0, -8.0))
        found = clipping.find(heard, trigger_s=14.5, span_s=24.5)
        assert found.why["opens_on"] == "a pause before it"
        assert 10.0 <= found.start_s <= 10.9, found.start_s

    def test_with_no_pause_to_find_it_still_opens_close(self):
        """Room either side of the moment, and no more. Asked for after
        watching seventeen clips that were correct and felt clipped short."""
        heard = build((14.0, -20.0), (10.0, -8.0))
        found = clipping.find(heard, trigger_s=14.0, span_s=24.0)
        assert found.lead_s == pytest.approx(clipping.ROOM_S, abs=0.5)

    def test_the_room_is_not_the_twenty_two_second_lead_again(self):
        """Five seconds around the whole loud stretch is a different thing
        from twenty-two seconds before the trigger, and has to stay one."""
        heard = build((30.0, -28.0), (6.0, -6.0), (30.0, -28.0))
        found = clipping.find(heard, trigger_s=32.0, span_s=66.0)
        assert found.lead_s <= clipping.ROOM_S + clipping.ROOM_SNAP_S + 1.0
        # ...and the moment still lands in the front half of the clip.
        assert (32.0 - found.start_s) / found.length_s < 0.5

    def test_a_moment_at_the_very_start_does_not_run_off_the_front(self):
        heard = build((30.0, -10.0))
        assert clipping.find(heard, trigger_s=0.5, span_s=30.0).start_s >= 0.0


class TestItEndsWhenTheMomentEnds:
    def test_it_ends_where_the_loud_part_ends(self):
        """A moment is a loud stretch, not a point with a decay after it.

        The decay model was tried first and does not survive a real stream:
        plotted, the envelope around a real moment runs -19, -43, -43, -22,
        +4, +4, +1, -5, -6, -48, -14, -11, -8. The moment is the +4 run and
        there is no decay anywhere in it - conversation swings fifty decibels
        continuously - so "wait for it to settle" fired within a second of
        every trigger and every clip collapsed onto the minimum length."""
        # Quiet, the moment, a long loud reaction, then back to quiet.
        heard = build((8.0, -28.0), (6.0, -6.0), (20.0, -28.0))
        found = clipping.find(heard, trigger_s=8.0, span_s=34.0)
        assert 14.0 <= found.end_s <= 22.0, found.as_dict()
        assert "loud part" in found.why["ends_on"]

    def test_it_finds_the_moment_even_when_the_trigger_lands_after_it(self):
        """The sensors point at a reaction, and a reaction is the back half of
        the thing - so the loud stretch often starts before the trigger."""
        heard = build((8.0, -28.0), (5.0, -6.0), (3.0, -28.0), (10.0, -28.0))
        found = clipping.find(heard, trigger_s=15.0, span_s=26.0)
        assert found.start_s <= 8.5, found.as_dict()
        assert found.why["loud_from"] <= 9.0

    def test_a_long_reaction_makes_a_long_clip(self):
        """The length comes from the moment, not from a constant."""
        short = build((8.0, -28.0), (3.0, -6.0), (20.0, -28.0))
        long = build((8.0, -28.0), (18.0, -6.0), (20.0, -28.0))
        a = clipping.find(short, trigger_s=8.0, span_s=31.0)
        b = clipping.find(long, trigger_s=8.0, span_s=46.0)
        assert b.length_s > a.length_s + 5.0, (a.as_dict(), b.as_dict())

    def test_what_is_said_after_it_is_kept(self):
        """"If they say something after that adds to it, keep that in." So
        after the reaction settles it runs to the end of the next sentence
        rather than stopping on the instant the laugh does."""
        heard = build(
            (8.0, -28.0),   # setup
            (5.0, -6.0),    # the moment and the laugh
            (3.0, -20.0),   # ...and then the line that adds to it
            (10.0, -40.0),  # silence
        )
        found = clipping.find(heard, trigger_s=8.0, span_s=26.0)
        assert found.end_s >= 15.0, "it cut the line after the laugh"

    def test_a_reaction_that_never_ends_is_still_cut_somewhere(self):
        heard = build((5.0, -28.0), (120.0, -6.0))
        found = clipping.find(heard, trigger_s=5.0, span_s=125.0)
        assert found.length_s <= clipping.MAX_CLIP_S


class TestWhatAClipMayBe:
    def test_never_longer_than_the_platforms_reward(self):
        heard = build((200.0, -10.0))
        found = clipping.find(heard, trigger_s=20.0, span_s=200.0)
        assert found.length_s <= clipping.MAX_CLIP_S

    def test_never_a_fragment(self):
        heard = build((30.0, -10.0))
        found = clipping.find(heard, trigger_s=10.0, span_s=30.0)
        assert found.length_s >= clipping.MIN_CLIP_S

    def test_it_never_runs_past_the_end_of_the_video(self):
        heard = build((20.0, -10.0))
        found = clipping.find(heard, trigger_s=18.0, span_s=20.0)
        assert found.end_s <= 20.0

    def test_a_deaf_reading_still_produces_a_sane_clip(self):
        """No audio is a real case - it should still not go back to a
        twenty-two second lead."""
        found = clipping.find(Heard([], 0.0), trigger_s=60.0, span_s=200.0)
        assert found.lead_s <= clipping.SETUP_MAX_S
        assert clipping.MIN_CLIP_S <= found.length_s <= clipping.MAX_CLIP_S


class TestPauses:
    def test_a_gap_between_sentences_is_found(self):
        heard = build((3.0, -20.0), (0.4, -40.0), (3.0, -20.0))
        found = clipping.pauses(heard.level_db, heard.window_s, over=(0.0, 6.4))
        assert found and 2.9 <= found[0] <= 3.1, found

    def test_a_gap_between_words_is_not(self):
        """Otherwise every clip opens on a comma."""
        heard = build((3.0, -20.0), (0.05, -40.0), (3.0, -20.0))
        assert not clipping.pauses(heard.level_db, heard.window_s, over=(0.0, 6.0))

    def test_quiet_is_relative_to_the_talking(self):
        """A shouting streamer's quiet is louder than a calm one's loud, and
        an absolute threshold works for exactly one of them."""
        loud = build((3.0, -6.0), (0.4, -20.0), (3.0, -6.0))
        soft = build((3.0, -40.0), (0.4, -54.0), (3.0, -40.0))
        assert clipping.pauses(loud.level_db, loud.window_s, over=(0.0, 6.4))
        assert clipping.pauses(soft.level_db, soft.window_s, over=(0.0, 6.4))
