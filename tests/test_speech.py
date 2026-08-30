"""Words as they are said, not paragraphs after the fact.

The bot was deaf until the very end: it transcribed a candidate *after*
deciding to cut it, so a moment made of words could never become a candidate,
because nothing had heard it. The word timestamps were always there - every
provider returns them - and the pipeline was throwing them away and keeping the
paragraph.
"""

from __future__ import annotations

import pytest

from core import moments, speech


def _words(text: str, start: float = 0.0, gap: float = 0.35):
    return [
        speech.Said(word=w, at_s=start + i * gap, end_s=start + i * gap + 0.3)
        for i, w in enumerate(text.split())
    ]


class TestTheRollingLog:
    def test_words_are_kept_in_order(self):
        log = speech.SpeechLog()
        log.extend(_words("hello there friend"))
        assert [w.word for w in log.words] == ["hello", "there", "friend"]

    def test_overlapping_windows_do_not_duplicate(self):
        """Windows overlap by ten seconds, so the same word arrives twice."""
        log = speech.SpeechLog()
        log.extend(_words("one two three", start=0.0))
        log.extend(_words("two three four", start=0.35))
        assert [w.word for w in log.words].count("three") == 1

    def test_old_words_are_forgotten_like_chat_is(self):
        log = speech.SpeechLog(window_s=10.0)
        log.extend(_words("this is ancient", start=0.0))
        log.extend(_words("this is now", start=100.0))
        assert all(w.at_s >= 90.0 for w in log.words)

    def test_it_can_be_asked_what_was_said_around_a_moment(self):
        log = speech.SpeechLog()
        log.extend(_words("the quick brown fox jumps over the lazy dog", gap=1.0))
        said = log.text_around(4.0, window_s=1.5)
        assert "jumps" in said
        assert "dog" not in said

    def test_the_status_is_json_shaped(self):
        import json

        log = speech.SpeechLog()
        log.extend(_words("hello there"))
        json.dumps(log.status())
        assert log.status()["words"] == 2


class TestReactingOutLoud:
    def test_a_reaction_is_found_where_it_was_said(self):
        log = speech.SpeechLog()
        log.extend(_words("so anyway I was saying oh my god did you see that", gap=0.5))
        found = speech.reactions(log.words)
        assert found
        assert 2.0 <= found[0][0] <= 6.5

    def test_ordinary_talking_is_not_a_reaction(self):
        log = speech.SpeechLog()
        log.extend(_words("so then I went to the shop and bought some bread", gap=0.4))
        assert speech.reactions(log.words) == []

    @pytest.mark.parametrize("line", [
        "oh my god", "no way", "what the hell", "are you serious",
        "did you just see that", "hold on hold on", "bro what",
    ])
    def test_the_things_people_actually_say(self, line):
        assert speech.reactions(_words(line))

    def test_nothing_said_is_no_reaction(self):
        assert speech.reactions([]) == []

    def test_a_run_of_them_is_one_reaction_not_five(self):
        log = speech.SpeechLog()
        log.extend(_words("oh my god oh my god oh my god", gap=0.3))
        assert len(speech.reactions(log.words)) == 1


class TestWordsAreEvidenceAndChatIsNot:
    def test_speech_can_nominate_a_moment(self):
        """The streamer reacting is first-hand. An audience typing is not."""
        assert "said" in moments.SENSED

    def test_and_outweighs_chat_agreeing_about_it(self):
        assert moments.WEIGHTS["said"] > moments.WEIGHTS["chat_burst"]

    def test_it_paints_either_side_of_the_words(self):
        found = moments.signals_from_speech([(30.0, 1.0)], duration_s=60.0)
        assert found["said"][30] > 0
        assert found["said"][28] > 0, "the thing reacted to came just before"
        assert found["said"][40] == 0


class TestItIsMetered:
    def test_live_listening_is_off_by_default(self):
        """It is the only thing here that costs per minute of stream."""
        from core.config import settings

        assert settings.speech_live is False

    def test_there_is_a_daily_ceiling_in_minutes(self):
        from core.config import settings

        assert settings.speech_minutes_per_day > 0

    def test_spend_is_counted_on_the_log(self):
        log = speech.SpeechLog()
        log.minutes_spent += 0.5
        assert log.status()["minutes_spent"] == 0.5


class TestPlacingWordsOnTheClock:
    def test_word_times_are_offset_onto_the_callers_timeline(self, monkeypatch):
        monkeypatch.setattr(
            "core.transcription.transcribe",
            lambda p: {"words": [{"w": "hello", "s": 2.0, "e": 2.4}]},
        )
        found = speech.transcribe_window("x.mp4", offset_s=270.0)
        assert found[0].at_s == 272.0

    def test_a_word_with_no_timestamp_is_skipped_not_guessed(self, monkeypatch):
        monkeypatch.setattr(
            "core.transcription.transcribe",
            lambda p: {"words": [{"w": "hello", "s": "nope"}, {"w": "there", "s": 1.0}]},
        )
        assert [w.word for w in speech.transcribe_window("x.mp4")] == ["there"]

    def test_an_empty_word_is_dropped(self, monkeypatch):
        monkeypatch.setattr(
            "core.transcription.transcribe",
            lambda p: {"words": [{"w": "  ", "s": 1.0}, {"w": "yes", "s": 2.0}]},
        )
        assert [w.word for w in speech.transcribe_window("x.mp4")] == ["yes"]
