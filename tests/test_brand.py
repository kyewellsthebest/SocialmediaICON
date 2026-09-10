"""The badge, and the promise that nothing else is touched.

The video belongs to somebody else and is already finished. They framed it,
they chose where it starts and stops, and several thousand people voted on the
result. So the test that matters is not "does the logo appear" - it is "did
anything else change".
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import brand
from core.config import settings

needs_ffmpeg = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg is a system binary and is not installed here",
)


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def stream(info: dict, kind: str) -> dict | None:
    return next((s for s in info["streams"] if s["codec_type"] == kind), None)


@pytest.fixture
def a_video(tmp_path):
    """Eight seconds of moving colour with a tone over it, 720x1280."""
    path = tmp_path / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=30:duration=8",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return path


class TestTheBadgeItself:
    def test_the_logo_ships_with_the_code(self):
        """Not fetched. A logo that fails to download is a run that posts
        unbranded video, and that cannot be taken back."""
        assert brand.LOGO.exists(), f"missing {brand.LOGO}"

    def test_it_is_a_circle_on_a_transparent_background(self):
        from PIL import Image

        badge = Image.open(brand.LOGO).convert("RGBA")
        width, height = badge.size
        assert width == height, "a badge that is not square arrives stretched"
        corners = [badge.getpixel(p)[3] for p in
                   ((1, 1), (width - 2, 1), (1, height - 2), (width - 2, height - 2))]
        assert max(corners) == 0, "the corners must be transparent, not black"
        assert badge.getpixel((width // 2, height // 2))[3] == 255

    def test_it_is_sized_against_the_video_not_in_pixels(self):
        """The same 96px logo is a discreet mark on a 1080-wide video and a
        sticker over someone's face on a 480-wide one, and Reddit serves both."""
        wide = brand.filtergraph(1080, width_share=0.1)
        narrow = brand.filtergraph(480, width_share=0.1)
        assert "scale=108:108" in wide
        assert "scale=48:48" in narrow

    def test_the_badge_is_square_so_the_circle_stays_round(self):
        graph = brand.filtergraph(1000, width_share=0.13)
        assert "scale=130:130" in graph

    def test_it_never_lands_on_an_odd_number_of_pixels(self):
        """An odd-sized overlay on a yuv420p stream sits on half a chroma
        sample, and ffmpeg rounds it somewhere of its own choosing."""
        for width in (481, 719, 1081, 1237):
            side = int(brand.filtergraph(width).split("scale=")[1].split(":")[0])
            assert side % 2 == 0, f"{width} gave an odd badge of {side}"

    def test_it_goes_in_the_top_left(self):
        graph = brand.filtergraph(1000, width_share=0.1, inset_share=0.04)
        # overlay=x:y, and both are the same positive inset from the origin.
        assert "overlay=40:40" in graph

    def test_the_corner_inset_is_not_flush(self):
        """Every platform draws a handle or a "reposted" chip near the top
        left. A badge tucked into the corner ends up half underneath it."""
        assert settings.brand_inset_share > 0.02


@needs_ffmpeg
class TestNothingElseChanges:
    def test_the_picture_keeps_its_size(self, a_video, tmp_path):
        out = brand.apply(a_video, tmp_path / "out")
        before, after = probe(a_video), probe(out)
        assert (stream(after, "video")["width"], stream(after, "video")["height"]) == \
               (stream(before, "video")["width"], stream(before, "video")["height"])

    def test_it_is_not_shortened(self, a_video, tmp_path):
        out = brand.apply(a_video, tmp_path / "out")
        before = float(probe(a_video)["format"]["duration"])
        after = float(probe(out)["format"]["duration"])
        assert abs(after - before) < 0.35

    def test_the_sound_survives(self, a_video, tmp_path):
        """The failure that cannot be caught by looking: a silent file plays
        perfectly and nobody notices until it is on the page."""
        out = brand.apply(a_video, tmp_path / "out")
        assert stream(probe(out), "audio") is not None

    def test_the_sound_is_copied_rather_than_re_encoded(self, a_video, tmp_path):
        """It was compressed once on Reddit already. A second pass costs
        quality and buys nothing, since the overlay does not touch it."""
        out = brand.apply(a_video, tmp_path / "out")
        assert stream(probe(out), "audio")["codec_name"] == \
               stream(probe(a_video), "audio")["codec_name"]

    def test_a_video_with_no_sound_still_brands(self, tmp_path):
        """A Reddit video can legitimately have no audio track, and without
        the optional map ffmpeg fails the whole render over it."""
        silent = tmp_path / "silent.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", "testsrc=size=480x854:rate=24:duration=3",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent)],
            check=True, capture_output=True,
        )
        out = brand.apply(silent, tmp_path / "out")
        assert out.exists() and stream(probe(out), "video") is not None

    def test_the_corner_actually_changed_and_the_middle_did_not(
            self, a_video, tmp_path):
        """Proof the badge landed where it was meant to, rather than
        somewhere plausible-looking, or nowhere."""
        out = brand.apply(a_video, tmp_path / "out")

        def frame(path, name):
            shot = tmp_path / name
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(path),
                 "-frames:v", "1", str(shot)],
                check=True, capture_output=True,
            )
            return shot

        from PIL import Image, ImageChops

        before = Image.open(frame(a_video, "a.png")).convert("RGB")
        after = Image.open(frame(out, "b.png")).convert("RGB")
        diff = ImageChops.difference(before, after)
        width, height = diff.size

        corner = diff.crop((0, 0, int(width * 0.22), int(width * 0.22)))
        middle = diff.crop((int(width * 0.35), int(height * 0.45),
                            int(width * 0.65), int(height * 0.55)))
        # A re-encode moves every pixel a little, so this is a comparison of
        # two changes rather than a test for zero change.
        corner_change = sum(corner.convert("L").getdata()) / (corner.size[0] * corner.size[1])
        middle_change = sum(middle.convert("L").getdata()) / (middle.size[0] * middle.size[1])
        assert corner_change > 12, f"the badge did not land: corner moved {corner_change:.1f}"
        assert middle_change < corner_change / 4, (
            f"the middle changed nearly as much as the corner "
            f"({middle_change:.1f} vs {corner_change:.1f}) - something is "
            f"re-framing the video, not just branding it")
