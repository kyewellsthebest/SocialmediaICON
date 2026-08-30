"""Watching the top ten, without spending the day reconnecting.

The point these tests defend: following the ranking literally makes the bot
churn through connections on noise, and every churn is fifteen seconds of a
stream that cannot be clipped. The hysteresis is not a nicety.
"""

from __future__ import annotations

import random

from core import roster
from core.roster import Live, Roster


def _listing(names_and_viewers: list[tuple[str, int]]) -> list[Live]:
    rows = [Live(channel=n, viewers=v) for n, v in names_and_viewers]
    return sorted(rows, key=lambda live: -live.viewers)


class TestPickingUp:
    def test_it_fills_its_slots_from_the_top(self):
        roster = Roster(slots=3)
        moved = roster.update(_listing([("a", 900), ("b", 800), ("c", 700), ("d", 600)]))
        assert moved["start"] == ["a", "b", "c"]
        assert "d" not in roster.watching

    def test_it_never_exceeds_its_slots(self):
        roster = Roster(slots=2)
        roster.update(_listing([(c, 100 - i) for i, c in enumerate("abcdef")]))
        assert len(roster.watching) == 2


class TestChurn:
    def test_two_streams_swapping_around_the_cutoff_do_not_churn(self):
        """The failure mode the hysteresis exists to prevent."""
        roster = Roster(slots=10, drop_rank=13, patience_s=240.0, min_tenure_s=300.0)
        base = [(f"ch{i}", 10_000 - i * 500) for i in range(12)]
        now = 0.0
        roster.update(_listing(base), now=now)

        random.seed(7)
        churn = 0
        for _ in range(60):  # five hours at five minute polls
            now += 300.0
            jittered = [
                (name, int(viewers * random.uniform(0.93, 1.07))) for name, viewers in base
            ]
            moved = roster.update(_listing(jittered), now=now)
            churn += len(moved["start"]) + len(moved["stop"])

        assert churn == 0, f"noise alone caused {churn} reconnections"

    def test_a_realistic_clustered_ladder_does_not_thrash(self):
        """The distribution that actually appears on Kick's Browse page.

        The top two run away with it and ranks 8-14 sit within a few hundred
        viewers of each other, so on noise alone they reorder constantly. This
        is where following the ranking literally becomes expensive: measured
        over a simulated day, the literal version churns roughly ten times as
        much as this one.
        """
        base = [
            ("ch0", 19_400), ("ch1", 15_600), ("ch2", 8_200), ("ch3", 6_900),
            ("ch4", 5_500), ("ch5", 5_100), ("ch6", 4_900), ("ch7", 4_700),
            ("ch8", 4_600), ("ch9", 4_550), ("ch10", 4_500), ("ch11", 4_450),
            ("ch12", 4_400), ("ch13", 4_300),
        ]
        roster = Roster(slots=10, drop_rank=13, patience_s=240.0, min_tenure_s=300.0)
        now = 0.0
        roster.update(_listing(base), now=now)

        random.seed(11)
        churn = 0
        for _ in range(288):  # a day of five-minute polls
            now += 300.0
            jittered = [
                (name, int(viewers * random.uniform(0.93, 1.07))) for name, viewers in base
            ]
            moved = roster.update(_listing(jittered), now=now)
            churn += len(moved["start"]) + len(moved["stop"])

        # Each reconnect costs about fifteen seconds of unclippable warmup.
        assert churn < 150, f"{churn} reconnects a day is {churn * 15 / 60:.0f} minutes blind"
        assert len(roster.watching) == 10

    def test_a_stream_that_genuinely_collapses_is_dropped(self):
        roster = Roster(slots=2, drop_rank=3, patience_s=100.0, min_tenure_s=100.0)
        now = 0.0
        roster.update(_listing([("a", 900), ("b", 800), ("c", 100), ("d", 90)]), now=now)
        assert set(roster.watching) == {"a", "b"}

        # b craters and stays down.
        dropped: list[str] = []
        for _ in range(5):
            now += 60.0
            moved = roster.update(
                _listing([("a", 900), ("c", 700), ("d", 600), ("e", 500), ("b", 10)]), now=now
            )
            dropped += moved["stop"]
        assert "b" not in roster.watching
        assert "b" in dropped

    def test_a_new_stream_is_given_time_before_it_can_be_dropped(self):
        roster = Roster(slots=1, drop_rank=1, patience_s=0.0, min_tenure_s=300.0)
        roster.update(_listing([("a", 500)]), now=0.0)
        # Immediately outranked, but only 60s in.
        roster.update(_listing([("b", 900), ("a", 500)]), now=60.0)
        assert "a" in roster.watching, (
            "dropping a stream a minute after attaching wastes the connection"
        )

    def test_the_slot_freed_by_a_drop_is_refilled(self):
        roster = Roster(slots=1, drop_rank=1, patience_s=0.0, min_tenure_s=0.0)
        roster.update(_listing([("a", 500)]), now=0.0)
        moved = roster.update(_listing([("b", 900), ("a", 100)]), now=1000.0)
        assert moved["stop"] == ["a"]
        assert moved["start"] == ["b"]


class TestOffline:
    def test_a_stream_that_ends_is_dropped_at_once(self):
        """Going offline is not noise, so patience does not apply."""
        roster = Roster(slots=2, min_tenure_s=99_999.0, patience_s=99_999.0)
        roster.update(_listing([("a", 900), ("b", 800)]), now=0.0)
        moved = roster.update(_listing([("a", 900), ("c", 700)]), now=10.0)
        assert moved["stop"] == ["b"]
        assert "c" in roster.watching

    def test_an_empty_listing_stops_everything_without_crashing(self):
        roster = Roster(slots=2)
        roster.update(_listing([("a", 900), ("b", 800)]), now=0.0)
        moved = roster.update([], now=10.0)
        assert set(moved["stop"]) == {"a", "b"}
        assert roster.watching == {}


class TestParsingTheDirectory:
    def test_streams_are_found_whatever_the_envelope(self):
        from core.roster import _parse_live

        rows = [{"slug": "x", "livestream": {"viewer_count": 50, "language": "english"}}]
        for payload in (rows, {"data": rows}, {"data": {"livestreams": rows}}):
            found = _parse_live(payload, "en")
            assert len(found) == 1 and found[0].channel == "x"

    def test_other_languages_are_filtered_out(self):
        from core.roster import _parse_live

        rows = [
            {"slug": "a", "livestream": {"viewer_count": 5, "language": "english"}},
            {"slug": "b", "livestream": {"viewer_count": 9, "language": "spanish"}},
        ]
        assert [live.channel for live in _parse_live(rows, "en")] == ["a"]

    def test_a_row_that_does_not_state_a_language_is_kept(self):
        from core.roster import _parse_live

        # Better a stream we might not want than silently losing the listing.
        rows = [{"slug": "a", "livestream": {"viewer_count": 5}}]
        assert len(_parse_live(rows, "en")) == 1

    def test_an_unknown_shape_returns_nothing_rather_than_raising(self):
        from core.roster import _parse_live

        assert _parse_live({"surprise": 1}, "en") == []
        assert _parse_live(None, "en") == []


class TestTheChannelIsNotTheStream:
    """The bug that made the first real run resolve nothing.

    The livestreams endpoint returns one row per *session*, and that row's own
    `slug` names the session - "81c9c0fc-locked-in-athon-day-49-of-90" - not
    the person. The parser took it first, so every playback lookup asked for
    kick.com/<session slug> and got a 404 that read like the channel had gone
    offline. Three streams, three 404s, nothing watched.
    """

    #: Exactly the shape that failed, with the real values from that run.
    SESSION_ROW = {
        "id": 40012,
        "slug": "81c9c0fc-locked-in-athon-day-49-of-90-w-at-ninadrama",
        "session_title": "LOCKED-IN-ATHON DAY 49 OF 90",
        "viewer_count": 16735,
        "language": "English",
        "categories": [{"name": "IRL"}],
        "channel": {"id": 668, "slug": "deenthegreat", "user": {"username": "DeenTheGreat"}},
    }

    def test_the_channel_slug_wins_over_the_session_slug(self):
        from core.roster import _parse_live

        found = _parse_live([self.SESSION_ROW], "en")
        assert len(found) == 1
        assert found[0].channel == "deenthegreat"
        assert found[0].page() == "https://kick.com/deenthegreat"

    def test_a_session_slug_is_never_used_as_a_channel(self):
        """Even with no channel object, a session slug must not be returned."""
        from core.roster import _channel_of

        assert _channel_of({"slug": "19c54e13-gamba-18-jackcom-giveaway-if-win"}) == ""

    def test_a_real_channel_row_still_works(self):
        """The channels endpoint does carry the right slug on the row itself."""
        from core.roster import _channel_of

        assert _channel_of({"slug": "xqc", "livestream": {"viewer_count": 5}}) == "xqc"

    def test_a_username_is_accepted_when_there_is_no_slug(self):
        from core.roster import _channel_of

        assert _channel_of({"channel": {"user": {"username": "trainwreckstv"}}}) == "trainwreckstv"
        assert _channel_of({"user": {"username": "adinross"}}) == "adinross"

    def test_a_row_with_no_channel_anywhere_is_dropped(self):
        from core.roster import _parse_live

        assert _parse_live([{"viewer_count": 900, "session_title": "hi"}], "en") == []

    def test_viewers_and_title_still_come_from_the_session(self):
        from core.roster import _parse_live

        live = _parse_live([self.SESSION_ROW], "en")[0]
        assert live.viewers == 16735
        assert "LOCKED-IN-ATHON" in live.title
        assert live.category == "IRL"

    def test_hex_that_is_not_a_session_slug_is_kept(self):
        """A channel legitimately named with hex characters is not a session."""
        from core.roster import _channel_of

        assert _channel_of({"slug": "deadbeef"}) == "deadbeef"
        assert _channel_of({"slug": "abc123-somebody"}) == "abc123-somebody"


class TestWhatAStreamIsWorthWatching:
    """Viewers first, chat rate as a bounded correction.

    Viewers alone ranked a stream with nine thousand people and a chat running
    at 477 a minute below one with sixteen thousand and a chat doing 177. The
    first of those is where the clips are. But an unbounded correction ranks a
    two-hundred viewer channel with a manic chat above everything on Kick, so
    the correction is capped - and the cap is the promise that a big audience
    is never overturned by a small difference in typing.
    """

    def _live(self, channel, viewers, rate=0.0):
        return roster.Live(channel=channel, viewers=viewers, messages_per_min=rate)

    def test_more_viewers_wins_when_nothing_else_is_known(self):
        found = roster.rank_streams([self._live("a", 3000), self._live("b", 10000)])
        assert [live.channel for live in found] == ["b", "a"]

    def test_a_small_chat_lead_does_not_overturn_a_large_audience(self):
        """The case as it was put: 3k with fifty more must not beat 10k with fifty fewer."""
        big = self._live("big", 10000, 190)
        small = self._live("small", 3000, 290)
        assert roster.worth(big) > roster.worth(small)
        assert roster.rank_streams([small, big])[0].channel == "big"

    def test_a_much_livelier_chat_can_overturn_a_comparable_audience(self):
        """oblivionsw at 8,958 and 477/min against DeenTheGreat at 16,732 and 177/min."""
        deen = self._live("DeenTheGreat", 16732, 177)
        oblivion = self._live("oblivionsw", 8958, 477)
        assert roster.rank_streams([deen, oblivion])[0].channel == "oblivionsw"

    def test_no_chat_rate_can_overturn_a_wide_enough_gap(self):
        """The cap is 1.6/0.6, so nothing under a 2.7x audience gap can flip."""
        quiet_giant = self._live("giant", 100000, 1)
        manic_minnow = self._live("minnow", 30000, 99999)
        assert roster.worth(quiet_giant) > roster.worth(manic_minnow)

    def test_a_rate_is_judged_against_what_a_stream_that_size_usually_does(self):
        """477 a minute is extraordinary at nine thousand and ordinary at ninety."""
        small = self._live("small", 9000, 477)
        huge = self._live("huge", 90000, 477)
        assert roster.worth(small) > small.viewers, "busier than its size predicts"
        assert roster.worth(huge) < huge.viewers, "quieter than its size predicts"

    def test_expected_rate_grows_with_size_but_not_in_step_with_it(self):
        """Ten times the audience does not type ten times as much."""
        ten_x = roster.expected_rate(100000) / roster.expected_rate(10000)
        assert 2.0 < ten_x < 6.0

    def test_an_unmeasured_stream_is_ranked_on_viewers_alone(self):
        """Not penalised for being one the bot has not listened to yet."""
        assert roster.worth(self._live("x", 5000)) == 5000.0

    def test_ranking_is_stable_for_streams_of_equal_worth(self):
        first = [self._live("b", 1000), self._live("a", 1000)]
        assert [live.channel for live in roster.rank_streams(first)] == ["a", "b"]
