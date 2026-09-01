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

#: How far either side of the trigger to look for the moment itself.
REACTION_MAX_S = 40.0
#: How far above the stretch's own normal counts as "this is the loud part",
#: and how long it has to hold to be a moment rather than a word.
#:
#: The model this replaces was "the reaction decays back to baseline", and it
#: does not survive contact with a real stream. Plotted, the envelope around a
#: real moment runs: -19, -43, -43, -22, +4, +4, +1, -5, -6, -48, -14, -11, -8.
#: The moment is the +4 run. There is no decay - the signal oscillates by
#: fifty decibels continuously, because that is what conversation looks like -
#: so "wait for it to settle" fired within a second of every trigger and every
#: clip collapsed onto the minimum length.
#:
#: A moment is a *stretch* that is loud, not a point followed by a decay. So
#: find the stretch.
LOUD_OVER_DB = 8.0
LOUD_MIN_S = 1.0
#: ...but eight decibels is only available on a stream that has eight
#: decibels, and a produced upload does not.
#:
#: Measured over two videos of the same streamer. The raw stream sits at a
#: median of -32 dBFS with its 95th percentile at -10: 22 dB between ordinary
#: talking and a reaction, and the eight-decibel bar picks those reactions out
#: cleanly - 81 loud stretches over 28 minutes. The uploaded, loudness-
#: normalised cut of a different session sits at a median of -14 with its 95th
#: at -8. Six decibels of headroom in total, so median-plus-eight lands above
#: the 99th percentile of the entire file and *nothing* clears it: zero loud
#: stretches in fourteen and a half minutes, and all sixteen clips fell back
#: to "there is nothing to end on" and grew to the minimum length.
#:
#: So the bar is eight decibels or half the window's own dynamic range,
#: whichever is smaller. Half, and measured against the 95th percentile rather
#: than the peak, because both of those were checked rather than chosen: at
#: half, every window of the raw stream still pins at the full eight decibels
#: and its clips do not move at all, while the compressed one gets a bar about
#: three decibels over its median and finds its moments again.
#:
#: A percentile of the window was tried first and is wrong. It collapses on
#: anything with two levels - quiet room, loud reaction, quiet room - because
#: when four fifths of the window sits at one level the seventieth percentile
#: *is* the median, the bar drops to it, and the whole window reads as loud.
LOUD_SPREAD_SHARE = 0.5
#: ...and a floor under that, because half of nothing is nothing.
#:
#: A window with no dynamic range at all - a level room, a constant hum - has
#: a spread of zero, so half of it is zero, so the bar lands exactly on the
#: median and every frame at or above the median reads as loud. Thirty seconds
#: of an unchanging room came back as one thirty-second moment. Two decibels
#: is below anything a compressed mix needs (the measured one wanted three)
#: and above the noise of a room where nothing is happening.
LOUD_OVER_MIN_DB = 2.0
#: The top of the range, taken off the loudest twentieth rather than the
#: single loudest frame - one consonant should not define how much room a
#: stream has.
LOUD_RANGE_TOP = 0.95
#: Two loud stretches closer together than this are one moment with a breath
#: in the middle.
LOUD_JOIN_S = 1.8
#: How far from the trigger a loud stretch may be and still be *this* moment.
#:
#: Without a limit the nearest stretch was taken however far away it was, and
#: on four of seventeen real clips that was a different moment entirely: the
#: clip was built around a loud run twenty seconds off, so it opened long
#: before its own trigger and the thing the sensors nominated was somewhere in
#: the back half. Past this the trigger is treated as the moment on its own.
LOUD_NEAR_S = 6.0
#: Once it has settled, run on to the end of whatever is being said, up to
#: this much. This is the "and then he says the thing that makes it" clause -
#: cutting on the instant the laugh stops truncates the best line in the clip.
RESOLVE_MAX_S = 6.0
#: A beat of air at the end, so it does not stop dead on a word.
TAIL_S = 0.6

#: Breathing room either side of the loud part.
#:
#: The stretch finder marks where the moment is *loud*, which is tighter than
#: where the moment is. Cut to the stretch exactly and the clips are correct
#: and feel clipped short - you arrive mid-reaction and leave before anyone has
#: finished responding to it. Five seconds each side, asked for after watching
#: seventeen of them.
#:
#: Not the same thing as the 22-second lead this file exists to undo. That was
#: 22 seconds before the *trigger*, with the payoff 73% of the way through.
#: This is five seconds around the *whole loud stretch*, which usually already
#: contains the setup - so the moment still lands early.
#: More after than before, because the reaction is the part worth watching and
#: it outlasts the thing that caused it. Asked for after a second viewing:
#: five seconds still felt like leaving before anyone had finished responding.
ROOM_BEFORE_S = 5.0
ROOM_AFTER_S = 7.0
#: How far the room may be nudged to land on a pause instead of mid-word.
ROOM_SNAP_S = 2.0

#: How far the trigger may be moved to land on the loudest part of the moment.
#:
#: The sensors say roughly where something happened; they are not precise about
#: when it peaked, and they do not have to be. But every boundary here is
#: measured *outwards from the trigger*, so a trigger sitting on the far side
#: of the reaction has nothing left to decay: measured on real video, triggers
#: landed anywhere from 3 dB below the stream's median loudness to 33 dB above
#: it, and the ones that landed late produced clips that ended immediately and
#: were padded up to the minimum length.
#:
#: So the trigger is nudged to the loudest moment near it before anything else
#: is decided. Four seconds: enough to find the peak of a reaction the sensors
#: pointed at, too little to wander off to a different one.
SNAP_S = 4.0

#: What a clip may be, at the outside.
#:
#: The floor is fifteen because that is where the guides put it and because
#: the loud part of a real moment is short - three to five seconds, since
#: conversation is bursty rather than sustained. A clip that is only its loud
#: part is a fragment with no setup and no landing. When the moment comes out
#: shorter than this the clip is grown outwards to the pauses either side,
#: which is the difference between giving it room and padding it: the extra
#: seconds are whole sentences, not an arbitrary number tacked on the end.
MIN_CLIP_S = 20.0
MAX_CLIP_S = 59.0

#: A dip this far below the local speech level, held this long, is a gap
#: between sentences rather than the gap between two words.
PAUSE_DEPTH_DB = 6.0
PAUSE_HOLD_S = 0.22

#: How much of the envelope to smooth before comparing any part of it to any
#: other part.
#:
#: Loudness per frame swings enormously - a single frame of a consonant can be
#: forty decibels above the median of the sentence around it. Comparing a
#: *peak* against a *median*, which is what this did, is not a comparison: one
#: loud frame put the "settled" line twenty decibels above ordinary speech, the
#: level fell under it within a second, and every clip ended immediately and
#: was padded back up to the eight-second minimum. Seventeen clips, nine of
#: them exactly eight seconds long, all of them cut in the same wrong place.
#:
#: Half a second, so a syllable cannot be a peak and a breath cannot be a
#: pause, but a laugh still is one.
SMOOTH_S = 0.5


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
    """(loudness per frame, seconds per frame), raw.

    Deliberately not smoothed here. The two things this module measures work
    at different timescales and one smoothing serves neither: finding a gap
    between sentences needs the fine signal, because half a second of blur is
    longer than the gap it is looking for and erases it, while comparing how
    loud a reaction got against how loud the room was needs the coarse one, or
    a single consonant counts as the peak. So the envelope is smoothed at the
    point of use, at the width that use requires.
    """
    return list(getattr(heard, "level_db", []) or []), float(
        getattr(heard, "window_s", 0.0) or 0.0
    )


def _smooth(values: list[float], span: int) -> list[float]:
    """A moving average, so peaks and baselines are the same kind of number."""
    if span <= 1:
        return values
    out, run = [], 0.0
    half = span // 2
    for i in range(len(values)):
        lo, hi = max(0, i - half), min(len(values), i + half + 1)
        run = sum(values[lo:hi]) / (hi - lo)
        out.append(run)
    return out


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


def stretches(
    envelope: list[float], step: float, *, over: tuple[float, float]
) -> list[tuple[float, float]]:
    """The loud runs in a span: (start, end) for each.

    Loud against the span's own median, so this works on a shouting streamer
    and a quiet one alike, and held long enough that a single emphatic word is
    not a moment.
    """
    lo, hi = _at(envelope, step, over[0]), _at(envelope, step, over[1])
    if hi - lo < 2:
        return []
    here = sorted(envelope[lo:hi])
    median = here[len(here) // 2]
    loudest = here[min(len(here) - 1, int(len(here) * LOUD_RANGE_TOP))]
    over = min(LOUD_OVER_DB, LOUD_SPREAD_SHARE * (loudest - median))
    bar = median + max(LOUD_OVER_MIN_DB, over)
    need = max(1, int(LOUD_MIN_S / step))

    runs: list[tuple[float, float]] = []
    run = 0
    for i in range(lo, hi):
        if envelope[i] >= bar:
            run += 1
        else:
            if run >= need:
                runs.append(((i - run) * step, i * step))
            run = 0
    if run >= need:
        runs.append(((hi - run) * step, hi * step))

    # A breath in the middle of a laugh is not the end of the laugh.
    joined: list[tuple[float, float]] = []
    for begin, finish in runs:
        if joined and begin - joined[-1][1] <= LOUD_JOIN_S:
            joined[-1] = (joined[-1][0], finish)
        else:
            joined.append((begin, finish))
    return joined


def find(heard, trigger_s: float, *, span_s: float) -> Bounds:  # noqa: ANN001
    """The edges of the clip around `trigger_s`.

    Walks outwards from the trigger: back to the start of the sentence that
    sets it up, forward until the reaction has settled and whatever was being
    said afterwards has finished.
    """
    levels, step = _levels(heard)
    why: dict[str, Any] = {}

    if levels and step > 0:
        # Land on the peak of the reaction before measuring outwards from it.
        rough = trigger_s
        envelope = _smooth(levels, max(1, int(SMOOTH_S / step)))
        lo = _at(levels, step, max(0.0, trigger_s - SNAP_S))
        hi = _at(levels, step, min(span_s, trigger_s + SNAP_S))
        if hi > lo:
            trigger_s = (lo + max(range(hi - lo), key=lambda i: envelope[lo + i])) * step
        if abs(trigger_s - rough) > 0.25:
            why["moved_to_the_peak_by"] = round(trigger_s - rough, 2)

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

    # --- the end: after the loud part of the moment has finished ------------
    look_to = min(span_s, trigger_s + REACTION_MAX_S)
    # Smoothed for this, and only for this: comparing how loud a moment got
    # against how loud the room is needs both sides to be the same kind of
    # number, or a single consonant is the peak.
    envelope = _smooth(levels, max(1, int(SMOOTH_S / step)))
    look_from = max(0.0, trigger_s - REACTION_MAX_S)
    runs = stretches(envelope, step, over=(look_from, look_to))

    def away(run: tuple[float, float]) -> float:
        if run[0] <= trigger_s <= run[1]:
            return 0.0
        return min(abs(run[0] - trigger_s), abs(run[1] - trigger_s))

    here = [r for r in runs if r[0] - 1.0 <= trigger_s <= r[1] + 1.0]
    nearest = min(runs, key=away) if runs else None
    if here:
        moment = here[0]
        why["ends_on"] = "the end of the loud part"
    elif nearest is not None and away(nearest) <= LOUD_NEAR_S:
        moment = nearest
        why["ends_on"] = "the end of the loud part near it"
    else:
        # Everything loud belongs to some other moment. Build the clip around
        # the trigger rather than around a stretch that has nothing to do
        # with it.
        moment = (trigger_s, trigger_s)
        why["ends_on"] = "nothing loud to end on"

    # Room either side of the loud part, then nudged onto a pause so the clip
    # opens and closes on a sentence rather than mid-word. The moment may well
    # start before the trigger - the sensors point at a reaction, and a
    # reaction is the back half of the thing.
    want = max(0.0, moment[0] - ROOM_BEFORE_S)
    near = [
        t for t in pauses(levels, step, over=(max(0.0, want - ROOM_SNAP_S),
                                              min(moment[0], want + ROOM_SNAP_S)))
    ]
    start = min(start, min(near, key=lambda t: abs(t - want)) if near else want)

    end = min(span_s, moment[1] + ROOM_AFTER_S)
    after = [
        t for t in pauses(levels, step, over=(end, min(span_s, end + ROOM_SNAP_S)))
        if t > end
    ]
    if after:
        end = after[0]
    why["loud_from"] = round(moment[0], 1)
    why["loud_to"] = round(moment[1], 1)

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
    # Too short: grow it outwards to the pauses either side rather than adding
    # seconds. A moment that lasted four seconds still needs the sentence that
    # set it up and the one that lands it.
    if end - start < MIN_CLIP_S:
        before = [t for t in pauses(levels, step, over=(max(0.0, start - 12.0), start))
                  if t < start]
        after = [t for t in pauses(levels, step, over=(end, min(span_s, end + 12.0)))
                 if t > end]
        why["grown"] = "the moment was shorter than a clip"
        while end - start < MIN_CLIP_S and (before or after):
            room_before = start - (before[-1] if before else start)
            room_after = (after[0] if after else end) - end
            if before and (room_before <= room_after or not after):
                start = before.pop()
            elif after:
                end = after.pop(0)
            else:
                break
    end = min(span_s, max(end, start + MIN_CLIP_S))
    if end - start > MAX_CLIP_S:
        end = start + MAX_CLIP_S
        why["trimmed"] = "hit the maximum length"

    return Bounds(start, end, trigger_s, why)
