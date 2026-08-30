"""Give the machine eyes: what the picture is doing, second by second.

Chat is a crowd's opinion about something that happened off-screen from the
bot's point of view. This is the something. It is deliberately the *cheap*
half of seeing - what moved, when the shot changed, when the room lit up - and
it exists to find the handful of seconds worth looking at properly, because
looking at a frame properly costs a model call and there are 86,400 seconds in
a day per stream.

Everything is measured at 96x54 greyscale. That is not a compromise made for
speed alone: at that size a frame is 5KB and a comparison between two of them
is a sum, so half a minute of 1080p reduces to a few thousand numbers without
a decoder ever building a full-size picture. What survives the shrink is
exactly what this stage needs - how much of the frame changed, where, and
whether the change was a cut or a movement. What does not survive is faces,
text and expression, and none of those are answerable by arithmetic anyway.

The one rule that matters here is the same one that runs through the rest of
the pipeline: **everything is measured against this stream's own recent past.**
A nightclub stream read 0.118 average motion while a man sitting at a desk read
0.009 - thirteen times more - and an absolute threshold tuned on either one is
worthless on the other. What is interesting is not that the club is moving. It
is that the club moved more than the club has been moving.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.ffmpeg_ops import require_binaries

log = logging.getLogger(__name__)

#: Analysis size. Wide enough to localise a change to a few percent of the
#: frame, small enough that a whole clip decodes in well under a second.
WIDTH, HEIGHT = 96, 54
STRIDE = WIDTH * HEIGHT
#: Ten frames a second. Four - what the crop tracker uses - is enough to follow
#: a person walking and too coarse to catch a punch, a flinch or a fall, which
#: are the events worth clipping.
FPS = 10.0
#: How far back "usual for this stream" reaches.
BASELINE_S = 30.0
#: No verdict before there is a past to compare against.
WARMUP_S = 5.0


class WatchingError(RuntimeError):
    pass


@dataclass
class Watching:
    """What the picture did, and when."""

    fps: float
    #: Mean absolute frame difference per sample, 0..1. Index 0 is always 0.
    motion: list[float] = field(default_factory=list)
    #: Mean frame brightness, 0..1.
    brightness: list[float] = field(default_factory=list)
    #: (time, size) where motion jumped far above this stream's own normal.
    surges: list[tuple[float, float]] = field(default_factory=list)
    #: Times where the whole frame changed at once - a cut, or the camera whipping.
    cuts: list[float] = field(default_factory=list)
    #: Times where the room lit up or went dark within a fraction of a second.
    flashes: list[float] = field(default_factory=list)
    #: (start, end) where nothing moved at all.
    stillness: list[tuple[float, float]] = field(default_factory=list)
    #: Per-frame, per-column change. Where in the frame it happened, which is
    #: what the portrait crop needs and what tells a whole-frame cut from
    #: something moving in one corner.
    columns: list[list[float]] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return len(self.motion) / self.fps

    def time_of(self, index: int) -> float:
        return index / self.fps

    @property
    def average_motion(self) -> float:
        return sum(self.motion) / len(self.motion) if self.motion else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "duration_s": round(self.duration_s, 2),
            "average_motion": round(self.average_motion, 5),
            "surges": [{"at_s": round(t, 2), "size": round(v, 2)} for t, v in self.surges],
            "cuts": [round(t, 2) for t in self.cuts],
            "flashes": [round(t, 2) for t in self.flashes],
            "still_s": round(sum(b - a for a, b in self.stillness), 1),
        }


#: One decode, two reductions, stacked into a 96x2 strip per frame: the top row
#: is how much each column changed since the last frame, the bottom row is how
#: bright each column is. Both reductions are ffmpeg's, which matters - doing
#: the vertical average in Python is 5,184 subtractions per frame pair, ten
#: times a second, on three streams, forever. Here it is 192 additions.
PROFILE = (
    "[0:v]fps={fps},scale={w}:{h},format=gray,split[a][b];"
    "[a]tblend=all_mode=difference,scale={w}:1[m];"
    "[b]scale={w}:1[l];"
    "[m][l]vstack=inputs=2[out]"
)


def profiles(
    src: Path | str, *, fps: float = FPS, seconds: float | None = None
) -> list[tuple[bytes, bytes]]:
    """(column motion, column brightness) per sampled frame, both 96 wide."""
    require_binaries()
    command = ["ffmpeg", "-v", "error"]
    if seconds:
        command += ["-t", f"{seconds:.2f}"]
    command += [
        "-i", str(src),
        "-filter_complex", PROFILE.format(fps=fps, w=WIDTH, h=HEIGHT),
        "-map", "[out]", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    proc = subprocess.run(command, capture_output=True)
    raw = proc.stdout
    if len(raw) < WIDTH * 2 * 2:
        raise WatchingError(
            f"not enough frames in {Path(str(src)).name}: "
            f"{proc.stderr.decode('utf-8', 'replace')[-300:]}"
        )
    step = WIDTH * 2
    return [
        (raw[i : i + WIDTH], raw[i + WIDTH : i + step])
        for i in range(0, len(raw) - step + 1, step)
    ]


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def watch(src: Path | str, *, fps: float = FPS, seconds: float | None = None) -> Watching:
    """Read a stretch of video for what the picture is doing."""
    strips = profiles(src, fps=fps, seconds=seconds)

    columns = [[v / 255.0 for v in change] for change, _ in strips]
    motion = [sum(row) / WIDTH for row in columns]
    brightness = [sum(light) / WIDTH / 255.0 for _, light in strips]
    # tblend has nothing to difference the first frame against, so whatever it
    # reports there is about the decoder starting up, not about the stream.
    if motion:
        motion[0] = 0.0

    found = Watching(fps=fps, motion=motion, brightness=brightness, columns=columns)
    found.surges = _find_surges(motion, fps)
    found.cuts = _find_cuts(motion, fps)
    found.flashes = _find_flashes(brightness, fps)
    found.stillness = _find_stillness(motion, fps)
    return found


def _find_surges(
    motion: list[float], fps: float, *, over: float = 2.2, floor: float = 0.004
) -> list[tuple[float, float]]:
    """Where the picture moved far more than this stream normally moves.

    A ratio, not a threshold. A nightclub stream sits at 0.118 average motion
    and a man at a desk at 0.009; a number that finds the interesting seconds
    in one of those finds every second of the other.
    """
    back = max(1, int(BASELINE_S * fps))
    least = max(1, int(WARMUP_S * fps))
    found: list[tuple[float, float]] = []
    for i in range(least, len(motion)):
        usual = _median(motion[max(0, i - back) : i])
        if usual < 1e-9 or motion[i] < floor:
            continue
        ratio = motion[i] / usual
        if ratio < over:
            continue
        at = i / fps
        if found and at - found[-1][0] < 0.8:
            found[-1] = (found[-1][0], max(found[-1][1], ratio))
        else:
            found.append((at, ratio))
    return found


def _find_cuts(motion: list[float], fps: float, *, over: float = 0.22) -> list[float]:
    """A cut changes most of the frame in one step. A movement does not.

    On a single-camera stream this mostly measures the camera being swung
    about, which is why it is worth little on its own and is here to agree
    with the other signals rather than to lead them.
    """
    least = max(1, int(WARMUP_S * fps))
    found: list[float] = []
    for i in range(least, len(motion)):
        if motion[i] < over:
            continue
        at = i / fps
        if not found or at - found[-1] >= 0.5:
            found.append(at)
    return found


def _find_flashes(
    brightness: list[float], fps: float, *, jump: float = 0.14
) -> list[float]:
    """The room lighting up or going dark inside a fraction of a second."""
    step = max(1, int(0.2 * fps))
    least = max(step, int(WARMUP_S * fps))
    found: list[float] = []
    for i in range(least, len(brightness)):
        if abs(brightness[i] - brightness[i - step]) < jump:
            continue
        at = i / fps
        if not found or at - found[-1] >= 0.5:
            found.append(at)
    return found


def _find_stillness(
    motion: list[float], fps: float, *, below: float = 0.004, min_s: float = 1.5
) -> list[tuple[float, float]]:
    """Where nothing moved. An empty chair, a frozen stream, a held shot."""
    least = max(1, int(min_s * fps))
    found: list[tuple[float, float]] = []
    start: int | None = None
    for i, value in enumerate(motion):
        if value <= below:
            start = i if start is None else start
            continue
        if start is not None and i - start >= least:
            found.append((start / fps, i / fps))
        start = None
    if start is not None and len(motion) - start >= least:
        found.append((start / fps, len(motion) / fps))
    return found
