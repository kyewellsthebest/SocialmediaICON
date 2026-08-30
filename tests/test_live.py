"""The rolling buffer, and the promise that makes it affordable.

The claim this file has to defend is not "it records" - anything records. It
is that disk use is bounded by the window rather than by how long the stream
runs, and that a clip can be cut out of the past after the moment has gone.
"""

from __future__ import annotations

import pytest

from core import chat
from core.live import LiveError, RollingBuffer


def _buffer(tmp_path, **kwargs) -> RollingBuffer:
    return RollingBuffer(
        url="udp://127.0.0.1:9999", work_dir=tmp_path, channel="test", **kwargs
    )


def _fake_playlist(buf: RollingBuffer, count: int, duration: float = 2.0) -> None:
    """Write a playlist and the segment files it names."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{duration:.0f}"]
    for i in range(count):
        name = f"seg_{i:06d}.ts"
        (buf.work_dir / name).write_bytes(b"\0" * 1000)
        lines += [f"#EXTINF:{duration:.3f},", name]
    buf.playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestStorageIsBounded:
    """The whole economic argument lives in these two settings."""

    def test_ffmpeg_is_told_to_delete_old_segments(self, tmp_path):
        command = " ".join(_buffer(tmp_path).command())
        assert "delete_segments" in command, (
            "without delete_segments this is a recorder that fills the disk"
        )

    def test_the_segment_list_is_capped_to_the_window(self, tmp_path):
        buf = _buffer(tmp_path, window_s=300.0, segment_s=4.0)
        assert buf.segment_count == 75
        assert "-hls_list_size" in buf.command()
        assert buf.command()[buf.command().index("-hls_list_size") + 1] == "75"

    def test_a_tiny_window_still_keeps_enough_to_cut_from(self, tmp_path):
        # Guards against a config that would leave nothing to extract.
        assert _buffer(tmp_path, window_s=1.0, segment_s=4.0).segment_count >= 4

    def test_the_stream_is_copied_not_re_encoded(self, tmp_path):
        """Ten buffers at once is only possible because none of them transcode."""
        command = _buffer(tmp_path).command()
        assert command[command.index("-c") + 1] == "copy"


class TestWhatTheBufferHolds:
    def test_held_time_and_size_come_from_the_playlist(self, tmp_path):
        buf = _buffer(tmp_path, segment_s=2.0)
        _fake_playlist(buf, count=10)
        assert buf.held_s() == pytest.approx(20.0)
        assert len(buf.segments()) == 10
        assert buf.bytes_on_disk() == 10_000

    def test_segments_carry_their_offset_from_the_start_of_the_buffer(self, tmp_path):
        buf = _buffer(tmp_path, segment_s=2.0)
        _fake_playlist(buf, count=5)
        assert [s.offset_s for s in buf.segments()] == [0.0, 2.0, 4.0, 6.0, 8.0]

    def test_a_segment_deleted_mid_roll_is_skipped_not_fatal(self, tmp_path):
        """delete_segments races with reading; a missing file is normal."""
        buf = _buffer(tmp_path, segment_s=2.0)
        _fake_playlist(buf, count=5)
        (buf.work_dir / "seg_000000.ts").unlink()
        segments = buf.segments()
        assert len(segments) == 4
        assert segments[0].offset_s == 0.0, "offsets must close the gap, not leave a hole"

    def test_no_playlist_yet_is_empty_rather_than_an_error(self, tmp_path):
        assert _buffer(tmp_path).segments() == []
        assert _buffer(tmp_path).held_s() == 0.0

    def test_status_is_readable_without_a_running_process(self, tmp_path):
        buf = _buffer(tmp_path, segment_s=2.0)
        _fake_playlist(buf, count=3)
        status = buf.status()
        assert status["running"] is False
        assert status["held_s"] == pytest.approx(6.0)
        assert status["channel"] == "test"


class TestExtracting:
    def test_asking_for_more_past_than_is_held_says_so(self, tmp_path):
        buf = _buffer(tmp_path, segment_s=2.0)
        _fake_playlist(buf, count=5)  # 10 seconds
        with pytest.raises(LiveError, match="only holds"):
            buf.extract(tmp_path / "x.mp4", ago_s=600.0)

    def test_an_empty_buffer_explains_itself(self, tmp_path):
        with pytest.raises(LiveError, match="holds nothing"):
            _buffer(tmp_path).extract(tmp_path / "x.mp4", ago_s=5.0)

    def test_the_default_lead_covers_the_reaction_delay(self):
        from core.live import LEAD_S

        # Chat types a second or two after the event, and the buffer's live
        # edge trails reality by about one segment. A lead shorter than that
        # opens the clip on the punchline.
        assert LEAD_S >= 15.0


class TestDiscard:
    def test_discard_leaves_nothing_behind(self, tmp_path):
        work = tmp_path / "buf"
        buf = RollingBuffer(url="udp://127.0.0.1:9999", work_dir=work)
        work.mkdir()
        _fake_playlist(buf, count=4)
        assert work.exists()
        buf.discard()
        assert not work.exists(), "the stream must leave no trace once clipped"

    def test_discard_is_safe_to_call_twice(self, tmp_path):
        buf = _buffer(tmp_path)
        buf.discard()
        buf.discard()


class TestMood:
    """Chat says how a moment felt. Nothing else in the pipeline can."""

    def test_laughing_and_shock_are_told_apart(self):
        funny = [chat.Message(10.0, t) for t in ("KEKW", "LMAO", "hahaha", "ICANT")]
        shock = [chat.Message(10.0, t) for t in ("OMG", "NO WAY", "monkaS", "WHAT??")]
        assert chat.mood_around(funny, 10.0)["dominant"] == "funny"
        assert chat.mood_around(shock, 10.0)["dominant"] == "shock"

    def test_confidence_separates_a_consensus_from_a_split(self):
        agreed = [chat.Message(5.0, t) for t in ("KEKW", "KEKW", "LMAO", "lol")]
        split = [chat.Message(5.0, t) for t in ("KEKW", "KEKW", "ratio", "cope")]
        assert chat.mood_around(agreed, 5.0)["confidence"] == 1.0
        assert chat.mood_around(split, 5.0)["confidence"] < 0.75

    def test_a_quiet_window_admits_it_does_not_know(self):
        neutral = [chat.Message(5.0, t) for t in ("what game is this", "hi chat")]
        mood = chat.mood_around(neutral, 5.0)
        assert mood["dominant"] is None
        assert mood["emotive_lines"] == 0

    def test_only_messages_inside_the_window_count(self):
        msgs = [chat.Message(5.0, "KEKW"), chat.Message(90.0, "monkaS")]
        assert chat.mood_around(msgs, 5.0, window_s=4.0)["counts"] == {"funny": 1}

    def test_a_line_can_carry_two_feelings(self):
        assert chat.Message(0, "OMG KEKW").emotions() == {"shock", "funny"}


class TestChatRetention:
    """Chat must forget on the same timer as the video it describes."""

    def test_messages_older_than_the_window_are_dropped(self):
        log = chat.LiveLog(window_s=60.0)
        log.extend([chat.Message(float(t), f"m{t}") for t in range(0, 200)])
        assert log.held_s() <= 60.0
        assert log.dropped == 139
        assert log.recent()[0].at_s >= 139.0

    def test_the_hard_cap_catches_a_flood_the_window_cannot(self):
        """A raid puts a million lines inside a five minute window."""
        log = chat.LiveLog(window_s=300.0, max_messages=1_000)
        # 50k messages all within one second - the window is satisfied and
        # would keep every one of them.
        log.extend([chat.Message(10.0, "RAID") for _ in range(50_000)])
        assert len(log.messages) == 1_000
        assert log.dropped == 49_000

    def test_memory_is_bounded_across_a_long_session(self):
        log = chat.LiveLog(window_s=300.0)
        for second in range(0, 8 * 3600, 2):  # eight hours, 30 msg/s
            log.extend([chat.Message(float(second), "KEKW") for _ in range(60)])
        assert len(log.messages) <= log.max_messages
        assert log.held_s() <= 300.0

    def test_a_curve_is_rebased_onto_what_is_still_held(self):
        log = chat.LiveLog(window_s=60.0)
        # Hours into a stream, so raw offsets are huge.
        log.extend([chat.Message(20_000.0 + t, "KEKW") for t in range(0, 60)])
        curve = log.curve()
        assert curve.duration_s <= 60.0
        assert len(curve.counts) <= 62, "the curve must not span the whole stream"
        assert sum(curve.counts) == len(log.messages)

    def test_an_empty_log_reports_zero_rather_than_failing(self):
        log = chat.LiveLog()
        assert log.held_s() == 0.0
        assert log.curve().duration_s == 0.0
        assert log.status()["messages"] == 0

    def test_what_was_dropped_is_visible(self):
        log = chat.LiveLog(window_s=10.0)
        log.extend([chat.Message(float(t), "x") for t in range(100)])
        status = log.status()
        assert status["dropped"] > 0
        assert status["held_s"] <= 10.0


LADDER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",NAME="Audio Only",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,CODECS="avc1.64002a,mp4a.40.2"
1080p60.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=852x480
480p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=230000,RESOLUTION=284x160
160p.m3u8
"""


class TestPayingForTheJob:
    """Detection and delivery need different bitrates by a factor of 26."""

    def test_every_rendition_is_found_including_audio_only(self):
        from core.live import parse_master

        found = parse_master(LADDER, "https://cdn.example/live/")
        assert len(found) == 4
        assert [v.bandwidth_bps for v in found] == sorted(v.bandwidth_bps for v in found)
        assert any(v.audio_only for v in found), (
            "audio-only is declared with EXT-X-MEDIA and carries no BANDWIDTH, "
            "so it is easy to miss - and it is the cheapest thing on the ladder"
        )

    def test_relative_urls_are_resolved_against_the_playlist(self):
        from core.live import parse_master

        found = parse_master(LADDER, "https://cdn.example/live/master.m3u8")
        assert all(v.url.startswith("https://cdn.example/live/") for v in found)

    def test_a_codec_string_containing_a_comma_does_not_break_parsing(self):
        """CODECS="avc1,mp4a" splits the tag if commas are read naively."""
        from core.live import parse_master

        found = [v for v in parse_master(LADDER) if v.height == 1080]
        assert found and found[0].bandwidth_bps == 6_000_000

    def test_detection_takes_the_cheapest_rendition_with_a_picture(self):
        from core.live import DETECT, choose_variant, parse_master

        chosen = choose_variant(parse_master(LADDER), DETECT)
        assert chosen.height == 160
        assert not chosen.audio_only, (
            "losing the picture costs real recall on moments chat barely reacted to"
        )

    def test_delivery_takes_the_best_picture(self):
        from core.live import DELIVER, choose_variant, parse_master

        assert choose_variant(parse_master(LADDER), DELIVER).height == 1080

    def test_audio_only_is_the_fallback_when_there_is_no_small_rendition(self):
        from core.live import DETECT, choose_variant, parse_master

        thin = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",NAME="Audio",URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080
1080p.m3u8
"""
        assert choose_variant(parse_master(thin), DETECT).audio_only

    def test_the_saving_is_the_whole_point(self):
        from core.live import DELIVER, DETECT, choose_variant, parse_master

        found = parse_master(LADDER)
        detect = choose_variant(found, DETECT)
        deliver = choose_variant(found, DELIVER)
        assert deliver.gb_per_day(10) / detect.gb_per_day(10) > 20

    def test_an_empty_playlist_is_an_error_not_a_silent_default(self):
        from core.live import LiveError, choose_variant

        with pytest.raises(LiveError, match="no renditions"):
            choose_variant([])


class TestEmotesAndVocabulary:
    """Kick's chat is not Twitch's, and the first live run proved it."""

    def test_an_emote_code_becomes_its_name(self):
        assert chat.clean("[emote:1579046:emojiGrimacing]") == "emojiGrimacing"
        assert chat.clean("nice [emote:37225:KEKLEO] one") == "nice KEKLEO one"

    def test_cleaning_is_about_reading_it_not_about_matching_it(self):
        """Two separate fixes, and it is worth not confusing them.

        Matching KEKLEO needed the vocabulary to key on roots rather than on
        exact Twitch names - and once it does, the root is visible inside the
        raw code too. Cleaning is what makes the line legible on the page,
        where "[emote:37225:KEKLEO]" is noise wrapped around a word.
        """
        raw = "[emote:37225:KEKLEO]"
        assert "funny" in chat.Message(0, raw).emotions()
        assert "funny" in chat.Message(0, chat.clean(raw)).emotions()
        assert chat.clean(raw) == "KEKLEO", "the page should show the word, not the id"

    def test_an_exact_name_list_would_have_missed_it(self):
        """The vocabulary before the first live run only knew KEKW."""
        import re

        assert not re.search(r"KEKW", "collectiblesGoldenKEKLEO")
        assert "funny" in chat.Message(0, "collectiblesGoldenKEKLEO").emotions()

    def test_kick_emotes_match_on_their_root(self):
        """Channel emotes wrap the root in prefixes: collectiblesGoldenKEKLEO."""
        for text in ("KEKLEO", "collectiblesGoldenKEKLEO", "LULEO", "OMEGALULiguess"):
            assert "funny" in chat.Message(0, text).emotions(), text

    def test_the_laughing_emoji_counts(self):
        assert "funny" in chat.Message(0, "😂😂😂😂").emotions()

    def test_w_and_l_still_read_as_hype_and_cringe(self):
        assert "hype" in chat.Message(0, "W").emotions()
        assert "hype" in chat.Message(0, "WW").emotions()
        assert "cringe" in chat.Message(0, "L").emotions()

    def test_ordinary_words_containing_a_root_are_not_dragged_in(self):
        """'flow' contains no root; 'below' must not read as lol."""
        assert chat.Message(0, "below the line").emotions() == set()

    def test_text_without_an_emote_is_untouched(self):
        assert chat.clean("BRO THIS SLOT IS BANNED") == "BRO THIS SLOT IS BANNED"
