"""ffmpeg / ffprobe wrappers: probe, audio extract, smart center crop, render.

Reframing for Phase 1 is a *smart* center crop: sample a handful of frames from
the segment, find where the visual activity is horizontally, and place the
1080-wide window there instead of blindly at the middle. Face / active-speaker
tracking is a later upgrade and explicitly must not block Phase 1.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TARGET_W = 1080
TARGET_H = 1920


class FFmpegError(RuntimeError):
    pass


def require_binaries() -> None:
    missing = [b for b in ("ffmpeg", "ffprobe") if shutil.which(b) is None]
    if missing:
        raise FFmpegError(
            f"missing required binary/binaries: {', '.join(missing)}. "
            "Install ffmpeg (macOS: brew install ffmpeg, Debian: apt install ffmpeg)."
        )


def run(cmd: list[str], capture: bool = False) -> bytes:
    log.debug("running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-2000:]
        raise FFmpegError(f"{cmd[0]} failed ({proc.returncode}):\n{tail}")
    return proc.stdout if capture else b""


@dataclass
class VideoInfo:
    width: int
    height: int
    duration_s: float
    has_audio: bool


def probe(path: Path | str) -> VideoInfo:
    require_binaries()
    raw = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        capture=True,
    )
    data = json.loads(raw or b"{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFmpegError(f"no video stream in {path}")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0.0)
    return VideoInfo(
        width=int(video["width"]),
        height=int(video["height"]),
        duration_s=duration,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def extract_audio(src: Path | str, dest: Path | str) -> Path:
    """Mono 16 kHz m4a — small enough to upload to a transcription API quickly."""
    require_binaries()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "aac", "-b:a", "64k",
            str(dest),
        ]
    )
    return dest


# --- smart center crop ----------------------------------------------------


def sample_gray_frames(
    src: Path | str, start_s: float, end_s: float, cols: int = 64, rows: int = 36, count: int = 9
) -> list[bytes]:
    """Grab `count` tiny greyscale frames spread across the segment."""
    require_binaries()
    duration = max(0.1, end_s - start_s)
    fps = max(count / duration, 0.05)
    raw = run(
        [
            "ffmpeg", "-loglevel", "error",
            "-ss", f"{start_s:.3f}", "-t", f"{duration:.3f}",
            "-i", str(src),
            "-vf", f"fps={fps},scale={cols}:{rows},format=gray",
            "-frames:v", str(count),
            "-f", "rawvideo", "-",
        ],
        capture=True,
    )
    size = cols * rows
    return [raw[i : i + size] for i in range(0, len(raw) - size + 1, size)]


def focus_x_ratio(frames: list[bytes], cols: int = 64, rows: int = 36) -> float:
    """Where the action is, horizontally, as a 0..1 ratio of frame width.

    Energy per column = temporal change between frames (people moving, cuts) +
    horizontal contrast (edges: faces, text, subjects). The result is pulled
    back towards the centre so a single bright edge cannot shove the crop into
    a corner.
    """
    frames = [f for f in frames if len(f) >= cols * rows]
    if not frames:
        return 0.5

    energy = [0.0] * cols
    for f_idx, frame in enumerate(frames):
        prev = frames[f_idx - 1] if f_idx else None
        for r in range(rows):
            row_off = r * cols
            for c in range(cols):
                pixel = frame[row_off + c]
                if c:
                    energy[c] += abs(pixel - frame[row_off + c - 1])
                if prev is not None:
                    energy[c] += 2.0 * abs(pixel - prev[row_off + c])

    floor = sorted(energy)[len(energy) // 2]  # median as a noise floor
    weights = [max(0.0, e - floor) ** 2 for e in energy]
    total = sum(weights)
    if total <= 0:
        return 0.5

    centroid = sum(w * (c + 0.5) / cols for c, w in enumerate(weights)) / total
    blended = 0.5 + (centroid - 0.5) * 0.75
    return min(0.85, max(0.15, blended))


@dataclass
class CropPlan:
    scale_w: int
    scale_h: int
    crop_x: int
    crop_y: int
    target_w: int = TARGET_W
    target_h: int = TARGET_H

    @property
    def filter_str(self) -> str:
        return (
            f"scale={self.scale_w}:{self.scale_h}:flags=lanczos,"
            f"crop={self.target_w}:{self.target_h}:{self.crop_x}:{self.crop_y}"
        )


def _even(value: float) -> int:
    return int(round(value / 2) * 2)


def compute_crop(
    src_w: int,
    src_h: int,
    focus_ratio: float = 0.5,
    target_w: int = TARGET_W,
    target_h: int = TARGET_H,
) -> CropPlan:
    """Scale to cover 1080x1920, then crop a window centred on the focus point.

    Works for landscape, square and already-vertical sources: the scale factor
    is whichever of the two dimensions needs the most enlargement.
    """
    if src_w <= 0 or src_h <= 0:
        raise ValueError("source dimensions must be positive")

    factor = max(target_w / src_w, target_h / src_h)
    scale_w = max(target_w, _even(src_w * factor))
    scale_h = max(target_h, _even(src_h * factor))

    focus_ratio = min(1.0, max(0.0, focus_ratio))
    crop_x = _even(focus_ratio * scale_w - target_w / 2)
    crop_x = max(0, min(scale_w - target_w, crop_x))
    # Vertically, bias slightly above centre — heads sit in the upper half.
    crop_y = _even((scale_h - target_h) * 0.4)
    crop_y = max(0, min(scale_h - target_h, crop_y))
    return CropPlan(scale_w=scale_w, scale_h=scale_h, crop_x=crop_x, crop_y=crop_y)


def escape_filter_path(path: Path | str) -> str:
    """Escape a path for use inside an ffmpeg filter argument."""
    text = str(path).replace("\\", "/")
    return text.replace(":", r"\:").replace("'", r"\'")


def render_clip(
    src: Path | str,
    dest: Path | str,
    start_s: float,
    end_s: float,
    ass_path: Path | str | None = None,
    focus_ratio: float | None = None,
    crf: int = 20,
    fps: int = 30,
) -> Path:
    """Cut [start, end], reframe to 1080x1920, burn captions, encode H.264/AAC."""
    require_binaries()
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    info = probe(src)
    if focus_ratio is None:
        try:
            focus_ratio = focus_x_ratio(sample_gray_frames(src, start_s, end_s))
        except FFmpegError as exc:
            log.warning("focus sampling failed, falling back to centre crop: %s", exc)
            focus_ratio = 0.5

    plan = compute_crop(info.width, info.height, focus_ratio)
    filters = [plan.filter_str, "setsar=1", f"fps={fps}"]
    if ass_path is not None:
        filters.append(f"ass='{escape_filter_path(ass_path)}'")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        # -ss before -i seeks fast; -accurate_seek keeps the cut frame-correct.
        "-accurate_seek", "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}",
        "-i", str(src),
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-profile:v", "high",
    ]
    if info.has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(dest)]

    run(cmd)
    return dest
