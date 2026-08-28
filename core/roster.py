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

    def page(self) -> str:
        return self.url or f"https://kick.com/{self.channel}"


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
        channel = (
            row.get("slug")
            or (row.get("channel") or {}).get("slug")
            or stream.get("slug")
            or ""
        )
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
            )
        )
    return out
