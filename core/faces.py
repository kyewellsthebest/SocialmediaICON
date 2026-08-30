"""Where the people are in frame, and when their faces change.

Everything else the eye does is about the whole picture: how much of it moved,
whether the shot cut, whether the lights came up. None of that knows the
difference between a camera panning across an empty room and a man's face
going from calm to horrified, and the second one is the entire product.

So this finds faces, and then watches them. Two things come out of it:

* **Where the people are** - how many, how big, whereabouts. A face filling a
  third of the frame is a reaction shot, which is what a clip of a reaction
  looks like. Three faces turning at once is a room reacting to something.
* **When a face changed** - not *what* it changed to. The pixels inside a face
  box changing sharply is a fast measurement and it is the cue that says
  "look here, now". Naming the expression is a different question with a
  different answer, and it is answered by the model that watches the clip,
  on crops taken from here rather than on the whole frame.

Faces are found at a lower rate than motion, and deliberately. A face does not
change meaningfully faster than about ten times a second - an expression takes
a third of a second to form - so sampling faster costs linearly and learns
nothing. Motion is different: a punch, a flinch and a fall are over in three
frames, which is why that runs at the source's own rate.

Haar cascades rather than a neural detector, because a neural detector means a
weights file to ship and 40ms a frame to pay, and this has to run on three
streams forever. They miss faces in profile and in bad light and this makes no
attempt to hide that: a missed face is a signal that did not fire, never a
claim that nobody was there.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.ffmpeg_ops import require_binaries

log = logging.getLogger(__name__)

#: Analysis size, set by a hard floor rather than by taste. The cascade finds
#: a face down to about twenty-two pixels across and fails below it, so the
#: frame size decides which faces exist at all. Measured on a real photograph
#: of a person, mid-shot: at 320x180 it is never found, at 480x270 it is found
#: a third of the time, at 640x360 always. The cost of that is real and is the
#: reason FPS below is what it is.
WIDTH, HEIGHT = 640, 360
#: Six a second. An expression takes about a third of a second to form, so
#: this samples one twice, and unlike motion the cost here is not the decode -
#: it is the detector, and it is linear in frames. Measured: 4 seconds of CPU
#: per 30-second window at ten a second, which on three streams every twenty
#: seconds is most of a core on its own.
FPS = 6.0
#: The smallest face worth finding, as a share of frame height. Below this it
#: is a person in a crowd, not somebody the clip is about - and below about
#: 22 pixels the cascade cannot see them anyway.
MIN_FACE = 0.07
#: How far back "usual for this stream" reaches.
BASELINE_S = 30.0
WARMUP_S = 3.0


class FacesError(RuntimeError):
    pass


@dataclass
class Face:
    """One face in one frame, in fractions of the frame."""

    x: float
    y: float
    w: float
    h: float

    @property
    def area(self) -> float:
        return self.w * self.h

    @property
    def centre(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2


@dataclass
class Watching:
    """Who was on screen, and when something happened to their face."""

    fps: float
    #: Faces per sampled frame.
    frames: list[list[Face]] = field(default_factory=list)
    #: How much the pixels inside the largest face changed, per frame, 0..1.
    face_change: list[float] = field(default_factory=list)
    #: (time, size) where a face changed far more than it had been.
    reactions: list[tuple[float, float]] = field(default_factory=list)
    #: (time, share) where a face suddenly filled much more of the frame.
    close_ups: list[tuple[float, float]] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return len(self.frames) / self.fps

    @property
    def on_screen(self) -> float:
        """Share of the time at least one face was found."""
        if not self.frames:
            return 0.0
        return sum(1 for f in self.frames if f) / len(self.frames)

    @property
    def biggest(self) -> float:
        """The largest face seen, as a share of the frame."""
        return max((f.area for frame in self.frames for f in frame), default=0.0)

    @property
    def most(self) -> int:
        return max((len(f) for f in self.frames), default=0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "duration_s": round(self.duration_s, 2),
            "on_screen": round(self.on_screen, 3),
            "biggest_face": round(self.biggest, 4),
            "most_faces": self.most,
            "reactions": [
                {"at_s": round(t, 2), "size": round(v, 2)} for t, v in self.reactions
            ],
            "close_ups": [
                {"at_s": round(t, 2), "share": round(v, 3)} for t, v in self.close_ups
            ],
        }


_cascade = None


def detector():  # noqa: ANN201 - cv2.CascadeClassifier
    """The face finder, loaded once."""
    global _cascade
    if _cascade is None:
        import cv2

        _cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        if _cascade.empty():
            raise FacesError("OpenCV shipped no frontal-face cascade")
    return _cascade


def find(frame, width: int = WIDTH, height: int = HEIGHT) -> list[Face]:  # noqa: ANN001
    """Faces in one greyscale frame, in fractions of the frame."""
    least = int(height * MIN_FACE)
    found = detector().detectMultiScale(
        frame, scaleFactor=1.2, minNeighbors=4, minSize=(least, least)
    )
    return [
        Face(x=x / width, y=y / height, w=w / width, h=h / height)
        for x, y, w, h in found
    ]


def frames(src: Path | str, *, fps: float = FPS, seconds: float | None = None):  # noqa: ANN201
    """Greyscale frames, as numpy arrays, at the face-sampling rate."""
    import numpy as np

    require_binaries()
    command = ["ffmpeg", "-v", "error"]
    if seconds:
        command += ["-t", f"{seconds:.2f}"]
    command += [
        "-i", str(src),
        "-vf", f"fps={fps},scale={WIDTH}:{HEIGHT},format=gray",
        "-f", "rawvideo", "-",
    ]
    proc = subprocess.run(command, capture_output=True)
    stride = WIDTH * HEIGHT
    raw = proc.stdout
    if len(raw) < stride:
        raise FacesError(
            f"no frames from {Path(str(src)).name}: "
            f"{proc.stderr.decode('utf-8', 'replace')[-300:]}"
        )
    count = len(raw) // stride
    return np.frombuffer(raw[: count * stride], dtype=np.uint8).reshape(
        count, HEIGHT, WIDTH
    )


def watch(src: Path | str, *, fps: float = FPS, seconds: float | None = None) -> Watching:
    """Find the people, then watch their faces."""
    import numpy as np

    shots = frames(src, fps=fps, seconds=seconds)
    per_frame = [find(shot) for shot in shots]

    # How much the pixels inside the largest face changed since the frame
    # before. Measured inside the box rather than over the whole picture,
    # because a camera panning moves every pixel and means nothing, and a face
    # going from calm to horrified moves very few and means everything.
    change = [0.0]
    for i in range(1, len(shots)):
        faces = per_frame[i]
        if not faces:
            change.append(0.0)
            continue
        face = max(faces, key=lambda f: f.area)
        x0, y0 = int(face.x * WIDTH), int(face.y * HEIGHT)
        x1, y1 = int((face.x + face.w) * WIDTH), int((face.y + face.h) * HEIGHT)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(WIDTH, max(x1, x0 + 1)), min(HEIGHT, max(y1, y0 + 1))
        before = shots[i - 1][y0:y1, x0:x1].astype(np.int16)
        after = shots[i][y0:y1, x0:x1].astype(np.int16)
        change.append(float(np.abs(after - before).mean()) / 255.0)

    found = Watching(fps=fps, frames=per_frame, face_change=change)
    found.reactions = _find_reactions(change, fps)
    found.close_ups = _find_close_ups(per_frame, fps)
    return found


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _find_reactions(
    change: list[float], fps: float, *, over: float = 2.5, floor: float = 0.01
) -> list[tuple[float, float]]:
    """Where a face changed far more than that face had been changing.

    A ratio against its own recent past, like everything else here. A person
    who gestures constantly and a person who sits perfectly still both have a
    normal, and the interesting thing is departure from it.
    """
    back = max(1, int(BASELINE_S * fps))
    least = max(1, int(WARMUP_S * fps))
    found: list[tuple[float, float]] = []
    for i in range(least, len(change)):
        usual = _median([v for v in change[max(0, i - back) : i] if v > 0])
        if usual <= 0 or change[i] < floor:
            continue
        ratio = change[i] / usual
        if ratio < over:
            continue
        at = i / fps
        if found and at - found[-1][0] < 0.7:
            found[-1] = (found[-1][0], max(found[-1][1], ratio))
        else:
            found.append((at, ratio))
    return found


def _find_close_ups(
    per_frame: list[list[Face]], fps: float, *, share: float = 0.06
) -> list[tuple[float, float]]:
    """Where a face suddenly filled much more of the frame than it had.

    Somebody leaning into the camera is what a person does when they have just
    seen something, and it is what a clip of a reaction looks like.
    """
    biggest = [max((f.area for f in frame), default=0.0) for frame in per_frame]
    back = max(1, int(BASELINE_S * fps))
    least = max(1, int(WARMUP_S * fps))
    found: list[tuple[float, float]] = []
    for i in range(least, len(biggest)):
        if biggest[i] < share:
            continue
        usual = _median(biggest[max(0, i - back) : i])
        if biggest[i] < max(share, usual * 1.8):
            continue
        at = i / fps
        if found and at - found[-1][0] < 1.5:
            found[-1] = (found[-1][0], max(found[-1][1], biggest[i]))
        else:
            found.append((at, biggest[i]))
    return found
