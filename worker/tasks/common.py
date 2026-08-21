"""Shared helpers for the DB-backed task entrypoints."""

from __future__ import annotations

from pathlib import Path

from core.config import settings
from core.models import Source
from core.storage import get_storage


def work_dir_for(source_id: int) -> Path:
    path = Path(settings.work_dir) / f"source-{source_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def local_source_path(source: Source) -> Path:
    """Materialise the source video locally, pulling from storage if needed."""
    if not source.storage_key:
        raise ValueError(f"source {source.id} has no storage_key - run ingest first")
    local = work_dir_for(source.id) / Path(source.storage_key).name
    if not local.exists():
        get_storage().get_file(source.storage_key, local)
    return local
