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


#: How often to actually decode audio out of the buffer. Every tick would be
#: three ffmpeg decodes every five seconds for a number that moves slowly;
#: this is the one genuinely expensive thing the loop can do.
AUDIO_EVERY_S = 20.0
#: How much of the recent past to measure. Long enough to show a shape, short
#: enough that the decode stays well under a second.
AUDIO_WINDOW_S = 24.0
#: Below this the room is silent rather than merely quiet. LosPollosTV asleep
#: read -57.9 mean and -40.2 peak; a person talking sits above -30.
DORMANT_DB = -45.0
DORMANT_PEAK_DB = -25.0
#: Mean per-pixel frame difference. A locked-off shot of a sleeping room is
#: near zero; anyone moving in frame is an order of magnitude above it.
DORMANT_MOTION = 0.004
#: How long it has to stay that way. One reading is a pause; three in a row,
#: a minute apart in practice, is nobody home.
DORMANT_READINGS = 3
#: How long a sleeping stream is passed over before it is considered again.
#: Long enough not to thrash, short enough that waking up is noticed.
DORMANT_REST_S = 900.0
#: A thirty second 1080p clip is about 30MB. Anything much past that is
#: not a clip and does not belong in Redis.
MAX_INLINE_CLIP_BYTES = 60 * 1024 * 1024


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
    #: The per-signal breakdown behind last_score. Without it the page can
    #: show a total and no explanation, which is the one thing this project
    #: promised never to do.
    last_why: dict[str, float] = field(default_factory=dict)
    #: What the directory said about them, for the page to render.
    display_name: str = ""
    avatar: str = ""
    thumbnail: str = ""
    title: str = ""
    category: str = ""
    #: The most recent audio reading, refreshed on its own slower timer.
    audio: dict[str, Any] = field(default_factory=dict)
    audio_at: float = 0.0
    #: Consecutive readings with nobody apparently there. Consecutive, not a
    #: running average: someone who goes quiet and then speaks is not asleep,
    #: and an average would take minutes to forgive them.
    asleep_readings: int = 0
    activity: dict[str, Any] = field(default_factory=dict)

    @property
    def dormant(self) -> bool:
        return self.asleep_readings >= DORMANT_READINGS

    def stop(self) -> None:
        self.chat.stop()
        self.buffer.discard()

    def read_audio(self, out_dir: Path) -> dict[str, Any]:
        """Decode the tail of the buffer and describe what it sounds like.

        The buffer is already on disk at delivery quality, so this costs a
        decode and no extra bandwidth. What comes back is the loudness curve -
        the shape the page draws - plus the jumps and quiet runs that the
        moment scorer would use, and a spectrogram, which is the one view that
        tells speech from music from an impact at a glance.
        """
        from core import listen

        segments = self.buffer.segments()
        if not segments:
            return {"ok": False, "why": "the buffer is still filling"}

        # Take whole segments from the end rather than seeking: they are
        # independently decodable, so this cannot land mid-GOP.
        wanted: list = []
        held = 0.0
        for segment in reversed(segments):
            wanted.insert(0, segment)
            held += segment.duration_s
            if held >= AUDIO_WINDOW_S:
                break

        out_dir.mkdir(parents=True, exist_ok=True)
        # The concat *protocol*, not the concat demuxer's list file: transport
        # streams can simply be joined byte-wise, and ffmpeg reads that from
        # one argument. A list file needs -f concat -safe 0 to go with it, and
        # feeding one to a plain -i produces no error and no output - which is
        # how the spectrogram silently came back empty the first time.
        joined = f"concat:{'|'.join(str(seg.path) for seg in wanted)}"

        try:
            envelope = listen.envelope(joined)
        except Exception as exc:  # noqa: BLE001 - audio is a nicety, not the job
            return {"ok": False, "why": f"{type(exc).__name__}: {exc}"}

        spectrogram: Path | None = out_dir / f"{self.channel}-spectrogram.png"
        try:
            listen.spectrogram_png(joined, spectrogram, width=760, height=220)
            # Hand it to the web service, which has no access to this disk.
            from core import livestate

            livestate.put_image(f"spectrogram:{self.channel}", spectrogram.read_bytes())
        except Exception as exc:  # noqa: BLE001
            log.debug("supervisor: no spectrogram for %s (%s)", self.channel, exc)
            spectrogram = None

        # The page draws a bar per point, so send a fixed number of them
        # regardless of window length rather than several hundred.
        curve = envelope.rms_db
        step = max(1, len(curve) // 120)
        return {
            "ok": True,
            "held_s": round(envelope.duration_s, 1),
            "loudness_db": [round(v, 1) for v in curve[::step]],
            "mean_db": round(sum(curve) / len(curve), 1) if curve else None,
            "peak_db": round(max(envelope.peak_db), 1) if envelope.peak_db else None,
            "jumps": envelope.jumps()[-6:],
            "quiet_runs": envelope.quiet_runs()[-4:],
            "has_spectrogram": spectrogram is not None,
        }

    def read_activity(self) -> dict[str, Any]:
        """Is anything actually happening, or is the streamer asleep?

        A stream with nobody in front of it cannot produce a clip, and while
        it holds a slot the fourth-placed stream - which might - is not being
        watched at all. LosPollosTV asleep on camera with 13,000 people
        watching a bed is the case this exists for: the viewer count says
        nothing, and chat carries on talking regardless.

        Two signals, and both have to agree. Silence alone is a pause between
        sentences; stillness alone is someone reading. Together, sustained,
        it is an empty chair.
        """
        audio = self.audio if self.audio.get("ok") else {}
        mean_db = audio.get("mean_db")
        peak_db = audio.get("peak_db")

        motion = self.read_motion()
        quiet = mean_db is not None and mean_db <= DORMANT_DB
        # A peak that never rises either says nobody has said anything at all,
        # not merely that the average is low.
        never_loud = peak_db is not None and peak_db <= DORMANT_PEAK_DB
        still = motion is not None and motion <= DORMANT_MOTION

        return {
            "mean_db": mean_db,
            "peak_db": peak_db,
            "motion": motion,
            "quiet": bool(quiet and never_loud),
            "still": bool(still),
            "asleep_now": bool(quiet and never_loud and still),
        }

    def read_motion(self) -> float | None:
        """How much the picture is moving, 0..1, from tiny greyscale frames.

        The same measurement core.reframe uses to decide where to point the
        crop, at the same cost: 64x36 frames, four a second. Deciding whether
        anybody is there does not need detail either.
        """
        from core import reframe

        segments = self.buffer.segments()
        if len(segments) < 2:
            return None
        joined = f"concat:{'|'.join(str(seg.path) for seg in segments[-2:])}"
        try:
            rows = reframe.motion_columns(joined)
        except Exception:  # noqa: BLE001 - a missing reading is not a fault
            return None
        if len(rows) < 2:
            return None

        # Mean absolute difference per pixel, normalised. A still camera on a
        # sleeping room sits near zero; a person talking is far above it.
        total = sum(sum(row) for row in rows[1:])
        pixels = max(1, (len(rows) - 1) * len(rows[0]) * 36)
        return round(total / pixels / 255.0, 5)

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
            "name": self.display_name or self.channel,
            "avatar": self.avatar,
            "thumbnail": self.thumbnail,
            "title": self.title,
            "category": self.category,
            "page": f"https://kick.com/{self.channel}",
            "viewers": self.viewers,
            "uptime_s": round(time.time() - self.started_at),
            "buffer": self.buffer.status(),
            "audio": self.audio,
            "activity": self.activity,
            "dormant": self.dormant,
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
            "why": {k: round(v, 3) for k, v in
                    sorted(self.last_why.items(), key=lambda kv: -kv[1])},
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
    #: Channels found asleep, and when they may be picked up again. A stream
    #: that wakes before this is simply picked up at the next poll like any
    #: other; the delay only stops the roster re-attaching to a sleeping room
    #: five seconds after letting go of it.
    dormant_until: dict[str, float] = field(default_factory=dict)
    #: Channels that only hold a slot because someone above them is asleep.
    #: They give it back the moment that stream is worth trying again - the
    #: roster on its own would keep a stand-in for hours, because by then it
    #: has tenure and its rank is perfectly respectable.
    fillers: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    running: bool = False

    # --- the roster ---------------------------------------------------------

    def poll_roster(self, *, now: float | None = None) -> dict[str, list[str]]:
        """Refresh which channels are worth holding, and act on the change."""
        now = time.time() if now is None else now
        listing = roster.fetch_kick_live(limit=25, language="en")
        self.dormant_until = {c: t for c, t in self.dormant_until.items() if t > now}

        # Hand the sleeping ones back before the roster decides anything, so
        # it sees the free slot in the same pass rather than the next one.
        for channel, watched in list(self.watching.items()):
            if watched.dormant:
                self.dormant_until[channel] = now + DORMANT_REST_S
                self.roster.watching.pop(channel, None)
                self.release(channel)

        moved = self.roster.update(listing, now=now)
        by_channel = {live_.channel: live_ for live_ in listing}
        ranks = {live_.channel: i + 1 for i, live_ in enumerate(listing)}

        for channel in moved["stop"]:
            self.release(channel)

        # The roster ranks by viewers and knows nothing about who is awake, so
        # it will hand back the stream we just let go of. Refuse it here.
        resting = [c for c in moved["start"] if c in self.dormant_until]
        for channel in resting:
            moved["start"].remove(channel)
            self.roster.watching.pop(channel, None)

        for channel in moved["start"]:
            self.attach(channel, entry=by_channel.get(channel))

        def rank(channel: str) -> int:
            return ranks.get(channel, len(ranks) + 1)

        def waiting_above(channel: str) -> list[str]:
            """Streams that outrank this one and are not being watched."""
            return [
                other.channel for other in listing
                if other.channel not in self.watching
                and other.channel not in self.dormant_until
                and rank(other.channel) < rank(channel)
            ]

        # A stand-in hands the slot back as soon as the stream it covered for
        # is due another look. Without this, second place never returns: the
        # roster sees a settled fourth place with tenure and no reason to move.
        for filler in sorted(self.fillers & set(self.watching), key=rank, reverse=True):
            if len(self.watching) <= settings.live_slots and waiting_above(filler):
                self.fillers.discard(filler)
                self.roster.watching.pop(filler, None)
                self.release(filler)
                moved["stop"].append(filler)

        # Fill whatever is now free from further down the listing, skipping
        # anyone still resting. This is what puts fourth place on screen while
        # second place is asleep.
        for live_ in listing:
            if len(self.watching) >= settings.live_slots:
                break
            if live_.channel in self.watching or live_.channel in self.dormant_until:
                continue
            if self.attach(live_.channel, entry=live_) is None:
                continue
            self.roster.watching[live_.channel] = roster.Watched(
                channel=live_.channel, started_at=now, last_seen_ok=now,
                last_rank=rank(live_.channel), viewers=live_.viewers,
            )
            moved["start"].append(live_.channel)
            if any(rank(c) < rank(live_.channel) for c in self.dormant_until):
                self.fillers.add(live_.channel)

        for channel, watched in self.watching.items():
            entry = by_channel.get(channel)
            if entry:
                self._describe(watched, entry)

        self.last_roster_poll = now
        return moved

    @staticmethod
    def _describe(watched: Watched, entry) -> None:  # noqa: ANN001 - roster.Live
        """Refresh what the page shows about a stream, but not what it is."""
        watched.viewers = entry.viewers
        watched.display_name = entry.name()
        watched.avatar = entry.avatar
        watched.thumbnail = entry.thumbnail
        watched.title = entry.title
        watched.category = entry.category

    def attach(self, channel: str, *, entry=None, viewers: int = 0) -> Watched | None:  # noqa: ANN001
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
        if entry is not None:
            self._describe(watched, entry)
        self.watching[channel] = watched
        log.info("supervisor: watching %s (%d viewers)", channel, watched.viewers)
        return watched

    def release(self, channel: str) -> None:
        self.fillers.discard(channel)
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
            # The window the score is measured over, which is not the clip
            # length: scoring wants a consistent width to compare moments
            # fairly, and the clip wants to run as long as the moment does.
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

            # Audio is the one expensive thing in this loop, so it runs on
            # its own timer rather than every tick.
            if now - watched.audio_at >= AUDIO_EVERY_S:
                watched.audio_at = now
                try:
                    watched.audio = watched.read_audio(self.work_dir / "audio")
                except Exception as exc:  # noqa: BLE001 - a graph is not the job
                    watched.audio = {"ok": False, "why": f"{type(exc).__name__}: {exc}"}
                try:
                    watched.activity = watched.read_activity()
                    if watched.activity.get("asleep_now"):
                        watched.asleep_readings += 1
                        if watched.asleep_readings == DORMANT_READINGS:
                            self._note(
                                f"{channel} looks asleep or away - freeing the slot"
                            )
                    else:
                        watched.asleep_readings = 0
                except Exception:  # noqa: BLE001 - never let this stop a tick
                    watched.asleep_readings = 0

            if watched.dormant:
                watched.last_score = 0.0
                watched.last_why = {}
                watched.last_reason = "asleep"
                continue

            value, why, peak_s = self.score(watched)
            watched.last_score = value
            watched.last_why = why
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

        # How long the moment actually ran, rather than a fixed length. The
        # lead is fixed because a clip that opens on the punchline is a clip
        # nobody understands; the tail is whatever chat says it is.
        trail_s = moments.moment_end(
            watched.chat.log.curve(),
            peak_s,
            min_s=settings.live_trail_s,
            max_s=max(settings.live_trail_s, settings.live_max_clip_s - settings.live_lead_s),
        )
        watched.buffer.extract(
            raw, ago_s=ago_s, lead_s=settings.live_lead_s, trail_s=trail_s
        )
        reframe.to_portrait(raw, final, work_dir=out_dir / "tmp")
        raw.unlink(missing_ok=True)

        # The clip is written here, on the worker, and watched in a browser
        # talking to the web service. Those are different containers with
        # different disks, so a path is not a way to hand it over - the first
        # catch was recorded perfectly and then could not be played, because
        # the file it named existed only on the machine that made it.
        stored = self.publish_clip(final)

        peak_at = origin + peak_s
        mood = chatlib.mood_around(held, peak_at, window_s=8.0)
        quotes = chatlib.quotes_around(held, peak_at, window_s=8.0)

        record = {
            "channel": watched.channel,
            "source_url": f"https://kick.com/{watched.channel}",
            "path": str(final),
            "storage_key": stored,
            "at_s": round(peak_at, 2),
            "duration_s": round(settings.live_lead_s + trail_s, 1),
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

        A database that cannot be reached refuses the cut but does not stop
        the watch. This is deliberate and it is the bug that killed the first
        live run: the check ran on every tick, threw, and took the whole
        supervisor down with it - three buffers and all - because the table it
        counts had not been created yet. Watching is still worth doing with no
        database; cutting without knowing the day's count is not, because the
        caps are the one thing here that must not be exceeded by accident.
        """
        return self.cap_state(now=now)["allowed"]

    def cap_state(self, *, now: float | None = None) -> dict[str, Any]:
        """Whether a clip may be cut, and - the point of this - *why not*.

        "No" has four meanings here and a bare False tells them apart for
        nobody: the day's ten are gone, the hour is not up, the database is
        unreachable, or nothing is wrong and it simply may. Only one of those
        is something to go and fix, so the reason has to travel with the
        answer rather than being inferred from a red word on a page.
        """
        now = time.time() if now is None else now
        try:
            recent = self.recent_catches(
                since=datetime.fromtimestamp(now, UTC) - timedelta(days=1)
            )
        except Exception as exc:  # noqa: BLE001 - a dead database is not a reason to stop
            self._note(f"cannot check the daily cap, so not cutting ({exc})")
            return {
                "allowed": False,
                "reason": "no database",
                "detail": (
                    "The worker cannot reach Postgres, so it cannot count the day's "
                    f"clips - and it will not cut without knowing. {type(exc).__name__}: {exc}"
                ),
                "cut_today": None,
                "wait_minutes": None,
            }

        cut_today = len(recent)
        if cut_today >= settings.live_clips_per_day:
            return {
                "allowed": False,
                "reason": "daily cap",
                "detail": f"{cut_today} cut in the last 24 hours, which is the cap.",
                "cut_today": cut_today,
                "wait_minutes": None,
            }

        newest = max((r.created_at for r in recent if r.created_at), default=None)
        if newest is not None:
            gap = (datetime.fromtimestamp(now, UTC) - newest).total_seconds() / 60.0
            if gap < settings.live_min_gap_minutes:
                wait = settings.live_min_gap_minutes - gap
                return {
                    "allowed": False,
                    "reason": "hourly gap",
                    "detail": f"Last clip was {gap:.0f} minutes ago; "
                              f"{wait:.0f} to go.",
                    "cut_today": cut_today,
                    "wait_minutes": round(wait),
                }

        return {
            "allowed": True,
            "reason": "clear",
            "detail": f"{cut_today} cut today, waiting for a moment worth cutting.",
            "cut_today": cut_today,
            "wait_minutes": 0,
        }

    def recent_catches(self, *, since: datetime) -> list:
        from core.db import session_scope
        from core.models import Catch

        with session_scope() as db:
            rows = db.query(Catch).filter(Catch.created_at >= since).all()
            db.expunge_all()
            return rows

    def publish_clip(self, path: Path) -> str | None:
        """Put the clip somewhere the web service can actually read it.

        With R2 configured that is object storage and the browser fetches it
        directly. Without it the bytes go through Redis, which is not what
        Redis is for and is capped accordingly - but a review queue nobody can
        watch is worse than a large value with a time limit on it.
        """
        from core import livestate
        from core.storage import get_storage

        try:
            storage = get_storage()
            if storage.kind != "local":
                key = f"catches/{path.name}"
                storage.put_file(path, key)
                return key
        except Exception as exc:  # noqa: BLE001 - fall through to Redis
            self._note(f"could not upload {path.name} ({exc})")

        try:
            data = path.read_bytes()
            if len(data) > MAX_INLINE_CLIP_BYTES:
                self._note(
                    f"{path.name} is {len(data) / 1e6:.0f}MB, too large to hold for "
                    "review without R2 - configure R2 to keep clips"
                )
                return None
            # Long enough to review a day's worth, short enough that Redis is
            # not quietly turned into the archive.
            livestate.put_image(f"clip:{path.name}", data, ttl_s=48 * 3600)
            return f"redis:{path.name}"
        except Exception as exc:  # noqa: BLE001
            self._note(f"could not hold {path.name} for review ({exc})")
            return None

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
                    storage_key=record.get("storage_key") or record["path"],
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
                # cap_state() already swallows its own failures; this is only
                # here so a status read can never be the thing that throws.
                **self._caps_quietly(),
            },
            "streams": [w.signals() for w in self.watching.values()],
            "errors": self.errors[-6:],
        }

    def _caps_quietly(self) -> dict[str, Any]:
        try:
            found = self.cap_state()
        except Exception as exc:  # noqa: BLE001 - a status read must not throw
            return {"allowed_now": None, "cap_reason": "unknown",
                    "cap_detail": f"{type(exc).__name__}: {exc}"}
        return {
            "allowed_now": found["allowed"],
            "cap_reason": found["reason"],
            "cap_detail": found["detail"],
            "cut_today": found["cut_today"],
            "wait_minutes": found["wait_minutes"],
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
