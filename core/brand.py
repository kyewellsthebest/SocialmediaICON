"""Put the mark on the video, and change nothing else.

The video is somebody else's and it is already finished: they framed it, they
chose where it starts and stops, and several thousand people voted on the
result. So nothing here crops, cuts, captions or re-frames. The only change is
a small circular badge in the top-left corner.

Two decisions are worth stating because they look like details and are not:

**The badge is sized as a fraction of the video's width, not in pixels.** The
same 96px logo is a discreet mark on a 1080-wide video and a sticker covering
someone's face on a 480-wide one, and Reddit serves both.

**The corner is inset by a fraction too, and the fraction is generous.** Every
platform draws its own furniture over the top of a reel - a handle, a follow
button, a sound name - and the top-left is where a "reposted" chip tends to
land. Tucking the badge right into the corner is how it ends up half under
something else.

The audio is copied, never re-encoded. Reddit's audio is already compressed
once; a second pass costs quality for nothing, since nothing here touches it.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from core.config import settings

log = logging.getLogger(__name__)

#: Shipped with the code rather than fetched: a logo that fails to download is
#: a run that posts unbranded video, which cannot be taken back.
LOGO = Path(__file__).resolve().parent / "assets" / "putitup.png"


class BrandingFailed(RuntimeError):
    pass


def video_width(path: Path) -> int:
    """How wide the video is, in pixels.

    Measured rather than assumed, because the badge is sized as a share of it
    and ffmpeg will not do that arithmetic inside a `scale` filter - `main_w`
    exists in `overlay` and not there, which fails at render time with a
    message about scale2ref that says nothing about the actual mistake.
    """
    found = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    try:
        return int(found.stdout.strip().split(",")[0])
    except (ValueError, IndexError):
        raise BrandingFailed(
            f"could not read the size of {path.name}: {found.stderr.strip()[:200]}"
        ) from None


def filtergraph(
    width: int,
    width_share: float | None = None,
    inset_share: float | None = None,
    opacity: float | None = None,
) -> str:
    """The overlay, in pixels worked out from the video's own width."""
    width_share = settings.brand_width_share if width_share is None else width_share
    inset_share = settings.brand_inset_share if inset_share is None else inset_share
    opacity = settings.brand_opacity if opacity is None else opacity

    # Even numbers: an odd-sized overlay on a yuv420p stream lands on a half
    # chroma sample and ffmpeg rounds it somewhere of its own choosing.
    side = max(2, int(round(width * width_share / 2)) * 2)
    inset = max(0, int(round(width * inset_share)))

    # Explicitly square. The badge is a circle, and a logo file that is one
    # pixel off square would otherwise arrive as an ellipse.
    scaled = f"[1:v]scale={side}:{side}[badge]"
    if opacity < 1.0:
        scaled += f";[badge]format=rgba,colorchannelmixer=aa={opacity:.3f}[badge]"
    return f"{scaled};[0:v][badge]overlay={inset}:{inset}:format=auto[out]"


def apply(video: Path, into: Path, logo: Path | None = None) -> Path:
    """Write a branded copy of `video` and return where it went."""
    logo = logo or LOGO
    if not logo.exists():
        raise BrandingFailed(f"the logo is missing from {logo}")
    if not video.exists():
        raise BrandingFailed(f"no video at {video}")

    into.mkdir(parents=True, exist_ok=True)
    out = into / f"{video.stem}-branded.mp4"

    command = [
        "ffmpeg", "-y", "-v", "error",
        "-i", str(video),
        "-i", str(logo),
        "-filter_complex", filtergraph(video_width(video)),
        "-map", "[out]",
        # Optional, because a Reddit video can legitimately have no audio
        # track - and without the ?, ffmpeg fails the whole render over it.
        "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(settings.brand_crf),
        "-pix_fmt", "yuv420p",
        # Compressed once already. A second pass costs quality and buys
        # nothing, since the overlay does not touch the sound.
        "-c:a", "copy",
        # Lets a player start before the whole file has arrived, which is what
        # every platform's ingest wants.
        "-movflags", "+faststart",
        str(out),
    ]
    done = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if done.returncode != 0 or not out.exists():
        raise BrandingFailed(
            f"ffmpeg refused to brand {video.name}: {done.stderr.strip()[:400]}")

    log.info("branded %s (%.1f MB)", out.name, out.stat().st_size / 1e6)
    return out
