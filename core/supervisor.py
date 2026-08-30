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
import subprocess
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
#: How much of the buffer the senses read, and how often. The windows overlap
#: by ten seconds so nothing falls between two reads. Reading is the expensive
#: thing the watcher does - about 0.9s of CPU per stream, per pass - and this
#: pair is what keeps three streams under a fifth of one core.
SENSE_WINDOW_S = 30.0
SENSE_EVERY_S = 20.0
#: The width a moment is scored over. Not the clip length: scoring wants a
#: consistent narrow window so two moments can be compared, and the clip wants
#: to run as long as the moment does.
SCORE_WIDTH_S = 12.0
#: Below this the room is silent rather than merely quiet. LosPollosTV asleep
#: read -57.9 mean and -40.2 peak; a person talking sits above -30.
DORMANT_DB = -45.0
DORMANT_PEAK_DB = -25.0
#: Mean per-pixel frame difference. A locked-off shot of a sleeping room is
#: near zero; anyone moving in frame is an order of magnitude above it. A man
#: sitting at a desk talking measured 0.0093 and a nightclub 0.118.
DORMANT_MOTION = 0.004
#: Nobody home is stillness plus nobody talking - not stillness plus silence.
#: Requiring silence is why a streamer asleep with a game or music playing was
#: watched all night: the silence never came, so the count never started. And
#: stillness on its own is not enough either, or a podcast on a locked-off
#: camera reads as an empty room. What separates the two is whether anyone is
#: speaking, which core.hearing already measures.
DORMANT_READINGS = 6
DORMANT_SPEECH = 0.10
DORMANT_STILL_WEIGHT = 1
DORMANT_SILENT_WEIGHT = 2
#: How far down the listing to keep a chat socket open. Chat is a websocket
#: and costs no video bandwidth at all, so the streams *below* the three being
#: buffered can still be measured - which is the only way the ranking can know
#: that the ninth-biggest stream is the liveliest one on Kick.
PROBE_DEPTH = 8
PROBE_WINDOW_S = 120.0
#: A probe that has only just connected has seen almost nothing, and reporting
#: that as a rate would demote a stream for the crime of being new to us.
PROBE_SETTLE_S = 45.0
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
    #: What a model said when it last watched a candidate from this channel.
    last_verdict: dict[str, Any] = field(default_factory=dict)
    #: What has been said out loud, word by word, on the same clock as chat.
    said: Any = None
    #: The most recent audio reading, refreshed on its own slower timer.
    audio: dict[str, Any] = field(default_factory=dict)
    audio_at: float = 0.0
    #: What was heard and seen over the last SENSE_WINDOW_S, and when that
    #: window ended. Held as objects because the scorer needs the events and
    #: as dicts because the dashboard needs the summary.
    heard: Any = None
    seen: Any = None
    senses_at: float = 0.0
    sense_window_s: float = 0.0
    senses: dict[str, Any] = field(default_factory=dict)
    #: Measured rate, carried on the row so the roster and the page agree.
    messages_per_min: float = 0.0
    #: Why this stream is being let go, when it is.
    skip_reason: str = ""
    #: What the research said this channel is, for the verdict prompt to read.
    about: str = ""
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

    def window(self, wanted_s: float) -> tuple[str, float]:
        """(a path ffmpeg can read, how much of the tail it covers).

        Whole segments from the end rather than a seek: they are independently
        decodable, so this cannot land mid-GOP. The concat *protocol*, not the
        demuxer's list file - transport streams join byte-wise and ffmpeg reads
        that from one argument, where a list file handed to a plain -i produces
        no error and no output.
        """
        segments = self.buffer.segments()
        if not segments:
            raise RuntimeError("the buffer is still filling")
        wanted: list = []
        held = 0.0
        for segment in reversed(segments):
            wanted.insert(0, segment)
            held += segment.duration_s
            if held >= wanted_s:
                break
        return f"concat:{'|'.join(str(seg.path) for seg in wanted)}", held

    def listen_for_words(self, now: float) -> int:
        """Transcribe the window just read, if there is anything in it to hear.

        Metered twice over: only windows the ear says contain speech, and only
        while the day's minutes last. This is the one measurement in the whole
        watcher that costs money per minute of stream rather than per clip.
        """
        from core import speech as speechlib

        if not settings.speech_live or self.said is None:
            return 0
        share = getattr(self.heard, "speech_share", 0.0) if self.heard else 0.0
        if share < settings.speech_min_share:
            return 0
        if self.said.minutes_spent >= settings.speech_minutes_per_day:
            return 0
        if not settings.has_whisper:
            return 0

        try:
            joined, held = self.window(SENSE_WINDOW_S)
            words = speechlib.transcribe_window(
                joined, offset_s=self.chat_offset(0.0)
            )
        except Exception as exc:  # noqa: BLE001 - a silent minute is not a fault
            log.info("supervisor: no words for %s (%s)", self.channel, exc)
            return 0

        self.said.minutes_spent += held / 60.0
        before = len(self.said.words)
        self.said.extend(words)
        return len(self.said.words) - before

    def read_senses(self, out_dir: Path, *, now: float | None = None) -> dict[str, Any]:
        """Listen to and look at the last half minute, and remember what for.

        This is the expensive thing the watcher does and the only one that
        looks at the stream itself. Everything the scorer treats as evidence
        comes from here; chat only ever gets to agree with it.
        """
        from core import hearing, watching

        now = time.time() if now is None else now
        joined, held = self.window(SENSE_WINDOW_S)

        problems: list[str] = []
        try:
            self.heard = hearing.listen(joined)
        except Exception as exc:  # noqa: BLE001 - a deaf tick is not a dead one
            self.heard, _ = None, problems.append(f"hearing: {type(exc).__name__}: {exc}")
        try:
            self.seen = watching.watch(joined)
        except Exception as exc:  # noqa: BLE001
            self.seen, _ = None, problems.append(f"watching: {type(exc).__name__}: {exc}")

        self.senses_at = now
        self.sense_window_s = held
        try:
            self.listen_for_words(now)
        except Exception as exc:  # noqa: BLE001 - words are a bonus, not the job
            problems.append(f"words: {type(exc).__name__}: {exc}")
        self.senses = {
            "window_s": round(held, 1),
            "heard": self.heard.as_dict() if self.heard else None,
            "seen": self.seen.as_dict() if self.seen else None,
            "said": self.said.status() if self.said is not None else None,
            "problems": problems,
        }
        return self.senses

    # --- one clock ---------------------------------------------------------
    #
    # Three timelines meet here and getting them confused puts the clip
    # somewhere else entirely. The senses run 0..sense_window_s and end at the
    # live edge as it stood at senses_at. Chat offsets are measured from when
    # the buffer opened. The buffer itself only answers to "how long ago".

    def wall_time(self, window_s: float) -> float:
        """A position in the sense window, as a wall-clock instant."""
        return self.senses_at - (self.sense_window_s - window_s)

    def chat_offset(self, window_s: float) -> float:
        """...as an offset into the chat log, for quoting and mood."""
        return self.wall_time(window_s) - self.started_at

    def seconds_ago(self, window_s: float, *, now: float) -> float:
        """...and as seconds before the live edge right now, which is all the
        buffer can be asked about."""
        return max(0.0, now - self.wall_time(window_s))

    def window_position(self, chat_s: float) -> float:
        """The reverse: a chat offset, as a position in the sense window."""
        return self.sense_window_s - (self.senses_at - (self.started_at + chat_s))

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

        silent = bool(quiet and never_loud)
        # Somebody talking is somebody there, whatever the picture is doing.
        # Without this a podcast on a locked-off camera reads as an empty room.
        share = getattr(self.heard, "speech_share", None) if self.heard else None
        speaking = share is not None and share >= DORMANT_SPEECH
        asleep = bool(still) and not speaking

        return {
            "mean_db": mean_db,
            "peak_db": peak_db,
            "motion": motion,
            "speech_share": share,
            "quiet": silent,
            "still": bool(still),
            "speaking": bool(speaking),
            "asleep_now": asleep,
            # A silent still room is believed twice as fast as a still room
            # with a game running in it, which is the difference between "gone
            # to bed" and "gone to make a cup of tea".
            "weight": (
                (DORMANT_SILENT_WEIGHT if silent else DORMANT_STILL_WEIGHT) if asleep else 0
            ),
        }

    def read_motion(self) -> float | None:
        """How much the picture is moving, 0..1.

        Read off the eye rather than measured again. This used to run its own
        ffmpeg decode over the same seconds core.watching had just decoded -
        five decodes of overlapping windows per stream per pass, for a number
        one of them already had.
        """
        if self.seen is not None:
            return round(self.seen.average_motion, 5)
        return None

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
            "messages_per_min": self.messages_per_min,
            "about": self.about,
            "buffer": self.buffer.status(),
            "audio": self.audio,
            "activity": self.activity,
            "dormant": self.dormant,
            # What it actually heard and saw, which is what decides. The page
            # showing a score with no sensed evidence behind it is the whole
            # reason two worthless clips got cut.
            "senses": self.senses,
            "verdict": self.last_verdict,
            "senses_age_s": round(time.time() - self.senses_at, 1) if self.senses_at else None,
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
class Found:
    """A moment, and the three clocks it has to be expressed in."""

    score: float = 0.0
    why: dict[str, float] = field(default_factory=dict)
    #: Where in the sense window the evidence peaked.
    at_s: float = 0.0
    #: The same instant, as seconds before the live edge - what the buffer wants.
    ago_s: float = 0.0
    #: ...and as a chat-log offset - what the quotes and the mood want.
    chat_s: float = 0.0

    def __bool__(self) -> bool:
        return self.score > 0 and bool(self.why)

    @property
    def event_score(self) -> float:
        return sum(v for k, v in self.why.items() if k in moments.SENSED)

    @property
    def crowd_score(self) -> float:
        return sum(v for k, v in self.why.items() if k in moments.CROWD)

    @property
    def top_reason(self) -> str:
        return max(self.why, key=self.why.get) if self.why else ""


@dataclass
class Held:
    """A moment cut out of the buffer and kept while it waits for a slot.

    The buffer remembers five minutes and the gap between clips is an hour, so
    a moment that is not cut the instant it is found is simply gone. Cutting is
    cheap; deciding is not. So everything cuts immediately and only the winner
    is watched, cropped and stored.

    The context travels with it because the context expires: by the time this
    is used, chat has forgotten the whole thing.
    """

    channel: str
    found: Found
    raw: Path
    cut_at: float
    duration_s: float
    viewers: int = 0
    senses: dict[str, Any] = field(default_factory=dict)
    mood: dict[str, Any] = field(default_factory=dict)
    quotes: list[str] = field(default_factory=list)
    #: What the research knows about the channel. Carried because the model
    #: judging a clip of someone should know who they are - a man shouting at
    #: a boxing weigh-in reads differently from a man shouting at nobody.
    about: str = ""
    #: What was being said in the seconds around the moment, caught live.
    said: str = ""

    @property
    def megabytes(self) -> float:
        try:
            return round(self.raw.stat().st_size / 1e6, 1)
        except OSError:
            return 0.0

    def overlaps(self, other: Held) -> bool:
        """Two nominations of the same moment, seconds apart."""
        return self.channel == other.channel and abs(self.cut_at - other.cut_at) < COOLDOWN_S

    def discard(self) -> None:
        self.raw.unlink(missing_ok=True)

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "score": round(self.found.score, 1),
            "event_score": round(self.found.event_score, 1),
            "reason": self.found.top_reason,
            "held_s": round(time.time() - self.cut_at),
            "duration_s": self.duration_s,
            "megabytes": self.megabytes,
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
    #: Chat-only readers on streams further down the listing, so the ranking
    #: knows how alive they are before deciding to spend a buffer on them.
    probes: dict[str, Any] = field(default_factory=dict)
    #: Channels the bot has decided against, and why, so the page can say what
    #: it is skipping rather than silently showing a shorter list.
    skipped: dict[str, str] = field(default_factory=dict)
    #: When each candidate was watched, so the bill has a ceiling: a refused
    #: candidate is not stored, so it does not count against the clip cap and
    #: cannot throttle itself.
    looked: list[float] = field(default_factory=list)
    #: Candidates that were cut, watched and thrown away, so the page can show
    #: what the bot decided against as well as what it kept.
    declined: list[dict[str, Any]] = field(default_factory=list)
    #: Moments cut and kept, waiting for an output slot. Strongest first, and
    #: across every stream at once - the best moment of the hour is the point,
    #: not the best moment of each channel.
    shortlist: list[Held] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    running: bool = False

    # --- the roster ---------------------------------------------------------

    # --- what it will and will not watch -------------------------------------

    def still_wanted(self, watched: Watched) -> bool:
        """Is this stream still one the bot watches?

        Re-asked while watching, not only before attaching, because a stream
        changes underneath you: a Just Chatting stream that loads a game is a
        gaming stream, and a chat that has been running for a minute says more
        about the language than the directory ever did.
        """
        from core import profile as profiles

        if profiles.blocked_category(watched.category):
            watched.skip_reason = f"{watched.category} is a game"
            return False

        # Chat is the most honest language signal a stream has: the directory
        # tag is optional and the title is marketing, but an audience types in
        # the language it thinks in.
        lines = [m.text for m in watched.chat.log.recent()][-120:]
        found = profiles.from_chat(watched.channel, lines)
        if found is not None:
            profiles.remember(found)
            watched.skip_reason = found.reason
            return False
        return True

    def release_unwanted(self, watched: Watched, *, now: float) -> None:
        """Let a stream go, and remember not to pick it straight back up."""
        channel = watched.channel
        why = "asleep or away" if watched.dormant else (watched.skip_reason or "not wanted")
        self.dormant_until[channel] = now + DORMANT_REST_S
        self.roster.watching.pop(channel, None)
        self.fillers.discard(channel)
        watched.last_score = 0.0
        watched.last_why = {}
        watched.last_reason = "asleep" if watched.dormant else "not wanted"
        self._note(f"{channel}: letting go - {why}")
        self.release(channel)

    def wanted(self, listing: list, *, now: float) -> list:
        """The listing, with everything the bot does not watch taken out.

        The gaming streams it spent an evening on were not a detector failing.
        They were a list sorted by viewers with two filters that were fiction:
        the directory's language tag is optional, so a row that does not say
        gets kept, and there was no category filter at all.
        """
        from core import profile as profiles

        kept = []
        for entry in listing:
            probe = self.probes.get(entry.channel)
            chat = [m.text for m in probe.log.recent()][-120:] if probe else None
            try:
                found = profiles.decide(
                    entry.channel,
                    category=entry.category,
                    title=entry.title,
                    language=entry.language,
                    chat=chat,
                )
            except Exception as exc:  # noqa: BLE001 - one bad lookup is not a listing
                log.debug("supervisor: no profile for %s (%s)", entry.channel, exc)
                continue
            if found.eligible:
                entry.about = found.summary()
                kept.append(entry)
            else:
                self.skipped[entry.channel] = found.reason
        # Only the channels in this listing: a skip list that grows forever is
        # a memory leak with a nice name on it.
        self.skipped = {
            c: why for c, why in self.skipped.items()
            if c in {entry.channel for entry in listing}
        }
        return kept

    def measure_chat(self, listing: list, *, now: float) -> list:
        """Fill in how busy each candidate's chat is, and re-rank on it.

        Viewers alone put a stream with nine thousand people and a chat running
        at four hundred and seventy a minute below one with sixteen thousand
        and a chat doing a hundred and seventy. The first of those is where the
        clips are. Chat rate is the cheapest available proxy for how much is
        happening, and it costs a websocket, so it can be measured on streams
        the bot is not spending a buffer on.
        """
        wanted = [live.channel for live in listing[:PROBE_DEPTH]]

        for channel in list(self.probes):
            if channel not in wanted or channel in self.watching:
                probe = self.probes.pop(channel)
                try:
                    probe.stop()
                except Exception:  # noqa: BLE001 - a probe closing is not news
                    pass

        for channel in wanted:
            if channel in self.watching or channel in self.probes:
                continue
            try:
                probe = livechat.LiveChat(
                    channel=channel,
                    log=chatlib.LiveLog(window_s=PROBE_WINDOW_S),
                    origin=now,
                )
                probe.start()
                self.probes[channel] = probe
            except Exception as exc:  # noqa: BLE001 - one silent channel is not fatal
                log.debug("supervisor: no chat probe for %s (%s)", channel, exc)

        for entry in listing:
            watched = self.watching.get(entry.channel)
            source = watched.chat if watched else self.probes.get(entry.channel)
            if source is None:
                continue
            # Rate it against the time it has actually been listening, not the
            # window it would like to have: a probe up for forty seconds has
            # forty seconds of evidence, not two minutes of silence.
            listening = now - source.origin
            if listening < PROBE_SETTLE_S:
                continue
            held = source.log.recent()
            entry.messages_per_min = round(
                len(held) / max(min(listening, PROBE_WINDOW_S) / 60.0, 1e-6), 1
            )

        return roster.rank_streams(listing)

    def poll_roster(self, *, now: float | None = None) -> dict[str, list[str]]:
        """Refresh which channels are worth holding, and act on the change."""
        now = time.time() if now is None else now
        # Measure first, then filter: the chat probes are what tell a Hindi
        # stream from an English one, and they are opened by measure_chat.
        listing = self.measure_chat(
            roster.fetch_kick_live(limit=40, language="en"), now=now
        )
        listing = self.wanted(listing, now=now)
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
        watched.messages_per_min = getattr(entry, "messages_per_min", 0.0)
        watched.about = getattr(entry, "about", "") or watched.about
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
        from core import speech as speechlib

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
            channel=channel, buffer=buffer, chat=talk, started_at=started, viewers=viewers,
            said=speechlib.SpeechLog(window_s=settings.speech_window_s),
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

    def score(self, watched: Watched, *, now: float | None = None) -> Found:
        """The strongest moment in what was just heard and seen.

        Everything is fused on one clock - the sense window, running from the
        oldest half-minute the ear and the eye were given up to the live edge
        as it stood when they read it. Chat is mapped onto that clock rather
        than the other way round, because chat is the thing being asked to
        agree.

        Bursts and clip requests are found against the whole five minutes chat
        remembers and only then mapped in: a spike is only a spike next to
        enough history to know what normal is, and there is not enough of that
        inside half a minute. Detect wide, score narrow.
        """
        now = time.time() if now is None else now
        window_s = watched.sense_window_s
        if window_s < SCORE_WIDTH_S or (watched.heard is None and watched.seen is None):
            return Found()

        signals: dict[str, Any] = {}
        if watched.heard is not None:
            signals |= moments.signals_from_hearing(watched.heard, duration_s=window_s)
        if watched.seen is not None:
            signals |= moments.signals_from_watching(watched.seen, duration_s=window_s)

        if watched.said is not None and watched.said.words:
            from core import speech as speechlib

            spoken = speechlib.reactions(
                [
                    speechlib.Said(w.word, watched.window_position(w.at_s), 0.0, w.confidence)
                    for w in watched.said.words
                ]
            )
            signals |= moments.signals_from_speech(spoken, duration_s=window_s)

        full = watched.chat.log.curve()
        place = watched.window_position
        signals |= moments.signals_from_chat_events(
            requests=[place(t) for t, _ in full.clip_requests()],
            bursts=[(place(t), ratio) for t, ratio in full.bursts()],
            voices=[float(v) for v in full.voices],
            voices_grid_s=full.bucket_s or 1.0,
            duration_s=window_s,
        )

        found = moments.rank(
            signals, duration_s=window_s, clip_s=SCORE_WIDTH_S, top=1,
        )
        if not found:
            return Found()

        best = found[0]
        return Found(
            score=best.score,
            why=best.why,
            at_s=best.peak_s,
            ago_s=watched.seconds_ago(best.peak_s, now=now),
            chat_s=watched.chat_offset(best.peak_s),
        )

    @staticmethod
    def event_score(why: dict[str, float]) -> float:
        """The part of a score that came from something happening."""
        return sum(v for k, v in why.items() if k in moments.EVENTS)

    def tick(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """One pass over every watched channel. Returns whatever was caught."""
        now = time.time() if now is None else now
        caught: list[dict[str, Any]] = []

        for channel, watched in list(self.watching.items()):
            if not watched.buffer.running:
                self._note(f"{channel}: buffer stopped ({watched.buffer.failure()[:120]})")
                self.release(channel)
                continue

            # Listening and looking are the expensive things in this loop, so
            # they run on their own timer rather than every tick. The windows
            # overlap, so a moment cannot fall between two reads.
            if now - watched.senses_at >= SENSE_EVERY_S:
                try:
                    watched.read_senses(self.work_dir / "senses", now=now)
                except Exception as exc:  # noqa: BLE001 - a deaf tick is not a dead one
                    watched.senses_at = now
                    watched.heard = watched.seen = None
                    watched.senses = {"problems": [f"{type(exc).__name__}: {exc}"]}

            if now - watched.audio_at >= AUDIO_EVERY_S:
                watched.audio_at = now
                try:
                    watched.audio = watched.read_audio(self.work_dir / "audio")
                except Exception as exc:  # noqa: BLE001 - a graph is not the job
                    watched.audio = {"ok": False, "why": f"{type(exc).__name__}: {exc}"}
                try:
                    watched.activity = watched.read_activity()
                    weight = int(watched.activity.get("weight") or 0)
                    if weight:
                        was = watched.asleep_readings
                        watched.asleep_readings += weight
                        if was < DORMANT_READINGS <= watched.asleep_readings:
                            self._note(
                                f"{channel} looks asleep or away - freeing the slot"
                            )
                    else:
                        watched.asleep_readings = 0
                except Exception:  # noqa: BLE001 - never let this stop a tick
                    watched.asleep_readings = 0

            # A stream that has become something the bot does not watch - a
            # streamer who has just loaded a game, or one whose chat has made
            # the language obvious - is let go now, not at the next roster
            # poll five minutes from now.
            if watched.dormant or not self.still_wanted(watched):
                self.release_unwanted(watched, now=now)
                continue

            found = self.score(watched, now=now)
            watched.last_score = found.score
            watched.last_why = found.why
            watched.last_reason = found.top_reason

            if not found:
                continue

            # Two bars, and both are about whether this is worth anyone's
            # time. The caps decide how many clips a day; these decide whether
            # there is a clip at all. Without them the watcher cut its best
            # five minutes of nothing every hour - a betting screen with music
            # over it, scored 18.0, entirely on how many people were typing.
            if found.score < settings.live_min_score:
                watched.last_reason = "too weak"
                continue
            if found.event_score < settings.live_min_event_score:
                watched.last_reason = "nothing happened"
                continue
            if now - watched.last_catch_at < COOLDOWN_S:
                continue

            # Cut it now and decide later. The output slot may be fifty
            # minutes away and the buffer only remembers five, so waiting for
            # permission means the moment is gone before it is granted - which
            # is why the watcher used to clip whatever happened to be
            # happening when the hour turned over.
            try:
                candidate = self.cut(watched, found=found, now=now)
                watched.last_catch_at = now
                if candidate is not None:
                    self.shortlist_add(candidate)
            except Exception as exc:  # noqa: BLE001 - a failed cut is not fatal
                self._note(f"{channel}: cut failed ({exc})")

        # ...and if there is somewhere for one to go, spend it on the best
        # thing being held rather than on whatever was cut most recently.
        caught.extend(self.harvest(now=now))
        return caught

    # --- looking at it -------------------------------------------------------

    def consider(self, candidate: Held, *, transcript: str = ""):  # noqa: ANN201
        """Transcribe the candidate and have a model watch it.

        Deliberately the last thing that happens and the only thing here that
        costs money per use, which is why every cheap signal runs first: a
        stream produces 86,400 seconds a day and this runs on a dozen of them.
        """
        from core import verdict as verdictlib

        if not settings.verdict_enabled:
            return verdictlib.Verdict(problems=["looking is switched off"])
        if len(self.looked) >= settings.verdict_per_day:
            return verdictlib.Verdict(
                problems=[f"the daily look budget of {settings.verdict_per_day} is spent"]
            )

        self.looked.append(time.time())
        del self.looked[: max(0, len(self.looked) - 500)]

        return verdictlib.look(
            candidate.raw,
            evidence=candidate.senses,
            about=candidate.about,
            said=candidate.said,
            transcript=transcript,
            quotes=[q["text"] for q in _top_quotes(candidate.quotes)],
            count=settings.verdict_frames,
        )

    @staticmethod
    def transcribe(raw: Path) -> str:
        """What was actually said, if there is anything configured to hear it.

        Optional on purpose: the frames carry most of the judgement and a
        missing transcript should cost a little accuracy, not the clip.
        """
        if not settings.has_whisper:
            return ""
        try:
            from core import transcription

            found = transcription.transcribe(raw)
            return str(found.get("text") or "")[:4000]
        except Exception as exc:  # noqa: BLE001 - a silent clip is still a clip
            log.info("supervisor: no transcript for %s (%s)", raw.name, exc)
            return ""

    @staticmethod
    def _acceptable(judged) -> bool:  # noqa: ANN001 - verdict.Verdict
        return bool(judged.worth_it) and judged.confidence >= settings.verdict_min_confidence

    @staticmethod
    def _tighten(raw: Path, judged, out_dir: Path) -> Path:  # noqa: ANN001
        """Trim to the part the model said was the moment, if it named one."""
        start, end = judged.best_start_s, judged.best_end_s
        if start is None or end is None or end - start < 6.0:
            return raw
        trimmed = out_dir / f"{raw.stem}-tight.mp4"
        proc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, start):.2f}",
             "-t", f"{end - start:.2f}", "-i", str(raw),
             "-c", "copy", "-avoid_negative_ts", "make_zero", str(trimmed)],
            capture_output=True,
        )
        if proc.returncode != 0 or not trimmed.exists() or trimmed.stat().st_size == 0:
            return raw
        raw.unlink(missing_ok=True)
        return trimmed

    def cut(self, watched: Watched, *, found: Found, now: float) -> Held | None:
        """Take the moment out of the buffer and keep it, undecided.

        Cheap on purpose - an extract and a copy, no reframe and no model. The
        expensive half waits until there is somewhere for the clip to go, so
        that money is spent on the best moment of the hour rather than on the
        first one.

        Everything the finished record needs is snapshotted here rather than
        read later, because "later" can be forty minutes and by then chat has
        forgotten the whole thing: the log only remembers five minutes.
        """
        held = watched.chat.log.recent()
        out_dir = Path(settings.work_dir) / "catches"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        raw = out_dir / f"{watched.channel}-{stamp}-raw.mp4"

        # How long the moment actually ran, rather than a fixed length. The
        # lead is fixed because a clip that opens on the punchline is a clip
        # nobody understands; the tail is whatever chat says it is.
        trail_s = moments.moment_end(
            watched.chat.log.curve(),
            found.chat_s,
            min_s=settings.live_trail_s,
            max_s=max(settings.live_trail_s, settings.live_max_clip_s - settings.live_lead_s),
        )
        # ...but the future has not happened yet. A moment whose peak was ten
        # seconds ago cannot have thirty seconds of tail, and asking the buffer
        # for footage past the live edge gets whatever it has plus a shorter
        # clip than the record claims. Clamp to what exists and record that.
        trail_s = min(trail_s, found.ago_s)
        try:
            watched.buffer.extract(
                raw, ago_s=found.ago_s, lead_s=settings.live_lead_s, trail_s=trail_s
            )
        except Exception as exc:  # noqa: BLE001 - one bad extract is not fatal
            self._note(f"{watched.channel}: could not cut ({exc})")
            raw.unlink(missing_ok=True)
            return None

        return Held(
            channel=watched.channel,
            found=found,
            raw=raw,
            cut_at=now,
            duration_s=round(settings.live_lead_s + trail_s, 1),
            viewers=watched.viewers,
            senses=dict(watched.senses),
            mood=chatlib.mood_around(held, found.chat_s, window_s=8.0),
            quotes=chatlib.quotes_around(held, found.chat_s, window_s=8.0),
            about=watched.about,
            said=(
                watched.said.text_around(found.chat_s, window_s=12.0)
                if watched.said is not None else ""
            ),
        )

    def shortlist_add(self, candidate: Held) -> None:
        """Keep it if it is worth keeping, and drop the weakest if it is not.

        The shortlist is what turns this from a watcher into a chooser. With
        one clip an hour, cutting the first moment that clears the bar is
        picking at random from everything that hour held; keeping several and
        taking the best is the entire difference between the two.
        """
        self.shortlist.append(candidate)
        self.shortlist.sort(key=lambda c: -c.found.score)
        for spare in self.shortlist[settings.live_shortlist_max :]:
            spare.discard()
        del self.shortlist[settings.live_shortlist_max :]

    def prune_shortlist(self, *, now: float) -> None:
        """Forget held moments that have gone stale."""
        keeping: list[Held] = []
        for candidate in self.shortlist:
            if now - candidate.cut_at > settings.live_hold_max_s:
                log.info("supervisor: letting go of %s, held too long", candidate.raw.name)
                candidate.discard()
            elif not candidate.raw.exists():
                pass  # the file went; so does the candidate
            else:
                keeping.append(candidate)
        self.shortlist = keeping

    def harvest(self, *, now: float) -> list[dict[str, Any]]:
        """Spend an open output slot on the best moment being held.

        Tried strongest first: a candidate the model refuses is dropped and
        the next one is offered, because a refusal says this moment is not
        worth posting, not that the hour had nothing in it.
        """
        made: list[dict[str, Any]] = []
        self.prune_shortlist(now=now)
        while self.shortlist and self.allowed(now=now):
            candidate = self.shortlist.pop(0)
            try:
                record = self.finish(candidate, now=now)
            except Exception as exc:  # noqa: BLE001 - a failed cut is not fatal
                self._note(f"{candidate.channel}: could not finish ({exc})")
                candidate.discard()
                continue
            if record is not None:
                made.append(record)
                break
        return made

    def finish(self, candidate: Held, *, now: float) -> dict[str, Any] | None:
        """Watch it, crop it, store it. The expensive half."""
        watched = self.watching.get(candidate.channel)
        out_dir = candidate.raw.parent
        final = out_dir / f"{candidate.raw.stem.removesuffix('-raw')}.mp4"

        # Look at it before it becomes a clip. Everything up to here is
        # arithmetic, and arithmetic cannot tell a man laughing at his own
        # joke about nothing from a man falling off a chair - they produce the
        # same envelope. This is the only step that can.
        spoken = self.transcribe(candidate.raw)
        judged = self.consider(candidate, transcript=spoken)
        if watched is not None:
            watched.last_verdict = judged.as_dict()

        if judged.watched and not self._acceptable(judged):
            candidate.discard()
            self.declined.append({
                "channel": candidate.channel,
                "at": datetime.fromtimestamp(candidate.cut_at, UTC).isoformat(),
                "score": round(candidate.found.score, 1),
                "why": judged.why or "nothing worth showing",
                "happening": judged.happening,
                "confidence": round(judged.confidence, 2),
            })
            del self.declined[:-8]
            log.info(
                "supervisor: declined %s after watching it (%s)",
                candidate.channel, judged.happening or judged.why,
            )
            return None
        if settings.verdict_required and settings.verdict_enabled and not judged.watched:
            candidate.discard()
            self._note(
                f"{candidate.channel}: not cutting something nothing has watched "
                f"({'; '.join(judged.problems) or 'no verdict'})"
            )
            return None

        # The model may have found the good part inside the window it was
        # given. Trusting it about where the moment is, is the same act as
        # trusting it about whether there is one.
        raw = self._tighten(candidate.raw, judged, out_dir)

        reframe.to_portrait(raw, final, work_dir=out_dir / "tmp")
        raw.unlink(missing_ok=True)

        # The clip is written here, on the worker, and watched in a browser
        # talking to the web service. Those are different containers with
        # different disks, so a path is not a way to hand it over - the first
        # catch was recorded perfectly and then could not be played, because
        # the file it named existed only on the machine that made it.
        stored = self.publish_clip(final)
        why = candidate.found.why

        record = {
            "channel": candidate.channel,
            "source_url": f"https://kick.com/{candidate.channel}",
            "path": str(final),
            "storage_key": stored,
            "at_s": round(candidate.found.chat_s, 2),
            "duration_s": candidate.duration_s,
            "score": round(sum(why.values()), 3),
            "why": {k: round(v, 3) for k, v in sorted(why.items(), key=lambda kv: -kv[1])},
            "heard": (candidate.senses or {}).get("heard"),
            "seen": (candidate.senses or {}).get("seen"),
            "verdict": judged.as_dict(),
            "transcript": spoken,
            # What the ear claimed, next to what the model actually heard.
            # These two columns are the only way either detector will ever be
            # checked against real audio, so they are kept on every clip.
            "ear_vs_model": {
                "ear": _ear_said(candidate.senses),
                "model": judged.heard,
            },
            "mood": candidate.mood,
            "quotes": _top_quotes(candidate.quotes),
            "peak_viewers": candidate.viewers,
            # When the moment happened, not when the slot opened for it.
            "caught_at": datetime.fromtimestamp(candidate.cut_at, UTC).isoformat(),
            "held_s": round(now - candidate.cut_at, 1),
        }
        self.store(record)
        log.info(
            "supervisor: kept %s (%s, score %.1f, held %.0fs) -> %s",
            candidate.channel, judged.kind or "unread",
            record["score"], now - candidate.cut_at, final.name,
        )
        return record

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
                    verdict=record.get("verdict") or {},
                    transcript=record.get("transcript") or None,
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
            "shortlist": [c.as_dict() for c in self.shortlist],
            "skipped": [
                {"channel": c, "why": why} for c, why in list(self.skipped.items())[:8]
            ],
            "declined": self.declined[-6:],
            "looked_today": len(self.looked),
            "look_budget": settings.verdict_per_day,
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
        for channel in list(self.probes):
            try:
                self.probes.pop(channel).stop()
            except Exception:  # noqa: BLE001
                pass
        # Held clips are only meaningful while this run is: nothing else knows
        # where they are, so leaving them behind is just a disk bill.
        for candidate in self.shortlist:
            candidate.discard()
        self.shortlist.clear()

    def _note(self, message: str) -> None:
        log.warning("supervisor: %s", message)
        self.errors.append(f"{datetime.now(UTC).strftime('%H:%M:%S')} {message}")
        del self.errors[:-20]


def _ear_said(senses: dict[str, Any] | None) -> dict[str, Any]:
    """What core.hearing claimed, in the same shape the model answers in."""
    heard = (senses or {}).get("heard") or {}
    return {
        "laughter": bool(heard.get("laughs")),
        "gasp": bool(heard.get("gasps")),
        "raised_voice": bool(heard.get("shouts")),
        "sigh": bool(heard.get("sighs")),
    }


def _top_quotes(quotes: list[str], limit: int = 6) -> list[dict[str, Any]]:
    from collections import Counter

    return [
        {"text": text, "count": n} for text, n in Counter(quotes).most_common(limit)
    ]
