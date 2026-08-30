"""What is being said, word by word, while it is being said.

Until now the bot was deaf until the very end. It transcribed a candidate
*after* deciding to cut it, which means a moment made of words - a deadpan
line, a confession, someone saying something they should not have - could
never become a candidate in the first place. Nothing nominated it, because
nothing had heard it.

The words were always there. All three transcription providers return
timestamps per word, not per sentence; the pipeline was throwing them away and
keeping the paragraph. So this holds them instead: a rolling log of what was
said and when, on the same clock as everything else, expiring the same way
chat does.

The cost is the reason this is not simply left on. Transcribing three streams
continuously is 4,300 stream-minutes a day, which at any provider's rate is
more than the whole budget for everything else put together. So it runs on the
same timer the ear does, only over windows where the ear can hear somebody
talking, and against a daily ceiling in minutes. A stream playing music to an
empty chair is not transcribed, and neither is anything once the day's minutes
are gone.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Said:
    """One word, and when it was said."""

    word: str
    at_s: float
    end_s: float = 0.0
    confidence: float = 1.0


@dataclass
class SpeechLog:
    """The last few minutes of what was said, in order, expiring at the edge."""

    window_s: float = 300.0
    words: list[Said] = field(default_factory=list)
    minutes_spent: float = 0.0

    def extend(self, words: list[Said]) -> None:
        if not words:
            return
        # Windows overlap by ten seconds, so the same word arrives twice.
        newest = self.words[-1].at_s if self.words else -1.0
        self.words.extend(w for w in words if w.at_s > newest)
        self.words.sort(key=lambda w: w.at_s)
        self.forget()

    def forget(self) -> None:
        if not self.words:
            return
        edge = self.words[-1].at_s - self.window_s
        self.words = [w for w in self.words if w.at_s >= edge]

    def between(self, low: float, high: float) -> list[Said]:
        return [w for w in self.words if low <= w.at_s <= high]

    def text_around(self, at_s: float, window_s: float = 12.0) -> str:
        return " ".join(w.word for w in self.between(at_s - window_s, at_s + window_s))

    def status(self) -> dict[str, Any]:
        return {
            "words": len(self.words),
            "held_s": round(
                (self.words[-1].at_s - self.words[0].at_s) if len(self.words) > 1 else 0.0, 1
            ),
            "minutes_spent": round(self.minutes_spent, 1),
            "recent": " ".join(w.word for w in self.words[-40:]),
        }


#: What a person says when something has just happened to them. First-person
#: and present-tense, which is what separates it from chat: the streamer
#: reacting is evidence, an audience typing is an opinion about evidence.
REACTIONS = re.compile(
    r"\b(oh my god|oh my days|what the|no way|are you serious|you're joking|"
    r"you are joking|hold on|wait what|did (?:you|he|she|they) (?:just|see)|"
    r"i can'?t breathe|i'm dead|shut up|stop it|bro what|what just happened|"
    r"oh my|jesus|christ|holy|damn|nah nah nah|no no no)\b",
    re.IGNORECASE,
)


def reactions(words: list[Said], *, grid_s: float = 1.0) -> list[tuple[float, float]]:
    """(time, strength) where the person on camera reacted out loud.

    Deliberately a short list of things people say at the moment of surprise,
    not a sentiment model. The point is not to understand the sentence - the
    model that watches the clip does that - it is to notice, cheaply and
    immediately, that something happened to somebody.
    """
    if not words:
        return []
    found: list[tuple[float, float]] = []
    for i, word in enumerate(words):
        # A few words either side, because these are phrases.
        phrase = " ".join(w.word for w in words[max(0, i - 2) : i + 3])
        if not REACTIONS.search(phrase):
            continue
        at = round(word.at_s / grid_s) * grid_s
        # "oh my god oh my god oh my god" is one reaction, not three, and it
        # takes about three seconds to say.
        if found and at - found[-1][0] < 4.0:
            found[-1] = (found[-1][0], min(1.0, found[-1][1] + 0.25))
        else:
            found.append((at, 0.5))
    return found


def transcribe_window(path: Path | str, *, offset_s: float = 0.0) -> list[Said]:
    """Words out of one window of audio, placed on the caller's clock.

    Word timestamps come back relative to the file; `offset_s` is where the
    file sits on the timeline everything else is measured against.
    """
    from core import transcription

    found = transcription.transcribe(path)
    out: list[Said] = []
    for row in found.get("words") or []:
        try:
            at = float(row.get("s", 0.0))
        except (TypeError, ValueError):
            continue
        text = str(row.get("w") or "").strip()
        if not text:
            continue
        out.append(
            Said(
                word=text,
                at_s=offset_s + at,
                end_s=offset_s + float(row.get("e", at) or at),
                confidence=float(row.get("c", 1.0) or 1.0),
            )
        )
    return out


def spent_today(log_: SpeechLog, *, day_started: float) -> float:
    """Minutes of audio transcribed since the day began."""
    return log_.minutes_spent if time.time() - day_started < 86400 else 0.0
