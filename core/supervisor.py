"""Watch the top streams, catch the moments, cut them, keep the record.

This is the piece that makes every other module do something. Each part has
been built and measured on its own; this is the loop that runs them:

    roster      which channels are worth watching, with hysteresis so two
                streams trading places do not cost a reconnect each time
    buffer      a bounded window of the recent past, per channel, at the
                delivery rendition - you cannot post 1080p from a 160p
                buffer, so the buffer holds what the clip will need
    livechat    the crowd, live, feeding a log that expires with the video
    moments     chat and audio fused into a score, with the reasons kept
    reframe     the landscape crop tracked into portrait
    Catch       one row of text that outlives the deleted stream

Three rules it will not break.

**Nothing is posted.** Clips are cut and held for review. Posting is a
separate decision and a separate switch.

**The caps are hard.** Ten a day and an hour apart, counted against what is
actually stored rather than against a number held in memory, so a restart
cannot reset them.

**A stream that fails is dropped, not retried forever.** One channel with a
dead socket must not stop the other two from working.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core import chat as chatlib
from core import live, livechat, moments, reframe, roster
from core.config import settings

log = logging.getLogger(__name__)

#: How often to re-score what chat is doing. Chat moves in seconds, and the
#: scoring itself is arithmetic over a few thousand numbers.
TICK_S = 5.0
#: Ignore a trigger this close to one already taken on the same channel: the
#: same moment keeps scoring for as long as chat keeps talking about it.
COOLDOWN_S = 180.0


class SupervisorError(RuntimeError):
    pass


@dataclass
class Watched:
    """One channel: its buffer, its chat, and when it last produced a clip."""

    channel: str
    buffer: live.RollingBuffer
    chat: livechat.LiveChat
    started_at: float = field(default_factory=time.time)
    viewers: int = 0
    last_catch_at: float = 0.0
    last_score: float = 0.0
    last_reason: str = ""

    def stop(self) -> None:
        self.chat.stop()
        self.buffer.discard()

    def signals(self) -> dict[str, Any]:
        """Everything the bot can currently see for this channel.

        Deliberately cheap: this runs every few seconds and feeds the
        dashboard, so it reads the chat log and the buffer's own bookkeeping
        rather than decoding any video.
        """
        curve = self.chat.log.curve()
        held = self.chat.log.recent()
        mood = (
            chatlib.mood_around(held, held[-1].at_s, window_s=30.0)
            if held
            else {"dominant": None, "confidence": 0.0, "emotive_lines": 0, "counts": {}}
        )
        bursts = curve.bursts()
        return {
            "channel": self.channel,
            "viewers": self.viewers,
            "uptime_s": round(time.time() - self.started_at),
            "buffer": self.buffer.status(),
            "chat": {
                **self.chat.status(),
                "per_minute": round(sum(curve.counts) / max(curve.duration_s / 60.0, 1e-6), 1),
                "bursts": bursts[-3:],
                "clip_requests": curve.clip_requests()[-3:],
                "mood": mood,
                "recent": [
                    {"at_s": m.at_s, "user": m.user, "text": m.text} for m in held[-12:]
                ],
            },
            "score": round(self.last_score, 2),
            "reason": self.last_reason,
            "last_catch_s_ago": (
                round(time.time() - self.last_catch_at) if self.last_catch_at else None
            ),
        }


@dataclass
class Supervisor:
    """The loop. One instance per worker process."""

    work_dir: Path = field(default_factory=lambda: Path(settings.work_dir) / "live")
    roster: roster.Roster = field(default_factory=lambda: roster.Roster(
        slots=settings.live_slots, drop_rank=settings.live_drop_rank
    ))
    watching: dict[str, Watched] = field(default_factory=dict)
    last_roster_poll: float = 0.0
    errors: list[str] = field(default_factory=list)
    running: bool = False

    # --- the roster ---------------------------------------------------------

    def poll_roster(self, *, now: float | None = None) -> dict[str, list[str]]:
        """Refresh which channels are worth holding, and act on the change."""
        now = time.time() if now is None else now
        listing = roster.fetch_kick_live(limit=25, language="en")
        moved = self.roster.update(listing, now=now)
        by_channel = {live_.channel: live_ for live_ in listing}

        for channel in moved["stop"]:
            self.release(channel)
        for channel in moved["start"]:
            entry = by_channel.get(channel)
            self.attach(channel, viewers=entry.viewers if entry else 0)

        for channel, watched in self.watching.items():
            entry = by_channel.get(channel)
            if entry:
                watched.viewers = entry.viewers

        self.last_roster_poll = now
        return moved

    def attach(self, channel: str, *, viewers: int = 0) -> Watched | None:
        """Open a buffer and a chat socket for one channel."""
        if channel in self.watching:
            return self.watching[channel]

        try:
            url = self.playback_url(channel)
        except Exception as exc:  # noqa: BLE001 - one bad channel is not fatal
            self._note(f"{channel}: could not resolve playback ({exc})")
            return None

        started = time.time()
        buffer = live.RollingBuffer(
            url=url,
            work_dir=self.work_dir / channel,
            window_s=settings.live_window_s,
            segment_s=settings.live_segment_s,
            channel=channel,
        )
        try:
            buffer.start()
        except Exception as exc:  # noqa: BLE001
            self._note(f"{channel}: buffer would not start ({exc})")
            return None

        # Chat offsets are measured from when the buffer opened, so a chat
        # burst at t lines up with the video the buffer holds at t.
        talk = livechat.LiveChat(
            channel=channel,
            log=chatlib.LiveLog(window_s=settings.live_window_s),
            origin=started,
        )
        try:
            talk.start()
        except Exception as exc:  # noqa: BLE001 - video without chat still works
            self._note(f"{channel}: chat would not start ({exc})")

        watched = Watched(
            channel=channel, buffer=buffer, chat=talk, started_at=started, viewers=viewers
        )
        self.watching[channel] = watched
        log.info("supervisor: watching %s (%d viewers)", channel, viewers)
        return watched

    def release(self, channel: str) -> None:
        watched = self.watching.pop(channel, None)
        if watched is not None:
            watched.stop()
            log.info("supervisor: released %s", channel)

    def playback_url(self, channel: str) -> str:
        """The rendition to buffer: the one the finished clip needs.

        Detection would be happy with 160p, but a clip can only ever be as
        good as what was downloaded, and these get posted at 1080p. So the
        buffer holds the delivery rendition and detection reads the same
        bytes. At three streams that is affordable; at ten it would not be,
        which is why there are three.
        """
        import yt_dlp

        from core import ytdlp

        def call(opts: dict) -> dict:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(f"https://kick.com/{channel}", download=False)

        info = ytdlp.run(call, ytdlp.base_options(skip_download=True)) or {}
        formats = [f for f in (info.get("formats") or []) if f.get("url")]
        if not formats:
            raise SupervisorError(f"{channel} offered no playable formats")

        wanted = settings.live_delivery_height
        # The best rendition at or below the target, else the smallest above
        # it - never silently ship something lower than asked for when
        # something higher exists.
        at_or_below = [f for f in formats if (f.get("height") or 0) <= wanted]
        chosen = (
            max(at_or_below, key=lambda f: f.get("height") or 0)
            if at_or_below
            else min(formats, key=lambda f: f.get("height") or 0)
        )
        log.info(
            "supervisor: %s buffering %sp (%s kbps)",
            channel, chosen.get("height"), round(chosen.get("tbr") or 0),
        )
        return str(chosen["url"])

    # --- catching -----------------------------------------------------------

    def score(self, watched: Watched) -> tuple[float, dict[str, float], float]:
        """(score, why, peak offset) for the strongest moment chat is showing."""
        curve = watched.chat.log.curve()
        if curve.duration_s < settings.live_lead_s:
            return 0.0, {}, 0.0

        signals = moments.signals_from_chat(curve, duration_s=curve.duration_s)
        found = moments.rank(
            signals,
            duration_s=curve.duration_s,
            clip_s=settings.live_lead_s + settings.live_trail_s,
            top=1,
        )
        if not found:
            return 0.0, {}, 0.0
        best = found[0]
        return best.score, best.why, best.peak_s

    def tick(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """One pass over every watched channel. Returns whatever was caught."""
        now = time.time() if now is None else now
        caught: list[dict[str, Any]] = []

        for channel, watched in list(self.watching.items()):
            if not watched.buffer.running:
                self._note(f"{channel}: buffer stopped ({watched.buffer.failure()[:120]})")
                self.release(channel)
                continue

            value, why, peak_s = self.score(watched)
            watched.last_score = value
            watched.last_reason = max(why, key=why.get) if why else ""

            if value <= 0 or not why:
                continue
            if now - watched.last_catch_at < COOLDOWN_S:
                continue
            if not self.allowed(now=now):
                continue

            try:
                caught.append(self.catch(watched, why=why, peak_s=peak_s, now=now))
                watched.last_catch_at = now
            except Exception as exc:  # noqa: BLE001 - a failed cut is not fatal
                self._note(f"{channel}: cut failed ({exc})")

        return caught

    def catch(
        self, watched: Watched, *, why: dict[str, float], peak_s: float, now: float
    ) -> dict[str, Any]:
        """Cut the moment out of the buffer, reframe it, and record it."""
        held = watched.chat.log.recent()
        # peak_s is an offset into the chat curve, which starts at the oldest
        # message still held. Convert it back to seconds before the live edge,
        # which is the only thing the buffer can be asked about.
        origin = held[0].at_s if held else 0.0
        newest = held[-1].at_s if held else 0.0
        ago_s = max(0.0, newest - (origin + peak_s))

        out_dir = Path(settings.work_dir) / "catches"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        raw = out_dir / f"{watched.channel}-{stamp}-raw.mp4"
        final = out_dir / f"{watched.channel}-{stamp}.mp4"

        watched.buffer.extract(
            raw, ago_s=ago_s, lead_s=settings.live_lead_s, trail_s=settings.live_trail_s
        )
        reframe.to_portrait(raw, final, work_dir=out_dir / "tmp")
        raw.unlink(missing_ok=True)

        peak_at = origin + peak_s
        mood = chatlib.mood_around(held, peak_at, window_s=8.0)
        quotes = chatlib.quotes_around(held, peak_at, window_s=8.0)

        record = {
            "channel": watched.channel,
            "source_url": f"https://kick.com/{watched.channel}",
            "path": str(final),
            "at_s": round(peak_at, 2),
            "duration_s": settings.live_lead_s + settings.live_trail_s,
            "score": round(sum(why.values()), 3),
            "why": {k: round(v, 3) for k, v in sorted(why.items(), key=lambda kv: -kv[1])},
            "mood": mood,
            "quotes": _top_quotes(quotes),
            "peak_viewers": watched.viewers,
            "caught_at": datetime.fromtimestamp(now, UTC).isoformat(),
        }
        self.store(record)
        log.info(
            "supervisor: caught %s (%s, score %.1f) -> %s",
            watched.channel, mood.get("dominant") or "unread", record["score"], final.name,
        )
        return record

    # --- the caps -----------------------------------------------------------

    def allowed(self, *, now: float | None = None) -> bool:
        """Whether another clip may be cut right now.

        Counted against the database rather than a counter in memory, so a
        restart cannot quietly reset the day's allowance.
        """
        now = time.time() if now is None else now
        recent = self.recent_catches(since=datetime.fromtimestamp(now, UTC) - timedelta(days=1))
        if len(recent) >= settings.live_clips_per_day:
            return False
        if recent:
            newest = max(r.created_at for r in recent if r.created_at)
            gap = (datetime.fromtimestamp(now, UTC) - newest).total_seconds() / 60.0
            if gap < settings.live_min_gap_minutes:
                return False
        return True

    def recent_catches(self, *, since: datetime) -> list:
        from core.db import session_scope
        from core.models import Catch

        with session_scope() as db:
            rows = db.query(Catch).filter(Catch.created_at >= since).all()
            db.expunge_all()
            return rows

    def store(self, record: dict[str, Any]) -> None:
        from core.db import session_scope
        from core.models import Catch

        with session_scope() as db:
            db.add(
                Catch(
                    platform="kick",
                    channel=record["channel"],
                    source_url=record["source_url"],
                    at_s=record["at_s"],
                    duration_s=record["duration_s"],
                    storage_key=record["path"],
                    why=record["why"],
                    score=record["score"],
                    mood=record["mood"],
                    quotes=record["quotes"],
                    peak_viewers=record["peak_viewers"],
                    status="caught",
                    source_deleted=True,
                )
            )

    # --- status and lifecycle ----------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "slots": settings.live_slots,
            "posting_enabled": settings.live_posting_enabled,
            "caps": {
                "per_day": settings.live_clips_per_day,
                "min_gap_minutes": settings.live_min_gap_minutes,
                "allowed_now": self.allowed(),
            },
            "streams": [w.signals() for w in self.watching.values()],
            "errors": self.errors[-6:],
        }

    def stop(self) -> None:
        self.running = False
        for channel in list(self.watching):
            self.release(channel)

    def _note(self, message: str) -> None:
        log.warning("supervisor: %s", message)
        self.errors.append(f"{datetime.now(UTC).strftime('%H:%M:%S')} {message}")
        del self.errors[:-20]


def _top_quotes(quotes: list[str], limit: int = 6) -> list[dict[str, Any]]:
    from collections import Counter

    return [
        {"text": text, "count": n} for text, n in Counter(quotes).most_common(limit)
    ]
