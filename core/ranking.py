"""How good is this clip, against every other clip?

The watcher's job used to end at "is this a moment". With the hourly gate gone
it does not: everything that clears the bar gets cut, so the question that
matters is no longer whether a clip is worth having but *which* clip is worth
having, and that is a different question with a different shape.

"Is this a moment" is a threshold. "Which is better" is an ordering, and an
ordering needs every axis at once, because the axes trade against each other.
A huge reaction on a small stream and a mild one on a big stream are both
plausible answers and the only way to compare them is to say, out loud and in
numbers, how much an audience is worth against how much a reaction is worth.

Five things decide it, and they are five because each one can be strong while
the others are weak, and each combination is a different kind of clip:

* **What happened** - the sensed evidence. Laughter, a raised voice, a surge of
  motion, a face changing. This is the event.
* **How people took it** - the reaction. Chat speeding up, chat asking for a
  clip, the mood swinging away from the channel's own baseline.
* **Whether it reads** - the production. A clip nobody can follow is not a
  clip: faces on screen, audible speech, a shot that is not chaos, a length
  that is not a fragment.
* **Who saw it** - the reach. Compressed hard, because a moment in front of
  forty thousand people is worth more than the same moment in front of four
  thousand, but not ten times more.
* **What a model made of it** - the verdict. It is the only judgement here made
  by something that actually watched, so it is weighted like one, and a clip
  it refused is not ranked at all.

Every part is 0..1 before it is weighted, and every part is kept. A number with
no breakdown is the one thing this project has refused to produce from the
beginning, and a ranking is exactly where that temptation is strongest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: What a clip is worth, before anything is taken off it.
#:
#: Only three things, and all three vary between two clips of the same stream.
#: That is the whole test for belonging here: a hundred clips off one channel
#: in an hour all scored within a point or two of each other, because 21 of
#: their 100 points came from production and reach - and a clip of people
#: talking is *perfectly* produced, while reach is the channel's viewer count
#: and is identical for every clip it will ever make. Two fifths of the score
#: was a constant, which is two fifths of the score saying nothing.
#:
#: The event outweighs everything because it is the only part that says
#: something happened. The verdict is next because it is the only opinion
#: formed by something that watched the video. The reaction is last because a
#: reaction to nothing is what chat does all day.
MERIT: dict[str, float] = {
    "event": 50.0,
    "verdict": 30.0,
    "reaction": 20.0,
}

#: ...and what can be taken off it. These do not add.
#:
#: Production and reach are floors, not contributions. Being watchable is not
#: an achievement - it is the absence of a fault - and a clip that is hard to
#: follow, or that happened in front of nobody, should lose something rather
#: than a normal one gaining something. So each is a multiplier that starts at
#: the floor below and reaches 1.0, and neither can ever push a clip up.
PENALTY_FLOOR: dict[str, float] = {
    #: An unwatchable clip of a huge moment keeps half its merit.
    "production": 0.5,
    #: A moment in front of nobody is still a moment; it is worth less, not
    #: nothing, and the bot did not earn the audience either way. Kept narrow
    #: for that reason: on a strong clip the whole span from 900 viewers to
    #: 43,000 is worth about six points, which is a tiebreak rather than a
    #: ranking.
    "reach": 0.75,
}

#: Kept so the page can still weight the parts it draws. Penalties are shown
#: at what they can cost, which is what a reader wants to know about them.
WEIGHTS: dict[str, float] = {
    **MERIT,
    "production": 100.0 * (1.0 - PENALTY_FLOOR["production"]),
    "reach": 100.0 * (1.0 - PENALTY_FLOOR["reach"]),
}

#: What a clip keeps when nothing ever looked at it.
#:
#: The verdict is 30 of the 100 merit points, and it used to be scored zero
#: whenever a clip had not been watched - on the reasoning that not having a
#: judgement is a real absence rather than a neutral one. That reasoning is
#: right about a clip and wrong about this bot, because the reason a clip goes
#: unwatched is almost never the clip. It is the day's budget, or the pace, or
#: the model being switched off. Scoring it zero ranks the bot's wallet and
#: calls it a ranking of the clip.
#:
#: What it did in practice: an unwatched clip could not exceed 70 even if
#: everything else about it were perfect, and an ordinary good one - a real
#: event, a chat that reacted, well framed, a big channel - landed on 23. Six
#: hours of watching produced a page of clips scored 17 to 25 and not one
#: above 50, and every one of them was a clip nothing had looked at.
#:
#: So merit is now scored over the parts that could actually be measured, and
#: an unwatched clip keeps this much of it. Less than a verified one, because
#: a judgement from something that saw the video is worth something and its
#: absence should still cost - but a discount, not a hole.
UNWATCHED_KEEPS = 0.9

#: Viewers at which reach counts as full marks. Logarithmic below it, because
#: the step from 1k to 10k matters far more than 40k to 50k.
REACH_FULL = 50_000


@dataclass
class Rank:
    """A clip's score, and every number that went into it."""

    score: float = 0.0
    parts: dict[str, float] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "parts": {k: round(v, 3) for k, v in self.parts.items()},
            "carried_by": self.best_part,
            "detail": self.detail,
        }

    @property
    def best_part(self) -> str:
        """What actually carried this clip - weighted, not the biggest fraction.

        Reach is 0.99 on almost every stream worth watching, so by raw
        fraction it wins nearly every clip and says nothing. What carried a
        clip is what it contributed: weight times fraction.
        """
        if not self.parts:
            return ""
        return max(self.parts, key=lambda name: WEIGHTS.get(name, 0.0) * self.parts[name])


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _biggest(rows: Any, key: str, *, index: int) -> float:
    """The largest value in a list of events, however they were written down.

    The same events are dicts coming out of as_dict() and tuples coming
    straight off a detector, and a ranking that crashes on one of those shapes
    is a ranking that vanishes the moment it is fed a real object instead of
    its JSON.
    """
    best = 0.0
    for row in rows or []:
        try:
            if isinstance(row, dict):
                best = max(best, float(row.get(key, 0) or 0))
            elif isinstance(row, (list, tuple)) and len(row) > index:
                best = max(best, float(row[index] or 0))
        except (TypeError, ValueError):
            continue
    return best


def _soft(value: float, full: float) -> float:
    """0..1, rising fast at first and flattening. Most things saturate."""
    if value <= 0 or full <= 0:
        return 0.0
    return _clamp(1.0 - math.exp(-2.5 * value / full))


def event_score(
    heard: dict[str, Any], seen: dict[str, Any], watched: dict[str, Any]
) -> tuple[float, dict[str, Any]]:
    """What actually happened, 0..1. Heard, seen, and on somebody's face."""
    heard, seen, watched = heard or {}, seen or {}, watched or {}

    strongest = _biggest(heard.get("laughs"), "confidence", index=2)
    loudest = _biggest(heard.get("shouts"), "rise_db", index=1)
    biggest = _biggest(seen.get("surges"), "size", index=1)
    face_move = _biggest(watched.get("reactions"), "size", index=1)

    detail = {
        "laughter": round(strongest, 2),
        "shout_db": round(loudest, 1),
        "motion_surge": round(biggest, 1),
        "face_change": round(face_move, 1),
        "close_ups": len(watched.get("close_ups") or []),
        "gasps": len(heard.get("gasps") or []),
    }

    # Not a sum. Several kinds of evidence at once is the strongest thing a
    # moment can be - a room laughing *and* the picture erupting *and* a face
    # changing is a different event from any one of them - so agreement is
    # rewarded, while one enormous signal alone cannot reach the top.
    kinds = [
        strongest,
        _soft(loudest, 20.0),
        _soft(max(0.0, biggest - 1.0), 4.0),
        _soft(max(0.0, face_move - 1.0), 4.0),
        _soft(len(watched.get("close_ups") or []), 2.0),
    ]
    kinds.sort(reverse=True)
    best = kinds[0]
    agreement = sum(kinds[1:]) / max(len(kinds) - 1, 1)
    detail["kinds"] = sum(1 for k in kinds if k > 0.05)
    # Weighted towards agreement rather than towards the loudest thing. At
    # 0.72/0.28 a single motion surge - the shape a phone carried down a
    # street makes all evening - reached 0.46 of this axis on its own, which
    # on a hundred clips of nothing was most of what separated them.
    return _clamp(0.5 * best + 0.5 * agreement * 2.0), detail


def reaction_score(
    mood: dict[str, Any], chat: dict[str, Any], said: dict[str, Any]
) -> tuple[float, dict[str, Any]]:
    """How the room took it, 0..1. Against the channel's own normal."""
    mood, chat, said = mood or {}, chat or {}, said or {}

    lift = float(mood.get("lift") or 0.0)
    background = bool(mood.get("background"))
    agreement = float(mood.get("confidence") or 0.0)
    burst = float(chat.get("burst_ratio") or 0.0)
    requests = int(chat.get("clip_requests") or 0)
    per_min = float(chat.get("per_minute") or 0.0)

    detail = {
        "mood": mood.get("dominant") or "",
        "mood_lift": round(lift, 2),
        "background_mood": background,
        "agreement": round(agreement, 2),
        "emotive_lines": int(mood.get("emotive_lines") or 0),
        "burst_ratio": round(burst, 1),
        "clip_requests": requests,
        "messages_per_min": round(per_min, 1),
        "reacted_aloud": bool(said.get("reactions")),
    }

    # A mood that is the channel's wallpaper counts for nothing however
    # unanimous it looks - this is the "100% agreement" that meant nothing.
    feeling = 0.0 if background else _soft(max(0.0, lift - 1.0), 3.0) * agreement
    speed = _soft(max(0.0, burst - 1.0), 6.0)
    asked = _soft(requests, 4.0)
    aloud = 1.0 if said.get("reactions") else 0.0

    return _clamp(0.34 * feeling + 0.30 * speed + 0.24 * asked + 0.12 * aloud), detail


def production_score(
    heard: dict[str, Any], seen: dict[str, Any], watched: dict[str, Any],
    *, duration_s: float = 0.0,
) -> tuple[float, dict[str, Any]]:
    """Whether the clip reads at all, 0..1.

    The axis that stops a huge moment filmed badly outranking a good one
    filmed well. A clip nobody can follow is not a clip, however loud it was.
    """
    heard, seen, watched = heard or {}, seen or {}, watched or {}

    speech = float(heard.get("speech_share") or 0.0)
    music = float(heard.get("music_share") or 0.0)
    on_screen = float(watched.get("on_screen") or 0.0)
    biggest = float(watched.get("biggest_face") or 0.0)
    still_s = float(seen.get("still_s") or 0.0)
    cuts = len(seen.get("cuts") or [])
    span = float(seen.get("duration_s") or duration_s or 1.0)

    detail = {
        "speech_share": round(speech, 2),
        "music_share": round(music, 2),
        "face_on_screen": round(on_screen, 2),
        "biggest_face": round(biggest, 3),
        "dead_air_s": round(still_s, 1),
        "cuts": cuts,
        "length_s": round(duration_s, 1),
    }

    # Somebody on screen, somebody audible, not mostly dead air, not a strobe,
    # and long enough to be watched but not so long it is a video.
    people = _clamp(on_screen * 1.6) * 0.5 + _soft(biggest, 0.12) * 0.5
    audible = _clamp(speech * 2.0) * (1.0 - 0.4 * _clamp(music))
    alive = 1.0 - _clamp(still_s / max(span, 1.0))
    calm = 1.0 - _clamp(max(0.0, cuts - 4) / 12.0)
    length = _clamp((duration_s - 8.0) / 12.0) * _clamp((62.0 - duration_s) / 12.0) \
        if duration_s else 0.6

    return _clamp(
        0.30 * people + 0.28 * audible + 0.18 * alive + 0.12 * calm + 0.12 * length
    ), detail


def reach_score(viewers: int) -> tuple[float, dict[str, Any]]:
    """How many people it happened in front of, 0..1, compressed hard."""
    viewers = max(int(viewers or 0), 0)
    if viewers <= 0:
        return 0.0, {"viewers": 0}
    # Logarithmic: 1k to 10k is a real difference, 40k to 50k is not.
    found = math.log10(1 + viewers) / math.log10(1 + REACH_FULL)
    return _clamp(found), {"viewers": viewers}


def verdict_score(verdict: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """What the model that watched it made of it, 0..1.

    An unwatched clip scores zero here rather than being excused. It is the
    only judgement in the ranking formed by something that saw the video, and
    not having one is a real absence, not a neutral one.
    """
    verdict = verdict or {}
    if not verdict.get("watched"):
        return 0.0, {"watched": False}
    confidence = float(verdict.get("confidence") or 0.0)
    worth = bool(verdict.get("worth_it"))
    kind = str(verdict.get("kind") or "")
    detail = {"watched": True, "worth_it": worth, "confidence": round(confidence, 2),
              "kind": kind, "happening": verdict.get("happening") or ""}
    detail["setting"] = verdict.get("setting") or ""
    detail["faces"] = [
        f.get("expression") for f in (verdict.get("faces") or []) if f.get("expression")
    ]
    if not worth:
        return 0.0, detail
    # "nothing" is a contradiction with worth_it, and worth distrusting.
    if kind == "nothing":
        return _clamp(confidence * 0.3), detail
    # A clip where the model could read an expression off somebody's face is a
    # clip about a person, which is what a short vertical clip is for.
    read_a_face = 1.0 if detail["faces"] else 0.0
    return _clamp(confidence * (0.88 + 0.12 * read_a_face)), detail


def from_stored(catch: Any) -> dict[str, Any]:
    """A stored catch, in the shape rank() reads.

    The evidence is kept on every row for exactly this: the weights change,
    and a queue ranked by rules that no longer exist tells you about the old
    rules rather than about the clips. Re-ranking has to be possible from what
    is in the database, without the video, which is long gone.
    """
    evidence = getattr(catch, "evidence", None) or {}
    return {
        "senses": evidence,
        "chat": evidence.get("chat") or {},
        "said": evidence.get("said") or {},
        "mood": getattr(catch, "mood", None) or {},
        "duration_s": getattr(catch, "duration_s", None) or 0.0,
        "peak_viewers": getattr(catch, "peak_viewers", None) or 0,
        "verdict": getattr(catch, "verdict", None) or {},
    }


def rank(record: dict[str, Any]) -> Rank:
    """Score one clip against every other, and show the working.

    `record` is a catch as the supervisor builds it, so this can be run on a
    fresh clip or on a stored one and give the same answer - which matters,
    because the Clips page re-ranks history whenever the weights change.
    """
    senses = record.get("senses") or {}
    heard = record.get("heard") or senses.get("heard") or {}
    seen = record.get("seen") or senses.get("seen") or {}
    watched = record.get("watched_faces") or senses.get("faces") or {}

    parts: dict[str, float] = {}
    detail: dict[str, Any] = {}

    parts["event"], detail["event"] = event_score(heard, seen, watched)
    parts["reaction"], detail["reaction"] = reaction_score(
        record.get("mood") or {}, record.get("chat") or {}, record.get("said") or {}
    )
    parts["production"], detail["production"] = production_score(
        heard, seen, watched, duration_s=float(record.get("duration_s") or 0.0)
    )
    parts["reach"], detail["reach"] = reach_score(record.get("peak_viewers") or 0)
    parts["verdict"], detail["verdict"] = verdict_score(record.get("verdict") or {})

    # Merit first, then what comes off it. A penalty can only ever reduce, so
    # a clip cannot climb the list by being well lit.
    #
    # Scored over the parts that could be measured. Only the verdict can be
    # missing - the rest come off the clip itself - so when nothing watched
    # it, the event and the reaction are scored out of the whole 100 between
    # them and the result is discounted. See UNWATCHED_KEEPS.
    measured = dict(MERIT)
    if not (record.get("verdict") or {}).get("watched"):
        del measured["verdict"]
    spread = sum(MERIT.values()) / sum(measured.values())
    merit = sum(measured[name] * parts[name] for name in measured) * spread
    if "verdict" not in measured:
        merit *= UNWATCHED_KEEPS
        detail.setdefault("verdict", {})["unwatched_keeps"] = UNWATCHED_KEEPS
    kept = 1.0
    for name, floor in PENALTY_FLOOR.items():
        factor = floor + (1.0 - floor) * parts[name]
        detail.setdefault(name, {})["keeps"] = round(factor, 3)
        kept *= factor
    score = merit * kept
    detail["merit"] = round(merit, 1)
    detail["kept"] = round(kept, 3)

    # A clip the model watched and refused is not ranked, it is rejected. The
    # ordering is over clips worth posting; something that failed the gate is
    # not at the bottom of that list, it is not on it.
    judged = record.get("verdict") or {}
    if judged.get("watched") and not judged.get("worth_it"):
        score = 0.0
        detail["rejected"] = judged.get("why") or "the model said no"
    elif parts["event"] <= 0.0:
        # Nothing was heard or seen. The same rule that stops chat nominating
        # a moment has to stop it ranking one, or a busy channel with a
        # generous audience floats to the top of a list of nothing.
        score = 0.0
        detail["rejected"] = "nothing was heard or seen"

    return Rank(score=round(score, 2), parts=parts, detail=detail)
