"""The instrument layer: drawn frames that sit on top of the footage.

This is the constant. The clip underneath changes every video; the scanlines,
the corner telemetry, the reticle, the tape counter and the timecode do not.
That is what makes a post recognisable in a feed before anyone has read a word,
and it is why the overlay draws instruments rather than scenery - scenery would
hide the very footage it is supposed to be sitting on.

Frames come out as RGBA PNGs for ffmpeg to composite. They are drawn at full
1080x1920 because the HUD is small type, and small type is the first thing to
turn to mush if you draw at half size and scale up.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from core.archives import Archive, Beat

log = logging.getLogger(__name__)

W = 1080
H = 1920

FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
)
FONT_FILES = {
    "mono": ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf"),
    "mono_bold": ("DejaVuSansMono-Bold.ttf", "DejaVuSansMono.ttf"),
    "display": ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"),
}


class OverlayError(RuntimeError):
    pass


@lru_cache(maxsize=64)
def font(role: str, size: int):  # noqa: ANN201 - PIL type, imported lazily
    from PIL import ImageFont

    for name in FONT_FILES.get(role, FONT_FILES["mono"]):
        for directory in FONT_DIRS:
            path = Path(directory) / name
            if path.exists():
                return ImageFont.truetype(str(path), size)
    log.warning("overlay: no DejaVu font found for %r - falling back to the default", role)
    return ImageFont.load_default()


def rgb(hex_colour: str) -> tuple[int, int, int]:
    text = hex_colour.lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def rgba(hex_colour: str, alpha: float) -> tuple[int, int, int, int]:
    r, g, b = rgb(hex_colour)
    return r, g, b, max(0, min(255, int(round(alpha * 255))))


def noise(n: float) -> float:
    """Deterministic pseudo-random in 0..1.

    Deterministic on purpose: the same video rendered twice must produce the
    same frames, or "regenerate and compare" stops meaning anything.
    """
    x = math.sin(n * 127.1) * 43758.5453
    return x - math.floor(x)


# --- painter ---------------------------------------------------------------


@dataclass
class Painter:
    draw: object  # PIL.ImageDraw.ImageDraw
    accent: str
    t: float
    local: float
    beat: Beat
    alpha: float = 1.0

    def a(self, value: float) -> float:
        """Scale an alpha by the overlay strength.

        Floored at half: the slider is there to dial the instruments back over
        busy footage, not to make the timecode unreadable. Below this the HUD
        stops being furniture and starts being a smudge.
        """
        return max(0.0, min(1.0, value * (0.5 + 0.5 * self.alpha)))

    def solid(self, value: float) -> float:
        """An alpha the overlay strength does not touch - titles and captions."""
        return max(0.0, min(1.0, value))

    def text(
        self,
        xy: tuple[float, float],
        body: str,
        *,
        role: str = "mono",
        size: int = 22,
        colour: str = "#FFFFFF",
        opacity: float = 0.8,
        anchor: str = "la",
        spacing: float = 0.0,
    ) -> None:
        if not body:
            return
        fill = rgba(colour, self.a(opacity))
        if spacing <= 0:
            self.draw.text(xy, body, font=font(role, size), fill=fill, anchor=anchor)
            return
        # PIL has no letter-spacing, and the HUD depends on it - so step the
        # glyphs by hand when a caller asks for tracking.
        face = font(role, size)
        widths = [face.getlength(ch) + spacing for ch in body]
        total = sum(widths) - spacing
        x, y = xy
        if anchor[0] == "m":
            x -= total / 2
        elif anchor[0] == "r":
            x -= total
        for ch, width in zip(body, widths, strict=True):
            self.draw.text((x, y), ch, font=face, fill=fill, anchor="l" + anchor[1])
            x += width

    def line(
        self,
        points: list[tuple[float, float]],
        colour: str,
        opacity: float,
        width: int = 2,
    ) -> None:
        if len(points) > 1:
            self.draw.line(points, fill=rgba(colour, self.a(opacity)), width=width, joint="curve")

    def box(
        self,
        xy: tuple[float, float, float, float],
        colour: str,
        opacity: float,
        width: int = 2,
    ) -> None:
        self.draw.rectangle(xy, outline=rgba(colour, self.a(opacity)), width=width)


#: Furniture that never moves, so it is drawn once and handed to ffmpeg as a
#: single image rather than redrawn nine hundred times. Scanlines and graph
#: paper were most of the per-frame cost and none of the per-frame motion.
STATIC: dict[str, dict[str, float | int | str]] = {
    "apollo": {"scan_opacity": 0.15, "scan_gap": 4},
    "af1": {"scan_opacity": 0.06, "scan_gap": 6},
    "buzzer": {
        "scan_opacity": 0.18,
        "scan_gap": 4,
        "grid_step": W // 8,
        "grid_opacity": 0.07,
    },
    "uap": {"scan_opacity": 0.07, "scan_gap": 5},
    "stargate": {"grid_step": 28, "grid_opacity": 0.05},
    "nixon": {"scan_opacity": 0.05, "scan_gap": 6},
}


def render_static(archive: Archive, path: Path | str, *, alpha: float = 0.62) -> Path:
    """Draw the unmoving furniture once: scanlines, graph paper."""
    from PIL import Image, ImageDraw

    spec = STATIC.get(archive.id, {})
    image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    step = int(spec.get("grid_step", 0) or 0)
    if step:
        opacity = max(0.0, min(1.0, float(spec["grid_opacity"]) * alpha))
        fill = rgba(archive.accent, opacity)
        for x in range(0, W, step):
            draw.line([(x, 0), (x, H)], fill=fill, width=1)
        for y in range(0, H, step):
            draw.line([(0, y), (W, y)], fill=fill, width=1)

    gap = int(spec.get("scan_gap", 0) or 0)
    if gap:
        opacity = max(0.0, min(1.0, float(spec["scan_opacity"]) * alpha))
        shade = (0, 0, 0, int(opacity * 255))
        for y in range(0, H, gap):
            draw.rectangle((0, y, W, y + 1), fill=shade)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, compress_level=6)
    return path


# --- per-source instruments ------------------------------------------------


def paint_apollo(p: Painter) -> None:
    """Mission control: telemetry corners, a live trace, and the Earth limb."""
    # The limb: one lit arc across the bottom, drawn thin so the footage
    # underneath still carries the frame.
    cx, cy, r = W * 0.5, H * 1.30, W * 1.02
    points = []
    for i in range(61):
        angle = math.pi * (1.02 + 0.96 * i / 60)
        points.append((cx + math.cos(angle) * r, cy + math.sin(angle) * r))
    p.line(points, p.accent, 0.55, width=3)

    # Signal trace. Amplitude rides the beat so it reads as audio without
    # needing the audio - the real waveform arrives with the caption anyway.
    energy = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(p.t * 5.1)) if p.beat.from_tape else 0.18
    trace = []
    for x in range(0, W + 1, 6):
        v = math.sin(x * 0.021 + p.t * 6) * math.sin(x * 0.005 - p.t * 2.3) * energy * 90
        trace.append((x, H * 0.80 + v))
    p.line(trace, p.accent, 0.6, width=3)

    o2 = "---" if p.t > 19 else "218"
    clock = f"GET  55:{55 + int(p.t // 30):02d}:{int(p.t * 2) % 60:02d}"
    p.text((44, H - 96), clock, size=26, colour=p.accent, opacity=0.8, spacing=2)
    p.text((44, H - 60), "CSM ODYSSEY", size=22, colour="#9FD8E8", opacity=0.5, spacing=3)
    p.text(
        (W - 44, H - 96),
        f"O2  {o2} PSI",
        size=26,
        colour="#E08B3A",
        opacity=0.85,
        anchor="ra",
        spacing=2,
    )
    p.text(
        (W - 44, H - 60),
        "LOOP 24 · FLIGHT",
        size=22,
        colour="#9FD8E8",
        opacity=0.45,
        anchor="ra",
        spacing=3,
    )


def paint_af1(p: Painter) -> None:
    """16mm: sprocket edge, gate weave marks, and a frame counter."""
    jitter = (noise(int(p.t * 22)) - 0.5) * 6

    for y in range(0, H, 120):
        yy = y + jitter
        p.draw.rectangle((10, yy, 34, yy + 62), fill=rgba("#D8D2C6", p.a(0.10)))
        p.draw.rectangle((W - 34, yy, W - 10, yy + 62), fill=rgba("#D8D2C6", p.a(0.10)))

    # An occasional frame flash and a dust hair, which is what actually reads
    # as film rather than a filter named "film".
    if noise(int(p.t * 11)) > 0.965:
        p.draw.rectangle((0, 0, W, H), fill=(255, 255, 255, int(p.a(0.09) * 255)))
    if noise(int(p.t * 8) + 3) > 0.9:
        hx = noise(int(p.t * 8) + 7) * W
        p.line([(hx, H * 0.2), (hx + 8, H * 0.34)], "#EFE9DC", 0.25, width=2)

    p.text((44, H - 96), "SAM 26000", size=26, colour="#D8D2C6", opacity=0.55, spacing=3)
    p.text((44, H - 60), "HF · 11180 kc", size=22, colour="#D8D2C6", opacity=0.35, spacing=3)
    p.text(
        (W - 44, H - 96),
        f"{int(p.t * 24):05d}",
        size=26,
        colour=p.accent,
        opacity=0.7,
        anchor="ra",
        spacing=3,
    )
    p.text(
        (W - 44, H - 60),
        "REEL 1",
        size=22,
        colour="#D8D2C6",
        opacity=0.35,
        anchor="ra",
        spacing=3,
    )


def paint_buzzer(p: Painter) -> None:
    """Oscilloscope. Everything else on the frame gets out of its way."""
    mid = H * 0.5
    amp = H * 0.11
    # The buzz is roughly 25 tones a minute: on for a beat, off for a beat.
    phase = (p.t % 2.4) / 2.4
    live = phase < 0.46
    trace = []
    for x in range(0, W + 1, 4):
        if live:
            v = (
                math.sin(x * 0.14 + p.t * 40) * 0.55
                + math.sin(x * 0.31 - p.t * 22) * 0.3
                + (noise(x + int(p.t * 60)) - 0.5) * 0.3
            ) * amp
        else:
            v = (noise(x + int(p.t * 60)) - 0.5) * amp * 0.10
        trace.append((x, mid + v))
    p.line(trace, p.accent, 0.9 if live else 0.45, width=4)

    sweep = (p.t * 0.28 % 1) * H
    p.line([(0, sweep), (W, sweep)], p.accent, 0.18, width=2)

    p.text((44, 150), "4625.0 kHz  USB", size=30, colour=p.accent, opacity=0.85, spacing=2)
    p.text((W - 44, 150), "S9+20", size=26, colour=p.accent, opacity=0.55, anchor="ra", spacing=2)
    p.text(
        (44, H - 60),
        "UNIDENTIFIED · CONTINUOUS",
        size=22,
        colour=p.accent,
        opacity=0.45,
        spacing=3,
    )
    p.text(
        (W - 44, H - 60),
        f"{int(p.t * 0.42):02d} TONES",
        size=22,
        colour=p.accent,
        opacity=0.45,
        anchor="ra",
        spacing=3,
    )


def paint_uap(p: Painter) -> None:
    """FLIR: a reticle that searches, then locks, and HUD numerals."""
    lock = p.t > 10.0
    ox = W * 0.5 + math.sin(p.t * 0.5) * W * (0.02 if lock else 0.16)
    oy = H * 0.46 + math.cos(p.t * 0.37) * H * (0.01 if lock else 0.08)
    size = W * (0.13 if lock else 0.19)

    arm = size * 0.42
    for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        cx, cy = ox + dx * size, oy + dy * size
        p.line([(cx, cy - dy * arm), (cx, cy)], "#FFFFFF", 0.95 if lock else 0.5, width=4)
        p.line([(cx - dx * arm, cy), (cx, cy)], "#FFFFFF", 0.95 if lock else 0.5, width=4)

    p.line([(W * 0.5, H * 0.42), (W * 0.5, H * 0.50)], "#FFFFFF", 0.3, width=2)
    p.line([(W * 0.44, H * 0.46), (W * 0.56, H * 0.46)], "#FFFFFF", 0.3, width=2)

    p.text((44, 150), "FLIR   WHT HOT", size=28, colour="#FFFFFF", opacity=0.85, spacing=2)
    p.text(
        (W - 44, 150),
        "L+S  TRACK" if lock else "L+S  SRCH",
        size=28,
        colour="#FFFFFF",
        opacity=0.85,
        anchor="ra",
        spacing=2,
    )
    p.text(
        (44, H - 130),
        f"RNG {max(4.0, 34 - p.t * 0.3):04.1f} NM",
        size=26,
        colour="#FFFFFF",
        opacity=0.6,
        spacing=2,
    )
    p.text((44, H - 92), "ALT  25000", size=26, colour="#FFFFFF", opacity=0.6, spacing=2)
    p.text(
        (W - 44, H - 92),
        f"BRG {int(54 + p.t * 2) % 360:03d}",
        size=26,
        colour="#FFFFFF",
        opacity=0.6,
        anchor="ra",
        spacing=2,
    )


def paint_stargate(p: Painter) -> None:
    """Graph paper, and a shape trying to resolve out of it."""
    if p.beat.look == "alt":
        # A structure the viewer is describing, drawn only while they describe
        # it, and never quite completed.
        reveal = min(1.0, max(0.0, p.local / 3.0))
        cw, cy = W * 0.26, H * 0.52
        top = cy - H * 0.20 * reveal
        p.box(
            (W * 0.5 - cw / 2, top, W * 0.5 + cw / 2, cy + H * 0.20),
            "#F0D8B8",
            0.5 * reveal,
            width=3,
        )
        steps = 24
        ellipse = [
            (
                W * 0.5 + math.cos(2 * math.pi * i / steps) * cw / 2,
                top + math.sin(2 * math.pi * i / steps) * cw * 0.16,
            )
            for i in range(steps + 1)
        ]
        p.line(ellipse, "#F0D8B8", 0.45 * reveal, width=3)

    p.text((44, 150), "SESSION 8402-A", size=26, colour=p.accent, opacity=0.6, spacing=2)
    p.text(
        (W - 44, 150),
        "VIEWER 018",
        size=26,
        colour=p.accent,
        opacity=0.45,
        anchor="ra",
        spacing=2,
    )
    p.text(
        (44, H - 60),
        "COORDINATES WITHHELD",
        size=22,
        colour="#D9CDBC",
        opacity=0.35,
        spacing=3,
    )


def paint_nixon(p: Painter) -> None:
    """Two reels, turning at different rates, and a counter that climbs."""

    def reel(cx: float, cy: float, r: float, spin: float) -> None:
        steps = 40
        ring = [
            (
                cx + math.cos(2 * math.pi * i / steps) * r,
                cy + math.sin(2 * math.pi * i / steps) * r,
            )
            for i in range(steps + 1)
        ]
        p.line(ring, p.accent, 0.5, width=3)
        for k in range(3):
            rr = r * (0.42 + k * 0.16)
            inner = [
                (
                    cx + math.cos(2 * math.pi * i / steps) * rr,
                    cy + math.sin(2 * math.pi * i / steps) * rr,
                )
                for i in range(steps + 1)
            ]
            p.line(inner, p.accent, 0.2, width=2)
        for s in range(3):
            angle = spin + s * 2.0944
            p.line(
                [
                    (cx + math.cos(angle) * r * 0.30, cy + math.sin(angle) * r * 0.30),
                    (cx + math.cos(angle) * r * 0.86, cy + math.sin(angle) * r * 0.86),
                ],
                p.accent,
                0.55,
                width=8,
            )

    ry, rr = H * 0.30, W * 0.19
    reel(W * 0.28, ry, rr, p.t * 1.5)
    reel(W * 0.72, ry, rr * 0.86, -p.t * 1.9)
    p.line(
        [
            (W * 0.28 + rr, ry + rr * 0.2),
            (W * 0.5, ry + rr * 0.95),
            (W * 0.72 - rr * 0.86, ry + rr * 0.2),
        ],
        "#6A5B4A",
        0.6,
        width=5,
    )

    # Not the room number: the chrome already prints that top-left, and the
    # same string twice in one corner reads as a rendering bug.
    p.text((44, 150), "CONV 741-2", size=28, colour=p.accent, opacity=0.7, spacing=3)
    p.text(
        (W - 44, 150),
        f"{int(p.t * 13):04d}",
        size=32,
        colour=p.accent,
        opacity=0.55,
        anchor="ra",
        spacing=3,
    )
    p.text((44, H - 60), "3.75 IPS · TRACK 2", size=22, colour="#E8DCC4", opacity=0.3, spacing=3)


PAINTERS = {
    "apollo": paint_apollo,
    "af1": paint_af1,
    "buzzer": paint_buzzer,
    "uap": paint_uap,
    "stargate": paint_stargate,
    "nixon": paint_nixon,
}


# --- common furniture ------------------------------------------------------


def paint_chrome(p: Painter, archive: Archive, elapsed: float) -> None:
    """Timecode, overline, and the title card - the same on every source.

    Drawn at full strength regardless of the overlay slider. These are the
    things a viewer is meant to read; the instruments are the things they are
    meant to feel.
    """
    full = Painter(
        draw=p.draw, accent=p.accent, t=p.t, local=p.local, beat=p.beat, alpha=1.0
    )
    full.text(
        (48, 96), archive.title_card[0][:18], size=24, colour=p.accent, opacity=0.85, spacing=4
    )
    full.text(
        (W - 48, 96),
        f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}",
        size=24,
        colour="#FFFFFF",
        opacity=0.7,
        anchor="ra",
        spacing=3,
    )

    if p.beat.kind == "title":
        fade = min(1.0, p.local / 0.4) * min(1.0, (p.beat.seconds - p.local) / 0.5)
        # A slab behind the title: over bright footage white-on-nothing is
        # unreadable, and a drop shadow alone does not survive compression.
        band = int(H * 0.44)
        p.draw.rectangle((0, band - 110, W, band + 190), fill=(0, 0, 0, int(150 * fade)))
        full.text(
            (W / 2, band),
            archive.title_card[0],
            role="display",
            size=112,
            colour="#FFFFFF",
            opacity=fade,
            anchor="ma",
        )
        full.text(
            (W / 2, band + 140),
            archive.title_card[1].upper(),
            size=28,
            colour=p.accent,
            opacity=fade,
            anchor="ma",
            spacing=6,
        )

    if p.beat.overline:
        full.text(
            (W / 2, 260),
            p.beat.overline.upper(),
            size=28,
            colour="#FFFFFF",
            opacity=0.92,
            anchor="ma",
            spacing=5,
        )


# --- frame sequence --------------------------------------------------------


def render_frames(
    archive: Archive,
    out_dir: Path | str,
    *,
    voice_hook: bool = False,
    fps: int = 24,
    alpha: float = 0.62,
    limit: int | None = None,
) -> int:
    """Draw the whole overlay to `out_dir` as frame-00001.png onwards.

    Returns the number of frames written. `limit` caps the count for tests so
    they can exercise the real drawing code without paying for a full video.
    """
    from PIL import Image, ImageDraw

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    timeline = archive.timeline(voice_hook)
    total = archive.duration_s(voice_hook)
    count = int(round(total * fps))
    if limit is not None:
        count = min(count, limit)

    paint = PAINTERS.get(archive.id, paint_apollo)
    written = 0

    for index in range(count):
        t = index / fps
        beat = timeline[-1][2]
        local = 0.0
        for start, end, candidate in timeline:
            if start <= t < end:
                beat, local = candidate, t - start
                break

        image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        painter = Painter(
            draw=ImageDraw.Draw(image),
            accent=archive.accent,
            t=t,
            local=local,
            beat=beat,
            alpha=alpha,
        )
        paint(painter)
        paint_chrome(painter, archive, t)

        # compress_level=1 rather than the default 6: these are throwaway
        # intermediates and ffmpeg reads them back within seconds, so spending
        # CPU on smaller files is spending it in the wrong place.
        image.save(out_dir / f"frame-{index + 1:05d}.png", compress_level=1)
        written += 1

    log.info("overlay: %d frames for %s at %dfps", written, archive.id, fps)
    return written
