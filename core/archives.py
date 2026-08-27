"""The archives the studio makes videos from, and the shape of those videos.

Everything here is public record: US federal works, FOIA releases, and one
shortwave transmitter anyone can record off the air. Each archive carries its
own look, its own sound bed and its own shot template, because a numbers
station should not look like a mission control loop.

The one rule that matters is on `Beat.verbatim`. A caption marked verbatim is
quoted from the record and must stay word for word; a caption marked otherwise
is written and is only ever narration. The whole premise of the format is that
the recording is real, so a written line presented as a quote is the single
mistake that ends it - hence a flag on every line rather than a note in a
docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- how footage is pushed into a source's world ---------------------------


@dataclass(frozen=True)
class Grade:
    """A colour treatment, applied to whatever stock clip is underneath.

    Two clips from completely different shoots come out of the same grade
    looking like the same camera on the same day. That, rather than owning
    more clips, is what stops stock footage reading as stock.
    """

    gray: float = 0.0
    sepia: float = 0.0
    hue: float = 0.0
    sat: float = 1.0
    contrast: float = 1.0
    brightness: float = 1.0
    tint: str = "#FFFFFF"
    tint_alpha: float = 0.0

    def ffmpeg_filter(self, strength: float = 1.0) -> str:
        """This grade as an ffmpeg filter chain, scaled by `strength` 0..1.

        `colorchannelmixer` does the desaturation and sepia in one pass; `eq`
        handles contrast, brightness and what saturation is left over. Both
        are cheap enough to run on every frame without thinking about it.
        """
        k = max(0.0, min(1.0, strength))
        parts: list[str] = []

        # Luma weights, blended toward identity by k. At k=1 with gray=1 every
        # channel becomes the same weighted average, i.e. true monochrome.
        g = self.gray * k
        if g > 0.001:
            rr, gg, bb = 0.299, 0.587, 0.114
            parts.append(
                "colorchannelmixer="
                f"{1 - g + rr * g:.3f}:{gg * g:.3f}:{bb * g:.3f}:0:"
                f"{rr * g:.3f}:{1 - g + gg * g:.3f}:{bb * g:.3f}:0:"
                f"{rr * g:.3f}:{gg * g:.3f}:{1 - g + bb * g:.3f}:0"
            )

        sep = self.sepia * k
        if sep > 0.001:
            parts.append(
                "colorchannelmixer="
                f"{1 - 0.607 * sep:.3f}:{0.769 * sep:.3f}:{0.189 * sep:.3f}:0:"
                f"{0.349 * sep:.3f}:{1 - 0.314 * sep:.3f}:{0.168 * sep:.3f}:0:"
                f"{0.272 * sep:.3f}:{0.534 * sep:.3f}:{1 - 0.869 * sep:.3f}:0"
            )

        sat = 1 + (self.sat - 1) * k
        con = 1 + (self.contrast - 1) * k
        bri = 1 + (self.brightness - 1) * k
        # ffmpeg's eq takes brightness as an offset around zero, not a factor.
        parts.append(
            f"eq=contrast={con:.3f}:brightness={bri - 1:.3f}:saturation={max(0.0, sat):.3f}"
        )

        if abs(self.hue * k) > 0.5:
            parts.append(f"hue=h={self.hue * k:.1f}")

        return ",".join(parts)


# --- the shot template -----------------------------------------------------

BEAT_KINDS = ("hook", "title", "tape", "narration", "ambient", "close")


@dataclass(frozen=True)
class Beat:
    """One shot.

    `seconds` is the shot's length, not its start - the template is a running
    order, so inserting or dropping a hook re-times everything after it without
    a table of offsets to keep in step.
    """

    kind: str
    seconds: float
    text: str = ""
    verbatim: bool = False
    overline: str = ""
    #: which drawn treatment to use; a source may have two so consecutive
    #: shots do not sit on an identical frame
    look: str = "main"

    @property
    def narrated(self) -> bool:
        """Whether this shot's text is spoken by the AI narrator."""
        return self.kind in {"narration", "close", "hook"}

    @property
    def from_tape(self) -> bool:
        """Whether this shot plays the archive recording underneath."""
        return self.kind in {"tape", "ambient"}


@dataclass(frozen=True)
class Archive:
    id: str
    name: str
    source: str
    #: one-line note on which captions are quoted and which are written
    provenance: str
    accent: str
    grade: Grade
    #: procedural ambience laid under everything: see core/beds.py
    bed: str
    #: Pexels searches, tried in order, for the footage underneath
    stock_terms: tuple[str, ...]
    title_card: tuple[str, str]
    #: archive.org identifier, when the recording can be fetched directly
    archive_item: str | None
    #: where a person can go and listen to the real thing
    listen_url: str
    #: seconds into the fetched recording where the interesting part starts
    tape_offset_s: float
    hook_cold: Beat
    hook_voice: Beat
    beats: tuple[Beat, ...]

    def running_order(self, voice_hook: bool = False) -> list[Beat]:
        return [self.hook_voice if voice_hook else self.hook_cold, *self.beats]

    def duration_s(self, voice_hook: bool = False) -> float:
        return sum(b.seconds for b in self.running_order(voice_hook))

    def timeline(self, voice_hook: bool = False) -> list[tuple[float, float, Beat]]:
        """(start, end, beat), which is what every renderer actually wants."""
        out: list[tuple[float, float, Beat]] = []
        at = 0.0
        for beat in self.running_order(voice_hook):
            out.append((at, at + beat.seconds, beat))
            at += beat.seconds
        return out

    @property
    def fetchable(self) -> bool:
        """Whether the worker can get the recording without a human."""
        return self.archive_item is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "provenance": self.provenance,
            "accent": self.accent,
            "listen_url": self.listen_url,
            "fetchable": self.fetchable,
            "archive_item": self.archive_item,
            "tape_offset_s": self.tape_offset_s,
            "stock_terms": list(self.stock_terms),
            "duration_s": round(self.duration_s(), 1),
            "duration_voice_hook_s": round(self.duration_s(voice_hook=True), 1),
            "title_card": list(self.title_card),
            "verbatim_lines": sum(1 for b in self.beats if b.verbatim),
            "written_lines": sum(1 for b in self.beats if b.text and not b.verbatim),
        }


# --- the six ---------------------------------------------------------------

APOLLO = Archive(
    id="apollo",
    name="Apollo 13",
    source="NASA flight director loop · public domain",
    provenance=(
        "Verbatim. Swigert and Lovell's lines and Lousma's reply are exactly as "
        "recorded at 55:55:19 ground elapsed time. Narration is written."
    ),
    accent="#1FE0A8",
    grade=Grade(
        gray=0.45,
        hue=-14,
        sat=0.7,
        contrast=1.35,
        brightness=0.58,
        tint="#0E3A44",
        tint_alpha=0.16,
    ),
    bed="radio",
    stock_terms=("earth from space", "space nebula", "rocket launch", "stars night sky"),
    title_card=("APOLLO 13", "55:55:20 ground elapsed"),
    archive_item="Apollo13Audio",
    listen_url="https://apolloinrealtime.org/13/",
    tape_offset_s=0.0,
    # The cold open plays the recording, so it is a tape beat: it must never be
    # handed to the narrator, and its words are the real ones, truncated.
    hook_cold=Beat(
        "tape",
        3.0,
        "Okay, Houston —",
        verbatim=True,
        overline="This is the actual recording",
    ),
    hook_voice=Beat("hook", 3.0, "Fifty-five hours in, something exploded."),
    beats=(
        Beat("title", 4.5),
        Beat(
            "tape",
            6.5,
            "Okay, Houston, we've had a problem here.",
            verbatim=True,
            overline="13 April 1970",
        ),
        Beat(
            "tape",
            5.0,
            "This is Houston. Say again, please.",
            verbatim=True,
            overline="13 April 1970",
        ),
        Beat(
            "narration",
            6.5,
            "A main bus undervolt. The power is dying, two hundred thousand miles from home.",
            look="alt",
        ),
        Beat(
            "tape",
            6.5,
            "Houston, we've had a problem. We've had a main B bus undervolt.",
            verbatim=True,
            overline="13 April 1970",
        ),
        Beat(
            "close",
            6.0,
            "Nobody in that room knew yet how bad it already was.",
            look="alt",
            overline="Part 1 of 4",
        ),
    ),
)

AIR_FORCE_ONE = Archive(
    id="af1",
    name="Air Force One",
    source="National Archives · 22 November 1963 · public domain",
    provenance=(
        "Narration only. This template ships no quoted lines because the tape has "
        "not been transcribed here - captions must come from the real transcript "
        "before anything is published."
    ),
    accent="#B03A2B",
    grade=Grade(
        gray=1.0,
        sepia=0.15,
        contrast=1.55,
        brightness=0.55,
        tint="#B03A2B",
        tint_alpha=0.10,
    ),
    bed="prop",
    stock_terms=(
        "vintage aircraft",
        "clouds from airplane window",
        "old film grain",
        "storm clouds aerial",
    ),
    title_card=("AIR FORCE ONE", "22 Nov 1963 · 14:32 CST"),
    archive_item=None,
    listen_url="https://www.archives.gov/research/jfk/air-force-one-tape",
    tape_offset_s=0.0,
    hook_cold=Beat("hook", 3.0, "This tape sat in a box for forty-eight years."),
    hook_voice=Beat("hook", 3.0, "They recorded the whole flight home."),
    beats=(
        Beat("title", 4.5),
        Beat("ambient", 6.0, overline="Air-to-ground · in flight"),
        Beat(
            "narration",
            6.5,
            "They are still in the air. The president is dead. The country has not been told.",
            look="alt",
        ),
        Beat("ambient", 6.0, overline="Air-to-ground · in flight"),
        Beat(
            "narration",
            5.5,
            "Two hours and twenty-two minutes of this exist. Almost nobody has heard it.",
            look="alt",
        ),
        Beat("close", 5.5, "This is minute eleven.", overline="Part 1 of 6"),
    ),
)

BUZZER = Archive(
    id="buzzer",
    name="UVB-76",
    source="4625 kHz · still transmitting · record it yourself",
    provenance=(
        "Narration only. The buzz itself is the content; any voice break shown as "
        "a caption must come from a dated enthusiast log, never from a script."
    ),
    accent="#7FFF6A",
    grade=Grade(
        gray=1.0,
        sepia=0.85,
        hue=75,
        sat=3.2,
        contrast=1.8,
        brightness=0.34,
        tint="#7FFF6A",
        tint_alpha=0.30,
    ),
    bed="buzz",
    stock_terms=("radio tower night", "empty forest fog", "abandoned building", "static noise"),
    title_card=("UVB-76", "4625 kHz · continuous"),
    archive_item="sraa-the-buzzer-uvb-76-numbers-station-may-13-2018",
    listen_url="https://shortwavearchive.com/",
    tape_offset_s=0.0,
    hook_cold=Beat("hook", 3.0, "This has been transmitting since before you were born."),
    hook_voice=Beat("hook", 3.0, "Nobody knows who this is for."),
    beats=(
        Beat("title", 4.5),
        Beat(
            "ambient",
            6.5,
            "Twenty-five tones a minute. Every minute. Since the seventies.",
            overline="Live · 4625 kHz",
        ),
        Beat("narration", 6.0, "No government has ever explained what it is."),
        Beat("ambient", 6.0, overline="Voice break · rare", look="alt"),
        Beat(
            "narration",
            6.0,
            "Then the buzzing comes back, and that is all that ever happens.",
            look="alt",
        ),
        Beat("close", 6.0, "It is transmitting right now, while you watch this."),
    ),
)

GIMBAL = Archive(
    id="uap",
    name="Gimbal",
    source="NAVAIR · cleared for public release · public domain",
    provenance=(
        "Verbatim. Both cockpit lines are as recorded on the Department of Defense "
        "release. Narration is written."
    ),
    accent="#FFFFFF",
    grade=Grade(gray=1.0, contrast=2.0, brightness=0.78, tint="#FFFFFF", tint_alpha=0.08),
    bed="cockpit",
    stock_terms=("fighter jet", "clouds aerial view", "military aircraft", "sky horizon"),
    title_card=("GIMBAL", "Dept. of Defense · released 2020"),
    archive_item=None,
    listen_url="https://www.navair.navy.mil/foia/documents",
    tape_offset_s=0.0,
    hook_cold=Beat("hook", 3.0, "The Pentagon released this themselves."),
    hook_voice=Beat("hook", 3.0, "This is the official copy. Not a leak."),
    beats=(
        Beat("title", 4.5),
        Beat(
            "tape",
            5.5,
            "There's a whole fleet of them. Look on the ASA.",
            verbatim=True,
            overline="Cockpit audio · F/A-18",
        ),
        Beat(
            "tape",
            5.5,
            "They're all going against the wind. The wind is 120 knots out of the west.",
            verbatim=True,
            overline="Cockpit audio · F/A-18",
        ),
        Beat(
            "narration",
            6.5,
            "Whatever they were watching was flying into a hundred and twenty knot wind. Together.",
            look="alt",
        ),
        Beat("narration", 6.0, "AARO has reviewed this. It is still listed as unresolved."),
        Beat(
            "close",
            6.0,
            "Nobody had to leak it. They handed it over.",
            look="alt",
            overline="Part 1 of 3",
        ),
    ),
)

STARGATE = Archive(
    id="stargate",
    name="STARGATE",
    source="CIA reading room · 12,473 documents · public domain",
    provenance=(
        "Narration only. Real session transcripts are in the reading room and read "
        "stranger than anything written for them - pull the lines from there."
    ),
    accent="#E0A15C",
    grade=Grade(
        gray=0.2,
        sepia=0.75,
        hue=-8,
        sat=1.35,
        contrast=1.25,
        brightness=0.5,
        tint="#E0A15C",
        tint_alpha=0.22,
    ),
    bed="room",
    stock_terms=("ink in water", "smoke abstract", "desert landscape", "old paper texture"),
    title_card=("PROJECT STARGATE", "Session transcript · declassified 2017"),
    archive_item=None,
    listen_url="https://www.cia.gov/readingroom/collection/stargate",
    tape_offset_s=0.0,
    hook_cold=Beat("hook", 3.0, "The CIA paid people to do this for twenty years."),
    hook_voice=Beat("hook", 3.0, "They wrote down every word of it."),
    beats=(
        Beat("title", 4.5),
        Beat("narration", 6.0, "The viewer is given a set of coordinates. Nothing else."),
        Beat("narration", 7.0, "He is asked to describe a place he has never been.", look="alt"),
        Beat("narration", 6.0, "The programme ran for two decades on public money."),
        Beat(
            "narration",
            6.0,
            "It was closed in 1995 and the files were released in 2017.",
            look="alt",
        ),
        Beat(
            "close",
            6.0,
            "Twelve thousand four hundred and seventy-three documents survived it.",
            overline="Part 1 of 5",
        ),
    ),
)

NIXON = Archive(
    id="nixon",
    name="The Nixon Tapes",
    source="Miller Center · 3,400 hours · public domain",
    provenance=(
        "Verbatim. Both lines are from the smoking-gun conversation of 23 June "
        "1972, Oval Office, 10:04 a.m. Narration is written."
    ),
    accent="#B9975B",
    grade=Grade(
        gray=0.25,
        sepia=0.6,
        hue=-4,
        sat=0.85,
        contrast=1.2,
        brightness=0.46,
        tint="#B9975B",
        tint_alpha=0.20,
    ),
    bed="tape",
    stock_terms=("old reel to reel tape", "vintage office", "typewriter", "washington dc"),
    title_card=("EOB 342", "23 June 1972 · 10:04 a.m."),
    archive_item=None,
    listen_url="https://millercenter.org/the-presidency/secret-white-house-tapes",
    tape_offset_s=0.0,
    hook_cold=Beat("hook", 3.0, "He recorded himself. On purpose."),
    hook_voice=Beat("hook", 3.0, "He installed the machine that ended him."),
    beats=(
        Beat("title", 4.5),
        Beat(
            "tape",
            6.5,
            "Look, the problem is that this will open the whole, the whole Bay of Pigs thing.",
            verbatim=True,
            overline="Verbatim · 23 June 1972",
        ),
        Beat("narration", 5.5, "That sentence is the reason he resigned.", look="alt"),
        Beat(
            "tape",
            7.0,
            "Don't lie to them to the extent to say there is no involvement, but just "
            "say this is sort of a comedy of errors.",
            verbatim=True,
            overline="Verbatim · 23 June 1972",
        ),
        Beat(
            "narration",
            5.5,
            "Nobody in the room knew the machine was running. He had it installed himself.",
            look="alt",
        ),
        Beat(
            "close",
            5.5,
            "Three thousand four hundred hours of this exist.",
            overline="Part 1 of 8",
        ),
    ),
)

ARCHIVES: dict[str, Archive] = {
    a.id: a for a in (APOLLO, AIR_FORCE_ONE, BUZZER, GIMBAL, STARGATE, NIXON)
}

#: Presentation order for the dashboard: strongest material first.
ORDER: tuple[str, ...] = ("apollo", "nixon", "uap", "af1", "buzzer", "stargate")


def get(archive_id: str) -> Archive:
    try:
        return ARCHIVES[archive_id]
    except KeyError:
        known = ", ".join(ORDER)
        raise KeyError(f"unknown archive {archive_id!r}; expected one of {known}") from None


def listing() -> list[dict[str, Any]]:
    return [ARCHIVES[a].as_dict() for a in ORDER if a in ARCHIVES]


# --- caption timing --------------------------------------------------------


@dataclass
class CaptionWord:
    w: str
    start: float
    end: float

    def as_dict(self) -> dict[str, Any]:
        return {"w": self.w, "start": round(self.start, 3), "end": round(self.end, 3)}


def spread_words(text: str, start: float, end: float, fill: float = 0.82) -> list[CaptionWord]:
    """Lay a line's words evenly across its shot.

    A stand-in for real word timings, used for narration - where the words are
    ours and there is nothing to align to - and for any shot whose audio has
    not been transcribed. `fill` leaves the tail of the shot silent so the last
    word does not butt against the cut.

    Longer words are given proportionally more time, which is closer to speech
    than an even split and costs nothing.
    """
    words = [w for w in text.split() if w]
    if not words or end <= start:
        return []
    span = (end - start) * max(0.1, min(1.0, fill))
    weights = [max(1.0, len(w) ** 0.6) for w in words]
    total = sum(weights)
    out: list[CaptionWord] = []
    at = start
    for word, weight in zip(words, weights, strict=True):
        width = span * weight / total
        out.append(CaptionWord(word, at, at + width))
        at += width
    return out


def caption_words(archive: Archive, voice_hook: bool = False) -> list[dict[str, Any]]:
    """Every caption in the video, as the word list the ASS builder expects."""
    words: list[CaptionWord] = []
    for start, end, beat in archive.timeline(voice_hook):
        if not beat.text:
            continue
        words.extend(spread_words(beat.text, start, end))
    return [w.as_dict() for w in words]


# --- what a render still needs before it can run ---------------------------


@dataclass
class Readiness:
    ok: bool
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "missing": self.missing, "notes": self.notes}


def readiness(archive: Archive, *, has_tts: bool, has_stock: bool) -> Readiness:
    """What is missing, in the words of the variable that fixes it.

    A render never hard-fails for want of a key: without stock the overlay
    plays over its own drawn plate, without narration the archive audio carries
    it alone. This says what you would get, so the dashboard can warn rather
    than refuse.
    """
    missing: list[str] = []
    notes: list[str] = []

    if not has_tts:
        missing.append("OPENAI_API_KEY")
        notes.append("no narration - the video will be archive audio and captions only")
    if not has_stock:
        missing.append("PEXELS_API_KEY")
        notes.append("no stock footage - the overlay will play over its own drawn plate")
    if not archive.fetchable:
        notes.append(
            f"{archive.name} has no fetchable recording - upload one, or the video "
            "renders with narration over ambience"
        )
    return Readiness(ok=not missing, missing=missing, notes=notes)
