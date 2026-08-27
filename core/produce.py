"""Make one video: archive audio, AI narration, stock footage, instruments.

The order of the layers is the whole design. Footage goes underneath and is
graded hard so two clips from different shoots match. The drawn instrument
layer goes on top and never changes, which is what makes a post recognisable.
The audio is the point of the thing, so it is mixed last and loudest: the
archive recording runs continuously and ducks under the narrator rather than
being cut around him.

Nothing here is required to succeed. No stock key means the footage is a drawn
gradient; no narration key means the recording carries it alone; an archive
with no fetchable audio renders over ambience until someone uploads a file.
A render that is missing a layer is worth watching and tells you what to fix.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from core import archives, beds, captions, overlay, pexels, tts
from core.archives import Archive
from core.config import settings
from core.ffmpeg_ops import FFmpegError, require_binaries
from core.storage import get_storage

log = logging.getLogger(__name__)

W, H = 1080, 1920
#: archive.org is generous but not infinite, and a two-hour master is not worth
#: pulling to use forty seconds of it.
MAX_TAPE_BYTES = 320 * 1024 * 1024
AUDIO_FORMAT_PREFERENCE = (".mp3", ".ogg", ".m4a", ".flac", ".wav")


class ProduceError(RuntimeError):
    pass


@dataclass
class Options:
    archive_id: str
    voice_hook: bool = False
    grade: float | None = None
    overlay: float | None = None
    use_stock: bool = True
    #: seconds into the recording to start from; None uses the archive default
    tape_offset_s: float | None = None
    #: a recording supplied by hand, for archives with nothing fetchable
    tape_path: Path | None = None
    fps: int | None = None

    def resolved_grade(self) -> float:
        if self.grade is None:
            return settings.studio_grade
        return max(0.0, min(1.0, self.grade))

    def resolved_overlay(self) -> float:
        if self.overlay is None:
            return settings.studio_overlay
        return max(0.0, min(1.0, self.overlay))

    def resolved_fps(self) -> int:
        return int(self.fps or settings.studio_fps)


@dataclass
class Result:
    archive_id: str
    path: Path
    storage_key: str
    duration_s: float
    layers: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "storage_key": self.storage_key,
            "duration_s": round(self.duration_s, 2),
            "layers": self.layers,
            "warnings": self.warnings,
            "cost_usd": round(self.cost_usd, 4),
            "elapsed_s": round(self.elapsed_s, 1),
        }


# --- the recording ---------------------------------------------------------


def tape_cache() -> Path:
    path = Path(settings.work_dir) / "tape"
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch_archive_audio(archive: Archive) -> Path | None:
    """Pull the recording from archive.org, or return None and say why.

    archive.org's metadata endpoint lists every file in an item, so the exact
    filename never has to be hardcoded - which matters, because those change
    and a 404 six months from now would be indistinguishable from a bug.
    """
    if not archive.archive_item:
        return None

    import httpx

    item = archive.archive_item
    try:
        meta = httpx.get(f"https://archive.org/metadata/{item}", timeout=30.0).json()
    except Exception as exc:  # noqa: BLE001 - a missing recording is not fatal
        log.warning("tape: metadata for %s failed: %s", item, exc)
        return None

    candidates: list[tuple[int, str]] = []
    for entry in meta.get("files", []) or []:
        name = str(entry.get("name", ""))
        suffix = Path(name).suffix.lower()
        if suffix not in AUDIO_FORMAT_PREFERENCE:
            continue
        try:
            size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size and size > MAX_TAPE_BYTES:
            continue
        candidates.append((AUDIO_FORMAT_PREFERENCE.index(suffix), name))

    if not candidates:
        log.warning("tape: no usable audio file in archive.org item %s", item)
        return None

    candidates.sort()
    name = candidates[0][1]
    dest = tape_cache() / f"{item}-{Path(name).name}"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    url = f"https://archive.org/download/{item}/{quote(name)}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as response:
            response.raise_for_status()
            with tmp.open("wb") as handle:
                for chunk in response.iter_bytes(1 << 16):
                    handle.write(chunk)
    except Exception as exc:  # noqa: BLE001
        log.warning("tape: download of %s failed: %s", url, exc)
        tmp.unlink(missing_ok=True)
        return None

    tmp.replace(dest)
    log.info("tape: cached %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
    return dest


# --- captions --------------------------------------------------------------


def _ass_colour(hex_colour: str) -> str:
    """#RRGGBB -> ASS &H00BBGGRR."""
    text = hex_colour.lstrip("#")
    return f"&H00{text[4:6]}{text[2:4]}{text[0:2]}".upper()


def write_captions(archive: Archive, path: Path, *, voice_hook: bool) -> Path:
    style = captions.CaptionStyle(
        highlight_colour=_ass_colour(archive.accent),
        font_size=78,
        max_words=6,
        max_chars=30,
    )
    return captions.write_ass(archives.caption_words(archive, voice_hook), path, style)


# --- the composite ---------------------------------------------------------


def _esc(path: Path | str) -> str:
    text = str(path).replace("\\", "/")
    return text.replace(":", r"\:").replace("'", r"\'")


def _plate_source(archive: Archive, duration: float, fps: int) -> list[str]:
    """A drawn gradient to stand in for footage when there is no stock clip."""
    accent = archive.accent.lstrip("#")
    return [
        "-f",
        "lavfi",
        "-i",
        f"gradients=s={W}x{H}:c0=0x1C2630:c1=0x{accent}:c2=0x0A0F14:nb_colors=3"
        f":x0=140:y0=260:x1=940:y1=1660:speed=0.012:d={duration:.2f}:r={fps}",
    ]


def build_command(
    *,
    archive: Archive,
    duration: float,
    fps: int,
    grade: float,
    stock: Path | None,
    frames_glob: str,
    static_png: Path,
    ass_path: Path,
    bed_wav: Path,
    tape: tuple[Path, float] | None,
    narration: list[tuple[Path, float]],
    dest: Path,
    duck_windows: list[tuple[float, float]],
    crf: int,
) -> list[str]:
    """Assemble the ffmpeg invocation.

    Split out from `produce` so a test can read the filter graph without
    needing a stock clip, an API key or forty seconds of encoding.
    """
    cmd: list[str] = ["ffmpeg", "-y", "-loglevel", "error"]

    if stock is not None:
        # Loop the clip: stock is often shorter than the video, and a frozen
        # last frame reads as a bug where a loop reads as a bed.
        cmd += ["-stream_loop", "-1", "-i", str(stock)]
    else:
        cmd += _plate_source(archive, duration, fps)

    cmd += ["-framerate", str(fps), "-i", frames_glob]
    cmd += ["-i", str(static_png)]

    audio_index = 3
    bed_i = audio_index
    cmd += ["-i", str(bed_wav)]
    audio_index += 1

    tape_i = None
    if tape is not None:
        tape_path, _offset = tape
        tape_i = audio_index
        cmd += ["-i", str(tape_path)]
        audio_index += 1

    narration_indices: list[tuple[int, float]] = []
    for path, at in narration:
        narration_indices.append((audio_index, at))
        cmd += ["-i", str(path)]
        audio_index += 1

    # The grade exists to drag arbitrary footage into this source's world. The
    # drawn plate is already in it, so grading that at full strength only
    # subtracts light from something that had little to spare.
    effective = grade if stock is not None else grade * 0.35
    grade_chain = archive.grade.ffmpeg_filter(effective)
    tint = archive.grade.tint.lstrip("#")
    tint_alpha = archive.grade.tint_alpha * effective

    steps: list[str] = [
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},fps={fps},setsar=1,{grade_chain}[base]",
        f"color=c=0x{tint}:s={W}x{H}:r={fps}:d={duration:.2f},format=rgba,"
        f"colorchannelmixer=aa={tint_alpha:.3f}[tintsrc]",
        "[base][tintsrc]overlay=0:0:shortest=1[tinted]",
        "[tinted][1:v]overlay=0:0:shortest=1[withframes]",
        "[withframes][2:v]overlay=0:0[withstatic]",
        # Grain last, so it sits over the instruments too and the whole frame
        # reads as one pass of film rather than a sticker on a video.
        f"[withstatic]noise=alls=7:allf=t+u,vignette=PI/5,ass='{_esc(ass_path)}'[v]",
    ]

    mix: list[str] = []
    steps.append(f"[{bed_i}:a]volume=0.9[bed]")
    mix.append("[bed]")

    if tape_i is not None:
        _path, offset = tape
        chain = (
            f"[{tape_i}:a]atrim=start={offset:.3f}:duration={duration:.3f},"
            "asetpts=PTS-STARTPTS,aresample=48000,volume=1.0"
        )
        # Duck under the narrator rather than cutting: the recording running
        # continuously underneath is most of why the format feels live.
        for start, end in duck_windows:
            chain += f",volume=enable='between(t,{start:.2f},{end:.2f})':volume=0.20"
        steps.append(chain + "[tape]")
        mix.append("[tape]")

    for n, (index, at) in enumerate(narration_indices):
        delay_ms = int(round(at * 1000))
        steps.append(
            f"[{index}:a]aresample=48000,adelay={delay_ms}|{delay_ms},volume=1.35[nar{n}]"
        )
        mix.append(f"[nar{n}]")

    steps.append(
        "".join(mix) + f"amix=inputs={len(mix)}:normalize=0:duration=longest,"
        f"alimiter=limit=0.94,atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[a]"
    )

    cmd += [
        "-filter_complex",
        ";".join(steps),
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    return cmd


# --- top level -------------------------------------------------------------


def produce(options: Options, *, work_dir: Path | None = None, keep: bool = False) -> Result:
    """Render one video and put it in storage."""
    require_binaries()
    started = time.time()

    archive = archives.get(options.archive_id)
    fps = options.resolved_fps()
    duration = archive.duration_s(options.voice_hook)
    timeline = archive.timeline(options.voice_hook)
    warnings: list[str] = []

    root = Path(work_dir or tempfile.mkdtemp(prefix="studio-", dir=str(settings.work_dir)))
    root.mkdir(parents=True, exist_ok=True)

    try:
        # 1. narration ------------------------------------------------------
        narration: list[tuple[Path, float]] = []
        narrated_seconds = 0.0
        duck_windows: list[tuple[float, float]] = []
        if tts.available():
            for start, end, beat in timeline:
                if not (beat.narrated and beat.text):
                    continue
                try:
                    narration.append((tts.speak(beat.text), start + 0.15))
                    narrated_seconds += end - start
                    duck_windows.append((start, end))
                except Exception as exc:  # noqa: BLE001 - a line is not the video
                    warnings.append(f"narration failed for {beat.kind!r}: {exc}")
        else:
            warnings.append("OPENAI_API_KEY is not set - rendering without narration")

        # 2. the recording --------------------------------------------------
        tape: tuple[Path, float] | None = None
        tape_path = options.tape_path or fetch_archive_audio(archive)
        if tape_path is not None:
            if options.tape_offset_s is None:
                offset = archive.tape_offset_s
            else:
                offset = options.tape_offset_s
            tape = (Path(tape_path), max(0.0, float(offset)))
        elif archive.fetchable:
            warnings.append("the recording could not be fetched - rendering over ambience")
        else:
            warnings.append(
                f"{archive.name} has no fetchable recording - upload one to hear the tape"
            )

        # 3. footage --------------------------------------------------------
        stock: Path | None = None
        if options.use_stock and settings.has_stock:
            clips = pexels.fetch_for(archive.stock_terms, want=1, min_s=4.0)
            stock = clips[0] if clips else None
            if stock is None:
                warnings.append("no stock clip came back - using the drawn plate")
        elif options.use_stock:
            warnings.append("PEXELS_API_KEY is not set - using the drawn plate")

        # 4. the drawn layers -----------------------------------------------
        frames_dir = root / "frames"
        overlay.render_frames(
            archive,
            frames_dir,
            voice_hook=options.voice_hook,
            fps=fps,
            alpha=options.resolved_overlay(),
        )
        static_png = overlay.render_static(
            archive, root / "static.png", alpha=options.resolved_overlay()
        )
        ass_path = write_captions(archive, root / "captions.ass", voice_hook=options.voice_hook)
        bed_wav = beds.synth(archive.bed, duration, root / "bed.wav")

        # 5. composite ------------------------------------------------------
        dest = root / f"{archive.id}.mp4"
        cmd = build_command(
            archive=archive,
            duration=duration,
            fps=fps,
            grade=options.resolved_grade(),
            stock=stock,
            frames_glob=str(frames_dir / "frame-%05d.png"),
            static_png=static_png,
            ass_path=ass_path,
            bed_wav=bed_wav,
            tape=tape,
            narration=narration,
            dest=dest,
            duck_windows=duck_windows,
            crf=settings.studio_crf,
        )
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace")[-2500:]
            raise FFmpegError(f"composite failed ({proc.returncode}):\n{tail}")

        # 6. store ----------------------------------------------------------
        key = f"studio/{archive.id}-{int(started)}.mp4"
        get_storage().put_file(dest, key)

        return Result(
            archive_id=archive.id,
            path=dest,
            storage_key=key,
            duration_s=duration,
            layers={
                "narration_lines": len(narration),
                "tape": Path(tape[0]).name if tape else None,
                "tape_offset_s": tape[1] if tape else None,
                "stock": stock.name if stock else None,
                "bed": archive.bed,
                "voice_hook": options.voice_hook,
                "grade": options.resolved_grade(),
                "overlay": options.resolved_overlay(),
                "fps": fps,
            },
            warnings=warnings,
            cost_usd=tts.estimate_cost(narrated_seconds),
            elapsed_s=time.time() - started,
        )
    finally:
        if not keep and work_dir is None:
            # The mp4 has already gone to storage; the frames have not and are
            # by far the biggest thing on disk.
            shutil.rmtree(root / "frames", ignore_errors=True)


def preflight(archive_id: str) -> dict[str, Any]:
    """What a render of this archive would and would not include."""
    archive = archives.get(archive_id)
    ready = archives.readiness(archive, has_tts=settings.has_tts, has_stock=settings.has_stock)
    narrated = sum(b.seconds for b in archive.running_order() if b.narrated and b.text)
    return {
        "archive": archive.as_dict(),
        "readiness": ready.as_dict(),
        "estimated_cost_usd": tts.estimate_cost(narrated),
    }
