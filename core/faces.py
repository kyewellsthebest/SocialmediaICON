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

Two rates, because the two halves cost wildly different amounts. *Finding* a
face is the expensive half - a cascade over the whole picture, 4 seconds of
CPU per 30-second window at ten frames a second - and it is also the half that
does not need speed: a face does not move far in a sixth of a second, so
finding one six times a second and carrying the box forward loses nothing.
*Watching* a face is the cheap half - the mean absolute difference of a few
thousand pixels inside a box already known - and it is the half that needs
every frame, because a flinch is three frames and a face at 60fps that is only
looked at 6 times a second is a face whose flinch was never sampled.

So: found six times a second, watched sixty. Measured on 30 seconds of real
1080p60 - 2.40s to find faces six times a second and look at nothing else,
3.46s to find them six times a second and watch every one of the 1800 frames.
Ten times the frames for 44% more CPU, because the decode was always the bill.

The frames are read one at a time rather than held, because 30 seconds of
640x360 at 60fps is 414MB and this has to run on three streams at once
forever. Streamed, the whole process peaks at 62MB.

Haar cascades rather than a neural detector, because a neural detector means a
weights file to ship and 40ms a frame to pay, and this has to run on three
streams forever. They miss faces in profile and in bad light and this makes no
attempt to hide that: a missed face is a signal that did not fire, never a
claim that nobody was there.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.ffmpeg_ops import probe, require_binaries

log = logging.getLogger(__name__)

#: Analysis size, set by a hard floor rather than by taste. The cascade finds
#: a face down to about twenty-two pixels across and fails below it, so the
#: frame size decides which faces exist at all. Measured on a real photograph
#: of a person, mid-shot: at 320x180 it is never found, at 480x270 it is found
#: a third of the time, at 640x360 always. The cost of that is real and is the
#: reason FPS below is what it is.
WIDTH, HEIGHT = 640, 360
#: How often the cascade is run. Six a second: an expression takes about a
#: third of a second to form, so a box is never more than a sixth of a second
#: stale, and the cost here is not the decode - it is the detector, and it is
#: linear in frames. Measured: 4 seconds of CPU per 30-second window at ten a
#: second, which on three streams every twenty seconds is most of a core.
DETECT_FPS = 6.0
#: The fallback read rate, and the ceiling, for a source whose container will
#: not say what it runs at. Everything else is read at the source's own rate.
FPS = 30.0
MAX_FPS = 60.0
#: The smallest face worth finding, as a share of frame height.
#:
#: 0.04 - about 14 pixels at the analysis size - down from 0.07, which was set
#: by what a Haar cascade could manage rather than by what is worth finding. A
#: webcam box in the corner of a game contains a face of about 20 pixels here,
#: and that is the most common layout on Kick; a floor set at 25 threw away
#: the one detection the stacked crop depends on. YuNet finds them, so the
#: floor can be what it should always have been: small enough for a facecam,
#: large enough that a face in a crowd behind somebody is not the subject.
MIN_FACE = 0.04
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
    #: How often the cascade actually ran. Lower than `fps` on purpose: see
    #: the module docstring.
    detect_fps: float = 0.0
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


#: YuNet, 232KB, vendored rather than fetched at build time so a deploy does
#: not depend on GitHub being up.
#:
#: It replaced the Haar cascade because Haar could not do the job. On a real
#: screenshot of Ninja - headset, side lighting, looking down and away - the
#: frontal, alt2 and profile cascades between them found nothing at all, and
#: that is the single most common layout on Kick: a game filling the frame
#: with the streamer in a box in the corner. Without a face there, the crop
#: falls back to following the motion, which on that layout lands between the
#: game and the webcam and shows neither.
#:
#: It is also faster: 2.62s against 3.10s for the same 180 frames, because a
#: single small network beats sliding a cascade over six scales.
MODEL = Path(__file__).with_name("assets") / "face_detection_yunet_2023mar.onnx"
#: How sure it has to be. YuNet reports a score per box; below this the boxes
#: are texture that happens to be face-shaped, which on a game stream is
#: constant.
MIN_SCORE = 0.65

_finder = None
_cascade = None


def detector():  # noqa: ANN201 - cv2.FaceDetectorYN or CascadeClassifier
    """The face finder, loaded once. YuNet if it is there, Haar if not."""
    global _finder, _cascade
    import cv2

    if _finder is None and MODEL.exists() and hasattr(cv2, "FaceDetectorYN"):
        try:
            _finder = cv2.FaceDetectorYN.create(
                str(MODEL), "", (WIDTH, HEIGHT), MIN_SCORE, 0.3, 500
            )
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail
            log.warning("faces: could not load %s (%s); using the cascade",
                        MODEL.name, exc)
            _finder = None
    if _finder is not None:
        return _finder

    if _cascade is None:
        _cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        if _cascade.empty():
            raise FacesError("OpenCV shipped no frontal-face cascade")
    return _cascade


def find(frame, width: int = WIDTH, height: int = HEIGHT) -> list[Face]:  # noqa: ANN001
    """Faces in one greyscale frame, in fractions of the frame."""
    import cv2

    found = detector()
    least = int(height * MIN_FACE)

    if not isinstance(found, cv2.CascadeClassifier):
        # YuNet wants three channels and its own idea of the frame size.
        found.setInputSize((width, height))
        _, boxes = found.detect(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))
        return [
            Face(x=x / width, y=y / height, w=w / width, h=h / height)
            for x, y, w, h in (
                (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
                for b in (boxes if boxes is not None else [])
            )
            if h >= least
        ]

    # scaleFactor 1.1, not 1.2. The step is how much the search window grows
    # between passes, so 1.2 leaves 20% gaps in the sizes it looks for and a
    # face that falls between two of them is not found at all.
    got = found.detectMultiScale(
        frame, scaleFactor=1.1, minNeighbors=4, minSize=(least, least)
    )
    return [
        Face(x=x / width, y=y / height, w=w / width, h=h / height)
        for x, y, w, h in got
    ]


def source_fps(src: Path | str) -> float:
    """The source's own frame rate, or FPS when it will not say."""
    try:
        declared = probe(src).fps
    except Exception:
        return FPS
    return min(declared, MAX_FPS) if declared > 0.0 else FPS


def stream(src: Path | str, *, fps: float, seconds: float | None = None) -> Iterator[Any]:
    """Greyscale frames one at a time, never all at once.

    30 seconds of 640x360 at 60fps is 414MB held, and three streams of that is
    the whole box. Read from the pipe a frame at a time it is 230KB.
    """
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
    stride = WIDTH * HEIGHT
    proc = subprocess.Popen(  # noqa: S603
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    seen = 0
    try:
        while True:
            raw = proc.stdout.read(stride)
            if len(raw) < stride:
                break
            seen += 1
            yield np.frombuffer(raw, dtype=np.uint8).reshape(HEIGHT, WIDTH)
    finally:
        # A caller that stops early leaves ffmpeg writing into a pipe nobody
        # reads, which blocks it forever.
        if proc.poll() is None:
            proc.kill()
        stderr = proc.stderr.read() if proc.stderr else b""
        proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
        proc.wait()
    if seen == 0:
        raise FacesError(
            f"no frames from {Path(str(src)).name}: "
            f"{stderr.decode('utf-8', 'replace')[-300:]}"
        )


def frames(src: Path | str, *, fps: float = FPS, seconds: float | None = None):  # noqa: ANN201
    """Every frame at once, as one array. Only for tests and short clips -
    `stream` is what the live path uses, and for the reason given there."""
    import numpy as np

    return np.stack(list(stream(src, fps=fps, seconds=seconds)))


def _box(face: Face) -> tuple[int, int, int, int]:
    """A face in pixels, clamped inside the frame and never empty."""
    x0, y0 = max(0, int(face.x * WIDTH)), max(0, int(face.y * HEIGHT))
    x1 = min(WIDTH, max(int((face.x + face.w) * WIDTH), x0 + 1))
    y1 = min(HEIGHT, max(int((face.y + face.h) * HEIGHT), y0 + 1))
    return x0, y0, x1, y1


def watch(
    src: Path | str,
    *,
    fps: float | None = None,
    detect_fps: float = DETECT_FPS,
    seconds: float | None = None,
) -> Watching:
    """Find the people, then watch their faces - every frame of them.

    `fps=None` reads at the source's own rate. The cascade still only runs
    `detect_fps` times a second; in between, the last boxes are carried
    forward, which is what makes reading every frame affordable.
    """
    import numpy as np

    if fps is None:
        fps = source_fps(src)
    every = max(1, int(round(fps / max(detect_fps, 0.1))))

    per_frame: list[list[Face]] = []
    # How much the pixels inside the largest face changed since the frame
    # before. Measured inside the box rather than over the whole picture,
    # because a camera panning moves every pixel and means nothing, and a face
    # going from calm to horrified moves very few and means everything.
    change: list[float] = []
    carried: list[Face] = []
    previous = None

    for i, shot in enumerate(stream(src, fps=fps, seconds=seconds)):
        if i % every == 0:
            carried = find(shot)
        per_frame.append(carried)

        if previous is None or not carried:
            change.append(0.0)
        else:
            x0, y0, x1, y1 = _box(max(carried, key=lambda f: f.area))
            before = previous[y0:y1, x0:x1].astype(np.int16)
            after = shot[y0:y1, x0:x1].astype(np.int16)
            change.append(float(np.abs(after - before).mean()) / 255.0)
        previous = shot

    found = Watching(fps=fps, detect_fps=fps / every, frames=per_frame, face_change=change)
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


#: How much of the frame a face fills before it counts as a close-up, by area.
#:
#: 0.03, down from 0.06, because the boxes got smaller when the detector got
#: finer. A coarse scale step snaps to whichever search window it happens to
#: reach and overshoots; stepping by 10% instead of 20% returns the face
#: rather than the face and some wall, and every box shrank by about a third.
#: A real lean-in measured 0.037 and was being missed by a floor set against
#: the old, inflated boxes. 0.03 of the area is a face about a fifth of the
#: frame across, which is a close-up by any reading.
CLOSE_UP_AREA = 0.03


def _find_close_ups(
    per_frame: list[list[Face]], fps: float, *, share: float = CLOSE_UP_AREA
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
