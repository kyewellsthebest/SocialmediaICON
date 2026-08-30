"""Video built around a real photograph of a person.

There is no corpus of streamer footage here, but scikit-image bundles a real
photograph of a human face, and a real face is what a Haar cascade needs -
there is no way to synthesise one. So the tests move that photograph around a
real H.264 video and ask the detector where it went.
"""

from __future__ import annotations

import functools
import subprocess
import tempfile
from pathlib import Path

W, H, FPS = 960, 540, 10


def where() -> Path:
    found = Path(tempfile.gettempdir()) / "clipengine-synth-faces"
    found.mkdir(parents=True, exist_ok=True)
    return found


def _person():
    import cv2
    from skimage import data

    return cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR)


def _place(canvas, scale: float, cx: float, cy: float):
    import cv2

    side = max(2, int(H * scale))
    small = cv2.resize(_person(), (side, side))
    x, y = int(cx * W - side / 2), int(cy * H - side / 2)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + side), min(H, y + side)
    if x1 > x0 and y1 > y0:
        canvas[y0:y1, x0:x1] = small[y0 - y : y1 - y, x0 - x : x1 - x]
    return canvas


def _build(name: str, frame_of, seconds: float = 30.0) -> Path:
    import numpy as np

    path = where() / f"{name}.mp4"
    if path.exists():
        return path
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264",
         "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-y", str(path)],
        stdin=subprocess.PIPE,
    )
    for i in range(int(seconds * FPS)):
        proc.stdin.write(frame_of(i, np.full((H, W, 3), 40, np.uint8)).tobytes())
    proc.stdin.close()
    proc.wait()
    return path


@functools.cache
def one_person() -> Path:
    """A person, mid-shot, sitting still. The ordinary case."""
    return _build("one", lambda i, c: _place(c, 0.62, 0.5, 0.5))


@functools.cache
def leans_in(at: float = 20.0) -> Path:
    """Somebody leaning into the camera - what a reaction looks like."""
    return _build(
        "leans",
        lambda i, c: _place(c, 0.62 if i < at * FPS else 1.5, 0.5,
                            0.5 if i < at * FPS else 0.55),
    )


@functools.cache
def two_people() -> Path:
    return _build(
        "two", lambda i, c: _place(_place(c, 0.55, 0.3, 0.5), 0.55, 0.72, 0.5)
    )


@functools.cache
def nobody() -> Path:
    """A moving shape and no people at all."""
    import cv2

    def frame(i, c):
        cv2.circle(c, (150 + (i * 6) % 600, 270), 60, (90, 90, 90), -1)
        return c

    return _build("nobody", frame)
