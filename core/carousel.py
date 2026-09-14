"""The two-slide Instagram post: a cover, then the video, both square.

A reel and a carousel are two different kinds of post and Instagram treats
them as such - a reel goes to the Reels surface, a carousel sits in the grid
and in the feed. Posting the same video as both, half an hour apart, puts it
in front of two different sets of eyes without either looking like a repeat.

Two rules shape everything here:

**Nothing is cropped away.** A 9:16 gym video squeezed into a square by
cutting the sides loses the barbell, and cutting top and bottom loses the
lift. So the whole frame is fitted inside the square and the gap is filled
with a blurred, darkened copy of itself - the video stays whole and the square
stays full.

**The cover is the first thing anyone sees and it is not the video.** It is a
still with a reason to swipe on it, which is the only job it has: a carousel
whose first slide is just a frame of the video gives nobody any reason to go
to the second.

**Neither slide is branded here.** Both are built from the reel that has
already been through `core.brand`, so the mark is in the picture before this
module sees it. Adding one put a second, larger logo on top of the first.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from core.brand import BrandingFailed, video_width
from core.config import settings

log = logging.getLogger(__name__)

#: Instagram's square. Larger than it displays, because it downscales better
#: than it upscales and the source is often 1080 wide already.
SIDE = 1080

#: Shipped with the image, so a cover cannot fail to render for want of a
#: typeface. fonts-dejavu-core is installed in the Dockerfile for exactly
#: this and for libass.
FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

GREEN = (85, 229, 32)
INK = (255, 255, 255)


def first_frame(video: Path, into: Path, at_s: float | None = None) -> Path:
    """A still from `at_s` seconds in.

    Not from zero. The literal first frame is very often black, a fade, or a
    hand still reaching for the phone - all of which make a cover that says
    nothing about the video behind it.
    """
    at_s = settings.carousel_frame_at_s if at_s is None else at_s
    into.mkdir(parents=True, exist_ok=True)
    out = into / f"{video.stem}-frame.png"

    done = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{at_s:.2f}", "-i", str(video),
         "-frames:v", "1", str(out)],
        capture_output=True, text=True, timeout=300,
    )
    if done.returncode != 0 or not out.exists():
        # A video shorter than the offset gives nothing at all, so fall back
        # to the very start rather than failing the whole post.
        done = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(video),
             "-frames:v", "1", str(out)],
            capture_output=True, text=True, timeout=300,
        )
    if done.returncode != 0 or not out.exists():
        raise BrandingFailed(
            f"could not take a frame from {video.name}: {done.stderr.strip()[:300]}")
    return out


def _fit_on_blur(image, side: int = SIDE):
    """The whole picture inside a square, on a blurred copy of itself.

    Letterboxing onto flat black would work and looks like a mistake. The
    blurred fill reads as deliberate, and keeps the eye on the middle.
    """
    from PIL import Image, ImageEnhance, ImageFilter

    source = image.convert("RGB")

    # The fill: scaled to cover, cropped to the square, blurred and darkened
    # so it cannot compete with the video sitting on top of it.
    scale = max(side / source.width, side / source.height)
    covered = source.resize(
        (max(1, round(source.width * scale)), max(1, round(source.height * scale))),
        Image.LANCZOS,
    )
    left = (covered.width - side) // 2
    top = (covered.height - side) // 2
    background = covered.crop((left, top, left + side, top + side))
    background = background.filter(ImageFilter.GaussianBlur(radius=side // 24))
    background = ImageEnhance.Brightness(background).enhance(0.45)

    # The picture: scaled to fit, whole, centred.
    scale = min(side / source.width, side / source.height)
    fitted = source.resize(
        (max(1, round(source.width * scale)), max(1, round(source.height * scale))),
        Image.LANCZOS,
    )
    background.paste(fitted, ((side - fitted.width) // 2, (side - fitted.height) // 2))
    return background


def cover(frame: Path, into: Path, swipe: str | None = None) -> Path:
    """The first slide: the still, and a reason to swipe.

    No mark is drawn here. The frame comes from the branded reel, so it is
    already carrying one - adding a second put two logos of different sizes
    over each other at the top of the slide.
    """
    from PIL import Image, ImageDraw, ImageFont

    swipe = settings.carousel_swipe_text if swipe is None else swipe
    into.mkdir(parents=True, exist_ok=True)
    out = into / f"{frame.stem}-cover.jpg"

    canvas = _fit_on_blur(Image.open(frame))
    draw = ImageDraw.Draw(canvas, "RGBA")

    # A gradient rather than a bar: a hard edge across a photograph looks like
    # a rendering fault, and the text needs the contrast only at the bottom.
    depth = int(SIDE * 0.34)
    for row in range(depth):
        shade = int(200 * (row / depth) ** 1.6)
        draw.line([(0, SIDE - depth + row), (SIDE, SIDE - depth + row)],
                  fill=(0, 0, 0, shade))

    # The swipe cue: a green pill, because the brand is a green mark on black
    # and the eye goes to the one saturated thing in a darkened photograph.
    size = int(SIDE * 0.058)
    try:
        font = ImageFont.truetype(str(FONT), size)
    except OSError:
        # Losing the typeface must not lose the post. The cue is smaller and
        # plainer, and everything else is unchanged.
        font = ImageFont.load_default()

    label = swipe.upper()
    box = draw.textbbox((0, 0), label, font=font)
    text_w, text_h = box[2] - box[0], box[3] - box[1]
    pad_x, pad_y = int(size * 0.85), int(size * 0.55)
    pill_w, pill_h = text_w + pad_x * 2, text_h + pad_y * 2
    pill_x = (SIDE - pill_w) // 2
    pill_y = SIDE - int(SIDE * 0.11) - pill_h // 2

    draw.rounded_rectangle(
        [pill_x, pill_y, pill_x + pill_w, pill_y + pill_h],
        radius=pill_h // 2, fill=GREEN,
    )
    draw.text((pill_x + pad_x - box[0], pill_y + pad_y - box[1]),
              label, font=font, fill=(6, 23, 10))

    canvas.save(out, "JPEG", quality=92, optimize=True)
    log.info("cover: %s (%.1f KB)", out.name, out.stat().st_size / 1e3)
    return out


def square(video: Path, into: Path) -> Path:
    """The second slide: the whole video, square.

    The mark is not added here either. This is built from the branded reel,
    which already has one - drawing a second put a large logo on top of the
    small one the video was carrying, at slightly different sizes, which read
    as a rendering fault rather than as branding.
    """
    into.mkdir(parents=True, exist_ok=True)
    out = into / f"{video.stem}-square.mp4"

    graph = (
        # The fill: cover the square, blur it hard, darken it. Same idea as
        # the cover slide, done in ffmpeg because it has to run per frame.
        f"[0:v]scale={SIDE}:{SIDE}:force_original_aspect_ratio=increase,"
        f"crop={SIDE}:{SIDE},gblur=sigma={SIDE // 24},eq=brightness=-0.22[bg];"
        # The picture: whole, fitted, centred. Nothing is cropped away - a
        # 9:16 lift cut to a square loses either the barbell or the lifter.
        f"[0:v]scale={SIDE}:{SIDE}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[out]"
    )

    command = [
        "ffmpeg", "-y", "-v", "error",
        "-i", str(video),
        "-filter_complex", graph,
        "-map", "[out]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(settings.brand_crf),
        "-pix_fmt", "yuv420p",
        # Re-encoded rather than copied, unlike the reel: the picture is being
        # rebuilt anyway, and Instagram is fussier about carousel video.
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out),
    ]
    done = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if done.returncode != 0 or not out.exists():
        raise BrandingFailed(
            f"could not square {video.name}: {done.stderr.strip()[:400]}")

    log.info("square: %s (%.1f MB)", out.name, out.stat().st_size / 1e6)
    return out


def build(video: Path, into: Path) -> tuple[Path, Path]:
    """Both slides, in order. Returns (cover, square video)."""
    frame = first_frame(video, into)
    slide_one = cover(frame, into)
    frame.unlink(missing_ok=True)
    return slide_one, square(video, into)


__all__ = ["SIDE", "build", "cover", "first_frame", "square", "video_width"]
