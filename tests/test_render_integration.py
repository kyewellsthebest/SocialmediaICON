"""End-to-end render against a synthetic source. Skipped when ffmpeg is absent."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from core.ffmpeg_ops import TARGET_H, TARGET_W, extract_audio, probe
from core.selection import Candidate
from worker.tasks.render import render_candidate

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


@pytest.fixture(scope="module")
def source_video(tmp_path_factory):
    """A 20s 1920x1080 clip with motion on one side and a tone."""
    path = tmp_path_factory.mktemp("src") / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30:duration=20",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _words(count: int, start: float, step: float) -> list[dict]:
    return [
        {"w": f"word{i}", "start": start + i * step, "end": start + (i + 1) * step - 0.05}
        for i in range(count)
    ]


def test_probe_reads_dimensions_and_audio(source_video):
    info = probe(source_video)
    assert (info.width, info.height) == (1920, 1080)
    assert info.has_audio is True
    assert info.duration_s == pytest.approx(20, abs=0.5)


def test_extract_audio_produces_a_playable_track(source_video, tmp_path):
    audio = extract_audio(source_video, tmp_path / "audio.m4a")
    assert audio.exists() and audio.stat().st_size > 0


def test_render_produces_a_vertical_captioned_clip(source_video, tmp_path):
    words = _words(16, start=4.0, step=0.5)
    candidate = Candidate(start_s=4.0, end_s=12.0, hook_score=8, payoff_score=8)

    rendered = render_candidate(
        source_video,
        words,
        candidate,
        tmp_path / "clip.mp4",
        tmp_path / "work",
        with_metadata=False,  # no API call in tests
    )

    assert rendered.path.exists()
    info = probe(rendered.path)
    assert (info.width, info.height) == (TARGET_W, TARGET_H)
    assert info.duration_s == pytest.approx(8, abs=0.6)
    assert info.has_audio is True
    assert rendered.transcript.startswith("word0")
    # the .ass built for this clip is rebased to the cut, not the source
    ass = (tmp_path / "work" / "clip.ass").read_text()
    assert "Dialogue: 0,0:00:00.00" in ass
