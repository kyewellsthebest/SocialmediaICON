"""Source registration and status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.config import settings as env_settings
from core.db import get_db
from core.models import LICENSES, SOURCE_KINDS, Niche, Source
from worker.queue import enqueue
from worker.tasks.ingest import adopt_upload, check_license
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


def _niche_id(db: Session, name: str | None) -> int | None:
    """Find or create a niche by name."""
    if not name:
        return None
    niche = db.query(Niche).filter(Niche.name == name).one_or_none()
    if niche is None:
        niche = Niche(name=name, config={})
        db.add(niche)
        db.flush()
    return niche.id


# Anything ffmpeg can open. Kept permissive: the probe is the real check.
UPLOAD_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".mpg", ".mpeg", ".ts"}


@router.post("/upload", response_model=SourceOut, status_code=201)
async def upload_source(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    niche: str | None = Form(default=None),
) -> Any:
    """Put a video file straight into the pipeline.

    The download step is the only part of this system that depends on a
    platform choosing to allow it. This route removes that dependency: you
    fetch the file however you like - saved from a browser, sent by a creator -
    and everything downstream is unchanged.

    No database session is taken until the file looks usable, so a wrong file
    is refused even when nothing else is configured.
    """
    import shutil
    import tempfile
    from pathlib import Path

    from core.db import session_scope

    name = Path(file.filename or "upload.mp4").name
    if Path(name).suffix.lower() not in UPLOAD_SUFFIXES:
        raise HTTPException(
            422, f"{Path(name).suffix or 'that file type'} is not a video ffmpeg can open"
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="upload-"))
    dest = tmp_dir / name
    try:
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out, length=1024 * 1024)
        if dest.stat().st_size == 0:
            raise HTTPException(422, "that file is empty")

        with session_scope() as session:
            source = Source(
                url=f"upload://{name}",
                kind="upload",
                license="none",
                niche_id=_niche_id(session, niche),
                status="downloading",
                title=title or Path(name).stem,
            )
            session.add(source)
            session.flush()
            source_id = source.id

        try:
            adopt_upload(source_id, dest, title=title or Path(name).stem)
        except Exception as exc:
            with session_scope() as session:
                failed = session.get(Source, source_id)
                if failed is not None:
                    failed.status = "failed"
                    failed.error = str(exc)[:2000]
            raise HTTPException(400, f"could not read that file: {exc}") from exc

        with session_scope() as session:
            source = session.get(Source, source_id)
            return SourceOut.model_validate(source)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.post("", response_model=SourceOut, status_code=201)
def create_source(payload: SourceIn, db: Session = Depends(get_db)) -> Any:
    if payload.kind not in SOURCE_KINDS:
        raise HTTPException(422, f"kind must be one of {SOURCE_KINDS}")
    license_tag = check_license(payload.license)

    niche_id = _niche_id(db, payload.niche)

    source = Source(url=payload.url, kind=payload.kind, license=license_tag, niche_id=niche_id)
    db.add(source)
    db.flush()
    if not env_settings.ingest_by_agent:
        enqueue("ingest", ingest_run, source.id)
    return source


@router.get("", response_model=list[SourceOut])
def list_sources(limit: int = 50, status: str | None = None, db: Session = Depends(get_db)) -> Any:
    """Recent sources, newest first. `status` filters - the local agent uses it
    to find work without reading everything."""
    query = db.query(Source)
    if status:
        query = query.filter(Source.status == status)
    return query.order_by(Source.id.desc()).limit(limit).all()


@router.post("/{source_id}/file", response_model=SourceOut)
async def attach_file(source_id: int, file: UploadFile = File(...)) -> Any:
    """Supply the video for a source that is waiting for one.

    This is how the local agent hands back what it downloaded: the source was
    registered here, the bytes were fetched somewhere with a residential IP,
    and the pipeline continues as though the download had happened in place.
    """
    import shutil
    import tempfile
    from pathlib import Path

    from core.db import session_scope

    name = Path(file.filename or "upload.mp4").name
    if Path(name).suffix.lower() not in UPLOAD_SUFFIXES:
        raise HTTPException(422, f"{Path(name).suffix or 'that file type'} is not a video")

    with session_scope() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise HTTPException(404, "source not found")
        if source.storage_key:
            raise HTTPException(409, "that source already has a video")
        title = source.title

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"agent-{source_id}-"))
    dest = tmp_dir / name
    try:
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out, length=1024 * 1024)
        if dest.stat().st_size == 0:
            raise HTTPException(422, "that file is empty")
        adopt_upload(source_id, dest, title=title)
    except HTTPException:
        raise
    except Exception as exc:
        with session_scope() as session:
            failed = session.get(Source, source_id)
            if failed is not None:
                failed.status = "failed"
                failed.error = str(exc)[:2000]
        raise HTTPException(400, f"could not read that file: {exc}") from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    with session_scope() as session:
        return SourceOut.model_validate(session.get(Source, source_id))


@router.get("/{source_id}", response_model=SourceOut)
def get_source(source_id: int, db: Session = Depends(get_db)) -> Any:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "source not found")
    return source
