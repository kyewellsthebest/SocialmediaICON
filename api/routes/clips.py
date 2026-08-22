"""Rendered clips."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.config import settings
from core.db import get_db
from core.models import Candidate, Clip
from core.storage import get_storage

router = APIRouter(prefix="/clips", tags=["clips"])


class ClipOut(BaseModel):
    id: int
    candidate_id: int
    title: str | None
    hashtags: list[str]
    duration_s: float | None
    status: str
    storage_key: str | None
    url: str | None = None
    start_s: float | None = None
    end_s: float | None = None
    predicted_score: float | None = None

    model_config = {"from_attributes": True}


def serialise(clip: Clip, db: Session, with_url: bool = True) -> ClipOut:
    candidate = db.get(Candidate, clip.candidate_id)
    out = ClipOut.model_validate(clip)
    if candidate is not None:
        out.start_s = candidate.start_s
        out.end_s = candidate.end_s
        out.predicted_score = candidate.predicted_score
    if with_url and clip.storage_key:
        storage = get_storage()
        # A file:// URI is useless to a browser, so local files are streamed
        # back through the API instead.
        out.url = (
            f"/api/clips/{clip.id}/file"
            if storage.kind == "local"
            else storage.url_for(clip.storage_key)
        )
    return out


@router.get("", response_model=list[ClipOut])
def list_clips(status: str | None = None, limit: int = 50, db: Session = Depends(get_db)) -> Any:
    query = db.query(Clip)
    if status:
        query = query.filter(Clip.status == status)
    clips = query.order_by(Clip.id.desc()).limit(limit).all()
    return [serialise(clip, db) for clip in clips]


@router.get("/{clip_id}", response_model=ClipOut)
def get_clip(clip_id: int, db: Session = Depends(get_db)) -> Any:
    clip = db.get(Clip, clip_id)
    if clip is None:
        raise HTTPException(404, "clip not found")
    return serialise(clip, db)


@router.get("/{clip_id}/file")
def clip_file(clip_id: int, db: Session = Depends(get_db)):
    """Stream the rendered mp4 so the review tab can play it.

    Local storage is served from disk; R2 redirects to a signed URL.
    """
    clip = db.get(Clip, clip_id)
    if clip is None or not clip.storage_key:
        raise HTTPException(404, "clip file not found")

    storage = get_storage()
    if storage.kind != "local":
        return RedirectResponse(storage.url_for(clip.storage_key))

    path = Path(settings.local_storage_dir) / clip.storage_key
    if not path.exists():
        raise HTTPException(404, f"file missing on disk: {clip.storage_key}")
    return FileResponse(path, media_type="video/mp4", filename=path.name)
