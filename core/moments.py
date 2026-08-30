"""Fuse every signal into a ranked list of moments, cheapest signal first.

The failure this project kept repeating was asking one signal to do the whole
job. A transcript alone cannot hear a scream. A loudness curve alone cannot
tell a laugh from a door slam. Frames alone cannot tell you anybody cared. Each
is weak; the places where several agree at the same second are not.

So this does two things.

**It orders the work by cost.** Chat is JSON and arithmetic - a ten hour stream
in the time it takes to download. The audio envelope touches every sample at
about thirty times realtime. Frames are the expensive one. Running them in that
order lets the cheap signals throw away 95% of the timeline before anything
expensive is decoded, which is the entire difference between scanning a stream
in minutes and scanning it in hours. It is the same trick as a database using
the cheap index first: the answer is identical, the bill is not.

**It keeps the reasons.** Every moment carries the per-signal contributions
that produced its score, so a bad ranking can be read rather than guessed at.
Every wrong answer this project has produced was expensive to diagnose because
the pipeline threw away its reasoning, and that is not a mistake worth making
twice.

Nothing here needs a model. It is normalisation and weighted sums over curves
other modules already produce.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: The timeline everything is resampled onto. One second is finer than any
#: clip boundary matters and coarse enough that an hour is only 3600 numbers.
GRID_S = 1.0

#: How much each signal is trusted, and why.
#:
#: The ordering is the argument. Chat asking for a clip is an audience
#: hand-labelling the moment, so it outranks everything. A chat burst is a
#: crowd reacting, which beats one microphone getting loud. Audio beats video
#: because a reaction is audible before it is visible, and because scene-cut
#: density on a single-camera stream mostly measures the camera, not the event.
WEIGHTS: dict[str, float] = {
    # Heard and seen. A moment is a thing that happened in front of a camera.
    "laughter": 5.0,
    "motion_surge": 4.0,
    "shout": 3.5,
    "audio_drop": 2.0,
    "flash": 1.0,
    "scene_cuts": 0.75,
    "audio_jump": 1.5,
    # The person on camera reacting out loud. First-hand, so it nominates.
    "said": 3.0,
    # Said. An audience agreeing that something happened, which is worth a
    # great deal as confirmation and nothing at all on its own.
    #
    # These look small next to laughter at 5.0, and they are not: a clip
    # request is painted across the ten seconds before it was typed, because
    # that is how much of the past it could be about, so it accumulates ten
    # buckets where a three-second laugh accumulates three. Weight per bucket
    # is not weight per moment, and the numbers that matter are the totals -
    # roughly 12 for a request against 13.5 for a laugh.
    "chat_request": 1.2,
    "chat_burst": 0.7,
    "heatmap": 3.0,
    # Neither. How busy or loud things are in general.
    "chat_voices": 0.4,
    "audio_energy": 0.5,
}

#: Signals that come from the stream itself - what the bot heard and what it
#: saw. Only these may nominate a moment, because only these are evidence that
#: something happened rather than evidence that people are present.
SENSED = frozenset({
    "laughter", "shout", "audio_drop", "audio_jump", "said",
    "motion_surge", "scene_cuts", "flash",
})

#: What the crowd thinks. Chat is the best confirmation available and the worst
#: possible driver: half of any Kick chat is the channel's own emote pasted
#: forty times, and a clip cut because a lot of people typed is a clip of
#: people typing. It can raise a moment the senses already found and it can
#: rank two of them against each other. It cannot make one.
CROWD = frozenset({"chat_request", "chat_burst", "heatmap"})

#: Kept for the parts of the pipeline that still ask, and for the dashboard.
EVENTS = SENSED | CROWD

#: Signals that are **levels**: always a value, because there is always a
#: loudness and always a number of people talking. A level can corroborate an
#: event and it can rank two events against each other. It must never be the
#: reason a clip exists, and the bug that produced two worthless clips is
#: exactly that: chat_voices, normalised against its own window, put 1.0 on
#: whichever second happened to be busiest and scored 18-38 points on a chat
#: where nothing had happened for five minutes. A level scaled to its own
#: range cannot ever say "nothing here" - there is always a maximum.
LEVELS = frozenset({"chat_voices", "audio_energy"})

#: How much *sensed* evidence a window needs before it counts as a moment at
#: all. In the same units as the score.
MIN_EVENT_SCORE = 1.0


@dataclass
class Moment:
    """A stretch of video worth looking at, and the evidence for it."""

    start_s: float
    end_s: float
    score: float
    #: where inside the window the evidence peaks - the actual instant
    peak_s: float = 0.0
    #: signal name -> what it contributed. The sum is the score.
    why: dict[str, float] = field(default_factory=dict)
    #: what chat was saying, when chat is one of the reasons
    quotes: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def top_reason(self) -> str:
        if not self.why:
            return "unexplained"
        return max(self.why.items(), key=lambda kv: kv[1])[0]

    @property
    def event_score(self) -> float:
        """How much of the score came from something the bot heard or saw."""
        return sum(v for k, v in self.why.items() if k in SENSED)

    @property
    def crowd_score(self) -> float:
        """...and how much from chat agreeing about it afterwards."""
        return sum(v for k, v in self.why.items() if k in CROWD)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_s": round(self.start_s, 2),
            "end_s": round(self.end_s, 2),
            "duration_s": round(self.duration_s, 2),
            "peak_s": round(self.peak_s, 2),
            "score": round(self.score, 3),
            "event_score": round(self.event_score, 3),
            "crowd_score": round(self.crowd_score, 3),
            "top_reason": self.top_reason(),
            "why": {k: round(v, 3) for k, v in sorted(self.why.items(), key=lambda kv: -kv[1])},
            # By frequency, not by arrival. Forty people typing the same three
            # letters is the finding; the first eight lines in list order are
            # whatever the idle chatter happened to be.
            "quotes": [
                {"text": text, "count": n} for text, n in Counter(self.quotes).most_common(6)
            ],
        }


def _grid(duration_s: float, grid_s: float = GRID_S) -> int:
    return max(1, int(duration_s / grid_s) + 1)


def _normalise(values: list[float]) -> list[float]:
    """Scale to 0..1 against this recording's own range.

    Against its own range, deliberately. An absolute threshold tuned on a
    shouting streamer finds nothing on a quiet one, and every signal here
    means "loud for this video", not "loud in general".
    """
    if not values:
        return []
    low, high = min(values), max(values)
    if high <= low:
        return [0.0] * len(values)
    span = high - low
    return [(v - low) / span for v in values]


def _excess(
    values: list[float], *, grid_s: float = GRID_S,
    look_back_s: float = 45.0, cap: float = 3.0, warmup_s: float = 20.0,
) -> list[float]:
    """How far a level sits above its own recent normal, 0..1. Flat means zero.

    This is the difference between "the busiest second in this window" and
    "busier than this channel usually is", and it is the whole reason a level
    signal is safe to include at all. _normalise() answers the first question,
    which always has an answer: whatever the largest value happens to be
    becomes 1.0 even if the curve is dead flat, so the signal nominates a
    winner on every window it is ever shown.

    Measured against a trailing median rather than a mean, because the median
    is not moved by the very spike being measured. The warm-up exists for the
    same reason: with no history there is no normal to be above, and a moment
    found four seconds into a buffer has neither a baseline nor a lead-in.
    """
    size = len(values)
    window = max(1, int(look_back_s / grid_s))
    least = max(1, int(warmup_s / grid_s))
    out = [0.0] * size
    for i in range(least, size):
        history = sorted(values[max(0, i - window) : i])
        if not history:
            continue
        baseline = max(history[len(history) // 2], 1.0)
        ratio = values[i] / baseline
        out[i] = max(0.0, min(1.0, (ratio - 1.0) / (cap - 1.0)))
    return out


def _louder_than_usual(
    rms_db: list[float], *, look_back_s: float = 45.0,
    grid_s: float = GRID_S, over_db: float = 9.0, warmup_s: float = 20.0,
) -> list[float]:
    """Loudness above the recent normal, 0..1. Steady sound scores zero.

    A difference rather than a ratio, because decibels are already a
    logarithm: nine dB over the trailing median is the same amount of "louder"
    at any volume, and a ratio of two negative numbers means nothing at all.
    """
    window = max(1, int(look_back_s / grid_s))
    least = max(1, int(warmup_s / grid_s))
    out = [0.0] * len(rms_db)
    for i in range(least, len(rms_db)):
        history = sorted(rms_db[max(0, i - window) : i])
        if not history:
            continue
        baseline = history[len(history) // 2]
        out[i] = max(0.0, min(1.0, (rms_db[i] - baseline) / over_db))
    return out


def _spread(
    events: list[tuple[float, float]], size: int, *, grid_s: float, before: float, after: float
) -> list[float]:
    """Paint point events onto the grid with a little width either side.

    A reaction is not an instant. Chat takes a couple of seconds to type and a
    laugh runs on, so an event at t is evidence about a short span around t -
    and giving it width is what lets two signals a second apart agree instead
    of landing in neighbouring buckets and cancelling out.
    """
    out = [0.0] * size
    for at_s, weight in events:
        start = max(0, int((at_s - before) / grid_s))
        end = min(size, int((at_s + after) / grid_s) + 1)
        for i in range(start, end):
            out[i] = max(out[i], weight)
    return out


def _from_ranges(
    ranges: list[tuple[float, float]], size: int, *, grid_s: float, weight: float = 1.0
) -> list[float]:
    out = [0.0] * size
    for start, end in ranges:
        for i in range(max(0, int(start / grid_s)), min(size, int(end / grid_s) + 1)):
            out[i] = weight
    return out


def _resample(values: list[float], size: int) -> list[float]:
    """Stretch or squash a curve onto the grid without pretending to interpolate."""
    if not values:
        return [0.0] * size
    if len(values) == size:
        return list(values)
    ratio = len(values) / size
    return [values[min(len(values) - 1, int(i * ratio))] for i in range(size)]


# --- collecting the signals -------------------------------------------------


def signals_from_chat_events(
    *,
    requests: list[float],
    bursts: list[tuple[float, float]],
    voices: list[float],
    voices_grid_s: float = GRID_S,
    duration_s: float,
    grid_s: float = GRID_S,
) -> dict:
    """Chat's curves from events already located on whatever timeline is in use.

    Split out because bursts and requests have to be *found* against the whole
    five minutes chat remembers - a spike is only a spike next to half a minute
    of history - while the scoring window is half a minute long. Detect wide,
    score narrow.
    """
    size = _grid(duration_s, grid_s)
    return {
        # Clip requests point backwards: by the time chat has typed it, the
        # thing they want clipped has already happened. That asymmetry is the
        # whole reason this is a window and not a point.
        "chat_request": _spread(
            [(t, 1.0) for t in requests], size, grid_s=grid_s, before=8.0, after=2.0
        ),
        "chat_burst": _spread(
            [(t, min(1.0, ratio / 10.0)) for t, ratio in bursts],
            size, grid_s=grid_s, before=4.0, after=3.0,
        ),
        # Not _normalise: see _excess. A busy channel is not a moment.
        "chat_voices": _resample(_excess(voices, grid_s=voices_grid_s), size),
    }


def signals_from_chat(curve, messages=None, *, duration_s: float, grid_s: float = GRID_S) -> dict:  # noqa: ANN001
    """Chat's three curves, on the grid, all measured over the same window."""
    out = signals_from_chat_events(
        requests=[t for t, _ in curve.clip_requests()],
        bursts=curve.bursts(),
        voices=[float(v) for v in curve.voices],
        voices_grid_s=curve.bucket_s or grid_s,
        duration_s=duration_s,
        grid_s=grid_s,
    )
    if messages is not None:
        out["_messages"] = messages  # carried through for quoting, not scored
    return out


def signals_from_audio(env, *, duration_s: float, grid_s: float = GRID_S) -> dict:  # noqa: ANN001
    """Loudness jumps and sustained energy, from core.listen."""
    size = _grid(duration_s, grid_s)
    return {
        "audio_jump": _spread(
            [(t, min(1.0, rise / 30.0)) for t, rise in env.jumps()],
            size, grid_s=grid_s, before=1.0, after=3.0,
        ),
        # Loudness above its own recent normal. Music playing over a stream is
        # loud for twenty minutes and is not a moment for any of them.
        "audio_energy": _resample(_louder_than_usual(env.rms_db), size),
    }


def signals_from_hearing(heard, *, duration_s: float, grid_s: float = GRID_S) -> dict:  # noqa: ANN001
    """What was heard, on the grid. See core.hearing for what any of it means."""
    size = _grid(duration_s, grid_s)
    out: dict[str, list[float]] = {}

    # A laugh is a stretch, not an instant, and it carries its own confidence.
    laughs = [0.0] * size
    for start, end, confidence in heard.laughs:
        for i in range(max(0, int(start / grid_s)), min(size, int(end / grid_s) + 1)):
            laughs[i] = max(laughs[i], confidence)
    out["laughter"] = laughs

    # A raised voice points forwards: whatever caused it is already underway.
    out["shout"] = _spread(
        [(t, min(1.0, rise / 20.0)) for t, rise in heard.shouts],
        size, grid_s=grid_s, before=1.5, after=3.0,
    )
    # A room going quiet points the other way - the pause is the setup, and
    # the thing worth watching is what happens next.
    out["audio_drop"] = _spread(
        [(start, 1.0) for start, _ in heard.drops],
        size, grid_s=grid_s, before=1.0, after=4.0,
    )
    return out


def signals_from_watching(seen, *, duration_s: float, grid_s: float = GRID_S) -> dict:  # noqa: ANN001
    """What was seen, on the grid. See core.watching."""
    size = _grid(duration_s, grid_s)
    surges = _spread(
        [(t, min(1.0, (size_ - 1.0) / 4.0)) for t, size_ in seen.surges],
        size, grid_s=grid_s, before=1.5, after=2.5,
    )
    cuts = _spread(
        [(t, 1.0) for t in seen.cuts], size, grid_s=grid_s, before=0.5, after=1.5
    )
    flashes = _spread(
        [(t, 1.0) for t in seen.flashes], size, grid_s=grid_s, before=0.5, after=1.5
    )
    # Nothing moving is the opposite of a moment. Subtracted rather than
    # ignored, so a coincidental cut cannot carry an empty chair.
    dead = _from_ranges(list(seen.stillness), size, grid_s=grid_s, weight=1.0)
    return {
        "motion_surge": [v * (1.0 - d) for v, d in zip(surges, dead, strict=True)],
        "scene_cuts": [v * (1.0 - d) for v, d in zip(cuts, dead, strict=True)],
        "flash": [v * (1.0 - d) for v, d in zip(flashes, dead, strict=True)],
    }


def signals_from_speech(
    spoken: list[tuple[float, float]], *, duration_s: float, grid_s: float = GRID_S
) -> dict:
    """What the person on camera said out loud, on the grid.

    Sensed, not crowd: the streamer reacting is first-hand evidence that
    something happened to them. An audience typing is an opinion about
    evidence, which is why chat cannot nominate a moment and this can.
    """
    size = _grid(duration_s, grid_s)
    return {
        "said": _spread(spoken, size, grid_s=grid_s, before=3.0, after=3.0),
    }


def signals_from_video(found, *, duration_s: float, grid_s: float = GRID_S) -> dict:  # noqa: ANN001
    """Scene-cut density, from core.scan. The weakest of the three, honestly.

    On a single-camera stream this mostly measures the camera moving. It earns
    its small weight by agreeing with the others, not by leading.
    """
    size = _grid(duration_s, grid_s)
    cuts = _spread(
        [(t, 1.0) for t in found.scene_cuts], size, grid_s=grid_s, before=1.0, after=1.0
    )
    # Black and freeze are the opposite of a moment: a dropped stream or an
    # empty chair. Subtracted rather than ignored so they cannot be carried by
    # a coincidental burst elsewhere.
    dead = _from_ranges(
        list(found.blacks) + list(found.freezes), size, grid_s=grid_s, weight=1.0
    )
    return {"scene_cuts": [c * (1.0 - d) for c, d in zip(cuts, dead, strict=True)]}


def signals_from_heatmap(heatmap, *, duration_s: float, grid_s: float = GRID_S) -> dict:  # noqa: ANN001
    """YouTube's most-replayed curve, when the source happens to be YouTube."""
    size = _grid(duration_s, grid_s)
    out = [0.0] * size
    for marker in heatmap or []:
        start = int(float(marker["start_s"]) / grid_s)
        end = int(float(marker["end_s"]) / grid_s) + 1
        for i in range(max(0, start), min(size, end)):
            out[i] = max(out[i], float(marker["value"]))
    return {"heatmap": out}


# --- fusing them ------------------------------------------------------------


def fuse(
    signals: dict[str, list[float]],
    *,
    duration_s: float,
    grid_s: float = GRID_S,
    weights: dict[str, float] | None = None,
) -> tuple[list[float], dict[str, list[float]]]:
    """Weighted sum of every signal, plus the contributions that made it."""
    weights = weights or WEIGHTS
    size = _grid(duration_s, grid_s)
    total = [0.0] * size
    parts: dict[str, list[float]] = {}

    for name, values in signals.items():
        if name.startswith("_") or name not in weights:
            continue
        weight = weights[name]
        curve = _resample(values, size)
        parts[name] = [v * weight for v in curve]
        for i, value in enumerate(parts[name]):
            total[i] += value

    return total, parts


def rank(
    signals: dict[str, list[float]],
    *,
    duration_s: float,
    clip_s: float = 30.0,
    top: int = 10,
    grid_s: float = GRID_S,
    weights: dict[str, float] | None = None,
    messages: list | None = None,
    min_event_score: float = MIN_EVENT_SCORE,
) -> list[Moment]:
    """The best `top` non-overlapping windows of `clip_s`, strongest first.

    Non-overlapping matters more than it sounds: a single big reaction will
    otherwise fill every slot with ten near-identical windows sliding one
    second apart, and a page needs ten different moments, not one moment ten
    times.

    A window with no *sensed* evidence is not returned at all, whatever its
    total. Two rules are folded into that one sentence and both were paid for:

    Levels are always positive - there is always a loudest second and a busiest
    second - so a ranking that lets them stand alone will always return its
    favourite five minutes of nothing, confidently and with a score.

    And chat is not evidence that something happened. It is evidence that
    people are present and typing, which on a big Kick channel they are doing
    at four hundred messages a minute regardless. Chat can raise a moment the
    senses already found; it cannot nominate one.
    """
    total, parts = fuse(signals, duration_s=duration_s, grid_s=grid_s, weights=weights)
    width = max(1, int(clip_s / grid_s))
    if len(total) < width:
        return []

    # The event evidence on its own. The peak is read off this rather than off
    # the total: a level drifting up two seconds later would otherwise pull the
    # peak - and with it the quotes, the clip's centre and its length - away
    # from the thing that actually happened.
    firing = [name for name in parts if name in SENSED]
    events = [sum(parts[name][i] for name in firing) for i in range(len(total))]

    # Prefix sums: every window total in one pass instead of width per window.
    prefix = [0.0]
    for value in total:
        prefix.append(prefix[-1] + value)

    scored = [
        (prefix[i + width] - prefix[i], i)
        for i in range(len(total) - width + 1)
    ]
    scored.sort(key=lambda x: (-x[0], x[1]))

    chosen: list[Moment] = []
    for score, index in scored:
        if score <= 0:
            break
        start_s = index * grid_s
        if any(start_s < m.end_s and m.start_s < start_s + clip_s for m in chosen):
            continue

        why = {
            name: sum(values[index : index + width])
            for name, values in parts.items()
        }
        # Where inside the window the evidence actually peaks. The event is not
        # reliably at the middle or the end - a clip request paints backwards,
        # a loudness jump paints forwards - so this is measured rather than
        # assumed, and it is what the quotes and the thumbnail should follow.
        peak = max(range(index, index + width), key=lambda i: events[i])
        moment = Moment(
            start_s=start_s,
            end_s=min(start_s + clip_s, duration_s),
            peak_s=peak * grid_s,
            score=score,
            why={k: v for k, v in why.items() if v > 0},
        )
        if moment.event_score < min_event_score:
            # Ranked highly on background alone. Skipped rather than broken
            # out of: a later window may hold a real one.
            continue
        if messages is not None and any(k.startswith("chat") for k in moment.why):
            from core.chat import quotes_around

            moment.quotes = quotes_around(messages, moment.peak_s, window_s=6.0)
        chosen.append(moment)
        if len(chosen) >= top:
            break

    log.info(
        "moments: %d candidates from %d signals over %.0fs",
        len(chosen), len(parts), duration_s,
    )
    return chosen


def find(
    video: str,
    *,
    duration_s: float | None = None,
    chat_curve=None,  # noqa: ANN001 - core.chat.Curve, optional
    messages: list | None = None,
    heatmap: list | None = None,
    clip_s: float = 30.0,
    top: int = 10,
    shortlist: int = 30,
    floor_ratio: float = 0.1,
    grid_s: float = GRID_S,
) -> dict[str, Any]:
    """The whole search, cheap signals first.

    Three passes, each one narrowing what the next has to look at:

    1. **Chat** (free, if there is any) and **audio** (~30x realtime over the
       whole file). Together these produce a shortlist.
    2. **Video signals** over the whole file - one ffmpeg decode, still cheap
       relative to sampling frames.
    3. **Frames**, sampled only inside the shortlisted windows. This is the
       expensive step and it now runs over minutes of video rather than hours.

    A ten hour stream at 60fps is 2.16 million frames. Looking at all of them
    is not clever, it is just slow: the answer is the same and the bill is
    hundreds of times larger. Nothing above throws away a moment the frames
    would have found on their own, because a moment with no chat reaction, no
    audio change and no visual change is not a moment.
    """
    from core import listen, scan

    from_scan = scan.signals(video)
    duration_s = duration_s or from_scan.duration_s
    if not duration_s:
        raise ValueError(f"could not determine the duration of {video}")

    signals: dict[str, list[float]] = {}
    used: list[str] = []

    if chat_curve is not None:
        signals |= signals_from_chat(chat_curve, messages, duration_s=duration_s, grid_s=grid_s)
        used.append("chat")

    env = listen.envelope(video)
    signals |= signals_from_audio(env, duration_s=duration_s, grid_s=grid_s)
    used.append("audio")

    signals |= signals_from_video(from_scan, duration_s=duration_s, grid_s=grid_s)
    used.append("video")

    if heatmap:
        signals |= signals_from_heatmap(heatmap, duration_s=duration_s, grid_s=grid_s)
        used.append("heatmap")

    # The shortlist is wider than the answer on purpose: ranking is cheap and
    # being wrong here is unrecoverable, since a window the shortlist drops can
    # never be looked at by the frames.
    candidates = rank(
        signals,
        duration_s=duration_s,
        clip_s=clip_s,
        top=max(top, shortlist),
        grid_s=grid_s,
        messages=messages,
    )

    # A count alone makes a bad shortlist: on a quiet stream the 30th best
    # window is indistinguishable from background, and shortlisting it spends
    # the expensive pass on nothing. Cut against the leader instead, so a
    # stream with one good moment shortlists one window and a stream with
    # thirty shortlists thirty.
    best = candidates[0].score if candidates else 0.0
    shortlisted = [
        m for m in candidates[:shortlist] if m.score >= best * floor_ratio
    ]
    scanned_s = sum(m.duration_s for m in shortlisted)
    log.info(
        "moments: %s - %.0fs of video, %s; frames needed for %.0fs (%.1f%%)",
        video, duration_s, "+".join(used), scanned_s,
        100.0 * scanned_s / duration_s if duration_s else 0.0,
    )

    return {
        "source": video,
        "duration_s": round(duration_s, 1),
        "signals_used": used,
        "grid_s": grid_s,
        "clip_s": clip_s,
        "moments": [m.as_dict() for m in candidates[:top]],
        # What the frames still have to be pointed at, and the saving that buys.
        "shortlist": [
            {"start_s": m.start_s, "end_s": m.end_s} for m in shortlisted
        ],
        "frames_needed_s": round(scanned_s, 1),
        "fraction_of_video": round(scanned_s / duration_s, 4) if duration_s else 0.0,
    }


def moment_end(
    curve,  # noqa: ANN001 - core.chat.Curve
    peak_s: float,
    *,
    min_s: float = 20.0,
    max_s: float = 59.0,
    settle: float = 1.35,
) -> float:
    """How long after the peak the moment is still going, in seconds.

    A fixed length cuts the good ones short and pads the thin ones. What
    actually ends a moment is chat going back to normal, so that is what this
    measures: from the peak, walk forward until the rate has been near its
    own pre-moment baseline for a few seconds running.

    `settle` is a multiple of that baseline rather than an absolute rate,
    because a channel doing 300 messages a minute idles where another one
    peaks. The floor and the ceiling exist because a moment nobody can see the
    start of is not worth posting, and because past a minute it is not a clip.
    """
    counts = curve.counts
    if not counts:
        return min_s
    bucket = curve.bucket_s or 1.0
    peak_i = max(0, min(len(counts) - 1, int(peak_s / bucket)))

    # The baseline is what chat was doing *before* the moment, not including
    # it - a window that swallows the burst calls the burst normal.
    back = max(0, peak_i - int(45.0 / bucket))
    history = sorted(counts[back:peak_i]) or [0]
    baseline = max(history[len(history) // 2], 1)

    calm_for = 0.0
    needed = 3.0
    for i in range(peak_i + 1, len(counts)):
        if counts[i] <= baseline * settle:
            calm_for += bucket
            if calm_for >= needed:
                return max(min_s, min(max_s, (i * bucket) - peak_s))
        else:
            calm_for = 0.0

    # Still going at the edge of what chat remembers: take the cap rather
    # than guessing an end that has not happened yet.
    return max(min_s, min(max_s, (len(counts) * bucket) - peak_s))
