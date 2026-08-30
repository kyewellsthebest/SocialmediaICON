"""Which streams to watch right now, and when to let one go.

The plan is "watch the top ten, and when tenth place changes, switch". The
intent is right; taken literally it thrashes.

Viewer counts move constantly. Ranks ten and eleven are usually within a
couple of percent of each other, so they trade places every few minutes on
noise alone - and every trade costs a buffer teardown, a new connection, and
roughly fifteen seconds during which the new stream is buffering and cannot
be clipped. Follow the ranking exactly and a real share of the watching time
is spent blind, swapping between two streams that were never meaningfully
different.

So this keeps the ranking but adds three things that make it stable:

* **A wider drop than add.** A stream joins at rank 10 but is only dropped
  once it falls past rank 13. In the band between, it stays. Two streams
  swapping around tenth place both simply keep being watched.
* **Patience.** A stream has to be out of favour for a sustained period, not
  one poll, before it is dropped. A momentary dip is not a departure.
* **A floor on tenure.** A stream just picked up is given a few minutes
  regardless. Attaching and immediately dropping is the worst possible use of
  the connection.

A stream that actually ends is dropped at once - that is not noise.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: How many to watch at once.
SLOTS = 10
#: Rank a stream must fall past before it is a candidate for dropping. The gap
#: between this and SLOTS is the hysteresis band.
DROP_RANK = 13
#: How long it must stay out of favour before it actually goes.
PATIENCE_S = 240.0
#: Minimum time to keep a stream once picked up.
MIN_TENURE_S = 300.0


@dataclass(frozen=True)
class Live:
    """One live stream as the directory reports it."""

    channel: str
    viewers: int
    title: str = ""
    category: str = ""
    language: str = "en"
    url: str = ""
    #: The streamer's avatar and the stream's current thumbnail. Both are
    #: plain CDN images the dashboard can show directly - a name and a number
    #: is not enough to tell three streams apart at a glance.
    avatar: str = ""
    thumbnail: str = ""
    display_name: str = ""
    #: Measured, not reported: the directory does not carry a chat rate, so
    #: this is filled in for candidates the supervisor has a chat probe on and
    #: left at zero for the rest.
    messages_per_min: float = 0.0

    def page(self) -> str:
        return self.url or f"https://kick.com/{self.channel}"

    def name(self) -> str:
        return self.display_name or self.channel


#: How chat rate is expected to grow with audience. Sub-linear, and markedly
#: so: a channel with ten times the viewers does not get ten times the
#: messages, because the share of an audience that types falls as the audience
#: grows. The exponent is what turns a raw rate into "busy for a stream this
#: size", which is the only version of the number worth comparing across
#: channels.
CHAT_SCALING = 0.6
#: Messages a minute a stream of one viewer would notionally produce. Only the
#: shape matters - it cancels out of every comparison - but it sets where the
#: ratio sits, so it is calibrated against real numbers: DeenTheGreat at 16.7k
#: viewers ran 177/min and oblivionsw at 9.0k ran 477/min.
CHAT_BASE = 1.35
#: How far chat rate may move a stream. A busy small channel should be able to
#: beat a comparable big one and should never beat a far bigger one: the whole
#: point of the cap is that three thousand viewers with fifty more messages a
#: minute must not displace ten thousand with fifty fewer.
CHAT_PULL_LOW, CHAT_PULL_HIGH = 0.6, 1.6


def expected_rate(viewers: int) -> float:
    """Messages a minute a stream this size would usually be doing."""
    return CHAT_BASE * max(viewers, 1) ** CHAT_SCALING


def worth(live: Live) -> float:
    """How much a stream is worth watching: its size, adjusted for its life.

    Audience first, because clips are worth what they reach and a moment in
    front of sixteen thousand people is worth more than the same moment in
    front of three. Chat rate then pulls that up or down, bounded, because how
    hard an audience is reacting is real information about how much is
    happening - and because an unbounded version of this ranks a two-hundred
    viewer channel with a manic chat above everything else on Kick.

    Measured against what a stream *this size* usually does, not against a flat
    number: 477 messages a minute is extraordinary at nine thousand viewers and
    ordinary at ninety thousand.
    """
    if live.messages_per_min <= 0:
        return float(live.viewers)
    ratio = live.messages_per_min / expected_rate(live.viewers)
    return live.viewers * max(CHAT_PULL_LOW, min(CHAT_PULL_HIGH, ratio))


def rank_streams(listing: list[Live]) -> list[Live]:
    """The listing, best first."""
    return sorted(listing, key=lambda live: (-worth(live), live.channel))


@dataclass
class Watched:
    channel: str
    started_at: float
    last_seen_ok: float
    last_rank: int = 0
    viewers: int = 0

    def tenure_s(self, now: float) -> float:
        return now - self.started_at


@dataclass
class Roster:
    """The set of streams currently worth holding a buffer open for."""

    slots: int = SLOTS
    drop_rank: int = DROP_RANK
    patience_s: float = PATIENCE_S
    min_tenure_s: float = MIN_TENURE_S
    watching: dict[str, Watched] = field(default_factory=dict)

    def update(self, listing: list[Live], *, now: float | None = None) -> dict[str, list[str]]:
        """Take a fresh directory listing; return what to start and stop.

        `listing` is expected already filtered to the language and sorted by
        viewers, which is what the directory itself returns.
        """
        now = time.time() if now is None else now
        ranks = {live.channel: i + 1 for i, live in enumerate(listing)}
        by_channel = {live.channel: live for live in listing}

        started: list[str] = []
        stopped: list[str] = []

        # A stream that has gone offline is not a ranking question.
        for channel in list(self.watching):
            if channel not in ranks:
                del self.watching[channel]
                stopped.append(channel)
                log.info("roster: %s went offline", channel)

        for channel, watched in list(self.watching.items()):
            rank = ranks[channel]
            watched.last_rank = rank
            watched.viewers = by_channel[channel].viewers
            if rank <= self.drop_rank:
                watched.last_seen_ok = now
                continue
            if watched.tenure_s(now) < self.min_tenure_s:
                continue
            if now - watched.last_seen_ok >= self.patience_s:
                del self.watching[channel]
                stopped.append(channel)
                log.info("roster: dropping %s (rank %d for %.0fs)", channel, rank, self.patience_s)

        for live in listing:
            if len(self.watching) >= self.slots:
                break
            if live.channel in self.watching:
                continue
            self.watching[live.channel] = Watched(
                channel=live.channel,
                started_at=now,
                last_seen_ok=now,
                last_rank=ranks[live.channel],
                viewers=live.viewers,
            )
            started.append(live.channel)
            log.info(
                "roster: watching %s (rank %d, %d viewers)",
                live.channel, ranks[live.channel], live.viewers,
            )

        return {"start": started, "stop": stopped}

    def status(self) -> list[dict[str, Any]]:
        now = time.time()
        return sorted(
            (
                {
                    "channel": w.channel,
                    "rank": w.last_rank,
                    "viewers": w.viewers,
                    "tenure_s": round(w.tenure_s(now)),
                }
                for w in self.watching.values()
            ),
            key=lambda row: row["rank"],
        )


# --- reading the directory --------------------------------------------------

#: The endpoint behind the Browse page in the screenshots: sorted by viewers,
#: filterable by language. Undocumented, so more than one shape is tried.
KICK_LIVE_PATHS = (
    "https://kick.com/stream/livestreams/en?page={page}&limit={limit}&sort=desc",
    "https://kick.com/api/v2/channels/livestreams?page={page}&limit={limit}",
)


def fetch_kick_live(
    limit: int = 25, *, language: str = "en", page: int = 1
) -> list[Live]:
    """The live directory, richest first. Needs a browser TLS fingerprint."""
    from curl_cffi import requests as cffi

    last_error = ""
    for template in KICK_LIVE_PATHS:
        url = template.format(page=page, limit=limit)
        try:
            response = cffi.get(url, impersonate="chrome", timeout=30.0)
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code} from {url}"
                continue
            found = _parse_live(response.json(), language)
            if found:
                return sorted(found, key=lambda live: -live.viewers)[:limit]
            last_error = f"{url} answered but held no streams"
        except Exception as exc:  # noqa: BLE001 - a dead path is data, not a crash
            last_error = f"{type(exc).__name__}: {exc}"

    raise RuntimeError(f"no Kick live-directory endpoint answered: {last_error}")


#: A livestream's own slug looks like "81c9c0fc-locked-in-athon-day-49-of-90":
#: eight hex characters, a dash, then the session title. It is not a channel
#: and https://kick.com/<that> is a 404 - which is exactly how three streams
#: were watched, resolved and rejected on the first real run.
SESSION_SLUG = re.compile(r"^[0-9a-f]{8}-")


def _channel_of(row: dict[str, Any]) -> str:
    """The channel slug, which is not the same thing as the stream's slug.

    The livestreams endpoint returns one row per *session*, and that row's own
    `slug` names the session, not the person. The channel hangs off it. Taking
    the row's slug first therefore produces a URL that cannot be resolved, and
    it fails late - at playback lookup, one stream at a time, with a 404 that
    reads like the channel went offline.
    """
    channel = row.get("channel")
    if isinstance(channel, dict):
        found = channel.get("slug") or (channel.get("user") or {}).get("username")
        if found:
            return str(found)

    # A row that is itself a channel (the channels endpoint) does carry the
    # right slug - but only when it is not a session slug in disguise.
    for key in ("slug", "username"):
        found = row.get(key)
        if found and not SESSION_SLUG.match(str(found)):
            return str(found)

    user = row.get("user")
    if isinstance(user, dict) and user.get("username"):
        return str(user["username"])
    return ""


def _parse_live(payload: Any, language: str) -> list[Live]:
    """Pull streams out of whatever shape the directory came back in."""
    rows = payload
    if isinstance(payload, dict):
        for key in ("data", "livestreams", "channels", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
            if isinstance(value, dict):
                return _parse_live(value, language)
    if not isinstance(rows, list):
        return []

    out: list[Live] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stream = row.get("livestream") if isinstance(row.get("livestream"), dict) else row
        channel = _channel_of(row)
        viewers = stream.get("viewer_count") or stream.get("viewers") or 0
        spoken = str(stream.get("language") or row.get("language") or "").lower()
        # Kick writes the language as a full word; accept both, and keep rows
        # that simply did not say rather than silently discarding the listing.
        if spoken and language and not spoken.startswith(language) and spoken != "english":
            continue
        if not channel:
            continue

        categories = stream.get("categories") or []
        out.append(
            Live(
                channel=str(channel),
                viewers=int(viewers or 0),
                title=str(stream.get("session_title") or ""),
                category=str(categories[0].get("name", "")) if categories else "",
                language=spoken or language,
                avatar=_avatar_of(row),
                thumbnail=_thumbnail_of(stream),
                display_name=_display_name_of(row) or str(channel),
            )
        )
    return out


def _user_of(row: dict[str, Any]) -> dict[str, Any]:
    """The user object, whichever level of the row it is hiding at."""
    channel = row.get("channel")
    if isinstance(channel, dict) and isinstance(channel.get("user"), dict):
        return channel["user"]
    return row.get("user") if isinstance(row.get("user"), dict) else {}


def _avatar_of(row: dict[str, Any]) -> str:
    user = _user_of(row)
    for key in ("profile_pic", "profilepic", "avatar"):
        found = user.get(key) or row.get(key)
        if found:
            return str(found)
    return ""


def _display_name_of(row: dict[str, Any]) -> str:
    return str(_user_of(row).get("username") or "")


def _thumbnail_of(stream: dict[str, Any]) -> str:
    """Kick gives the thumbnail as a bare string on some rows, a dict on others."""
    found = stream.get("thumbnail")
    if isinstance(found, dict):
        return str(found.get("url") or found.get("src") or "")
    return str(found or "")
