"""Live chat as the retention curve Kick does not give you.

YouTube hands out a most-replayed graph. Kick hands out nothing of the kind,
and the obvious reading is that Kick is the poorer source. It is the opposite.

A retention curve is an aggregate of people who did not leave, measured after
the fact, smoothed to about a hundred buckets. Chat is thousands of humans
reacting at the exact second something happens, and saying why. It is causal
rather than correlated, it arrives at full time resolution, and unlike a
retention curve it is *labelled* - when the chat types "CLIP THAT" the
audience has hand-annotated the moment for you.

Three curves come out of here, in ascending order of how much they are worth:

1. **velocity** - messages per second. The crowd getting louder.
2. **bursts** - velocity climbing hard out of its own recent baseline, which
   is what stops a busy channel reading as one continuous highlight.
3. **requests** - people explicitly asking for a clip. Rare, and close to
   ground truth when it happens.

None of this needs a key, a model or a GPU. It is JSON and arithmetic, and it
runs over a ten hour stream in the time it takes to download the JSON.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

TIMEOUT_S = 30.0
#: Kick returns chat in windows around a timestamp; this is the stride used to
#: walk a VOD from start to end.
STEP_S = 60
#: A second is the finest bucket worth keeping. Chat reactions to one event
#: spread over two or three seconds as people finish typing.
BUCKET_S = 1.0

#: The audience labelling the moment for you. Deliberately narrow: "clip" on
#: its own appears in ordinary conversation, and a false positive here is
#: expensive because this signal is weighted highest.
CLIP_REQUEST = re.compile(
    r"\b(clip (?:that|it|this)|clipped|someone clip|clip it up|"
    r"!clip|needs? a clip|clip please)\b",
    re.IGNORECASE,
)

#: Reaction vocabulary, not sentiment. These are the words and emotes people
#: reach for when something just happened, across the shared Twitch/Kick
#: lexicon plus the plain-English equivalents.
REACTIONS = re.compile(
    r"(KEKW|LULW|OMEGALUL|LUL|PogChamp|POGGERS|POG\b|Pog\b|MonkaS|monkaW|"
    r"Sadge|PepeLaugh|EZ Clap|OMG|LMAO|LMFAO|LOL{2,}|WTF|NO WAY|HOLY|"
    r"WHAT\?|HUH\?|SHEESH|BRUH|W\b|L\b)",
    re.IGNORECASE,
)


class ChatError(RuntimeError):
    pass


@dataclass(frozen=True)
class Message:
    """One chat line, placed on the video's own timeline."""

    at_s: float
    text: str
    user: str = ""

    @property
    def is_clip_request(self) -> bool:
        return bool(CLIP_REQUEST.search(self.text))

    @property
    def reactions(self) -> int:
        return len(REACTIONS.findall(self.text))


@dataclass
class Curve:
    """Chat activity bucketed onto the video's timeline."""

    bucket_s: float = BUCKET_S
    duration_s: float = 0.0
    #: messages per bucket
    counts: list[int] = field(default_factory=list)
    #: reaction-word hits per bucket
    reactions: list[int] = field(default_factory=list)
    #: explicit clip requests per bucket
    requests: list[int] = field(default_factory=list)
    #: distinct chatters per bucket - the difference between a crowd reacting
    #: and one person spamming, which raw message count cannot tell apart
    voices: list[int] = field(default_factory=list)

    def time_of(self, index: int) -> float:
        return index * self.bucket_s

    def bursts(
        self, min_ratio: float = 3.0, look_back_s: float = 30.0
    ) -> list[tuple[float, float]]:
        """(time, ratio) where chat speeds up sharply against its own baseline.

        Measured against a rolling median of the preceding half minute rather
        than a fixed threshold, because a channel with 50k viewers idles louder
        than one with 200 and a single global cutoff would only ever find the
        big channel.
        """
        window = max(1, int(look_back_s / self.bucket_s))
        found: list[tuple[float, float]] = []
        for i in range(window, len(self.counts)):
            history = sorted(self.counts[i - window : i])
            baseline = history[len(history) // 2]
            # A dead-quiet baseline would divide every blip into a spike.
            floor = max(baseline, 1)
            ratio = self.counts[i] / floor
            if ratio >= min_ratio and self.counts[i] >= 5:
                found.append((round(self.time_of(i), 2), round(ratio, 2)))
        return _collapse(found, within_s=5.0)

    def clip_requests(self) -> list[tuple[float, int]]:
        """(time, count) where chat asked for a clip. The strongest signal here."""
        return [
            (round(self.time_of(i), 2), n) for i, n in enumerate(self.requests) if n
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "bucket_s": self.bucket_s,
            "duration_s": round(self.duration_s, 1),
            "messages": sum(self.counts),
            "buckets": len(self.counts),
            "bursts": self.bursts(),
            "clip_requests": self.clip_requests(),
        }


def _collapse(found: list[tuple[float, float]], *, within_s: float) -> list[tuple[float, float]]:
    """One event per reaction, keeping the strongest of each cluster."""
    kept: list[tuple[float, float]] = []
    for time, value in sorted(found, key=lambda x: -x[1]):
        if all(abs(time - other) > within_s for other, _ in kept):
            kept.append((time, value))
    return sorted(kept)


def build_curve(
    messages: list[Message], duration_s: float, *, bucket_s: float = BUCKET_S
) -> Curve:
    """Bucket messages onto the timeline. Empty stretches stay in, as zeros."""
    buckets = max(1, int(duration_s / bucket_s) + 1)
    curve = Curve(bucket_s=bucket_s, duration_s=duration_s)
    curve.counts = [0] * buckets
    curve.reactions = [0] * buckets
    curve.requests = [0] * buckets
    speakers: list[set[str]] = [set() for _ in range(buckets)]

    for message in messages:
        index = int(message.at_s / bucket_s)
        if not 0 <= index < buckets:
            continue
        curve.counts[index] += 1
        curve.reactions[index] += message.reactions
        if message.is_clip_request:
            curve.requests[index] += 1
        if message.user:
            speakers[index].add(message.user)

    curve.voices = [len(s) for s in speakers]
    log.info(
        "chat: %d messages over %.0fs, %d bursts, %d clip requests",
        sum(curve.counts), duration_s, len(curve.bursts()), len(curve.clip_requests()),
    )
    return curve


# --- Kick ------------------------------------------------------------------

#: The replay endpoint the Kick web player itself calls, and two shapes it has
#: used. Tried in order; the first that answers wins. Written as a list rather
#: than a constant because an undocumented endpoint is a moving target, and a
#: single hard-coded path turns a rename into a dead feature.
KICK_REPLAY_PATHS = (
    "https://kick.com/api/v2/channels/{channel_id}/messages",
    "https://kick.com/api/v1/channels/{channel_id}/messages",
)


def _get(url: str, params: dict[str, Any]) -> Any:
    """A GET that survives Cloudflare, which means looking like a browser."""
    from curl_cffi import requests as cffi

    response = cffi.get(url, params=params, impersonate="chrome", timeout=TIMEOUT_S)
    if response.status_code >= 400:
        raise ChatError(f"HTTP {response.status_code} from {url}")
    return response.json()


def fetch_kick_replay(
    channel_id: str | int,
    started_at: datetime,
    duration_s: float,
    *,
    step_s: int = STEP_S,
    limit: int | None = None,
) -> list[Message]:
    """Walk a Kick VOD's chat replay from start to end.

    `started_at` is when the stream began; every message is converted to an
    offset from it, so the curve lines up with the video file rather than with
    wall-clock time.

    Returns what it managed to collect. A replay that runs out part way through
    is still worth having - the alternative is discarding an hour of signal
    because the last request failed.
    """
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)

    collected: dict[str, Message] = {}
    path = None
    offset = 0.0

    while offset < duration_s:
        moment = started_at + timedelta(seconds=offset)
        params = {"start_time": moment.isoformat().replace("+00:00", "Z")}

        payload = None
        for candidate in ([path] if path else KICK_REPLAY_PATHS):
            try:
                payload = _get(candidate.format(channel_id=channel_id), params)
                path = candidate
                break
            except Exception as exc:  # noqa: BLE001 - a dead path is data, not a crash
                log.debug("chat: %s did not answer: %s", candidate, exc)

        if payload is None:
            if not collected:
                raise ChatError(
                    f"no Kick chat replay endpoint answered for channel {channel_id}. "
                    "The undocumented path may have moved - check KICK_REPLAY_PATHS."
                )
            log.warning("chat: replay stopped at %.0fs of %.0fs", offset, duration_s)
            break

        for raw in _messages_in(payload):
            message = _to_message(raw, started_at)
            if message is not None:
                collected[raw.get("id") or f"{message.at_s}:{message.text}"] = message

        offset += step_s
        if limit and len(collected) >= limit:
            break

    return sorted(collected.values(), key=lambda m: m.at_s)


def _messages_in(payload: Any) -> list[dict[str, Any]]:
    """Find the message list wherever this version of the API put it."""
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("messages", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [m for m in value if isinstance(m, dict)]
        if isinstance(value, dict):
            return _messages_in(value)
    return []


def _to_message(raw: dict[str, Any], started_at: datetime) -> Message | None:
    stamp = raw.get("created_at") or raw.get("timestamp")
    text = raw.get("content") or raw.get("message") or ""
    if not stamp or not text:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)

    sender = raw.get("sender") or {}
    user = sender.get("username", "") if isinstance(sender, dict) else str(sender)
    return Message(
        at_s=round((when - started_at).total_seconds(), 2),
        text=str(text),
        user=str(user),
    )


def quotes_around(messages: list[Message], at_s: float, window_s: float = 6.0) -> list[str]:
    """What chat actually said around a moment - the reason, in their words.

    Worth surfacing next to any candidate: a score says a moment is strong,
    and forty people typing the same three letters says what kind of strong.

    Scans rather than bisects. A binary search would be faster and would be
    silently wrong the moment a caller passes messages in any order but
    ascending time - it returns a plausible-looking window from the wrong part
    of the stream rather than an error. The scan costs nothing next to decoding
    the video and cannot be wrong.
    """
    low, high = at_s - window_s, at_s + window_s
    return [m.text for m in messages if low <= m.at_s < high]
