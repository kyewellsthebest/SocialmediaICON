"""Source registration and status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.db import get_db
from core.models import LICENSES, SOURCE_KINDS, Niche, Source
from worker.queue import enqueue
from worker.tasks.ingest import check_license
from worker.tasks.ingest import run as ingest_run

router = APIRouter(prefix="/sources", tags=["sources"])


class SourceIn(BaseModel):
    url: str
    # Optional and unenforced; recorded so a source can be traced later.
    license: str = Field(default="none", description=f"one of {LICENSES}")
    kind: str = "youtube"
    niche: str | None = None


class SourceOut(BaseModel):
    id: int
    url: str
    kind: str
    license: str
    title: str | None
    duration_s: float | None
    status: str
    error: str | None

    model_config = {"from_attributes": True}


@router.post("", response_model=SourceOut, status_code=201)
def create_source(payload: SourceIn, db: Session = Depends(get_db)) -> Any:
    if payload.kind not in SOURCE_KINDS:
        raise HTTPException(422, f"kind must be one of {SOURCE_KINDS}")
    license_tag = check_license(payload.license)

    niche_id = None
    if payload.niche:
        niche = db.query(Niche).filter(Niche.name == payload.niche).one_or_none()
        if niche is None:
            niche = Niche(name=payload.niche, config={})
            db.add(niche)
            db.flush()
        niche_id = niche.id

    source = Source(url=payload.url, kind=payload.kind, license=license_tag, niche_id=niche_id)
    db.add(source)
    db.flush()
    enqueue("ingest", ingest_run, source.id)
    return source


@router.get("", response_model=list[SourceOut])
def list_sources(limit: int = 50, db: Session = Depends(get_db)) -> Any:
    return db.query(Source).order_by(Source.id.desc()).limit(limit).all()


@router.get("/{source_id}", response_model=SourceOut)
def get_source(source_id: int, db: Session = Depends(get_db)) -> Any:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "source not found")
    return source
