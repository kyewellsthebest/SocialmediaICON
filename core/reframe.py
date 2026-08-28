"""Crop a 16:9 stream to portrait, following whatever is actually happening.

A fixed centre crop is why most auto-clipped verticals look wrong. Streamers
are rarely centred: the webcam sits in a corner, the action is off to one
side, and a centre crop spends two thirds of a phone screen on an empty wall
while the person talking is half out of frame.

So the crop has to move. The question is what it should follow, and the
answer here is motion rather than faces. Face detection needs a model, misses
anyone turned away, and finds nothing at all when the interesting thing is a
game, a screen or a dog. Motion needs no model, works on anything, and is
what the eye follows anyway - in a still room the crop simply stays put,
which is the correct behaviour.

The measurement is deliberately tiny. Frames are decoded at 64 pixels wide
and 4 per second, and the whole path for a thirty second clip is under eight
thousand numbers. Deciding where to look does not need detail; it needs to
know which third of the frame moved.

Two things then keep it watchable:

* the path is **smoothed hard**, because a crop that follows every twitch is
  unwatchable in a way a slightly late crop is not;
* the crop **only moves when it is worth moving**, so a person shifting in
  their chair does not send the frame drifting.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.ffmpeg_ops import FFmpegError, require_binaries

log = logging.getLogger(__name__)

#: Portrait output. 1080x1920 is what every short-form platform wants.
OUT_W, OUT_H = 1080, 1920
#: Analysis resolution. Wide enough to localise motion to a few percent of the
#: frame, small enough that a whole clip decodes in well under a second.
PROBE_W, PROBE_H = 64, 36
PROBE_FPS = 4.0
#: How much of the recent path to average over. Just under a second: long
#: enough to ignore a gesture, short enough to follow someone walking.
SMOOTH_S = 0.9
#: Don't move for less than this share of the frame width. Below it the crop
#: drifts constantly and the viewer feels seasick without knowing why.
DEADZONE = 0.035
#: Cap on how fast the crop may travel, as a share of frame width per second.
#: A crop that snaps across the frame reads as a mistake; one that eases feels
#: like a camera operator.
MAX_PAN_PER_S = 0.28


class ReframeError(RuntimeError):
    pass


@dataclass
class Path_:
    """Where to look, over time."""

    #: (time, centre-x as 0..1) after smoothing and limiting
    points: list[tuple[float, float]]
    source_w: int
    source_h: int

    @property
    def crop_w(self) -> int:
        """The widest 9:16 slice that fits in the source."""
        width = int(self.source_h * OUT_W / OUT_H)
        return min(self.source_w, width - width % 2)

    def x_at(self, t: float) -> float:
        """Crop left edge in source pixels, clamped inside the frame."""
        centre = _sample(self.points, t)
        x = centre * self.source_w - self.crop_w / 2
        return max(0.0, min(float(self.source_w - self.crop_w), x))

    def travel(self) -> float:
        """Total horizontal movement, 0..1. Useful for spotting a jittery path."""
        return sum(
            abs(b - a) for (_, a), (_, b) in zip(self.points, self.points[1:], strict=False)
        )


def _sample(points: list[tuple[float, float]], t: float) -> float:
    if not points:
        return 0.5
    if t <= points[0][0]:
        return points[0][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:], strict=False):
        if t0 <= t <= t1:
            span = t1 - t0
            return v0 if span <= 0 else v0 + (v1 - v0) * (t - t0) / span
    return points[-1][1]


def probe_size(src: Path | str) -> tuple[int, int]:
    require_binaries()
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=x", str(src),
        ],
        capture_output=True,
    )
    try:
        width, height = proc.stdout.decode().strip().split("\n")[0].split("x")
        return int(width), int(height)
    except (ValueError, IndexError) as exc:
        raise ReframeError(f"could not read the size of {Path(src).name}") from exc


def motion_columns(src: Path | str) -> list[list[float]]:
    """Per-frame, per-column motion energy, from tiny greyscale frames.

    Returns one row of PROBE_W numbers per sampled frame. Row 0 is all zeros -
    the first frame has nothing to differ from.
    """
    require_binaries()
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(src),
            "-vf", f"fps={PROBE_FPS},scale={PROBE_W}:{PROBE_H},format=gray",
            "-f", "rawvideo", "-",
        ],
        capture_output=True,
    )
    raw = proc.stdout
    stride = PROBE_W * PROBE_H
    if len(raw) < stride * 2:
        raise ReframeError(
            f"not enough frames to measure motion in {Path(src).name}: "
            f"{proc.stderr.decode('utf-8', 'replace')[-300:]}"
        )

    frames = [raw[i : i + stride] for i in range(0, len(raw) - stride + 1, stride)]
    rows: list[list[float]] = [[0.0] * PROBE_W]
    for previous, current in zip(frames, frames[1:], strict=False):
        columns = [0.0] * PROBE_W
        for offset in range(0, stride, PROBE_W):
            for x in range(PROBE_W):
                columns[x] += abs(current[offset + x] - previous[offset + x])
        rows.append(columns)
    return rows


def build_path(src: Path | str, *, smooth_s: float = SMOOTH_S) -> Path_:
    """Where the action is, second by second, smoothed into something watchable."""
    width, height = probe_size(src)
    rows = motion_columns(src)

    # Centroid of motion per frame. A still frame keeps the previous target
    # rather than snapping to the middle, which is what makes the crop hold
    # steady through a pause instead of wandering home.
    raw: list[float] = []
    last = 0.5
    for columns in rows:
        total = sum(columns)
        if total < 1e-6:
            raw.append(last)
            continue
        centroid = sum(x * v for x, v in enumerate(columns)) / total / (PROBE_W - 1)
        last = centroid
        raw.append(centroid)

    window = max(1, int(smooth_s * PROBE_FPS))
    smoothed: list[float] = []
    for i in range(len(raw)):
        chunk = raw[max(0, i - window + 1) : i + 1]
        smoothed.append(sum(chunk) / len(chunk))

    # Deadzone and speed limit, applied in order: ignore small moves, then ease
    # into the ones that survive.
    points: list[tuple[float, float]] = []
    current = smoothed[0] if smoothed else 0.5
    step = 1.0 / PROBE_FPS
    for i, target in enumerate(smoothed):
        if abs(target - current) > DEADZONE:
            limit = MAX_PAN_PER_S * step
            current += max(-limit, min(limit, target - current))
        points.append((round(i * step, 3), round(current, 4)))

    path = Path_(points=points, source_w=width, source_h=height)
    log.info(
        "reframe: %s - %dx%d, crop %dpx wide, %d points, travel %.2f",
        Path(src).name, width, height, path.crop_w, len(points), path.travel(),
    )
    return path


def sendcmd_file(path: Path_, dest: Path | str, *, rate_hz: float = 10.0) -> Path:
    """ffmpeg commands that walk the crop along the path.

    sendcmd rather than a giant nested expression: an expression with one
    branch per keyframe becomes thousands of characters and is unreadable when
    it goes wrong, while this file can simply be opened and looked at.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    end = path.points[-1][0] if path.points else 0.0
    step = 1.0 / rate_hz

    lines = []
    t = 0.0
    while t <= end:
        lines.append(f"{t:.3f} crop x {path.x_at(t):.1f};")
        t += step
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def to_portrait(
    src: Path | str,
    dest: Path | str,
    *,
    work_dir: Path | str | None = None,
    extra_filters: str = "",
) -> Path:
    """Reframe a landscape clip to 1080x1920, following the action."""
    require_binaries()
    src, dest = Path(src), Path(dest)
    work = Path(work_dir) if work_dir else dest.parent
    work.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    path = build_path(src)
    commands = sendcmd_file(path, work / "crop.cmd")

    chain = (
        f"sendcmd=f='{commands.as_posix()}',"
        f"crop=w={path.crop_w}:h={path.source_h}:x={path.x_at(0):.1f}:y=0,"
        f"scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1"
    )
    if extra_filters:
        chain = f"{chain},{extra_filters}"

    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src), "-vf", chain,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-movflags", "+faststart", str(dest),
        ],
        capture_output=True,
    )
    if proc.returncode != 0 or not dest.exists():
        raise FFmpegError(proc.stderr.decode("utf-8", "replace")[-700:])
    return dest
