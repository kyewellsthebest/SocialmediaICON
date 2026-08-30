"""The signals, and whether fusing them actually finds a planted moment.

Every previous attempt at moment-finding in this project was judged by looking
at the output and going "hmm". These tests plant a moment at a known second and
insist it comes back, which is the only version of this that can fail honestly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core import chat, moments


def _messages(spec: list[tuple[float, str, str]]) -> list[chat.Message]:
    return [chat.Message(at_s=t, text=text, user=user) for t, text, user in spec]


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
        best = moments.rank(signals, duration_s=duration, clip_s=20.0, top=1)[0]

        assert best.why, "a moment with no explanation is not usable"
        assert abs(sum(best.why.values()) - best.score) < 1e-6
        assert best.top_reason() == "chat_request"

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
