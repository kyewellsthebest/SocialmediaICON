"""Where a clip starts and where it ends.

Finding the moment and framing the moment are different problems, and this
project only ever solved the first. A trigger was located and then a fixed
window was hung around it: `live_lead_s` seconds before, `live_trail_s`
after - 22 and 8. So every clip opened with twenty-two seconds of preamble and
ended eight seconds after the trigger whether anything had resolved or not.
The payoff landed 73% of the way through a clip nobody would still be watching.

The reasoning in the config for that 22 is sound and is about the wrong thing.
Chat reacts *after* the fact, so a moment nominated by chat really did happen
earlier than the nomination - but that is a correction to apply to the
*trigger*, not a reason to open the clip twenty-two seconds early. The two got
conflated and the clip paid for it.

What the people who do this for a living say a clip is - consistently, across
guides on clipping streams for TikTok, Reels and Shorts:

* **Open on the action.** The strongest moment belongs in the first seconds
  because that is where a viewer decides. Cut the head-scratching, the
  headset-adjusting, the "wait, watch this".
* **Setup is short and only as long as it needs to be.** Enough that the
  payoff makes sense - the line that sets up the joke - and no more. If it
  needs a "wait for it" caption the setup was too long, not too short.
* **End on a resolution.** A punchline, a reaction landing, a beat of silence
  after. Clips that stop on a non-moment get abandoned; clips with an ending
  get replayed.
* **Keep what adds to it.** If somebody says something after the moment that
  makes it funnier, that is part of the clip. The end is where the moment
  finishes, not a fixed number of seconds later.
* **15 to 60 seconds**, most comfortably around half a minute.

So a clip is not a window. It is: a short setup, the thing, the reaction, and
the beat where the reaction resolves. This module finds those edges from what
was heard, and the only fixed numbers in it are the bounds of what a clip may
be - everything between them comes from the audio.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: The least setup a clip can have. Something has to precede the trigger or the
#: clip opens mid-syllable, but it is a beat, not a story.
SETUP_MIN_S = 1.2
#: ...and the most. Beyond this the clip is asking a scroller to wait, which
#: is the thing every guide on the subject says not to do. The old lead was
#: 22 seconds - nearly three times this - and it was applied to every clip.
SETUP_MAX_S = 6.0
#: Used when nothing better presents itself: no pause to open on, nothing to
#: cut to. Short on purpose.
SETUP_DEFAULT_S = 2.5

#: How far past the trigger to look for the moment to finish.
REACTION_MAX_S = 40.0
#: How quiet, relative to the reaction's own peak, counts as "settled". Not an
#: absolute level: a loud room settles to loud.
SETTLED_SHARE = 0.35
#: ...and for how long it has to stay settled before the moment is over. A
#: breath between two halves of a laugh is not the end of the laugh.
SETTLED_HOLD_S = 1.1
#: Once it has settled, run on to the end of whatever is being said, up to
#: this much. This is the "and then he says the thing that makes it" clause -
#: cutting on the instant the laugh stops truncates the best line in the clip.
RESOLVE_MAX_S = 6.0
#: A beat of air at the end, so it does not stop dead on a word.
TAIL_S = 0.6

#: What a clip may be, at the outside.
MIN_CLIP_S = 8.0
MAX_CLIP_S = 59.0

#: A dip this far below the local speech level, held this long, is a gap
#: between sentences rather than the gap between two words.
PAUSE_DEPTH_DB = 6.0
PAUSE_HOLD_S = 0.22


@dataclass
class Bounds:
    """Where the clip runs, and why it was cut there."""

    start_s: float
    end_s: float
    trigger_s: float
    why: dict[str, Any] = field(default_factory=dict)

    @property
    def length_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def lead_s(self) -> float:
        """How much setup precedes the trigger. The number that was 22."""
        return self.trigger_s - self.start_s

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_s": round(self.start_s, 2),
            "end_s": round(self.end_s, 2),
            "length_s": round(self.length_s, 2),
            "lead_s": round(self.lead_s, 2),
            "why": self.why,
        }


def _levels(heard) -> tuple[list[float], float]:  # noqa: ANN001 - hearing.Hearing
    """(loudness per frame, seconds per frame)."""
    return list(getattr(heard, "level_db", []) or []), float(
        getattr(heard, "window_s", 0.0) or 0.0
    )


def _at(levels: list[float], step: float, t: float) -> int:
    return max(0, min(len(levels) - 1, int(t / step))) if step > 0 and levels else 0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def pauses(levels: list[float], step: float, *, over: tuple[float, float]) -> list[float]:
    """Times where the talking stops long enough to be between sentences.

    Measured against the local speech level rather than an absolute one,
    because "quiet" on a shouting streamer is louder than "loud" on a calm
    one, and this has to work on both.
    """
    start_s, end_s = over
    lo, hi = _at(levels, step, start_s), _at(levels, step, end_s)
    if hi - lo < 2:
        return []
    speech = _median(levels[lo:hi])
    floor = speech - PAUSE_DEPTH_DB
    need = max(1, int(PAUSE_HOLD_S / step))

    found: list[float] = []
    run = 0
    for i in range(lo, hi):
        if levels[i] <= floor:
            run += 1
        else:
            if run >= need:
                # The *start* of the gap: that is where the previous sentence
                # ended, and where a clip should open or close.
                found.append((i - run) * step)
            run = 0
    if run >= need:
        found.append((hi - run) * step)
    return found


def find(heard, trigger_s: float, *, span_s: float) -> Bounds:  # noqa: ANN001
    """The edges of the clip around `trigger_s`.

    Walks outwards from the trigger: back to the start of the sentence that
    sets it up, forward until the reaction has settled and whatever was being
    said afterwards has finished.
    """
    levels, step = _levels(heard)
    why: dict[str, Any] = {}

    if not levels or step <= 0:
        # Nothing was heard. Fall back to a short setup and an ordinary length,
        # which is still a better shape than the fixed window it replaces.
        why["heard"] = False
        start = max(0.0, trigger_s - SETUP_DEFAULT_S)
        return Bounds(start, min(span_s, start + 30.0), trigger_s, why)

    # --- the start: open on the line that sets it up ------------------------
    window = (max(0.0, trigger_s - SETUP_MAX_S), trigger_s - SETUP_MIN_S)
    gaps = [t for t in pauses(levels, step, over=window) if window[0] <= t <= window[1]]
    if gaps:
        start = gaps[-1]  # the latest one: the closest setup that still makes sense
        why["opens_on"] = "a pause before it"
    else:
        start = max(0.0, trigger_s - SETUP_DEFAULT_S)
        why["opens_on"] = "no pause to open on"
    start = max(0.0, min(start, trigger_s - SETUP_MIN_S))

    # --- the end: after the reaction has landed and settled -----------------
    look_to = min(span_s, trigger_s + REACTION_MAX_S)
    lo, hi = _at(levels, step, trigger_s), _at(levels, step, look_to)
    before = _median(levels[_at(levels, step, start) : lo + 1]) if lo > 0 else 0.0
    peak = max(levels[lo:hi], default=before)

    # "Settled" is measured between what it was before and how big it got, so a
    # small reaction is allowed to settle to a small level.
    settled_at = before + (peak - before) * SETTLED_SHARE
    need = max(1, int(SETTLED_HOLD_S / step))
    end = look_to
    run = 0
    for i in range(lo, hi):
        if levels[i] <= settled_at:
            run += 1
            if run >= need:
                end = (i - run + 1) * step
                why["ends_on"] = "the reaction settling"
                break
        else:
            run = 0
    else:
        why["ends_on"] = "the reaction never settled"

    # ...then run on to the end of whatever is being said. This is the clause
    # that keeps the line after the laugh - the one that adds to it.
    after = [
        t for t in pauses(levels, step, over=(end, min(span_s, end + RESOLVE_MAX_S)))
        if t > end
    ]
    if after:
        end = after[0]
        why["ends_on"] = why.get("ends_on", "") + ", then the next pause"
    end += TAIL_S

    # --- and what a clip may be --------------------------------------------
    end = min(span_s, max(end, start + MIN_CLIP_S))
    if end - start > MAX_CLIP_S:
        end = start + MAX_CLIP_S
        why["trimmed"] = "hit the maximum length"

    why["peak_db"] = round(peak, 1)
    why["before_db"] = round(before, 1)
    return Bounds(start, end, trigger_s, why)
