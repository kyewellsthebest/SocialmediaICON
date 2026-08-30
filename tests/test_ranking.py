"""Which clip is better than which, and why.

With the hourly gate gone, everything that clears the bar gets cut, so the
question is no longer whether a clip is worth having but which one is. That is
an ordering rather than a threshold, and an ordering needs every axis at once
because the axes trade against each other.

These tests are mostly pairs: the same clip with one thing changed, and an
insistence that the change moves the score in the direction it should.
"""

from __future__ import annotations

import pytest

from core import ranking


def clip(**patch):
    """A strong clip, which each test then spoils in one specific way."""
    base = {
        "heard": {
            "laughs": [{"confidence": 0.93}], "shouts": [{"rise_db": 14}],
            "speech_share": 0.6, "music_share": 0.05,
        },
        "seen": {
            "surges": [{"size": 3.4}], "cuts": [1, 2], "still_s": 0.0, "duration_s": 38,
        },
        "watched_faces": {
            "on_screen": 0.8, "biggest_face": 0.11,
            "reactions": [{"size": 4.1}], "close_ups": [{"at_s": 12}],
        },
        "mood": {
            "dominant": "funny", "lift": 4.2, "confidence": 0.88,
            "background": False, "emotive_lines": 140,
        },
        "chat": {"burst_ratio": 6.0, "clip_requests": 3, "per_minute": 470},
        "said": {"reactions": [[12.0, 1.0]]},
        "verdict": {"watched": True, "worth_it": True, "confidence": 0.86, "kind": "funny"},
        "peak_viewers": 43120,
        "duration_s": 38,
    }
    base.update(patch)
    return base


def score(**patch) -> float:
    return ranking.rank(clip(**patch)).score


class TestTheOrdering:
    def test_a_clip_with_everything_scores_near_the_top(self):
        assert score() > 85

    def test_more_evidence_beats_less(self):
        one = score(heard={"laughs": [{"confidence": 0.9}], "speech_share": 0.6})
        several = score()
        assert several > one, "agreement between kinds of evidence is the strongest signal"

    def test_a_bigger_audience_wins_all_else_equal(self):
        assert score(peak_viewers=43120) > score(peak_viewers=900)

    def test_but_not_by_much(self):
        """Reach is the one thing the bot did not earn."""
        assert score(peak_viewers=43120) - score(peak_viewers=900) < 8

    def test_a_clip_nobody_can_follow_ranks_below_one_they_can(self):
        readable = score()
        muddy = score(heard={"laughs": [{"confidence": 0.93}],
                             "speech_share": 0.03, "music_share": 0.95})
        assert readable > muddy

    def test_faces_on_screen_count_for_something(self):
        assert score() > score(watched_faces={})


class TestWhatIsNotAllowedToCarryAClip:
    def test_chat_alone_ranks_nothing(self):
        """The rule that stops chat nominating a moment stops it ranking one."""
        assert score(heard={}, seen={}, watched_faces={}) == 0.0

    def test_a_mood_that_is_the_channels_wallpaper_counts_for_nothing(self):
        """100% agreement on a chat that always feels that way is not a reaction."""
        real = score()
        wallpaper = score(mood={"dominant": "hype", "lift": 0.8,
                                "background": True, "confidence": 1.0})
        assert real > wallpaper

    def test_a_clip_the_model_refused_is_not_ranked_at_all(self):
        """It is not at the bottom of the list, it is not on the list."""
        assert score(verdict={"watched": True, "worth_it": False,
                              "confidence": 0.9, "why": "a man reads a menu"}) == 0.0

    def test_and_the_refusal_is_kept(self):
        found = ranking.rank(clip(verdict={
            "watched": True, "worth_it": False, "confidence": 0.9, "why": "nothing happens"
        }))
        assert found.detail["rejected"] == "nothing happens"

    def test_a_clip_nothing_watched_loses_the_whole_verdict(self):
        watched = score()
        unwatched = score(verdict={})
        assert watched - unwatched == pytest.approx(
            ranking.WEIGHTS["verdict"] * 0.86, abs=0.5
        )

    def test_worth_it_but_kind_nothing_is_distrusted(self):
        """The model contradicting itself is worth less than the model agreeing."""
        assert score(verdict={"watched": True, "worth_it": True,
                              "confidence": 0.9, "kind": "nothing"}) < score()


class TestItShowsItsWorking:
    def test_every_part_is_kept(self):
        found = ranking.rank(clip())
        assert set(found.parts) == set(ranking.WEIGHTS)

    def test_and_the_numbers_behind_each_part(self):
        found = ranking.rank(clip())
        assert found.detail["event"]["laughter"] == 0.93
        assert found.detail["reaction"]["mood_lift"] == 4.2
        assert found.detail["production"]["face_on_screen"] == 0.8
        assert found.detail["reach"]["viewers"] == 43120

    def test_it_names_what_carried_the_clip(self):
        found = ranking.rank(clip())
        assert found.best_part in ranking.WEIGHTS

    def test_the_parts_are_all_fractions(self):
        found = ranking.rank(clip())
        assert all(0.0 <= v <= 1.0 for v in found.parts.values())

    def test_the_answer_is_json_shaped(self):
        import json

        json.dumps(ranking.rank(clip()).as_dict())

    def test_the_event_is_worth_more_than_the_reaction_to_it(self):
        """A reaction to nothing is what chat does all day."""
        assert ranking.WEIGHTS["event"] > ranking.WEIGHTS["reaction"]

    def test_and_watching_it_is_worth_more_than_the_reaction_too(self):
        assert ranking.WEIGHTS["verdict"] > ranking.WEIGHTS["reaction"]


class TestItSurvivesMissingData:
    def test_an_empty_record_does_not_crash(self):
        assert ranking.rank({}).score == 0.0

    def test_nulls_where_numbers_were_expected(self):
        assert ranking.rank({
            "heard": None, "seen": None, "mood": None, "chat": None,
            "verdict": None, "peak_viewers": None, "duration_s": None,
        }).score == 0.0

    def test_a_clip_from_before_faces_existed_still_ranks(self):
        found = ranking.rank(clip(watched_faces=None))
        assert found.score > 0
