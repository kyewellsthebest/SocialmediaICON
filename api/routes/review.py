"""Review queue (Phase 2).

Approve marks the clip ready to post. In Phase 2 a human downloads and posts
it; in Phase 3 approval is what feeds the publish queue.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from core.models import Clip

from .clips import ClipOut, serialise

router = APIRouter(prefix="/review", tags=["review"])

REVIEWABLE = ("rendered", "queued")


class MetadataIn(BaseModel):
    title: str | None = None
    hashtags: list[str] | None = None
    caption_style: str | None = None


@router.get("/queue", response_model=list[ClipOut])
def queue(limit: int = 50, db: Session = Depends(get_db)) -> Any:
    clips = (
        db.query(Clip)
        .filter(Clip.status.in_(REVIEWABLE))
        .order_by(Clip.id.desc())
        .limit(limit)
        .all()
    )
    return [serialise(clip, db) for clip in clips]


def _get(clip_id: int, db: Session) -> Clip:
    clip = db.get(Clip, clip_id)
    if clip is None:
        raise HTTPException(404, "clip not found")
    return clip


@router.patch("/{clip_id}", response_model=ClipOut)
def edit_metadata(clip_id: int, payload: MetadataIn, db: Session = Depends(get_db)) -> Any:
    clip = _get(clip_id, db)
    if payload.title is not None:
        clip.title = payload.title
    if payload.hashtags is not None:
        clip.hashtags = payload.hashtags
    if payload.caption_style is not None:
        clip.caption_style = payload.caption_style
    db.flush()
    return serialise(clip, db)


@router.post("/{clip_id}/approve", response_model=ClipOut)
def approve(clip_id: int, db: Session = Depends(get_db)) -> Any:
    clip = _get(clip_id, db)
    clip.status = "approved"
    db.flush()
    return serialise(clip, db)


@router.post("/{clip_id}/reject", response_model=ClipOut)
def reject(clip_id: int, db: Session = Depends(get_db)) -> Any:
    clip = _get(clip_id, db)
    clip.status = "rejected"
    db.flush()
    return serialise(clip, db)
