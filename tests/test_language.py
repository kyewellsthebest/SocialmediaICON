"""Title-based language filtering.

The asymmetry matters: a false reject loses one video out of thousands, while
a false accept costs a download, a transcription, a model call and a render
before anyone notices the audio is in a language the audience cannot follow.
So these tests care more about the rejects being right than about catching
every last foreign title.
"""

from __future__ import annotations

import pytest

from core.language import looks_english

FOREIGN = [
    "¡NO CREERÁS TODO LO QUE PIERDEN! cuando saltan desde aquí",
    "GANHEI MEU DIA COM O DETECTOR NA PRAIA DOS PICOS!",
    "To Miała Być Zwykła Wizyta w Polsce",
    "IDEMO MOTORIMA NA PECANJE S MAGNETOM!",
    "Найдено сокровище на пляже",
    "海で金属探知機を使ってみた",
    "Détection métallique avec mon nouveau détecteur",
    "Ich habe einen Schatz mit dem Metalldetektor gefunden",
]

ENGLISH = [
    "I Never Expected to Find THIS at the Minelab 500 Rally!",
    "We Found IRON MAN Underwater While Magnet Fishing!",
    "Gold Prospecting the Yukon - Best Day Yet",
    "I HAD to See What Was Under It...",
    "Metal Detecting a 1700s Farm Field",
    "Minelab Manticore Deep Silver",
    "Sluice Box Cleanup",
    "Solo Overnight Bushcraft Shelter Build",
    "Huge Silver Coin Spill on Old Permission",
]


@pytest.mark.parametrize("title", FOREIGN)
def test_foreign_titles_are_rejected(title):
    assert looks_english(title) is False


@pytest.mark.parametrize("title", ENGLISH)
def test_english_titles_are_kept(title):
    assert looks_english(title) is True


def test_nothing_to_judge_is_kept():
    """An absent title is not evidence of anything."""
    assert looks_english(None) is True
    assert looks_english("") is True
    assert looks_english("   ") is True


def test_a_number_only_title_is_kept():
    assert looks_english("2026 #4") is True


def test_a_stray_borrowed_word_does_not_condemn_an_english_title():
    assert looks_english("The Del Mar Beach Hunt - Best Finds of the Day") is True


def test_an_accent_or_two_is_tolerated():
    assert looks_english("Café Field Permission - First Dig of the Year") is True
