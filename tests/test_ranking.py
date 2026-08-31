"""Which clip is better than which, and why.

With the hourly gate gone, everything that clears the bar gets cut, so the
question is no longer whether a clip is worth having but which one is. That is
an ordering rather than a threshold, and an ordering needs every axis at once
because the axes trade against each other.

These tests are mostly pairs: the same clip with one thing changed, and an
insistence that the change moves the score in the direction it should.
"""

from __future__ import annotations

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
        """One kind of evidence, with the others actually taken away.

        Patching `heard` alone left the surges and the face reactions in
        place, so "one kind" was three kinds with a quieter laugh - and the
        test passed on a ranking where a lone signal reached half the event
        axis on its own."""
        one = score(
            heard={"laughs": [{"confidence": 0.93}], "speech_share": 0.6},
            seen={"surges": [], "cuts": [], "still_s": 0.0, "duration_s": 38},
            watched_faces={"on_screen": 0.8, "biggest_face": 0.11,
                           "reactions": [], "close_ups": []},
        )
        several = score()
        assert several > one + 10, "agreement is the strongest thing a moment has"

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
        assert unwatched < watched
        assert ranking.rank(clip(verdict={})).parts["verdict"] == 0.0

    def test_a_clip_where_a_face_could_be_read_is_worth_more(self):
        """A short vertical clip is about a person. A readable face says so."""
        blank = score(verdict={"watched": True, "worth_it": True,
                               "confidence": 0.86, "kind": "funny"})
        faces = score(verdict={"watched": True, "worth_it": True,
                               "confidence": 0.86, "kind": "funny",
                               "faces": [{"expression": "shocked"}]})
        assert faces > blank

    def test_the_setting_is_kept_for_the_page_to_show(self):
        found = ranking.rank(clip(verdict={
            "watched": True, "worth_it": True, "confidence": 0.9, "kind": "shocking",
            "setting": "at a roulette wheel", "faces": [{"expression": "stunned"}],
        }))
        assert found.detail["verdict"]["setting"] == "at a roulette wheel"
        assert found.detail["verdict"]["faces"] == ["stunned"]

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


class TestWhatCarriedTheClip:
    def test_it_is_the_biggest_contribution_not_the_biggest_fraction(self):
        """Reach is 0.99 on almost every stream worth watching.

        By raw fraction it therefore wins nearly every clip and says nothing;
        by contribution it wins only when nothing else did.
        """
        assert ranking.rank(clip()).best_part != "reach"

    def test_a_clip_that_only_had_an_audience_says_so(self):
        found = ranking.rank(clip(
            heard={"laughs": [{"confidence": 0.2}], "speech_share": 0.3},
            seen={}, watched_faces={},
            mood={"dominant": "hype", "lift": 1.0, "background": True, "confidence": 0.4},
            chat={"burst_ratio": 0, "clip_requests": 0, "per_minute": 100},
            said={},
            verdict={"watched": True, "worth_it": True, "confidence": 0.3, "kind": "funny"},
        ))
        assert found.best_part in ("reach", "production")

    def test_a_clip_carried_by_what_happened_says_that(self):
        assert ranking.rank(clip()).best_part == "event"

    def test_it_is_reported_alongside_the_score(self):
        assert ranking.rank(clip()).as_dict()["carried_by"] == "event"


class TestTheScoreHasToVaryBetweenClips:
    """A hundred clips from one channel in an hour all scored within a point
    or two of each other. 21 of their 100 points came from production and
    reach - and a clip of people talking is *perfectly* produced, while reach
    is the channel's viewer count and is the same for every clip it will ever
    make. Two fifths of the score was a constant."""

    def _nothing(self, **patch):
        base = {
            "heard": {"speech_share": 0.7, "music_share": 0.05, "laughs": [], "shouts": []},
            "seen": {"surges": [{"size": 2.6}], "cuts": [], "still_s": 0.0, "duration_s": 40},
            "watched_faces": {"on_screen": 0.8, "biggest_face": 0.12,
                              "reactions": [], "close_ups": []},
            "mood": {"dominant": "funny", "lift": 1.4, "confidence": 0.5,
                     "emotive_lines": 20},
            "chat": {"per_minute": 180.0, "burst_ratio": 1.2, "clip_requests": 0},
            "said": {}, "verdict": {}, "peak_viewers": 15000, "duration_s": 40,
        }
        base.update(patch)
        return base

    def test_nothing_happening_ranks_far_below_something(self):
        """Not a few points below - far below, or a hundred of them cannot be
        told apart on a page."""
        assert ranking.rank(self._nothing()).score < score() / 3

    def test_being_well_lit_cannot_carry_a_clip(self):
        """Production is the absence of a fault, not an achievement."""
        flawless = ranking.rank(self._nothing()).score
        rough = ranking.rank(self._nothing(
            heard={"speech_share": 0.05, "music_share": 0.9, "laughs": [], "shouts": []},
        )).score
        assert flawless > rough, "a fault should still cost something"
        assert flawless - rough < 12, "but being normal should not pay"

    def test_a_penalty_can_never_raise_a_clip(self):
        """The whole point of the shape: they multiply down from 1.0."""
        for part in ("production", "reach"):
            assert ranking.PENALTY_FLOOR[part] < 1.0
        best = ranking.rank(clip()).score
        assert best <= sum(ranking.MERIT.values())

    def test_merit_is_only_things_that_vary_within_a_channel(self):
        """Reach is a property of the channel, not of the clip, so it cannot
        be allowed to rank two clips from the same channel against each other."""
        assert set(ranking.MERIT) == {"event", "verdict", "reaction"}
        assert sum(ranking.MERIT.values()) == 100.0

    def test_the_spread_across_a_days_clips_is_wide(self):
        """Two clips of nothing and one real moment have to land far apart."""
        got = sorted(
            ranking.rank(r).score for r in (
                self._nothing(),
                self._nothing(chat={"per_minute": 400.0, "burst_ratio": 1.0,
                                    "clip_requests": 0}),
                clip(),
            )
        )
        assert got[-1] - got[0] > 50


class TestAClipNothingLookedAtIsStillRanked:
    """Six hours of watching produced a page of clips ranked 17 to 25 and not
    one above 50, and every one of them was UNWATCHED.

    That was arithmetic, not taste. The verdict is 30 of the 100 merit points
    and it used to be scored zero whenever nothing had watched the clip, which
    capped an unwatched clip at 70 even if everything else about it were
    perfect, and put an ordinary good one - a real event, a chat that reacted,
    well framed, a big channel - on 23.

    And the reason a clip goes unwatched is almost never the clip. It is the
    day's budget, or the pace, or the model being switched off. Scoring it
    zero ranks the bot's wallet and calls it a ranking of the clip.
    """

    def test_a_strong_unwatched_clip_can_beat_fifty(self):
        got = score(verdict={"watched": False})
        assert got > 50.0, f"a strong clip nothing looked at ranked {got}"

    def test_it_still_ranks_below_the_same_clip_verified(self):
        """A judgement from something that saw the video is worth something,
        and its absence should still cost - a discount, not a hole."""
        looked = score()
        blind = score(verdict={"watched": False})
        assert blind < looked
        assert blind > looked * 0.7, "a discount, not a 30-point hole"

    def test_the_discount_is_the_only_thing_between_them(self):
        assert ranking.UNWATCHED_KEEPS < 1.0
        looked = ranking.rank(clip())
        blind = ranking.rank(clip(verdict={"watched": False}))
        # Same evidence, so the merit before the discount is the same.
        assert blind.detail["verdict"]["unwatched_keeps"] == ranking.UNWATCHED_KEEPS
        assert "unwatched_keeps" not in looked.detail["verdict"]

    def test_a_weak_unwatched_clip_is_still_weak(self):
        """Renormalising must not float the floor up with the ceiling - the
        motion-only clips are exactly what should stay at the bottom."""
        got = score(
            verdict={"watched": False},
            heard={"laughs": [], "shouts": [], "speech_share": 0.2},
            watched_faces={"on_screen": 0.1, "reactions": [], "close_ups": []},
            seen={"surges": [{"size": 1.6}], "cuts": [], "still_s": 0.0,
                  "duration_s": 32},
            mood={"dominant": None, "lift": 0.4, "confidence": 0.1,
                  "emotive_lines": 3},
            chat={"burst_ratio": 1.1, "clip_requests": 0, "per_minute": 40},
        )
        assert got < 20.0, f"a motion-only clip ranked {got}"

    def test_a_clip_the_model_refused_is_still_rejected(self):
        """Not watched and refused are opposite cases, and only one of them
        is an absence."""
        assert score(verdict={"watched": True, "worth_it": False}) == 0.0


class TestARankCanBeRecomputedFromWhatWasStored:
    """A rank is written once, when the clip is made, and never revisited - so
    a queue outlives the rules that ordered it. That is fine while the rules
    are right and is the whole problem when they are not.

    Re-ranking has to work off the database, because the video is deleted as
    soon as the portrait version exists. The evidence column is kept for
    exactly this, and the test that matters is that a stored clip re-ranks to
    the number it was given: if the key mapping between the two shapes drifts,
    the evidence silently reads as empty and every clip re-ranks to zero.
    """

    def _stored(self, record):
        """The row store() would write, as an object rank() has never seen."""
        return type("Row", (), {
            "evidence": {
                "heard": record.get("heard"),
                "seen": record.get("seen"),
                "faces": record.get("watched_faces"),
                "chat": record.get("chat"),
                "said": record.get("said"),
            },
            "mood": record.get("mood"),
            "duration_s": record.get("duration_s"),
            "peak_viewers": record.get("peak_viewers"),
            "verdict": record.get("verdict"),
        })()

    def test_it_re_ranks_to_the_number_it_was_given(self):
        record = clip()
        fresh = ranking.rank(record).score
        again = ranking.rank(ranking.from_stored(self._stored(record))).score
        assert again == fresh

    def test_an_unwatched_clip_re_ranks_the_same_way_too(self):
        record = clip(verdict={"watched": False})
        fresh = ranking.rank(record).score
        again = ranking.rank(ranking.from_stored(self._stored(record))).score
        assert again == fresh
        assert again > 50.0, "and it is the corrected number, not the old one"

    def test_every_part_survives_the_round_trip(self):
        """Not just the total. A total can match while two parts have swapped
        or both gone to zero and cancelled out."""
        record = clip()
        fresh = ranking.rank(record).parts
        again = ranking.rank(ranking.from_stored(self._stored(record))).parts
        assert again == fresh
        assert all(v > 0 for v in again.values()), again

    def test_a_row_with_nothing_on_it_is_not_a_crash(self):
        assert ranking.rank(ranking.from_stored(type("Row", (), {})())).score == 0.0
