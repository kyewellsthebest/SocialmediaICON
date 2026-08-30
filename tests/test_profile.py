"""No gaming, no sleeping, English only - and the first two were fiction.

The bot spent an evening on Battlegrounds Mobile India tournaments in Hindi.
Not because a detector failed: because it was choosing from a list sorted by
viewers with two filters that did not work. The directory's language tag is
optional, so a row that simply does not say was kept; and there was no category
filter at all.

The model call is not tested here - a test that asserts what a model says is a
test of the model. What is tested is the free layers that catch most of it, and
that an unknown answer is never mistaken for a yes.
"""

from __future__ import annotations

import pytest

from core import profile
from core.config import settings


@pytest.fixture(autouse=True)
def clean():
    profile._local.clear()
    yield
    profile._local.clear()


class TestGamesAreOut:
    @pytest.mark.parametrize("category", [
        "Battlegrounds Mobile India", "Call of Duty: Warzone", "VALORANT",
        "Grand Theft Auto V", "League of Legends", "Fortnite", "Minecraft",
        "EA Sports FC 25", "Counter-Strike 2", "Free Fire",
    ])
    def test_a_game_category_is_a_rejection_on_sight(self, category):
        found = profile.from_listing("x", category=category)
        assert found is not None and found.eligible is False
        assert found.is_gaming is True

    @pytest.mark.parametrize("category", [
        "Just Chatting", "IRL", "Music", "Sports", "Pools, Hot Tubs & Beaches",
        "Travel & Outdoors", "ASMR", "Food & Drink",
    ])
    def test_the_categories_it_wants_are_not_rejected(self, category):
        assert profile.from_listing("x", category=category) is None

    def test_it_costs_nothing_to_decide(self, monkeypatch):
        """No HTTP, no model - the cheap layer is the one that runs on 40 rows."""
        monkeypatch.setattr(
            profile, "from_kick", lambda c: pytest.fail("looked up a blocked category")
        )
        monkeypatch.setattr(
            profile, "research", lambda c, **k: pytest.fail("researched a game")
        )
        assert profile.decide("x", category="PUBG Mobile").eligible is False


class TestEnglishOnly:
    def test_a_title_in_another_script_is_a_rejection(self):
        found = profile.from_listing("x", title="बीजीएमआई मास्टर्स सीरीज़ लाइव")
        assert found is not None and found.eligible is False

    def test_an_english_title_is_not(self):
        assert profile.from_listing("x", title="CATCH ONE (club hosting)") is None

    def test_a_declared_language_is_believed_when_it_says_no(self):
        found = profile.from_listing("x", language="hindi")
        assert found is not None and found.eligible is False

    def test_a_row_that_declares_nothing_is_not_waved_through(self):
        """This is the hole the gaming streams came through.

        The old filter only rejected rows that *said* they were foreign, so a
        row with no language at all was kept as English.
        """
        found = profile.from_listing("x", category="Just Chatting", title="live now")
        assert found is None, "it must not be decided here"
        # ...and with research off it is still not an automatic yes: the
        # decision has to come from somewhere.
        assert profile.from_chat("x", ["bhai kya kar raha hai"] * 30) is not None


class TestChatIsTheHonestSignal:
    def _lines(self, text, n=40):
        return [text] * n

    def test_a_chat_in_another_script_settles_it(self):
        found = profile.from_chat("x", self._lines("क्या भाई मस्त है"))
        assert found is not None and found.eligible is False

    def test_romanised_hindi_is_caught_too(self):
        """"bhai kya kar raha hai" is all Latin letters and trips no script test."""
        found = profile.from_chat("x", self._lines("bhai kya kar raha hai yaar"))
        assert found is not None and found.eligible is False

    def test_an_english_chat_is_left_alone(self):
        assert profile.from_chat("x", self._lines("KEKW that was insane bro")) is None

    def test_one_stray_word_is_not_a_language(self):
        lines = ["nice clip", "KEKW", "lets go", "bhai"] * 10
        assert profile.from_chat("x", lines) is None

    def test_too_little_chat_to_tell_says_nothing(self):
        assert profile.from_chat("x", ["क्या भाई"] * 5) is None


class TestUnknownIsNotYes:
    def test_research_that_cannot_run_refuses_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "profile_required", True)
        monkeypatch.setattr(profile, "from_kick", lambda c: {})
        monkeypatch.setattr(
            profile, "research",
            lambda c, **k: (_ for _ in ()).throw(profile.ProfileError("no key")),
        )
        found = profile.decide("someone", category="Just Chatting")
        assert found.eligible is False
        assert "could not find out" in found.reason

    def test_and_that_refusal_is_not_cached(self, monkeypatch):
        """A broken key must not blacklist the whole directory for a week."""
        monkeypatch.setattr(profile, "from_kick", lambda c: {})
        monkeypatch.setattr(
            profile, "research",
            lambda c, **k: (_ for _ in ()).throw(profile.ProfileError("no key")),
        )
        profile.decide("someone", category="Just Chatting")
        assert profile.recall("someone") is None

    def test_it_can_be_told_to_allow_unknowns(self, monkeypatch):
        monkeypatch.setattr(settings, "profile_required", False)
        monkeypatch.setattr(profile, "from_kick", lambda c: {})
        monkeypatch.setattr(
            profile, "research",
            lambda c, **k: (_ for _ in ()).throw(profile.ProfileError("no key")),
        )
        assert profile.decide("someone", category="Just Chatting").eligible is True


class TestItOnlyAsksOnce:
    def test_a_decision_is_remembered(self, monkeypatch):
        asked = []
        monkeypatch.setattr(profile, "from_kick", lambda c: {})
        monkeypatch.setattr(
            profile, "research",
            lambda c, **k: asked.append(c) or profile.Profile(
                channel=c, eligible=True, confidence=0.9, decided_by="research"
            ),
        )
        for _ in range(5):
            profile.decide("someone", category="Just Chatting")
        assert asked == ["someone"], "a streamer does not change between polls"

    def test_a_rejection_is_remembered_too(self):
        profile.decide("gamer", category="VALORANT")
        assert profile.recall("gamer").eligible is False

    def test_it_can_be_asked_again_on_purpose(self, monkeypatch):
        asked = []
        monkeypatch.setattr(profile, "from_kick", lambda c: {})
        monkeypatch.setattr(
            profile, "research",
            lambda c, **k: asked.append(c) or profile.Profile(channel=c, confidence=0.9),
        )
        profile.decide("someone", category="Just Chatting")
        profile.decide("someone", category="Just Chatting", refresh=True)
        assert len(asked) == 2


class TestWhatItTellsTheRestOfThePipeline:
    def test_the_dossier_reads_as_a_paragraph(self):
        found = profile.Profile(
            channel="x", about="An IRL streamer from Los Angeles.",
            known_for="Boxing events and street interviews.",
        )
        assert "Los Angeles" in found.summary()
        assert "Boxing" in found.summary()

    def test_the_prompt_says_watching_a_game_is_still_gaming(self):
        """A reaction stream over gameplay is the obvious loophole."""
        assert "reacting to it on camera is still gameplay" in profile.SYSTEM

    def test_the_schema_demands_both_decisions_and_a_reason(self):
        for key in ("is_english", "is_gaming", "confidence", "reason"):
            assert key in profile.SCHEMA["required"]
