"""Watch a stream live, and already have the moment before chat asks for it.

Everything here follows from one fact: **by the time chat reacts, the moment
is over.** Someone falls off a roof at 14:02:31 and the first KEKW lands at
14:02:33. A bot that starts recording when it sees the reaction has missed
the thing it wanted.

So you cannot record on the trigger. You have to be holding the recent past
already - which sounds like it means recording everything, and does not.

**The rolling buffer.** ffmpeg writes the stream to short segments and deletes
any segment older than the window. Disk use is bounded by the window, not by
how long the stream runs: a five minute buffer of 720p is about 130MB whether
the stream lasts an hour or fourteen. Ten streams cost 1.3GB, constant, all
day. Recording those same ten streams for eight hours would be 125GB - and
would find the same clips.

When chat spikes, the last five minutes are already on disk. Cut backwards
from the trigger, write the clip, and let the segments expire on their own.
Nothing else is ever stored: what survives is the clip, a row of text, and
the URL it came from.

This is the same trick as OBS's replay buffer, and as an aircraft's flight
recorder. Neither of them records the whole flight.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.ffmpeg_ops import FFmpegError, require_binaries

log = logging.getLogger(__name__)

#: Segment length. Shorter means finer clip boundaries and more files; four
#: seconds is what most live encoders emit anyway, so it costs nothing.
SEGMENT_S = 4.0
#: How much past to keep. Long enough to cut a clip that starts well before
#: whatever chat reacted to, short enough that disk stays trivial.
WINDOW_S = 300.0
#: How long before a moment the clip should start. Reactions are to something
#: that has already happened, and a clip that opens on the punchline is a clip
#: nobody understands.
LEAD_S = 22.0
TRAIL_S = 8.0


class LiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class Segment:
    path: Path
    duration_s: float
    #: seconds from the start of the currently-held buffer
    offset_s: float


@dataclass
class RollingBuffer:
    """A bounded window of the recent past, on disk, continuously overwritten.

    Not a recorder. It never grows: ffmpeg is told to delete segments as they
    age out, so the directory holds the same number of files an hour in as it
    did at the start.
    """

    url: str
    work_dir: Path
    window_s: float = WINDOW_S
    segment_s: float = SEGMENT_S
    #: A stream that names itself, so a clip can carry its source forward
    channel: str = ""
    _process: subprocess.Popen | None = field(default=None, repr=False)
    _started_at: float = 0.0

    @property
    def playlist(self) -> Path:
        return self.work_dir / "buffer.m3u8"

    @property
    def segment_count(self) -> int:
        return max(4, int(self.window_s / self.segment_s))

    def command(self) -> list[str]:
        """The ffmpeg call. Separated out so it can be read and tested."""
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            # A live source that stalls must not wedge the watcher forever.
            "-rw_timeout", "15000000",
            "-i", self.url,
            # No re-encode. The buffer is a copy of what arrived, which costs
            # almost no CPU - the point of running ten of these at once.
            "-c", "copy",
            "-f", "hls",
            "-hls_time", f"{self.segment_s:.0f}",
            "-hls_list_size", str(self.segment_count),
            # delete_segments is the whole storage guarantee. Without it this
            # is a recorder that fills the disk.
            "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments",
            "-hls_segment_filename", str(self.work_dir / "seg_%06d.ts"),
            str(self.playlist),
        ]

    def start(self) -> RollingBuffer:
        require_binaries()
        self.work_dir = Path(self.work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        if self._process is not None:
            raise LiveError("this buffer is already running")

        self._process = subprocess.Popen(
            self.command(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            # Its own process group, so stopping the buffer cannot signal the
            # worker that started it.
            start_new_session=True,
        )
        self._started_at = time.time()
        log.info("live: buffering %s (%.0fs window) -> %s", self.url, self.window_s, self.work_dir)
        return self

    def __enter__(self) -> RollingBuffer:
        return self.start() if self._process is None else self

    def __exit__(self, *_: object) -> None:
        self.stop()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def failure(self) -> str:
        """Why ffmpeg stopped, if it has. Empty while it is still running."""
        if self._process is None or self._process.poll() is None:
            return ""
        stderr = b""
        if self._process.stderr is not None:
            try:
                stderr = self._process.stderr.read() or b""
            except ValueError:
                stderr = b""
        return stderr.decode("utf-8", "replace")[-600:] or f"exit {self._process.returncode}"

    def stop(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
        self._process = None

    def discard(self) -> None:
        """Stop and delete every byte. The stream leaves no trace behind."""
        self.stop()
        shutil.rmtree(self.work_dir, ignore_errors=True)

    # --- what the buffer is currently holding -------------------------------

    def segments(self) -> list[Segment]:
        """The segments in the live playlist, oldest first, with offsets."""
        try:
            lines = self.playlist.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

        out: list[Segment] = []
        offset = 0.0
        duration = self.segment_s
        for line in lines:
            if line.startswith("#EXTINF:"):
                try:
                    duration = float(line.split(":", 1)[1].rstrip(","))
                except ValueError:
                    duration = self.segment_s
            elif line and not line.startswith("#"):
                path = self.work_dir / line.strip()
                # A segment named in the playlist but already deleted is normal
                # during a roll; it is not an error, it is just gone.
                if path.exists():
                    out.append(Segment(path=path, duration_s=duration, offset_s=offset))
                    offset += duration
        return out

    def held_s(self) -> float:
        return sum(s.duration_s for s in self.segments())

    def bytes_on_disk(self) -> int:
        return sum(p.stat().st_size for p in self.work_dir.glob("*.ts") if p.exists())

    def status(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "url": self.url,
            "running": self.running,
            "held_s": round(self.held_s(), 1),
            "window_s": self.window_s,
            "segments": len(self.segments()),
            "megabytes": round(self.bytes_on_disk() / 1e6, 1),
            "uptime_s": round(time.time() - self._started_at, 1) if self._started_at else 0.0,
        }

    # --- taking a clip out of it --------------------------------------------

    def extract(
        self,
        dest: Path | str,
        *,
        ago_s: float,
        lead_s: float = LEAD_S,
        trail_s: float = TRAIL_S,
    ) -> Path:
        """Cut a clip ending `ago_s` seconds before the newest frame held.

        `ago_s` is measured back from the live edge because that is the only
        clock a caller actually has: chat reacted a moment ago, and the
        buffer's own start time keeps moving as segments expire.
        """
        segments = self.segments()
        if not segments:
            raise LiveError(
                "the buffer holds nothing yet - "
                f"{'ffmpeg exited: ' + self.failure() if not self.running else 'still filling'}"
            )

        held = sum(s.duration_s for s in segments)
        centre = held - ago_s
        start = max(0.0, centre - lead_s)
        end = min(held, centre + trail_s)
        if end - start < 1.0:
            raise LiveError(
                f"asked for a clip {ago_s:.0f}s back but the buffer only holds {held:.0f}s"
            )

        # Concatenate only the segments the window touches, then trim inside
        # them. Handing ffmpeg the rolling playlist instead would race with
        # deletion: segments can vanish mid-read.
        wanted = [s for s in segments if s.offset_s + s.duration_s > start and s.offset_s < end]
        if not wanted:
            raise LiveError("no segments cover that window")

        listing = self.work_dir / "cut.txt"
        listing.write_text(
            "".join(f"file '{s.path.name}'\n" for s in wanted), encoding="utf-8"
        )
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-ss", f"{start - wanted[0].offset_s:.3f}",
                "-t", f"{end - start:.3f}",
                # Re-encoded, not copied: a stream copy can only cut on a
                # keyframe, which drifts the clip by up to a segment and can
                # open on a grey frame.
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-movflags", "+faststart",
                str(dest),
            ],
            capture_output=True,
        )
        listing.unlink(missing_ok=True)
        if proc.returncode != 0 or not dest.exists():
            raise FFmpegError(proc.stderr.decode("utf-8", "replace")[-600:])

        log.info(
            "live: cut %.1fs from %s (%.0fs back) -> %s",
            end - start, self.channel or self.url, ago_s, dest.name,
        )
        return dest
