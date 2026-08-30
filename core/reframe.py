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

Four things then keep it watchable, and all four exist because leaving any
one of them out produced visible jitter:

* motion is localised to the **strongest region**, not averaged across the
  whole frame. A plain centroid parks the crop halfway between the streamer
  and a flashing alert box, then slides every time their relative brightness
  changes;
* the path is **median-filtered then averaged**, because a single frame of
  nonsense - a cut, an explosion, a chat overlay repainting - drags a plain
  mean far enough to move the crop;
* the crop moves under **hysteresis**: it takes a real displacement to start
  it and it runs until it has arrived. A single threshold makes the crop
  chatter on and off around the boundary, which is the jitter people notice;
* speed *and* acceleration are capped, so a move eases in and eases out
  instead of snapping to full pan speed and stopping dead.
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
#: Width of the region the crop actually tracks, as a share of the frame. The
#: strongest column plus this much either side; anything outside is ignored,
#: so a second moving thing cannot drag the crop off the subject.
FOCUS_SHARE = 0.22
#: Spike filter, applied before averaging. Odd-sized median over this many
#: seconds of samples throws away single-frame nonsense entirely rather than
#: mixing a fraction of it into the result.
MEDIAN_S = 1.25
#: How much of the recent path to average over. Long, deliberately: a crop
#: that arrives a beat late is invisible, a crop that twitches is not.
SMOOTH_S = 2.5
#: Displacement needed to start the crop moving, as a share of frame width.
DEADZONE = 0.06
#: ...and how close it has to get before it stops again. Lower than DEADZONE
#: on purpose: one threshold for both makes the crop stutter on and off as the
#: error sits on the line. This is the single biggest source of jitter.
HOLD_ZONE = 0.02
#: Time constant of the approach. Velocity is the remaining error divided by
#: this, so the crop decelerates into position instead of overshooting.
EASE_S = 0.8
#: Cap on how fast the crop may travel, as a share of frame width per second.
MAX_PAN_PER_S = 0.18
#: Cap on how fast that speed may change. Without it the crop hits full pan
#: speed in one frame, which reads as a shove rather than a camera move.
MAX_ACCEL_PER_S2 = 0.35


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


def _focus_centre(columns: list[float], fallback: float) -> float:
    """Centre of the strongest patch of motion, 0..1.

    Not the centroid of everything: that sits between two moving things and
    slides whenever either one gets brighter. This finds where the motion
    actually is, then centres on that neighbourhood so the answer stays smooth
    when the peak column wobbles by one.
    """
    total = sum(columns)
    if total < 1e-6:
        return fallback

    # A little spatial blur first, so the peak is the middle of a busy region
    # rather than whichever single column happened to win by a hair.
    blurred = [
        (columns[max(0, x - 1)] + columns[x] + columns[min(PROBE_W - 1, x + 1)]) / 3.0
        for x in range(PROBE_W)
    ]
    peak = max(range(PROBE_W), key=blurred.__getitem__)

    half = max(1, int(FOCUS_SHARE * PROBE_W / 2))
    left, right = max(0, peak - half), min(PROBE_W - 1, peak + half)
    window = columns[left : right + 1]
    weight = sum(window)
    if weight < 1e-6:
        return fallback
    centre = sum((left + i) * v for i, v in enumerate(window)) / weight
    return centre / (PROBE_W - 1)


def _median_filter(values: list[float], window: int) -> list[float]:
    """Trailing median. Discards outliers outright instead of diluting them."""
    if window <= 1:
        return list(values)
    out: list[float] = []
    for i in range(len(values)):
        chunk = sorted(values[max(0, i - window + 1) : i + 1])
        out.append(chunk[len(chunk) // 2])
    return out


def _follow(targets: list[float], *, step: float) -> list[float]:
    """Walk a crop centre towards a moving target like a camera operator would.

    Hysteresis decides *whether* to move, a proportional approach decides how
    fast, and an acceleration cap makes the start and the stop gentle.
    """
    if not targets:
        return []
    current = targets[0]
    velocity = 0.0
    moving = False
    out: list[float] = []
    for target in targets:
        error = target - current
        if moving:
            if abs(error) <= HOLD_ZONE:
                moving = False
        elif abs(error) > DEADZONE:
            moving = True

        wanted = 0.0
        if moving:
            wanted = max(-MAX_PAN_PER_S, min(MAX_PAN_PER_S, error / EASE_S))
        cap = MAX_ACCEL_PER_S2 * step
        velocity += max(-cap, min(cap, wanted - velocity))
        current = max(0.0, min(1.0, current + velocity * step))
        out.append(current)
    return out


def build_path(src: Path | str, *, smooth_s: float = SMOOTH_S) -> Path_:
    """Where the action is, second by second, smoothed into something watchable."""
    width, height = probe_size(src)
    rows = motion_columns(src)

    # Where the motion is, per frame. A still frame keeps the previous target
    # rather than snapping to the middle, which is what makes the crop hold
    # steady through a pause instead of wandering home.
    raw: list[float] = []
    last = 0.5
    for columns in rows:
        last = _focus_centre(columns, last)
        raw.append(last)

    despiked = _median_filter(raw, max(1, int(MEDIAN_S * PROBE_FPS)) | 1)

    window = max(1, int(smooth_s * PROBE_FPS))
    smoothed: list[float] = []
    for i in range(len(despiked)):
        chunk = despiked[max(0, i - window + 1) : i + 1]
        smoothed.append(sum(chunk) / len(chunk))

    step = 1.0 / PROBE_FPS
    points = [
        (round(i * step, 3), round(value, 4))
        for i, value in enumerate(_follow(smoothed, step=step))
    ]

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
