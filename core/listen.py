"""Give the machine ears: what the audio is doing, not just what was said.

A transcript tells you the words. It does not tell you that someone shouted
them, that the room went silent for two seconds first, that something hit the
floor, or that the crowd noise doubled. Most of what makes a moment a moment
lives in exactly those places, and a words-only pipeline is deaf to all of it.

Two things come out of here.

The **envelope** touches every sample: loudness and peak per short window,
plus the derivative, which is where impacts and shouts show up. Cheap enough
to run over a whole recording - it is arithmetic over PCM, not inference.

The **pictures** - a waveform and a spectrogram of the entire file - exist so
a model with vision can look at the audio the way an engineer does. Speech,
music, silence, a scream and a door slam do not sound alike and they do not
*look* alike on a spectrogram either. One image covers an hour.

Together they answer "where is something happening" without a single API call.
"""

from __future__ import annotations

import logging
import math
import subprocess
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.ffmpeg_ops import FFmpegError, require_binaries

log = logging.getLogger(__name__)

#: Analysis rate. Speech events are tens of milliseconds; 50ms windows catch a
#: consonant hitting a microphone without producing a curve too long to reason
#: about over an hour of tape.
WINDOW_MS = 50
SAMPLE_RATE = 16000


class ListenError(RuntimeError):
    pass


@dataclass
class Envelope:
    """Loudness over time, measured over every sample in the file."""

    window_s: float
    #: dBFS per window; -90 is silence
    rms_db: list[float] = field(default_factory=list)
    peak_db: list[float] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return len(self.rms_db) * self.window_s

    def at(self, index: int) -> float:
        return self.rms_db[index] if 0 <= index < len(self.rms_db) else -90.0

    def time_of(self, index: int) -> float:
        return index * self.window_s

    def jumps(self, min_rise_db: float = 12.0, look_back: int = 6) -> list[tuple[float, float]]:
        """(time, rise) where loudness climbs sharply out of a quieter stretch.

        This is the shape of a reaction: a beat of relative quiet, then someone
        shouts or something lands. Measuring the rise against a short window of
        history rather than the previous sample alone is what stops ordinary
        speech syllables from registering as events.
        """
        found: list[tuple[float, float]] = []
        for i in range(look_back, len(self.rms_db)):
            floor = min(self.rms_db[i - look_back : i])
            rise = self.rms_db[i] - floor
            if rise >= min_rise_db:
                found.append((round(self.time_of(i), 2), round(rise, 1)))
        # One event, not thirty: collapse anything inside a second of a louder
        # neighbour, keeping the biggest.
        collapsed: list[tuple[float, float]] = []
        for time, rise in sorted(found, key=lambda x: -x[1]):
            if all(abs(time - kept) > 1.0 for kept, _ in collapsed):
                collapsed.append((time, rise))
        return sorted(collapsed)

    def quiet_runs(self, below_db: float = -45.0, min_s: float = 0.8) -> list[tuple[float, float]]:
        """(start, end) of stretches quiet enough to read as a pause."""
        runs: list[tuple[float, float]] = []
        start: int | None = None
        for i, value in enumerate(self.rms_db):
            if value <= below_db:
                if start is None:
                    start = i
            elif start is not None:
                if (i - start) * self.window_s >= min_s:
                    runs.append((round(self.time_of(start), 2), round(self.time_of(i), 2)))
                start = None
        if start is not None and (len(self.rms_db) - start) * self.window_s >= min_s:
            runs.append((round(self.time_of(start), 2), round(self.duration_s, 2)))
        return runs

    def energy_windows(self, window_s: float = 20.0) -> list[tuple[float, float, float]]:
        """(start, end, mean dB) per window, loudest first."""
        per = max(1, int(window_s / self.window_s))
        out: list[tuple[float, float, float]] = []
        for start in range(0, len(self.rms_db), per):
            chunk = self.rms_db[start : start + per]
            if not chunk:
                continue
            out.append(
                (
                    round(self.time_of(start), 1),
                    round(self.time_of(min(start + per, len(self.rms_db))), 1),
                    round(sum(chunk) / len(chunk), 1),
                )
            )
        out.sort(key=lambda w: -w[2])
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_s": self.window_s,
            "windows": len(self.rms_db),
            "duration_s": round(self.duration_s, 2),
            "jumps": self.jumps(),
            "quiet_runs": self.quiet_runs(),
        }


def _pcm(src: Path | str, *, rate: int = SAMPLE_RATE) -> array:
    """Every sample of the audio, mono, as signed 16-bit."""
    require_binaries()
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-vn", "-ac", "1", "-ar", str(rate),
            "-f", "s16le", "-",
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-800:]
        raise FFmpegError(f"could not read audio from {Path(src).name}:\n{tail}")
    samples = array("h")
    samples.frombytes(proc.stdout[: len(proc.stdout) // 2 * 2])
    if not samples:
        raise ListenError(f"{Path(src).name} has no audio")
    return samples


def _rms_db(chunk: array) -> tuple[float, float]:
    """(rms, peak) in dBFS for one window."""
    try:
        import audioop  # deprecated in 3.13; the fallback below covers its removal

        rms = audioop.rms(chunk.tobytes(), 2)
        peak = audioop.max(chunk.tobytes(), 2)
    except Exception:  # noqa: BLE001 - arithmetic is the point, not the module
        total = 0
        peak = 0
        for sample in chunk:
            total += sample * sample
            peak = max(peak, abs(sample))
        rms = int(math.sqrt(total / len(chunk))) if chunk else 0

    def to_db(value: int) -> float:
        return -90.0 if value <= 0 else max(-90.0, 20.0 * math.log10(value / 32768.0))

    return to_db(rms), to_db(peak)


def envelope(src: Path | str, *, window_ms: int = WINDOW_MS) -> Envelope:
    """Loudness of the whole file, window by window, sample by sample."""
    samples = _pcm(src)
    per_window = max(1, int(SAMPLE_RATE * window_ms / 1000))
    out = Envelope(window_s=per_window / SAMPLE_RATE)

    for start in range(0, len(samples) - per_window + 1, per_window):
        rms, peak = _rms_db(samples[start : start + per_window])
        out.rms_db.append(round(rms, 1))
        out.peak_db.append(round(peak, 1))

    log.info(
        "listen: %s - %d windows over %.0fs (%d samples)",
        Path(src).name, len(out.rms_db), out.duration_s, len(samples),
    )
    return out


# --- the audio, as something to look at -------------------------------------


def waveform_png(
    src: Path | str, dest: Path | str, *, width: int = 1800, height: int = 320
) -> Path:
    """The loudness shape of the whole file in one image."""
    require_binaries()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-filter_complex",
            f"[0:a]showwavespic=s={width}x{height}:colors=0x2E7D8F|0x2E7D8F:split_channels=0",
            "-frames:v", "1", str(dest),
        ],
        capture_output=True,
    )
    if proc.returncode != 0 or not dest.exists():
        raise FFmpegError(proc.stderr.decode("utf-8", "replace")[-800:])
    return dest


def spectrogram_png(
    src: Path | str, dest: Path | str, *, width: int = 1800, height: int = 540
) -> Path:
    """The whole file as a spectrogram - frequency up, time across.

    This is the one that carries information a transcript cannot. Speech sits
    in a band and looks striped; music is horizontal lines; an impact is a
    vertical smear across every frequency at once; silence is empty. A model
    that can see reads all of that off one picture, for an hour of audio, with
    no per-second cost at all.
    """
    require_binaries()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-lavfi",
            # Log frequency scale: the interesting detail for voices and
            # effects is all in the bottom few kHz, which a linear scale
            # squashes into a strip at the base of the image.
            f"[0:a]showspectrumpic=s={width}x{height}:mode=combined"
            ":color=intensity:scale=log:fscale=log:legend=1",
            "-frames:v", "1", str(dest),
        ],
        capture_output=True,
    )
    if proc.returncode != 0 or not dest.exists():
        raise FFmpegError(proc.stderr.decode("utf-8", "replace")[-800:])
    return dest


def listen(src: Path | str, out_dir: Path | str) -> dict[str, Any]:
    """Envelope plus the two pictures, and where they say to look."""
    src, out_dir = Path(src), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    env = envelope(src)
    wave = waveform_png(src, out_dir / "waveform.png")
    spec = spectrogram_png(src, out_dir / "spectrogram.png")

    jumps = env.jumps()
    return {
        "source": src.name,
        "waveform": str(wave),
        "spectrogram": str(spec),
        "envelope": env.as_dict(),
        "loudest_windows": [
            {"start_s": a, "end_s": b, "mean_db": d} for a, b, d in env.energy_windows()[:10]
        ],
        "candidates": [
            {"at_s": time, "rise_db": rise, "why": "loudness jumps out of a quieter stretch"}
            for time, rise in jumps[:20]
        ],
    }
