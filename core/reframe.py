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
#: How often the target is measured. Twelve rather than four: the decode has
#: to happen either way, so the extra samples are nearly free, and a target
#: measured four times a second cannot describe a person leaning out of frame
#: in a fifth of one.
PROBE_FPS = 12.0
#: How often the crop position is *computed*, which is a different question.
#: The measurement can be coarse because it is smoothed; the motion cannot,
#: because a crop that only changes twelve times a second holds still for five
#: frames of a 60fps clip and then jumps. Measured on real 1080p60: at 10Hz
#: the worst single-frame jump was 34.6 pixels, at 60Hz it is under 2. That
#: snap is what "jittery" means - the path was always smooth, the delivery of
#: it was a staircase.
PATH_FPS = 60.0
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
    """Where to look at time t, interpolated smoothly between knots.

    Smoothstep rather than a straight line. Linear interpolation is continuous
    in position but not in velocity: the crop changes speed instantly at every
    knot, and a corner in the velocity is exactly as visible as a corner in
    the position. This eases into and out of each knot instead.
    """
    if not points:
        return 0.5
    if t <= points[0][0]:
        return points[0][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:], strict=False):
        if t0 <= t <= t1:
            span = t1 - t0
            if span <= 0:
                return v0
            u = (t - t0) / span
            return v0 + (v1 - v0) * u * u * (3.0 - 2.0 * u)
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

    # numpy rather than three nested loops: this is 2,304 subtractions per
    # frame pair and the sampling rate tripled, which in Python would have
    # been most of a second per clip and here is not measurable.
    import numpy as np

    count = len(raw) // stride
    frames = np.frombuffer(raw[: count * stride], dtype=np.uint8).reshape(
        count, PROBE_H, PROBE_W
    ).astype(np.int16)
    diffs = np.abs(frames[1:] - frames[:-1]).sum(axis=1)
    return [[0.0] * PROBE_W, *diffs.astype(float).tolist()]


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


def _follow(
    targets: list[float], *, step: float, bounds: tuple[float, float] = (0.0, 1.0)
) -> list[float]:
    """Walk a crop centre towards a moving target like a camera operator would.

    Hysteresis decides *whether* to move, a proportional approach decides how
    fast, and an acceleration cap makes the start and the stop gentle.

    `bounds` is the range of centres for which the crop still fits inside the
    frame, and the target is held inside it rather than the result being
    clipped afterwards. Clipping afterwards was worth a 6.9-pixel snap: the
    follower would drive happily towards a centre of 0.1, the crop would stop
    dead at the edge with all the acceleration limiting bypassed, and then sit
    there not moving until the target came back into the legal range.
    """
    if not targets:
        return []
    low, high = bounds
    if high < low:
        low = high = (low + high) / 2.0
    current = min(high, max(low, targets[0]))
    velocity = 0.0
    moving = False
    out: list[float] = []
    for raw_target in targets:
        target = min(high, max(low, raw_target))
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
        current = min(high, max(low, current + velocity * step))
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

    # The target is now known 12 times a second. Resample it to the rate the
    # crop will actually move at *before* following it, so the speed and
    # acceleration caps are applied per output frame rather than per
    # measurement. Following at the measurement rate and interpolating
    # afterwards is what produced the staircase: the caps made a smooth
    # sequence of twelve positions a second, and then the crop teleported
    # between them.
    probe_step = 1.0 / PROBE_FPS
    coarse = [(i * probe_step, v) for i, v in enumerate(smoothed)]
    end = coarse[-1][0] if coarse else 0.0
    step = 1.0 / PATH_FPS
    fine = [_sample(coarse, i * step) for i in range(int(end * PATH_FPS) + 1)]

    # The centres for which the crop still fits. Everything outside is a wall,
    # and the follower has to know where the wall is to decelerate into it.
    crop_w = Path_(points=[], source_w=width, source_h=height).crop_w
    half = crop_w / 2.0 / width
    points = [
        (round(i * step, 4), round(value, 5))
        for i, value in enumerate(
            _follow(fine, step=step, bounds=(half, 1.0 - half))
        )
    ]

    path = Path_(points=points, source_w=width, source_h=height)
    log.info(
        "reframe: %s - %dx%d, crop %dpx wide, %d points, travel %.2f",
        Path(src).name, width, height, path.crop_w, len(points), path.travel(),
    )
    return path


def sendcmd_file(path: Path_, dest: Path | str, *, rate_hz: float = PATH_FPS) -> Path:
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
    # Four decimal places on the timestamp, not three: at 60Hz the interval is
    # 16.67ms, and rounding that to a millisecond drifts a whole frame every
    # two seconds - which puts a command on the wrong side of a frame boundary
    # and drops it.
    while t <= end:
        lines.append(f"{t:.4f} crop x {path.x_at(t):.1f};")
        t += step
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


# --- the desk layout --------------------------------------------------------
#
# A screen-share stream breaks the whole idea of following the action, because
# the action is in two places at once: the thing being talked about is on the
# screen and the person talking is in a box in the corner. A crop that follows
# motion oscillates between them and settles between them, which shows
# neither - a strip of desktop with half a face at the edge.
#
# So that layout gets a different answer: stack them. The webcam fills the top
# third of the portrait frame and the middle of the screen fills the bottom
# two thirds. Nothing moves, because nothing needs to.

#: The webcam's share of the output height.
CAM_SHARE = 1.0 / 3.0
#: A face bigger than this is the shot, not an overlay on one.
CAM_MAX_H = 0.34
#: ...and this far from the middle on its longest axis, which is where an
#: overlay lives and where a person being filmed does not.
#:
#: Measured across every case to hand, as distance from centre on whichever
#: axis is further out:
#:
#:     two people on a couch   0.22   not a desk stream
#:     Ninja, cam bottom-left  0.33   desk stream
#:     a Lego streamer         0.32   desk stream
#:     a screen with a cam     0.37   desk stream
#:
#: This is geometry, and geometry is a proxy. The real difference between a
#: captured screen and a camera pointed at a room is that screen pixels are
#: *identical* between frames and camera pixels never are, because of sensor
#: noise - and that would separate them without depending on where anybody is
#: sitting. It needs real footage of both to calibrate and there is none here,
#: so this is what there is, and a channel that gets it wrong can say so: a
#: webcam box handed to to_portrait beats anything found by looking.
CAM_CORNER = 0.28
#: How much of the clip a face has to be found in before it is furniture
#: rather than somebody walking past.
CAM_STEADY = 0.55
#: How much bigger the overlay is than the face inside it. The detector
#: returns the face - eyes to chin - and cropping to that fills the top third
#: with a nose. A webcam box is head and shoulders, and a face is a bit under
#: half its height, so the box is found by growing outwards from the face.
CAM_ZOOM_OUT = 2.6


@dataclass
class Webcam:
    """A camera box pinned to a corner of a screen-share, in 0..1 of frame."""

    x: float
    y: float
    w: float
    h: float
    seen: float

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (
            max(0, int(self.x * width)), max(0, int(self.y * height)),
            max(2, int(self.w * width)), max(2, int(self.h * height)),
        )


def find_webcam(src: Path | str) -> Webcam | None:
    """The webcam overlay on a desk stream, or None if this is not one.

    Three things have to hold at once, and each of them on its own is a
    different kind of shot: the face is small (an overlay, not the subject),
    it sits away from the middle (a corner, not a person on camera), and it
    stays there (furniture, not somebody walking past).
    """
    try:
        from core import faces as facelib
    except Exception:  # noqa: BLE001 - no OpenCV is not a crash
        return None
    try:
        # Looked at twice the width the continuous sense uses. This runs once
        # per clip and what it is hunting is deliberately small, so it is the
        # one place worth the pixels - see faces.LOOK_W.
        watched = facelib.watch(
            src, fps=facelib.DETECT_FPS,
            size=(facelib.LOOK_W, facelib.LOOK_H), min_face=facelib.LOOK_MIN_FACE,
        )
    except Exception as exc:  # noqa: BLE001 - a blind guess is no layout
        log.debug("reframe: could not look for a webcam (%s)", exc)
        return None

    boxes = [max(frame, key=lambda f: f.area) for frame in watched.frames if frame]
    if not boxes or not watched.frames:
        return None
    steady = len(boxes) / len(watched.frames)
    if steady < CAM_STEADY:
        return None

    mid = lambda vals: sorted(vals)[len(vals) // 2]  # noqa: E731
    x, y = mid([b.x for b in boxes]), mid([b.y for b in boxes])
    w, h = mid([b.w for b in boxes]), mid([b.h for b in boxes])
    cx, cy = x + w / 2, y + h / 2

    if h > CAM_MAX_H:
        return None
    if max(abs(cx - 0.5), abs(cy - 0.5)) < CAM_CORNER:
        return None
    return Webcam(x=x, y=y, w=w, h=h, seen=steady)


def _clip_box(x: float, y: float, w: float, h: float,
              width: int, height: int) -> tuple[int, int, int, int]:
    """A box moved and trimmed until it sits inside the frame, even-sided."""
    w, h = min(w, float(width)), min(h, float(height))
    x = min(max(x, 0.0), width - w)
    y = min(max(y, 0.0), height - h)
    even = lambda v: max(2, int(v) - int(v) % 2)  # noqa: E731
    return even(x) if x else 0, even(y) if y else 0, even(w), even(h)


def _fit(box: tuple[float, float, float, float], aspect: float,
         width: int, height: int) -> tuple[int, int, int, int]:
    """Grow a box to an aspect ratio about its own centre, inside the frame.

    Grown rather than cropped, so a webcam that is 4:3 inside a 27:16 slot
    keeps all of the face and gains some of what is beside it, instead of
    losing the top of somebody's head. Only when there is nothing left to grow
    into does it crop, and then from the middle.
    """
    x, y, w, h = box
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    cx, cy = x + box[2] / 2, y + box[3] / 2
    w, h = min(w, float(width)), min(h, float(height))
    x = min(max(cx - w / 2, 0.0), width - w)
    y = min(max(cy - h / 2, 0.0), height - h)
    even = lambda v: int(v) - int(v) % 2  # noqa: E731
    return even(x), even(y), even(w), even(h)


def stacked_filter(cam: Webcam, width: int, height: int) -> str:
    """Webcam over screen, as one ffmpeg chain."""
    top_h = int(OUT_H * CAM_SHARE) // 2 * 2
    bottom_h = OUT_H - top_h

    fx, fy, fw, fh = cam.pixels(width, height)
    # Out from the face to the box it is sitting in, about the face's centre.
    # No aspect to satisfy any more - the strip is filled by the blur behind -
    # so this is the overlay and nothing else.
    grow_w, grow_h = fw * CAM_ZOOM_OUT, fh * CAM_ZOOM_OUT
    cx, cy, cw, ch = _clip_box(
        fx + fw / 2 - grow_w / 2, fy + fh / 2 - grow_h / 2, grow_w, grow_h,
        width, height,
    )
    # The middle of the screen, at the shape of the space left for it. Not the
    # middle minus the webcam: the thing being pointed at is in the middle of
    # what the streamer is looking at, which is the middle of the screen.
    sw = min(float(width), height * (OUT_W / bottom_h))
    sh = min(float(height), sw * bottom_h / OUT_W)
    sx, sy, sw_, sh_ = _fit(
        ((width - sw) / 2, (height - sh) / 2, sw, sh), OUT_W / bottom_h, width, height
    )
    return (
        f"[0:v]split=2[cam][scr];"
        f"{_top_strip(cw, ch, cx, cy, top_h)}"
        f"[scr]crop={sw_}:{sh_}:{sx}:{sy},scale={OUT_W}:{bottom_h}:flags=lanczos,setsar=1[bot];"
        f"[top][bot]vstack=inputs=2[out]"
    )


def _top_strip(cw: int, ch: int, cx: int, cy: int, top_h: int) -> str:
    """The webcam in the top third, without dragging the game in with it.

    The strip is 1080x640 - much wider than any webcam box - so filling it by
    growing the crop sideways takes whatever is beside the person, which on a
    game stream is the game. Rendered that way, Ninja arrived with a Fortnite
    character standing next to him.

    So the camera is scaled to *fit* rather than to fill, and what is left at
    the sides is the same picture blurred and enlarged behind it. Nothing from
    outside the overlay is ever shown sharp, and the strip is full.
    """
    return (
        f"[cam]crop={cw}:{ch}:{cx}:{cy},split=2[cam1][cam2];"
        f"[cam1]scale={OUT_W}:{top_h}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{top_h},gblur=sigma=24[camblur];"
        f"[cam2]scale={OUT_W}:{top_h}:force_original_aspect_ratio=decrease:"
        f"flags=lanczos[camfit];"
        f"[camblur][camfit]overlay=(W-w)/2:(H-h)/2,setsar=1[top];"
    )


def to_portrait(
    src: Path | str,
    dest: Path | str,
    *,
    work_dir: Path | str | None = None,
    extra_filters: str = "",
    layout: str = "auto",
    webcam: Webcam | None = None,
    report: dict | None = None,
) -> Path:
    """Reframe a landscape clip to 1080x1920.

    `layout="auto"` stacks a webcam over a screen when it finds a desk stream
    and follows the action otherwise. "follow" and "stacked" force one.
    """
    require_binaries()
    src, dest = Path(src), Path(dest)
    work = Path(work_dir) if work_dir else dest.parent
    work.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # A desk stream is not a tracking problem, it is a layout one: following
    # the motion between a screen and a webcam settles between them and shows
    # neither.
    # A box handed in wins over looking for one. Haar misses plenty of real
    # facecams - a headset, side lighting and a glance away is enough - so a
    # channel known to be a desk stream can say where its camera is instead of
    # falling back to a crop that follows two things at once.
    cam = webcam
    if cam is None and layout in ("auto", "stacked"):
        cam = find_webcam(src)
    if cam is not None:
        width, height = probe_size(src)
        log.info(
            "reframe: %s - webcam at %.2f,%.2f on %.0f%% of frames, stacking",
            Path(src).name, cam.x, cam.y, cam.seen * 100,
        )
        if report is not None:
            report.update({
                "layout": "stacked",
                "webcam": {"x": round(cam.x, 3), "y": round(cam.y, 3),
                           "w": round(cam.w, 3), "h": round(cam.h, 3),
                           "seen": round(cam.seen, 2)},
            })
        chain = stacked_filter(cam, width, height)
        if extra_filters:
            chain = chain.replace("[out]", "[stacked];[stacked]") + f"{extra_filters}[out]"
        return _render(src, dest, chain, complex_=True)

    path = build_path(src)
    if report is not None:
        report.update({"layout": "followed", "travel": round(path.travel(), 3)})
    commands = sendcmd_file(path, work / "crop.cmd")

    chain = (
        f"sendcmd=f='{commands.as_posix()}',"
        f"crop=w={path.crop_w}:h={path.source_h}:x={path.x_at(0):.1f}:y=0,"
        f"scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1"
    )
    if extra_filters:
        chain = f"{chain},{extra_filters}"
    return _render(src, dest, chain, complex_=False)


def _render(src: Path, dest: Path, chain: str, *, complex_: bool) -> Path:
    """One encode, whichever way the frame was arrived at.

    Both branches map their streams explicitly, and the `-vf` one has to even
    though it reads as redundant. A single `-map` anywhere on the command line
    turns off ffmpeg's automatic stream selection *for every stream*, so
    `-vf chain -map 0:a?` does not mean "the filtered video, plus audio if
    there is any" - it means "audio if there is any", and nothing else. That
    shipped: every clip from a stream that was followed rather than stacked
    came out as a correct-length, correctly-audible file with no video track
    at all, which plays as a black screen. Never map one stream here without
    mapping the other.
    """
    where = (
        ["-filter_complex", chain, "-map", "[out]"]
        if complex_
        else ["-vf", chain, "-map", "0:v:0"]
    )
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src), *where, "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-movflags", "+faststart", str(dest),
        ],
        capture_output=True,
    )
    if proc.returncode != 0 or not dest.exists():
        raise FFmpegError(proc.stderr.decode("utf-8", "replace")[-700:])
    return dest
