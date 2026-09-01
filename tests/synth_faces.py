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


@functools.cache
def desk_stream(share: float = 0.20) -> Path:
    """A desk stream with the facecam at `share` of frame height.

    The fixture that exists because screen_share() uses a big one. A real
    facecam is commonly 0.15 to 0.30 of frame height, and at 640x360 - the
    size the continuous sense pass looks at - the detector could not see
    anything under about 0.32. Every desk stream below that was framed as
    though it had no webcam, which is most of them.
    """
    import cv2

    def frame(i, c):
        c[:] = (28, 28, 30)
        for row in range(6):
            y = 90 + row * 55
            cv2.rectangle(c, (60, y), (60 + 380 + (row * 37) % 180, y + 16),
                          (170, 170, 175), -1)
        for bar in range(5):
            h = 40 + int(60 * abs(((i / FPS) + bar) % 4 - 2))
            cv2.rectangle(c, (520 + bar * 46, 430 - h), (556 + bar * 46, 430),
                          (90, 150, 220), -1)
        return _place(c, share, 0.83, 0.24)

    return _build(f"desk{int(share * 100)}", frame, seconds=12.0)


@functools.cache
def person_in_a_room() -> Path:
    """Somebody small, off to one side, in a room with no screen in it.

    The exact shape that was mistaken for a desk stream: the face is small
    (an overlay-sized face), it sits well away from the middle (a corner), and
    it stays there (furniture, not somebody walking past). All three of the
    old tests pass. There is no computer anywhere in the shot, and stacking it
    put a zoomed crop of the person's head over a wide shot of the same room.

    The background is deliberately organic - blobs and gradients, no straight
    lines and no flat panels - because that is what actually separates a room
    from a screen.
    """
    import cv2
    import numpy as np

    rng = np.random.default_rng(11)
    blobs = [(int(rng.integers(0, W)), int(rng.integers(0, H)),
              int(rng.integers(40, 140))) for _ in range(26)]

    # Texture everywhere, which is the point. A real room is carpet, fabric
    # and sensor noise - never a flat panel. A first attempt at this fixture
    # painted the wall in bands of flat colour and read as 92% flat, which is
    # more screen-like than an actual screen; the fixture was wrong, not the
    # detector.
    grain = rng.integers(-14, 15, size=(H, W, 3), dtype=np.int16)

    def frame(i, c):
        for x in range(0, W, 8):                       # a soft wall gradient
            c[:, x : x + 8] = 40 + int(38 * x / W)
        for x, y, r in blobs:                          # furniture-ish shapes
            cv2.circle(c, (x, y), r, (int(60 + r % 70), int(52 + r % 40), 48), -1)
        rolled = np.roll(grain, i % 7, axis=0)         # and it never sits still
        np.clip(c.astype(np.int16) + rolled, 0, 255, out=c.astype(np.int16))
        c[:] = np.clip(c.astype(np.int16) + rolled, 0, 255).astype(np.uint8)
        return _place(c, 0.22, 0.17, 0.55)             # small, far left

    return _build("room-textured", frame, seconds=12.0)


@functools.cache
def screen_share() -> Path:
    """A computer screen with a small webcam in the corner.

    The layout the crop tracker could not handle: the interesting thing is in
    two places at once, so following the motion lands between them and shows
    neither. Text on the left, a chart that changes, and the person in a box
    top-right - the shape of every desk stream.
    """
    import cv2

    def frame(i, c):
        c[:] = (28, 28, 30)
        # A "screen": rows of text and a bar chart that moves a little.
        for row in range(6):
            y = 90 + row * 55
            cv2.rectangle(c, (60, y), (60 + 380 + (row * 37) % 180, y + 16),
                          (170, 170, 175), -1)
        for bar in range(5):
            h = 40 + int(60 * abs(((i / FPS) + bar) % 4 - 2))
            cv2.rectangle(c, (520 + bar * 46, 430 - h), (556 + bar * 46, 430),
                          (90, 150, 220), -1)
        # ...and the webcam, small, top right.
        # 0.45 of frame height. A real facecam is ~350px on a 1080p stream,
        # which is a third of the height; this fixture is half that size, so
        # the same overlay has to be the same *share* to stay findable.
        return _place(c, 0.45, 0.80, 0.25)

    return _build("screenshare2", frame)
