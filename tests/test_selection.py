from __future__ import annotations

from conftest import make_words

from core.selection import (
    Candidate,
    chunk_words,
    clamp_to_words,
    dedupe,
    format_words,
    overlap_ratio,
    select_top,
    text_between,
    words_between,
)


def test_chunk_words_covers_everything_with_overlap():
    words = make_words(600)  # 300 seconds
    windows = chunk_words(words, window_s=60, overlap_s=10)

    assert len(windows) > 1
    assert windows[0].start_s == 0.0
    # every word appears in at least one window
    seen = {w["start"] for win in windows for w in win.words}
    assert seen == {w["start"] for w in words}
    # consecutive windows overlap in time
    assert windows[1].start_s < windows[0].end_s


def test_chunk_words_empty():
    assert chunk_words([]) == []


def test_format_words_is_timestamped_lines():
    text = format_words(make_words(25), words_per_line=10)
    lines = text.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("[0.0] ")
    assert lines[1].startswith("[5.0] ")


def test_clamp_snaps_onto_word_boundaries():
    words = make_words(200)  # 100 seconds
    clamped = clamp_to_words(Candidate(start_s=10.3, end_s=40.2), words, min_s=15, max_s=60)
    assert clamped is not None
    assert any(abs(w["start"] - clamped.start_s) < 1e-6 for w in words)
    assert any(abs(w["end"] - clamped.end_s) < 1e-6 for w in words)


def test_clamp_extends_a_too_short_window():
    words = make_words(200)
    clamped = clamp_to_words(Candidate(start_s=10.0, end_s=13.0), words, min_s=15, max_s=60)
    assert clamped is not None
    assert clamped.duration_s >= 15


def test_clamp_truncates_a_too_long_window_keeping_the_opening():
    words = make_words(400)
    clamped = clamp_to_words(Candidate(start_s=20.0, end_s=180.0), words, min_s=15, max_s=60)
    assert clamped is not None
    assert clamped.start_s == 20.0
    assert clamped.duration_s <= 60


def test_clamp_rejects_windows_with_no_words():
    words = make_words(20)  # 0-10s
    assert clamp_to_words(Candidate(start_s=500.0, end_s=530.0), words) is None
    assert clamp_to_words(Candidate(start_s=5.0, end_s=5.0), words) is None


def test_clamp_returns_none_when_source_is_shorter_than_min():
    words = make_words(10)  # 5 seconds total
    assert clamp_to_words(Candidate(start_s=0.0, end_s=5.0), words, min_s=15, max_s=60) is None


def test_overlap_ratio():
    a = Candidate(start_s=0, end_s=30)
    b = Candidate(start_s=15, end_s=45)
    assert overlap_ratio(a, b) == 0.5
    assert overlap_ratio(a, Candidate(start_s=60, end_s=90)) == 0.0
    # contained window overlaps the shorter one completely
    assert overlap_ratio(a, Candidate(start_s=10, end_s=20)) == 1.0


def test_dedupe_keeps_the_best_of_an_overlapping_pair():
    weak = Candidate(start_s=0, end_s=30, hook_score=5, payoff_score=5, novelty=5)
    strong = Candidate(start_s=5, end_s=35, hook_score=9, payoff_score=9, novelty=9)
    far = Candidate(start_s=200, end_s=230, hook_score=6, payoff_score=6, novelty=6)

    kept = dedupe([weak, strong, far])
    assert [c.start_s for c in kept] == [5, 200]


def test_predicted_score_wins_over_detection_scores():
    detected = Candidate(start_s=0, end_s=30, hook_score=10, payoff_score=10, novelty=10)
    ranked = Candidate(start_s=100, end_s=130, hook_score=1, payoff_score=1, predicted_score=90)
    assert select_top([detected, ranked], 1) == [ranked]


def test_select_top_returns_chronological_order():
    cands = [
        Candidate(start_s=300, end_s=330, predicted_score=90),
        Candidate(start_s=100, end_s=130, predicted_score=95),
        Candidate(start_s=200, end_s=230, predicted_score=10),
    ]
    top = select_top(cands, 2)
    assert [c.start_s for c in top] == [100, 300]


def test_text_and_words_between_are_rebased():
    words = make_words(40)  # 20 seconds
    assert text_between(words, 5.0, 7.0) == "w10 w11 w12 w13"

    rebased = words_between(words, 5.0, 7.0)
    assert rebased[0]["start"] == 0.0
    assert rebased[-1]["end"] <= 2.0
