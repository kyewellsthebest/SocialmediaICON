"""Give the machine eyes: turn a video into something it can actually look at.

The whole studio failed on one thing - finding the moment - and the reason was
never subtle. The bot could read a transcript but could not see the picture, so
"something is happening here" was a guess made from words alone.

This closes that. A video goes in; what comes out is a set of contact sheets
(frames sampled on a grid, each stamped with its timestamp) plus the cheap
signals ffmpeg already knows how to compute - scene cuts, loudness peaks,
silence, freezes, black. A model with vision reads the sheets and the signals
together and can say *where* to look and *why*, on evidence rather than
inference.

Nothing here needs a GPU or an API. It is ffmpeg and arithmetic.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.ffmpeg_ops import FFmpegError, require_binaries

log = logging.getLogger(__name__)

#: A frame every few seconds is enough to see what a video is doing. Denser
#: than this and the sheets stop being readable; sparser and short events slip
#: between samples.
DEFAULT_EVERY_S = 3.0
#: Frames per contact sheet. 5x4 at 320px wide is about 1600x760 - large
#: enough to read a face, small enough to take in at once.
COLS, ROWS = 5, 4
THUMB_W = 320


class ScanError(RuntimeError):
    pass


@dataclass
class Signals:
    """What ffmpeg can tell us about a video without anyone watching it."""

    duration_s: float = 0.0
    #: timestamps where the picture changes hard - cuts, or a camera whipping
    scene_cuts: list[float] = field(default_factory=list)
    #: (start, end) of near-silence: dead air, or the beat before something
    silences: list[tuple[float, float]] = field(default_factory=list)
    #: (start, end) of black frames - transitions, or a stream dropping out
    blacks: list[tuple[float, float]] = field(default_factory=list)
    #: (start, end) where the picture stops moving - AFK, or a frozen stream
    freezes: list[tuple[float, float]] = field(default_factory=list)
    mean_volume_db: float | None = None
    max_volume_db: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "duration_s": round(self.duration_s, 2),
            "scene_cuts": [round(t, 2) for t in self.scene_cuts],
            "silences": [[round(a, 2), round(b, 2)] for a, b in self.silences],
            "blacks": [[round(a, 2), round(b, 2)] for a, b in self.blacks],
            "freezes": [[round(a, 2), round(b, 2)] for a, b in self.freezes],
            "mean_volume_db": self.mean_volume_db,
            "max_volume_db": self.max_volume_db,
        }

    def busy_windows(self, window_s: float = 30.0) -> list[tuple[float, float, int]]:
        """(start, end, cuts) per window, busiest first.

        A crude but honest proxy for "something is going on": a stretch with
        many scene changes is a stretch where the picture keeps changing. It
        does not know if it is interesting - that judgement needs eyes on the
        frames - but it is a good place to point them.
        """
        if not self.duration_s:
            return []
        out: list[tuple[float, float, int]] = []
        start = 0.0
        while start < self.duration_s:
            end = min(start + window_s, self.duration_s)
            cuts = sum(1 for t in self.scene_cuts if start <= t < end)
            out.append((start, end, cuts))
            start = end
        out.sort(key=lambda w: -w[2])
        return out


def _run(cmd: list[str]) -> str:
    """Run ffmpeg and hand back stderr, which is where its analysis lands."""
    proc = subprocess.run(cmd, capture_output=True)
    return proc.stderr.decode("utf-8", "replace")


def probe_duration(src: Path | str) -> float:
    require_binaries()
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(src),
        ],
        capture_output=True,
    )
    try:
        return float(proc.stdout.decode().strip())
    except (ValueError, AttributeError):
        return 0.0


def signals(src: Path | str, *, scene_threshold: float = 0.35) -> Signals:
    """Everything ffmpeg knows, in one pass over the file.

    All five detectors run in a single decode: on a long recording the decode
    dominates, so running them separately would cost five times as much for
    the same answer.
    """
    require_binaries()
    src = Path(src)
    if not src.exists():
        raise ScanError(f"no such video: {src}")

    out = Signals(duration_s=probe_duration(src))

    stderr = _run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(src),
        "-filter_complex",
        f"[0:v]scdet=threshold={scene_threshold * 100:.0f}[v];"
        "[v]blackdetect=d=0.4:pix_th=0.10[vb];"
        "[vb]freezedetect=n=-60dB:d=4[vf];"
        # volumedetect, not astats: astats reports per-channel statistics on
        # its own metadata channel, while the mean/max lines parsed below come
        # from volumedetect. Using one and reading the other returns nothing
        # and looks like a silent video.
        "[0:a]silencedetect=n=-38dB:d=1.2,volumedetect[a]",
        "-map", "[vf]", "-map", "[a]", "-f", "null", "-",
    ])

    for match in re.finditer(r"lavfi\.scd\.time:\s*([\d.]+)", stderr):
        out.scene_cuts.append(float(match.group(1)))
    # scdet also reports through its own log line on some builds
    for match in re.finditer(r"scene detected.*?time:([\d.]+)", stderr):
        out.scene_cuts.append(float(match.group(1)))
    out.scene_cuts = sorted(set(round(t, 2) for t in out.scene_cuts))

    for match in re.finditer(r"silence_start:\s*(-?[\d.]+)", stderr):
        out.silences.append((float(match.group(1)), float(match.group(1))))
    ends = [float(m.group(1)) for m in re.finditer(r"silence_end:\s*([\d.]+)", stderr)]
    out.silences = [
        (start, ends[i] if i < len(ends) else start)
        for i, (start, _) in enumerate(out.silences)
    ]

    for match in re.finditer(
        r"black_start:([\d.]+)\s+black_end:([\d.]+)", stderr
    ):
        out.blacks.append((float(match.group(1)), float(match.group(2))))

    for match in re.finditer(r"freeze_start:\s*([\d.]+)", stderr):
        out.freezes.append((float(match.group(1)), float(match.group(1))))

    mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", stderr)
    peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", stderr)
    if mean:
        out.mean_volume_db = float(mean.group(1))
    if peak:
        out.max_volume_db = float(peak.group(1))

    log.info(
        "scan: %s - %.0fs, %d cuts, %d silences, %d blacks",
        src.name, out.duration_s, len(out.scene_cuts), len(out.silences), len(out.blacks),
    )
    return out


def contact_sheets(
    src: Path | str,
    out_dir: Path | str,
    *,
    every_s: float = DEFAULT_EVERY_S,
    cols: int = COLS,
    rows: int = ROWS,
    thumb_w: int = THUMB_W,
    start_s: float = 0.0,
    duration_s: float | None = None,
) -> list[Path]:
    """Sample frames onto timestamped grids. Returns the sheets written.

    The timestamp burned into each cell is the point of the whole thing: a
    sheet without them shows you that something happened but not when, which
    is half an answer.
    """
    require_binaries()
    src, out_dir = Path(src), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    fps = 1.0 / max(0.2, every_s)
    per_sheet = cols * rows

    # drawtext computes the source timestamp from the output frame number:
    # after fps= the frame index is n, so n/fps seconds back in the original.
    label = (
        f"drawtext=text='%{{eif\\:trunc((n*{every_s}+{start_s})/60)\\:d\\:2}}"
        f"\\:%{{eif\\:mod(trunc(n*{every_s}+{start_s})\\,60)\\:d\\:2}}'"
        ":x=6:y=6:fontsize=22:fontcolor=white:box=1:boxcolor=black@0.75:boxborderw=4"
    )
    chain = f"fps={fps},scale={thumb_w}:-2,{label},tile={cols}x{rows}:margin=4:padding=3"

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if start_s:
        cmd += ["-ss", f"{start_s:.3f}"]
    if duration_s:
        cmd += ["-t", f"{duration_s:.3f}"]
    cmd += [
        "-i", str(src),
        "-vf", chain,
        "-vsync", "vfr",
        str(out_dir / "sheet-%03d.png"),
    ]
    stderr = _run(cmd)
    sheets = sorted(out_dir.glob("sheet-*.png"))
    if not sheets:
        raise FFmpegError(f"no contact sheets produced from {src.name}:\n{stderr[-1200:]}")

    log.info(
        "scan: %d sheet(s) of %d frames each, one frame every %.1fs",
        len(sheets), per_sheet, every_s,
    )
    return sheets


def scan(
    src: Path | str,
    out_dir: Path | str,
    *,
    every_s: float = DEFAULT_EVERY_S,
) -> dict[str, Any]:
    """Signals plus contact sheets, and a manifest tying them together."""
    src, out_dir = Path(src), Path(out_dir)
    found = signals(src)
    sheets = contact_sheets(src, out_dir, every_s=every_s)

    per_sheet = COLS * ROWS
    manifest = {
        "source": src.name,
        "every_s": every_s,
        "frames_per_sheet": per_sheet,
        "sheets": [
            {
                "path": str(sheet),
                "covers_s": [
                    round(index * per_sheet * every_s, 1),
                    round(min((index + 1) * per_sheet * every_s, found.duration_s), 1),
                ],
            }
            for index, sheet in enumerate(sheets)
        ],
        "signals": found.as_dict(),
        "busiest_windows": [
            {"start_s": round(a, 1), "end_s": round(b, 1), "cuts": c}
            for a, b, c in found.busy_windows()[:10]
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
