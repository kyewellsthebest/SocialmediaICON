"""Is this title in the language we post in?

YouTube's `defaultAudioLanguage` is unset on most videos, so filtering on it
alone lets almost everything through - which is how a feed meant to be English
fills up with Spanish and Portuguese. The title is the only other signal
available before downloading anything, and for this purpose it is enough: we
are not identifying a language, only deciding whether a title is confidently
*not* English.

That asymmetry matters. A false reject loses one video out of thousands; a
false accept costs a download, a transcription, a model call and a render
before anyone notices the audio is in a language the audience does not speak.
So the rule is: reject on positive evidence of another language, keep
otherwise.
"""

from __future__ import annotations

import re
import unicodedata

# Scripts that settle it on sight.
NON_LATIN = re.compile(
    r"[Ѐ-ӿ"  # Cyrillic
    r"Ͱ-Ͽ"  # Greek
    r"֐-׿"  # Hebrew
    r"؀-ۿ"  # Arabic
    r"฀-๿"  # Thai
    r"぀-ヿ"  # Japanese kana
    r"一-鿿"  # CJK
    r"가-힯]"  # Hangul
)

# Punctuation and letters used by one language and essentially no other.
# The Slavic letters are separate characters rather than accented ones, so the
# diacritic ratio below never sees them.
GIVEAWAY_CHARS = ("¡", "¿", "ł", "Ł", "ą", "ę", "ż", "ź", "ś", "ć", "ń", "đ", "ğ", "ı", "ş")

# Function words that are common in their own language and vanishingly rare in
# English titles. Deliberately short: every entry is a word that would be odd
# to find in an English sentence, so a single hit is evidence rather than noise.
FOREIGN_WORDS = {
    # Spanish / Portuguese
    "que",
    "para",
    "con",
    "por",
    "los",
    "las",
    "del",
    "una",
    "como",
    "cuando",
    "desde",
    "todo",
    "todos",
    "muito",
    "mais",
    "não",
    "você",
    "com",
    "uma",
    "meu",
    "minha",
    "dia",
    "praia",
    "aqui",
    "esto",
    "esta",
    "este",
    # French
    "avec",
    "pour",
    "dans",
    "vous",
    "cette",
    "tout",
    # German
    "und",
    "mit",
    "das",
    "ist",
    "nicht",
    "auf",
    "ein",
    "eine",
    "für",
    # Italian
    "che",
    "non",
    "sono",
    "questo",
    "molto",
    # Polish
    "nie",
    "jest",
    "sie",
    "się",
    "przy",
    "jak",
    "tego",
    "który",
    # Dutch / Scandinavian
    "een",
    "het",
    "voor",
    "och",
    "som",
    "för",
    "ikke",
    # Turkish / Romanian
    "için",
    "bir",
    "ile",
    "pentru",
    "care",
    # Serbo-Croatian / Slovene
    "idemo",
    "sam",
    "sve",
    "ovo",
    "biti",
    "kako",
    "gdje",
    "jako",
}

# Words whose presence argues the other way, for titles that mix languages.
ENGLISH_WORDS = {
    "the",
    "and",
    "with",
    "found",
    "this",
    "that",
    "have",
    "from",
    "what",
    "when",
    "beach",
    "gold",
    "metal",
    "detecting",
    "detector",
    "hunt",
    "finds",
    "treasure",
    "camp",
    "river",
    "day",
    "best",
    "first",
    "my",
    "we",
    "i",
    "you",
    "how",
    "why",
    "was",
    "were",
    "it",
    "of",
    "in",
    "on",
    # Niche vocabulary, so a title made only of equipment and terrain
    # still reads as English rather than as an unknown language.
    "deep",
    "silver",
    "coin",
    "coins",
    "ring",
    "dig",
    "digging",
    "field",
    "farm",
    "woods",
    "creek",
    "nugget",
    "panning",
    "sluice",
    "box",
    "cleanup",
    "hunting",
    "survival",
    "bushcraft",
    "shelter",
    "fire",
    "wild",
    "camping",
    "solo",
    "overnight",
    "old",
    "new",
    "big",
    "lost",
    "buried",
    "relic",
    "relics",
    "hoard",
    "site",
    "permission",
    "beep",
    "signal",
    "build",
}

WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def _accent_ratio(text: str) -> float:
    """Share of letters carrying a diacritic.

    English borrows a few (café, naïve); a title made of them is not English.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    accented = sum(1 for c in letters if unicodedata.combining(c) or _has_mark(c))
    return accented / len(letters)


def _has_mark(char: str) -> bool:
    decomposed = unicodedata.normalize("NFD", char)
    return len(decomposed) > 1 and unicodedata.combining(decomposed[-1]) != 0


def looks_english(text: str | None) -> bool:
    """False only when there is positive evidence of another language."""
    if not text or not text.strip():
        return True  # nothing to judge; let the other filters decide

    if NON_LATIN.search(text):
        return False
    if any(char in text for char in GIVEAWAY_CHARS):
        return False
    if _accent_ratio(text) > 0.08:
        return False

    words = [w.lower() for w in WORD.findall(text)]
    if not words:
        return True

    foreign = sum(1 for w in words if w in FOREIGN_WORDS)
    english = sum(1 for w in words if w in ENGLISH_WORDS)

    # One foreign function word is enough unless English words outnumber it -
    # titles legitimately contain a stray "con" or "die" as a brand or a noun.
    if foreign and foreign >= english:
        return False

    # A sentence-length title with not one recognisable English word is the
    # shape of a language this list does not cover. The bar is five words,
    # because a four-word English title can legitimately be all proper nouns
    # and equipment names.
    if len(words) >= 5 and english == 0:
        return False

    return True
