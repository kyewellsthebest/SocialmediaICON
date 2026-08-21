"""Stage 2 - audio -> transcript with word-level timestamps."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.db import session_scope
from core.ffmpeg_ops import extract_audio
from core.models import Source, Transcript
from core.transcription import transcribe
from worker.queue import enqueue
from worker.tasks.common import local_source_path, work_dir_for

log = logging.getLogger(__name__)


def transcribe_file(video_path: Path | str, work_dir: Path | str) -> dict[str, Any]:
    """Extract audio and transcribe it. Returns the transcript payload."""
    video_path = Path(video_path)
    audio_path = Path(work_dir) / f"{video_path.stem}.m4a"
    if not audio_path.exists():
        extract_audio(video_path, audio_path)
    result = transcribe(audio_path)
    log.info("transcribed %s: %d words", video_path.name, len(result["words"]))
    return result


def run(source_id: int) -> int:
    from worker.tasks.detect_moments import run as detect_run

    with session_scope() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise ValueError(f"no source {source_id}")
        source.status = "transcribing"
        video_path = local_source_path(source)

    try:
        result = transcribe_file(video_path, work_dir_for(source_id))
    except Exception as exc:
        with session_scope() as session:
            source = session.get(Source, source_id)
            if source is not None:
                source.status = "failed"
                source.error = str(exc)[:2000]
        raise

    with session_scope() as session:
        existing = (
            session.query(Transcript).filter(Transcript.source_id == source_id).one_or_none()
        )
        if existing is None:
            session.add(
                Transcript(
                    source_id=source_id,
                    words=result["words"],
                    full_text=result["full_text"],
                    provider=result["provider"],
                )
            )
        else:
            existing.words = result["words"]
            existing.full_text = result["full_text"]
            existing.provider = result["provider"]
        session.get(Source, source_id).status = "transcribed"

    enqueue("detect", detect_run, source_id)
    return source_id
