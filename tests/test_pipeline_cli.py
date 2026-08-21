"""The Phase 1 CLI end to end, with the model calls stubbed.

Covers the glue that the unit tests cannot: local file input, cached
transcript, detect -> rank -> render, and what lands in ./out.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core import llm
from core.ffmpeg_ops import probe

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


@pytest.fixture
def fake_model(monkeypatch):
    """Answer each prompt type with canned JSON instead of calling the API."""

    def stub(system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict:
        if system is llm.DETECT_SYSTEM:
            return {
                "candidates": [
                    {
                        "start_s": 2.0,
                        "end_s": 20.0,
                        "hook_score": 9,
                        "emotion": "surprise",
                        "payoff_score": 8,
                        "context_ok": True,
                        "novelty": 7,
                        "one_line_reason": "strong open",
                    },
                    {
                        "start_s": 30.0,
                        "end_s": 50.0,
                        "hook_score": 4,
                        "emotion": "none",
                        "payoff_score": 3,
                        "context_ok": True,
                        "novelty": 2,
                        "one_line_reason": "filler",
                    },
                ]
            }
        if system is llm.RANK_SYSTEM:
            count = user.count("--- CANDIDATE id=")
            return {
                "rankings": [
                    {
                        "id": i,
                        "predicted_score": 90 - i * 30,
                        "rationale": {"hook": "h", "payoff": "p", "risk": "r"},
                    }
                    for i in range(count)
                ]
            }
        return {"title": "A Title", "caption": "A caption", "hashtags": ["shorts", "#test"]}

    monkeypatch.setattr(llm, "json_message", stub)


@pytest.fixture
def source_and_transcript(tmp_path):
    video = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=24:duration=60",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=60",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video),
        ],
        check=True,
        capture_output=True,
    )
    words = [
        {"w": f"word{i}", "start": round(i * 0.5, 2), "end": round(i * 0.5 + 0.45, 2)}
        for i in range(120)
    ]
    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps({"words": words, "full_text": "x", "provider": "test"}))
    return video, transcript


def test_cli_renders_clips_and_writes_metadata(
    fake_model, source_and_transcript, tmp_path, monkeypatch
):
    import run_pipeline

    video, transcript = source_and_transcript
    out_dir = tmp_path / "out"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(run_pipeline.settings, "anthropic_api_key", "test-key")

    exit_code = run_pipeline.main(
        [
            "--file", str(video),
            "--transcript", str(transcript),
            "--license", "own",
            "--top-n", "1",
            "--out", str(out_dir),
            "--work", str(tmp_path / "work"),
        ]
    )
    assert exit_code == 0

    clips = json.loads((out_dir / "clips.json").read_text())
    assert len(clips) == 1
    clip = clips[0]
    # the higher-scored candidate wins
    assert clip["start_s"] == pytest.approx(2.0, abs=0.6)
    assert clip["predicted_score"] == 90
    assert clip["hashtags"] == ["#shorts", "#test"]  # missing '#' is repaired

    rendered = Path(clip["path"])
    info = probe(rendered)
    assert (info.width, info.height) == (1080, 1920)
    assert info.duration_s == pytest.approx(clip["duration_s"], abs=0.6)

    sidecar = rendered.with_suffix(".txt")
    assert "A Title" in sidecar.read_text()
    assert "strong open" in sidecar.read_text()
