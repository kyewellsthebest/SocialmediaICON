from __future__ import annotations

from conftest import make_words

from core.captions import (
    BOTTOM_UNSAFE,
    PLAY_H,
    PLAY_W,
    CaptionStyle,
    build_ass,
    escape_text,
    format_ts,
    group_words_into_lines,
)


def test_format_ts():
    assert format_ts(0) == "0:00:00.00"
    assert format_ts(61.5) == "0:01:01.50"
    assert format_ts(3661.239) == "1:01:01.24"
    assert format_ts(-5) == "0:00:00.00"
    # rounding must not produce ".100"
    assert format_ts(1.999) == "0:00:02.00"


def test_escape_text_neutralises_ass_syntax():
    assert escape_text("{\\an8}hi") == "(/an8)hi"
    assert escape_text(" line\nbreak ") == "line break"


def test_lines_break_on_word_count_and_pauses():
    style = CaptionStyle(max_words=3, max_chars=100, max_line_s=100, max_gap_s=0.6)
    lines = group_words_into_lines(make_words(9, step=0.4), style)
    assert [len(line) for line in lines] == [3, 3, 3]

    words = [
        {"w": "a", "start": 0.0, "end": 0.3},
        {"w": "b", "start": 0.3, "end": 0.6},
        {"w": "c", "start": 5.0, "end": 5.3},  # long pause
    ]
    assert [len(line) for line in group_words_into_lines(words, style)] == [2, 1]


def test_build_ass_structure_and_safe_zone():
    ass = build_ass(make_words(12))
    assert f"PlayResX: {PLAY_W}" in ass
    assert f"PlayResY: {PLAY_H}" in ass

    style_line = next(line for line in ass.splitlines() if line.startswith("Style: Clip"))
    margin_v = int(style_line.split(",")[-2])
    # captions must clear the bottom 20% where the platform UI sits
    assert margin_v >= PLAY_H * BOTTOM_UNSAFE

    dialogues = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    # one event per word: the whole line redrawn with a different word lit
    assert len(dialogues) == 12
    assert CaptionStyle().highlight_colour in dialogues[0]


def test_events_are_ordered_and_non_overlapping_within_a_line():
    style = CaptionStyle(max_words=4, max_chars=100, max_line_s=100)
    ass = build_ass(make_words(4), style)
    dialogues = [line.split(",") for line in ass.splitlines() if line.startswith("Dialogue:")]
    starts = [d[1] for d in dialogues]
    ends = [d[2] for d in dialogues]
    assert starts == sorted(starts)
    # each word hands over to the next with no gap
    assert ends[:-1] == starts[1:]


def test_zero_length_word_still_gets_a_visible_event():
    words = [{"w": "hi", "start": 1.0, "end": 1.0}]
    dialogue = next(line for line in build_ass(words).splitlines() if line.startswith("Dialogue:"))
    start, end = dialogue.split(",")[1:3]
    assert end > start


def test_line_holds_briefly_into_a_short_pause_but_not_a_long_one():
    style = CaptionStyle(max_words=2, max_chars=100, max_line_s=100, max_gap_s=0.3)
    words = [
        {"w": "a", "start": 0.0, "end": 0.4},
        {"w": "b", "start": 0.4, "end": 0.8},
        {"w": "c", "start": 1.0, "end": 1.4},  # 0.2s pause -> new line
        {"w": "d", "start": 1.4, "end": 1.8},
    ]
    ends = [
        line.split(",")[2]
        for line in build_ass(words, style).splitlines()
        if line.startswith("Dialogue:")
    ]
    # first line holds until the next line starts, not past it
    assert ends[1] == format_ts(1.0)
    # last line holds for line_hold_s past the final word
    assert ends[3] == format_ts(1.8 + style.line_hold_s)
