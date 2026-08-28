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
from collections import deque
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


#: What kind of reaction, not just how much of one.
#:
#: This is the part a loudness curve can never tell you. Forty people typing
#: KEKW and forty typing "NOOOO" produce identical spikes on every numeric
#: signal in this repo, and they are completely different videos - different
#: hook, different caption, different music, different audience. Chat is the
#: only place that distinction is written down.
#:
#: Regex over a fixed vocabulary rather than a sentiment model, deliberately.
#: Chat is not English - it is emotes, single letters and deliberate
#: misspelling - and a general sentiment model reads "L" and "OMEGALUL" as
#: neutral noise while scoring "I love this" as the strongest line in the
#: window. The vocabulary is small, public, and changes slowly.
EMOTIONS: dict[str, re.Pattern[str]] = {
    "funny": re.compile(
        r"(KEKW|LULW|OMEGALUL|LUL\b|PepeLaugh|ICANT|LMAO|LMFAO|"
        r"\bha(?:ha)+\b|\blo+l+\b|\bdying\b|\bcrying\b|\bwheeze)",
        re.IGNORECASE,
    ),
    "shock": re.compile(
        r"(monkaS|monkaW|WTF|OMG|OH MY|NO WAY|NOWAY|WHAT THE|HOLY|"
        r"\bJESUS\b|SHEESH|\bWHAT\?+|HUH\?+|D:|POGGERS|\bWOAH\b|\bWHOA\b)",
        re.IGNORECASE,
    ),
    "hype": re.compile(
        r"(\bPOG\b|PogChamp|LETS ?GO|LFG\b|EZ Clap|\bW\b|GOATED|"
        r"INSANE|CRACKED|\bCLUTCH\b|BANGER)",
        re.IGNORECASE,
    ),
    "sad": re.compile(
        r"(Sadge|PepeHands|FeelsBadMan|\bNO+O+\b|\bnooo|\brip\b|"
        r"\bsorry\b|\bpoor\b|heartbreak|\bF\b)",
        re.IGNORECASE,
    ),
    "cringe": re.compile(
        r"(\bL\b|\byikes\b|\boof\b|\bcringe\b|Aware\b|"
        r"\bawkward\b|\bbruh\b|\bwhy\b.{0,12}\bdo that\b)",
        re.IGNORECASE,
    ),
    "angry": re.compile(
        r"(\bratio\b|\bcope\b|\bmald|\btrash\b|\bscam\b|"
        r"\brigged\b|\bfake\b|\bclown\b|\bdisgusting\b)",
        re.IGNORECASE,
    ),
}


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

    def emotions(self) -> set[str]:
        """Which feelings this line expresses. A line can carry more than one."""
        return {name for name, pattern in EMOTIONS.items() if pattern.search(self.text)}


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


def mood_around(
    messages: list[Message], at_s: float, window_s: float = 8.0
) -> dict[str, Any]:
    """What the crowd felt at this moment, and how strongly they agreed.

    `dominant` is the emotion, `confidence` is the share of emotive lines that
    agreed on it. The second number matters as much as the first: chat split
    evenly between "funny" and "angry" is a controversy, chat 90% on "shock"
    is a highlight, and posting them the same way is how a page loses an
    audience. A window with no emotive lines returns dominant=None rather
    than guessing.
    """
    low, high = at_s - window_s, at_s + window_s
    counts: dict[str, int] = {}
    emotive = 0
    for message in messages:
        if not low <= message.at_s < high:
            continue
        found = message.emotions()
        if found:
            emotive += 1
        for name in found:
            counts[name] = counts.get(name, 0) + 1

    if not counts:
        return {"dominant": None, "confidence": 0.0, "emotive_lines": 0, "counts": {}}

    dominant, top = max(counts.items(), key=lambda kv: kv[1])
    return {
        "dominant": dominant,
        "confidence": round(top / emotive, 3),
        "emotive_lines": emotive,
        "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
    }


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


@dataclass
class LiveLog:
    """Chat for a live channel, kept only as long as the video it explains.

    The video buffer forgets on a timer; this has to forget on the same one.
    A chat log that outlives the frames it describes is the same leak in a
    cheaper unit - a busy channel runs 30-60 messages a second, so ten
    streams held for a full session is tens of millions of objects in RAM,
    and every one of them is describing video that was deleted hours ago.

    Bounded twice, because either bound alone can fail. The window is what
    matters and does the work; the hard cap on count is a backstop for the
    case the window cannot handle - a raid or a bot flood, where a few
    seconds of chat is a million lines and the window is still technically
    being honoured while memory is gone.
    """

    window_s: float = 300.0
    #: Backstop against a flood. Roughly a minute of a very busy channel.
    max_messages: int = 20_000
    messages: deque[Message] = field(default_factory=deque)
    #: Messages discarded so far - visible, because silently dropping the
    #: evidence for a moment is exactly the kind of thing that gets noticed
    #: three weeks later as "the scores look wrong sometimes".
    dropped: int = 0
    _newest_s: float = 0.0

    def add(self, message: Message) -> None:
        self.messages.append(message)
        self._newest_s = max(self._newest_s, message.at_s)
        self.prune()

    def extend(self, messages: list[Message]) -> None:
        for message in messages:
            self.messages.append(message)
            self._newest_s = max(self._newest_s, message.at_s)
        self.prune()

    def prune(self, *, now_s: float | None = None) -> int:
        """Drop anything older than the window. Returns how many went."""
        edge = (self._newest_s if now_s is None else now_s) - self.window_s
        went = 0
        while self.messages and self.messages[0].at_s < edge:
            self.messages.popleft()
            went += 1
        while len(self.messages) > self.max_messages:
            self.messages.popleft()
            went += 1
        self.dropped += went
        return went

    def held_s(self) -> float:
        if not self.messages:
            return 0.0
        return self.messages[-1].at_s - self.messages[0].at_s

    def recent(self) -> list[Message]:
        """What is still held, oldest first."""
        return list(self.messages)

    def curve(self, *, bucket_s: float = BUCKET_S) -> Curve:
        """Bucket what is held, on a timeline starting at the oldest message.

        Offsets are rebased to the window rather than to the start of the
        stream, because the start of the stream is hours ago and has been
        deleted. Everything downstream measures against the buffer it can
        actually cut from.
        """
        held = self.recent()
        if not held:
            return Curve(bucket_s=bucket_s, duration_s=0.0)
        origin = held[0].at_s
        rebased = [
            Message(at_s=m.at_s - origin, text=m.text, user=m.user) for m in held
        ]
        return build_curve(rebased, duration_s=self.held_s(), bucket_s=bucket_s)

    def status(self) -> dict[str, Any]:
        return {
            "messages": len(self.messages),
            "held_s": round(self.held_s(), 1),
            "window_s": self.window_s,
            "dropped": self.dropped,
            "at_cap": len(self.messages) >= self.max_messages,
        }
