"""The signals, and whether fusing them actually finds a planted moment.

Every previous attempt at moment-finding in this project was judged by looking
at the output and going "hmm". These tests plant a moment at a known second and
insist it comes back, which is the only version of this that can fail honestly.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from core import chat, moments


def _messages(spec: list[tuple[float, str, str]]) -> list[chat.Message]:
    return [chat.Message(at_s=t, text=text, user=user) for t, text, user in spec]


class _Heard:
    """A hearing.Hearing with only the fields signals_from_hearing reads."""

    def __init__(self, laughs=(), shouts=(), drops=()):
        self.laughs, self.shouts, self.drops = list(laughs), list(shouts), list(drops)


class _Seen:
    """Likewise for watching.Watching."""

    def __init__(self, surges=(), cuts=(), flashes=(), stillness=()):
        self.surges, self.cuts = list(surges), list(cuts)
        self.flashes, self.stillness = list(flashes), list(stillness)


def _laughing_at(at: float, duration_s: float, *, length: float = 3.0) -> dict:
    """Something the bot actually heard, so chat has something to corroborate."""
    return moments.signals_from_hearing(
        _Heard(laughs=[(at, at + length, 0.9)]), duration_s=duration_s
    )


class TestChatCurve:
    def test_a_quiet_stream_with_one_reaction_finds_the_reaction(self):
        # Two messages a second for five minutes, then forty in one second.
        spec = [(float(t), "hello", f"u{t % 7}") for t in range(0, 300)]
        spec += [(200.0, "KEKW", f"burst{i}") for i in range(40)]
        curve = chat.build_curve(_messages(spec), duration_s=300)

        bursts = curve.bursts()
        assert bursts, "a 40x spike over baseline should register"
        assert any(abs(t - 200.0) <= 2.0 for t, _ in bursts)

    def test_one_person_spamming_is_not_a_crowd(self):
        spec = [(100.0, "LUL", "spammer") for _ in range(50)]
        curve = chat.build_curve(_messages(spec), duration_s=200)
        # It still registers as volume - that is correct, it is loud - but the
        # distinct-voice count is what separates it from a real reaction.
        assert curve.voices[100] == 1
        assert curve.counts[100] == 50

    def test_clip_requests_are_recognised_and_ordinary_talk_is_not(self):
        spec = [
            (10.0, "clip that", "a"),
            (11.0, "SOMEONE CLIP IT", "b"),
            (12.0, "i watched a clip earlier", "c"),
            (13.0, "clip please", "d"),
        ]
        curve = chat.build_curve(_messages(spec), duration_s=60)
        found = {t for t, _ in curve.clip_requests()}
        assert found == {10.0, 11.0, 13.0}, "casual use of 'clip' must not count"

    def test_reactions_are_counted_per_message(self):
        assert chat.Message(0, "KEKW KEKW OMEGALUL").reactions == 3
        assert chat.Message(0, "what time is the stream").reactions == 0

    def test_messages_outside_the_video_are_dropped_not_crashed(self):
        curve = chat.build_curve(_messages([(-50.0, "early", "a"), (9999.0, "late", "b")]), 60)
        assert sum(curve.counts) == 0

    def test_quotes_come_back_from_around_the_moment(self):
        msgs = _messages([(float(t), f"m{t}", "u") for t in range(0, 100)])
        got = chat.quotes_around(msgs, at_s=50.0, window_s=3.0)
        assert "m50" in got and "m90" not in got


class TestKickReplayParsing:
    """The replay endpoint is undocumented, so the parser has to be forgiving."""

    def test_messages_are_found_whatever_the_envelope(self):
        one = {"messages": [{"id": "1", "content": "hi", "created_at": "2026-01-01T00:00:00Z"}]}
        two = {"data": {"messages": [{"id": "1", "content": "hi"}]}}
        three = [{"id": "1", "content": "hi"}]
        for payload in (one, two, three):
            assert len(chat._messages_in(payload)) == 1

    def test_a_shape_we_have_never_seen_returns_nothing_rather_than_raising(self):
        assert chat._messages_in({"unexpected": 42}) == []
        assert chat._messages_in(None) == []

    def test_timestamps_become_offsets_from_the_stream_start(self):
        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        message = chat._to_message(
            {"content": "hi", "created_at": "2026-01-01T12:05:30Z", "sender": {"username": "x"}},
            start,
        )
        assert message is not None
        assert message.at_s == 330.0
        assert message.user == "x"

    def test_a_message_with_no_timestamp_is_skipped(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        assert chat._to_message({"content": "hi"}, start) is None


class TestFusion:
    def test_a_planted_moment_is_ranked_first(self):
        duration = 600.0
        # Chat reacts at 300s and asks for a clip two seconds later.
        spec = [(float(t), "chat", f"u{t % 9}") for t in range(0, 600, 2)]
        spec += [(300.0, "KEKW", f"r{i}") for i in range(60)]
        spec += [(302.0, "CLIP THAT", f"r{i}") for i in range(4)]
        curve = chat.build_curve(_messages(spec), duration_s=duration)

        signals = moments.signals_from_chat(curve, duration_s=duration)
        # ...and the bot heard the room laugh, which is what chat is agreeing
        # about. Chat on its own is no longer allowed to nominate anything.
        signals |= _laughing_at(300.0, duration)
        found = moments.rank(signals, duration_s=duration, clip_s=30.0, top=3)

        assert found, "the fused signals should produce at least one moment"
        best = found[0]
        assert best.start_s <= 300.0 <= best.end_s, (
            f"the planted moment at 300s should be inside the top window "
            f"({best.start_s}-{best.end_s})"
        )

    def test_the_score_is_explained_by_its_parts(self):
        duration = 120.0
        curve = chat.build_curve(
            _messages([(60.0, "clip that", f"u{i}") for i in range(5)]), duration
        )
        signals = moments.signals_from_chat(curve, duration_s=duration)
        signals |= _laughing_at(59.0, duration)
        best = moments.rank(signals, duration_s=duration, clip_s=20.0, top=1)[0]

        assert best.why, "a moment with no explanation is not usable"
        assert abs(sum(best.why.values()) - best.score) < 1e-6
        assert "chat_request" in best.why
        assert best.top_reason() == "laughter", (
            "what the bot heard outranks what chat said about it"
        )

    def test_windows_do_not_overlap(self):
        duration = 600.0
        spec = [(float(t), "KEKW", f"u{t % 20}") for t in range(0, 600)]
        spec += [(t, "KEKW", f"x{i}") for t in (100.0, 300.0, 500.0) for i in range(50)]
        curve = chat.build_curve(_messages(spec), duration_s=duration)
        signals = moments.signals_from_chat(curve, duration_s=duration)
        found = moments.rank(signals, duration_s=duration, clip_s=30.0, top=5)

        for a, b in zip(found, found[1:], strict=False):
            assert not (a.start_s < b.end_s and b.start_s < a.end_s)

    def test_clip_requests_look_backwards_not_forwards(self):
        """Chat types the request after the thing it wants clipped."""
        duration = 200.0
        curve = chat.build_curve(
            _messages([(100.0, "clip it", f"u{i}") for i in range(3)]), duration
        )
        signals = moments.signals_from_chat(curve, duration_s=duration, grid_s=1.0)
        weight = signals["chat_request"]
        assert weight[95] > 0, "the seconds before the request must be covered"
        assert weight[110] == 0, "well after the request is not evidence"

    def test_normalisation_is_per_recording(self):
        """A quiet stream's peak must score like a loud stream's peak."""
        quiet = moments._normalise([1.0, 2.0, 10.0])
        loud = moments._normalise([100.0, 200.0, 1000.0])
        assert quiet == loud == [0.0, pytest.approx(1 / 9), 1.0]

    def test_a_flat_signal_produces_no_moments(self):
        duration = 300.0
        curve = chat.build_curve(
            _messages([(float(t), "hi", f"u{t % 5}") for t in range(300)]), duration
        )
        signals = moments.signals_from_chat(curve, duration_s=duration)
        found = moments.rank(signals, duration_s=duration, clip_s=30.0, top=5)
        # Uniform chat has no bursts and no requests; voices normalise flat to
        # zero. Anything that comes back must at least not claim a burst.
        assert all("chat_burst" not in m.why for m in found)

    def test_dead_air_cancels_a_scene_cut(self):
        class FakeScan:
            duration_s = 100.0
            scene_cuts = [50.0]
            blacks = [(49.0, 52.0)]
            freezes: list = []

        signals = moments.signals_from_video(FakeScan(), duration_s=100.0)
        assert signals["scene_cuts"][50] == 0.0, "a cut inside black is a dropout, not a moment"

    def test_video_shorter_than_the_clip_length_returns_nothing(self):
        curve = chat.build_curve(_messages([(1.0, "clip that", "a")]), duration_s=5.0)
        signals = moments.signals_from_chat(curve, duration_s=5.0)
        assert moments.rank(signals, duration_s=5.0, clip_s=30.0) == []


class TestQuotesDoNotAssumeOrder:
    """Quotes were silently pulled from the wrong minute when input was unsorted."""

    def test_unsorted_messages_still_quote_the_right_window(self):
        msgs = _messages([(float(t), f"m{t}", "u") for t in range(100)])
        msgs = msgs[60:] + msgs[:60]  # the order a naive collector produces
        got = chat.quotes_around(msgs, at_s=70.0, window_s=3.0)
        assert set(got) == {"m67", "m68", "m69", "m70", "m71", "m72"}

    def test_the_moment_carries_what_chat_actually_said(self):
        duration = 200.0
        spec = [(float(t), "hi", f"u{t % 9}") for t in range(200)]
        spec += [(100.0, "KEKW", f"r{i}") for i in range(50)]
        spec += [(103.0, "clip that", f"r{i}") for i in range(4)]
        msgs = _messages(spec)
        curve = chat.build_curve(msgs, duration_s=duration)
        signals = moments.signals_from_chat(curve, msgs, duration_s=duration)
        signals |= _laughing_at(100.0, duration)
        best = moments.rank(
            signals, duration_s=duration, clip_s=30.0, top=1, messages=msgs
        )[0]
        assert "KEKW" in best.quotes, f"expected the reaction in {best.quotes[:5]}"


class TestWhereTheMomentEnds:
    """A fixed thirty seconds cut the good ones off and padded the thin ones.

    What actually ends a moment is chat going back to normal, so that is what
    these plant: a baseline, a burst of a known length, and a check that the
    answer lands where the burst stopped.
    """

    def _curve(self, *, idle: float, burst_from: float, burst_to: float,
               burst: float, length: float = 300.0) -> chat.Curve:
        spec = []
        t = 0.0
        while t < length:
            rate = burst if burst_from <= t < burst_to else idle
            for i in range(int(rate)):
                spec.append((t, "KEKW", f"u{i}"))
            t += 1.0
        return chat.build_curve(_messages(spec), duration_s=length, bucket_s=1.0)

    def test_a_burst_that_stops_ends_the_clip_near_where_it_stopped(self):
        curve = self._curve(idle=2, burst_from=120.0, burst_to=145.0, burst=30)
        found = moments.moment_end(curve, 120.0, min_s=8.0)
        assert found == pytest.approx(28.0, abs=4.0)

    def test_a_moment_that_never_calms_down_is_capped(self):
        curve = self._curve(idle=2, burst_from=120.0, burst_to=300.0, burst=30)
        assert moments.moment_end(curve, 120.0, min_s=8.0, max_s=59.0) == 59.0

    def test_a_short_reaction_still_gets_a_watchable_minimum(self):
        curve = self._curve(idle=2, burst_from=120.0, burst_to=124.0, burst=30)
        assert moments.moment_end(curve, 120.0, min_s=20.0) == 20.0

    def test_it_never_exceeds_the_cap(self):
        curve = self._curve(idle=2, burst_from=120.0, burst_to=300.0, burst=40)
        assert moments.moment_end(curve, 120.0, min_s=8.0, max_s=37.0) == 37.0

    def test_a_busy_channel_is_judged_against_its_own_idle_rate(self):
        """300 a minute is dead air on one stream and a peak on another."""
        busy = self._curve(idle=25, burst_from=120.0, burst_to=140.0, burst=60)
        quiet = self._curve(idle=1, burst_from=120.0, burst_to=140.0, burst=8)
        assert moments.moment_end(busy, 120.0, min_s=8.0) == pytest.approx(
            moments.moment_end(quiet, 120.0, min_s=8.0), abs=5.0
        )

    def test_an_empty_curve_falls_back_to_the_minimum(self):
        empty = chat.Curve(bucket_s=1.0, duration_s=0.0)
        assert moments.moment_end(empty, 120.0, min_s=14.0) == 14.0

    def test_a_peak_past_the_end_of_the_curve_does_not_crash(self):
        curve = self._curve(idle=2, burst_from=120.0, burst_to=140.0, burst=30)
        assert moments.moment_end(curve, 9999.0, min_s=11.0) == 11.0


class TestBackgroundIsNotAMoment:
    """The two worthless clips this file exists for.

    Both scored on chat_voices alone. Both were cut from five minutes in which
    nothing happened: the second one was a streamer typing bet amounts into a
    gambling site with music over it, and chat had not reacted to anything.

    The mechanism was that chat_voices came through _normalise(), which scales
    a curve against its own min and max. There is always a maximum, so the
    busiest second in any window scored 1.0 whether that second was a crowd
    gasping or the same steady spam as the four minutes either side of it. A
    level normalised against its own range cannot ever say "nothing here".
    """

    def _flat(self, per_second: int = 8, seconds: int = 300) -> list[chat.Message]:
        """A busy channel where nothing happens. Steady rate, no reaction."""
        spam = ["deenthegreatWdeenthegreatW", "deenthegreatDigg", "CaptFail", "W"]
        return _messages([
            (float(t), spam[i % len(spam)], f"u{(t * per_second + i) % 400}")
            for t in range(seconds)
            for i in range(per_second)
        ])

    def _reaction(self, at: float = 200.0, size: int = 60) -> list[chat.Message]:
        return _messages([(at + (i % 25) * 0.12, "KEKW", f"r{i}") for i in range(size)])

    def _rank(self, msgs, *, heard=None, seen=None, **kwargs):
        curve = chat.build_curve(msgs, duration_s=300.0, bucket_s=1.0)
        signals = moments.signals_from_chat(curve, messages=msgs, duration_s=300.0)
        if heard is not None:
            signals |= moments.signals_from_hearing(heard, duration_s=300.0)
        if seen is not None:
            signals |= moments.signals_from_watching(seen, duration_s=300.0)
        return moments.rank(signals, duration_s=300.0, clip_s=30.0, top=1,
                            messages=msgs, **kwargs)

    def test_a_busy_channel_with_no_reaction_produces_nothing(self):
        assert self._rank(self._flat()) == []

    def test_a_quiet_channel_with_no_reaction_produces_nothing(self):
        assert self._rank(self._flat(per_second=1)) == []

    def test_the_same_channel_with_a_real_reaction_still_produces_one(self):
        """The bar has to reject nothing without also rejecting everything."""
        found = self._rank(self._flat(per_second=1) + self._reaction(),
                           heard=_Heard(laughs=[(200.0, 203.0, 0.9)]))
        assert found, "a burst on top of the same background must still be caught"
        assert found[0].peak_s == pytest.approx(200.0, abs=12.0)

    def test_chat_reacting_to_something_unheard_and_unseen_produces_nothing(self):
        """The rule, stated on its own. Half a Kick chat is the channel's emote."""
        assert self._rank(self._flat(per_second=1) + self._reaction()) == []

    def _jittery(self, seconds: int = 300) -> list[chat.Message]:
        """A real chat wobbles. Nothing happens; the rate is not a straight line."""
        rng = random.Random(4)
        spam = ["deenthegreatWdeenthegreatW", "deenthegreatDigg", "CaptFail", "W"]
        return _messages([
            (float(t), rng.choice(spam), f"u{rng.randint(0, 400)}")
            for t in range(seconds)
            for _ in range(rng.randint(6, 10))
        ])

    def test_an_ordinary_wobble_in_the_rate_is_not_a_moment(self):
        assert self._rank(self._jittery()) == []

    def test_levels_alone_never_clear_the_bar(self):
        """With the bar off, the wobble does score - which is why the bar exists."""
        found = self._rank(self._jittery(), min_event_score=0.0)
        assert found, "the level does have an opinion; the bar is what ignores it"
        assert found[0].event_score == 0.0
        assert not (set(found[0].why) & moments.SENSED)

    def test_a_level_scores_zero_on_a_flat_curve(self):
        assert moments._excess([7.0] * 200) == [0.0] * 200

    def test_a_level_scores_on_a_rise_above_its_own_normal(self):
        values = [4.0] * 120 + [20.0] * 10
        found = moments._excess(values)
        assert max(found[:120]) == 0.0
        assert max(found[120:]) == 1.0

    def test_a_level_has_no_opinion_before_it_has_history(self):
        """A moment found four seconds in has neither a baseline nor a lead-in."""
        found = moments._excess([1.0] * 10 + [99.0] * 10, warmup_s=20.0)
        assert found[:20] == [0.0] * 20

    def test_steady_loudness_is_not_a_moment(self):
        """Music playing over a stream is loud for twenty minutes."""
        assert moments._louder_than_usual([-21.0] * 200) == [0.0] * 200

    def test_a_shout_over_steady_loudness_is(self):
        found = moments._louder_than_usual([-21.0] * 120 + [-9.0] * 5)
        assert max(found[:120]) == 0.0
        assert max(found[120:]) == 1.0

    def test_the_peak_follows_the_event_not_the_level(self):
        """The quotes, the clip centre and its length all hang off the peak."""
        msgs = self._flat(per_second=1) + self._reaction(at=150.0)
        # ...plus a crowd arriving well after, which moves the level only.
        msgs += _messages([(170.0 + i * 0.05, "hello", f"crowd{i}") for i in range(120)])
        found = self._rank(msgs, heard=_Heard(laughs=[(150.0, 153.0, 0.9)]))
        assert found[0].peak_s == pytest.approx(150.0, abs=12.0)


class TestMoodKnowsWallpaperFromReaction:
    def test_a_channel_that_always_spams_W_does_not_read_as_hype(self):
        """Its own emote is the letter W. Every second of every stream was 100% hype."""
        msgs = _messages([(float(t), "W", f"u{t % 40}") for t in range(600)])
        found = chat.mood_around(msgs, 300.0)
        assert found["dominant"] == "hype"
        assert found["background"] is True, "chat feels exactly like this all day"
        assert found["lift"] == pytest.approx(1.0, abs=0.25)

    def test_a_real_swing_is_not_marked_as_background(self):
        msgs = _messages([(float(t), "hello", f"u{t % 40}") for t in range(600)])
        msgs += _messages([(300.0 + i * 0.1, "OH MY GOD", f"r{i}") for i in range(60)])
        found = chat.mood_around(msgs, 300.0)
        assert found["dominant"] == "shock"
        assert found["background"] is False
        assert found["lift"] > 3.0

    def test_no_emotive_lines_still_returns_the_full_shape(self):
        found = chat.mood_around(_messages([(1.0, "hello", "u")]), 1.0)
        assert found["dominant"] is None
        for key in ("confidence", "emotive_lines", "lift", "background", "counts"):
            assert key in found


class TestOneSignalIsNotAgreement:
    """A phone carried down a street surges against its own baseline all
    evening. Motion 40, flash 3, chat 1 scored 44 and cleared every bar, and
    not one of those readings was a moment."""

    def test_motion_and_flash_are_one_family_not_two(self):
        """One eye, one picture, reported twice."""
        why = {"motion_surge": 40.2, "flash": 3.0}
        assert list(moments.families(why)) == ["motion"]
        assert moments.agreeing(why) == ["motion"]

    def test_a_rounding_error_is_not_a_second_opinion(self):
        """Chat at 3% of a motion reading is noise."""
        assert moments.agreeing({"motion_surge": 40.2, "chat_voices": 1.1}) == ["motion"]

    def test_two_real_families_are_agreement(self):
        found = moments.agreeing({"motion_surge": 40.0, "shout": 12.0})
        assert set(found) == {"motion", "voice"}

    def test_the_families_cover_every_scored_signal(self):
        """A signal in no family is invisible to the agreement test, which
        would let it through as neither corroborating nor corroborated."""
        covered = set().union(*moments.FAMILIES.values())
        assert moments.EVENTS - covered == set()

    def test_no_signal_is_in_two_families_at_once(self):
        """It would count as its own corroboration."""
        seen = [k for keys in moments.FAMILIES.values() for k in keys]
        assert len(seen) == len(set(seen))

    def test_nothing_scoring_agrees_about_nothing(self):
        assert moments.agreeing({}) == []
        assert moments.families({}) == {}
