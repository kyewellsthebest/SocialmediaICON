"""Stage 1 - download a source and register it.

The license check is the point of this stage as much as the download is: in
prod an untagged source never enters the pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from core import ytdlp
from core.config import settings
from core.db import session_scope
from core.models import LICENSES, Source
from core.storage import get_storage
from worker.queue import enqueue

log = logging.getLogger(__name__)


class LicenseError(RuntimeError):
    pass


@dataclass
class Download:
    path: Path
    title: str
    duration_s: float
    url: str


def check_license(license_tag: str, env: str | None = None) -> str:
    """`license=none` is refused in prod. This is the guardrail the whole
    schema is shaped around - reposting someone else's stream is the thing
    that kills the operation."""
    tag = (license_tag or "").strip().lower()
    if tag not in LICENSES:
        raise LicenseError(f"unknown license {license_tag!r}; expected one of {LICENSES}")
    is_prod = settings.is_prod if env is None else env.lower() in {"prod", "production"}
    if tag == "none" and is_prod:
        raise LicenseError(
            "refusing to ingest a source with license=none in prod. Tag the source as "
            "own / licensed / campaign / permitted, or run with ENV=dev for testing."
        )
    return tag


def download_source(url: str, dest_dir: Path | str, max_height: int = 1080) -> Download:
    """yt-dlp pull, best quality at or below `max_height`."""
    import yt_dlp

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    options = ytdlp.base_options(
        format=f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b",
        merge_output_format="mp4",
        outtmpl=str(dest_dir / "%(id)s.%(ext)s"),
    )

    def attempt(opts: dict) -> tuple[dict, Path]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return info, Path(ydl.prepare_filename(info))

    info, path = ytdlp.run(attempt, options)

    if not path.exists():
        # yt-dlp rewrote the container during merge (e.g. .webm -> .mp4).
        matches = sorted(dest_dir.glob(f"{info['id']}.*"))
        if not matches:
            raise FileNotFoundError(f"yt-dlp reported success but no file for {url}")
        path = matches[0]

    return Download(
        path=path,
        title=info.get("title") or path.stem,
        duration_s=float(info.get("duration") or 0.0),
        url=info.get("webpage_url") or url,
    )


def run(source_id: int) -> int:
    """RQ entrypoint: download the source, store it, enqueue transcription."""
    from worker.tasks.transcribe import run as transcribe_run

    with session_scope() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise ValueError(f"no source {source_id}")
        check_license(source.license)
        source.status = "downloading"
        url = source.url

    try:
        work_dir = Path(settings.work_dir) / f"source-{source_id}"
        download = download_source(url, work_dir)
        key = f"sources/{source_id}/{download.path.name}"
        get_storage().put_file(download.path, key)
    except Exception as exc:
        with session_scope() as session:
            source = session.get(Source, source_id)
            if source is not None:
                source.status = "failed"
                source.error = str(exc)[:2000]
        raise

    with session_scope() as session:
        source = session.get(Source, source_id)
        source.title = download.title
        source.duration_s = download.duration_s
        source.storage_key = key
        source.status = "downloaded"

    log.info("ingested source %s (%s)", source_id, download.title)
    enqueue("transcribe", transcribe_run, source_id)
    return source_id
