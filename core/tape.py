"""Find the moment in a recording, and know what is being said in it.

This is the part the studio was missing, and its absence produced exactly one
kind of failure: a video whose captions were a script and whose audio was
whatever happened to be at the start of the file. Two things that had nothing
to do with each other, presented as if one were a transcript of the other.

The rule that follows from that, and which the rest of the studio enforces:
**never play tape that has not been transcribed.** A recording we cannot
caption honestly is one we must not use, because the whole premise of the
format is that the words on screen are the words on the tape.

The flow is: cut a scan window out of a long recording, transcribe it for word
timings, ask Claude which stretch of it is worth watching, then cut that.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import transcription
from core.config import settings
from core.ffmpeg_ops import FFmpegError, require_binaries

log = logging.getLogger(__name__)

#: Bounds on the stretch of tape a video is built around. Under this it is a
#: fragment with no shape; over it and the narration cannot frame it inside a
#: short-form runtime.
MIN_MOMENT_S = 10.0
MAX_MOMENT_S = 32.0
DEFAULT_MOMENT_S = 22.0


class TapeError(RuntimeError):
    pass


@dataclass
class Moment:
    """A stretch of a recording worth building a video around."""

    start_s: float
    end_s: float
    #: what is actually said in it, from the transcript - never written
    transcript: str
    #: word timings, relative to the start of the moment
    words: list[dict[str, Any]] = field(default_factory=list)
    #: three lines of narration written about *this* moment
    hook: str = ""
    context: str = ""
    closer: str = ""
    why: str = ""
    #: how it was chosen, for the dashboard: "claude" or "density"
    chosen_by: str = "density"

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_s": round(self.start_s, 2),
            "end_s": round(self.end_s, 2),
            "duration_s": round(self.duration_s, 2),
            "transcript": self.transcript,
            "hook": self.hook,
            "context": self.context,
            "closer": self.closer,
            "why": self.why,
            "chosen_by": self.chosen_by,
        }


# --- cutting ---------------------------------------------------------------


def cut_audio(src: Path | str, dest: Path | str, start_s: float, duration_s: float) -> Path:
    """Mono 16 kHz mp3 of [start, start+duration].

    Mono and 16 kHz because the next thing to read it is a transcription API,
    and a stereo 44.1 kHz copy of a radio loop is four times the upload for no
    extra words.
    """
    require_binaries()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-accurate_seek", "-ss", f"{max(0.0, start_s):.3f}",
        "-t", f"{max(0.1, duration_s):.3f}",
        "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", "64k",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-1200:]
        raise FFmpegError(f"cutting {Path(src).name} failed:\n{tail}")
    if not dest.exists() or dest.stat().st_size == 0:
        raise TapeError(f"cut produced nothing from {Path(src).name} at {start_s:.0f}s")
    return dest


def audio_duration_s(path: Path | str) -> float:
    require_binaries()
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True,
    )
    try:
        return float(proc.stdout.decode().strip())
    except (ValueError, AttributeError):
        return 0.0


# --- choosing ---------------------------------------------------------------


def by_density(words: list[dict[str, Any]], target_s: float = DEFAULT_MOMENT_S) -> Moment:
    """The busiest stretch of talking, with no model involved.

    A fallback for when there is no Anthropic key. It finds where people are
    talking rather than where something is happening, which is a much weaker
    signal - but it is a real stretch of real speech, which is the part that
    matters. Nothing here fabricates anything.
    """
    if not words:
        raise TapeError("no words to choose from")

    best_start, best_score = words[0]["start"], -1.0
    for word in words:
        start = float(word["start"])
        end = start + target_s
        inside = [w for w in words if start <= float(w["start"]) < end]
        if len(inside) < 4:
            continue
        # Words per second, penalised for a big gap - a stretch with a long
        # silence in the middle reads as dead air however many words surround it.
        gaps = [
            float(b["start"]) - float(a["end"])
            for a, b in zip(inside, inside[1:], strict=False)
        ]
        score = len(inside) / target_s - 2.0 * max(gaps, default=0.0)
        if score > best_score:
            best_score, best_start = score, start

    return _build(words, best_start, best_start + target_s, chosen_by="density")


def _build(
    words: list[dict[str, Any]],
    start_s: float,
    end_s: float,
    *,
    chosen_by: str,
    **narration: str,
) -> Moment:
    """Snap a window to word boundaries and carry its real words with it."""
    start_s = max(0.0, float(start_s))
    end_s = max(start_s + MIN_MOMENT_S, float(end_s))
    if end_s - start_s > MAX_MOMENT_S:
        end_s = start_s + MAX_MOMENT_S

    inside = [w for w in words if float(w["start"]) >= start_s and float(w["end"]) <= end_s]
    if not inside:
        # The model picked a stretch with nothing in it. Widen to whatever
        # words are nearest rather than shipping a silent "moment".
        inside = [w for w in words if float(w["end"]) > start_s][:40]
    if not inside:
        raise TapeError("the chosen window contains no words")

    start_s = float(inside[0]["start"])
    end_s = min(float(inside[-1]["end"]) + 0.35, start_s + MAX_MOMENT_S)

    shifted = [
        {
            "w": w["w"],
            "start": round(float(w["start"]) - start_s, 3),
            "end": round(float(w["end"]) - start_s, 3),
        }
        for w in inside
        if float(w["start"]) - start_s < end_s - start_s
    ]
    return Moment(
        start_s=start_s,
        end_s=end_s,
        transcript=" ".join(w["w"] for w in inside).strip(),
        words=shifted,
        chosen_by=chosen_by,
        **narration,
    )


def find(
    words: list[dict[str, Any]],
    *,
    archive_name: str,
    archive_source: str,
) -> Moment:
    """The strongest moment in a transcript, and narration written for it.

    Claude reads the transcript and picks; without a key we fall back to the
    density heuristic and write no narration, because narration about a moment
    nobody has read would be invention.
    """
    if not words:
        raise TapeError("nothing was transcribed - there is no moment to find")

    if not settings.anthropic_api_key:
        log.warning("moment: no ANTHROPIC_API_KEY - falling back to speech density")
        return by_density(words)

    from core.llm import find_moment

    try:
        picked = find_moment(archive_name, archive_source, words)
    except Exception as exc:  # noqa: BLE001 - a weaker moment beats no video
        log.warning("moment: Claude failed (%s) - falling back to speech density", exc)
        return by_density(words)

    return _build(
        words,
        picked["start_s"],
        picked["end_s"],
        chosen_by="claude",
        hook=picked.get("hook", ""),
        context=picked.get("context", ""),
        closer=picked.get("closer", ""),
        why=picked.get("why", ""),
    )


# --- the whole job ----------------------------------------------------------


def prepare(
    tape_path: Path,
    *,
    archive_name: str,
    archive_source: str,
    work_dir: Path,
    offset_s: float = 0.0,
    scan_minutes: float | None = None,
) -> tuple[Moment, Path]:
    """Scan a recording, find the moment, and cut it. Returns (moment, audio)."""
    scan_s = float(scan_minutes or settings.studio_scan_minutes) * 60.0
    total = audio_duration_s(tape_path)
    if total and offset_s >= total:
        raise TapeError(f"offset {offset_s:.0f}s is past the end of a {total:.0f}s recording")
    if total:
        scan_s = min(scan_s, total - offset_s)

    provider = transcription.best_provider()
    if provider is None:
        raise TapeError(
            "no transcription key: set OPENAI_API_KEY or ASSEMBLYAI_API_KEY. "
            "The studio will not play a recording it cannot caption."
        )

    scan = cut_audio(tape_path, work_dir / "scan.mp3", offset_s, scan_s)
    log.info(
        "moment: transcribing %.0f minutes of %s via %s",
        scan_s / 60.0, tape_path.name, provider,
    )
    result = transcription.transcribe(scan, provider=provider)
    words = result.get("words") or []
    if not words:
        raise TapeError(f"{tape_path.name} transcribed to nothing - there may be no speech in it")

    moment = find(words, archive_name=archive_name, archive_source=archive_source)
    log.info(
        "moment: %.0fs-%.0fs (%s) - %s",
        moment.start_s, moment.end_s, moment.chosen_by, moment.transcript[:90],
    )

    audio = cut_audio(
        scan, work_dir / "moment.mp3", moment.start_s, moment.duration_s
    )
    return moment, audio
