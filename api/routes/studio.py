"""The studio: make a video, watch it, keep it or bin it.

Deliberately manual. Every other stage in this app runs itself; this one waits
for someone to press a button, because the point right now is to look at what
comes out. Automation is a later switch, not a missing feature.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import archives, tts
from core.config import settings
from core.db import get_db
from core.models import Render
from core.produce import preflight
from core.storage import get_storage
from worker.queue import enqueue
from worker.tasks.produce import run as produce_run

log = logging.getLogger(__name__)

router = APIRouter(prefix="/studio", tags=["studio"])


class GenerateIn(BaseModel):
    archive_id: str
    #: three seconds of AI presenter before the recording, instead of opening
    #: cold on the tape itself
    voice_hook: bool = False
    grade: float | None = Field(default=None, ge=0.0, le=1.0)
    overlay: float | None = Field(default=None, ge=0.0, le=1.0)
    use_stock: bool = True
    tape_offset_s: float | None = Field(default=None, ge=0.0)


class RenderOut(BaseModel):
    id: int
    archive_id: str
    archive_name: str
    status: str
    created_at: str
    duration_s: float | None
    storage_key: str | None
    options: dict[str, Any]
    layers: dict[str, Any]
    warnings: list[str]
    cost_usd: float | None
    elapsed_s: float | None
    error: str | None
    approved: bool
    url: str | None


def _out(row: Render) -> RenderOut:
    name = row.archive_id
    try:
        name = archives.get(row.archive_id).name
    except KeyError:  # an archive removed after the render was made
        pass
    return RenderOut(
        id=row.id,
        archive_id=row.archive_id,
        archive_name=name,
        status=row.status,
        created_at=row.created_at.isoformat(),
        duration_s=row.duration_s,
        storage_key=row.storage_key,
        options=row.options or {},
        layers=row.layers or {},
        warnings=list(row.warnings or []),
        cost_usd=row.cost_usd,
        elapsed_s=row.elapsed_s,
        error=row.error,
        approved=row.approved,
        url=f"/api/studio/renders/{row.id}/file" if row.storage_key else None,
    )


@router.get("/archives")
def list_archives() -> dict[str, Any]:
    """Every source, what it would cost, and what is missing to render it."""
    items = [preflight(a) for a in archives.ORDER]
    return {
        "archives": items,
        "keys": {
            "narration": {
                "var": "OPENAI_API_KEY",
                "set": settings.has_tts,
                "what": "the AI narrator, and the only part with no free fallback",
                "voice": settings.tts_voice,
                "model": settings.tts_model,
                "per_minute_usd": tts.COST_PER_MINUTE,
            },
            "stock": {
                "var": "PEXELS_API_KEY",
                "set": settings.has_stock,
                "what": "the footage underneath - free, 200 requests an hour",
            },
            "storage": {
                "var": "R2_BUCKET",
                "set": settings.has_r2,
                "what": "where finished videos live; local disk is used without it",
            },
            "queue": {
                "var": "REDIS_URL",
                "set": settings.has_redis,
                "what": "renders run on the worker; without it they run in the web process",
            },
        },
        "defaults": {
            "grade": settings.studio_grade,
            "overlay": settings.studio_overlay,
            "fps": settings.studio_fps,
            "manual_only": settings.studio_manual_only,
        },
    }


@router.post("/generate", response_model=RenderOut, status_code=201)
def generate(
    payload: GenerateIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Any:
    """Queue one video.

    With Redis the worker picks it up; without, it runs in a background task on
    this process. A render is about a minute and a half of CPU either way, so
    the request returns straight away and the dashboard polls.
    """
    try:
        archives.get(payload.archive_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None

    row = Render(
        archive_id=payload.archive_id,
        status="queued",
        options={
            "voice_hook": payload.voice_hook,
            "grade": payload.grade,
            "overlay": payload.overlay,
            "use_stock": payload.use_stock,
            "tape_offset_s": payload.tape_offset_s,
        },
    )
    db.add(row)
    db.flush()

    if settings.has_redis:
        enqueue("render", produce_run, row.id, job_timeout=1800)
    else:
        background.add_task(produce_run, row.id)

    return _out(row)


@router.get("/renders", response_model=list[RenderOut])
def list_renders(limit: int = 30, status: str | None = None, db: Session = Depends(get_db)) -> Any:
    query = db.query(Render)
    if status:
        query = query.filter(Render.status == status)
    return [_out(r) for r in query.order_by(Render.id.desc()).limit(limit).all()]


def _get(render_id: int, db: Session) -> Render:
    row = db.get(Render, render_id)
    if row is None:
        raise HTTPException(404, f"render {render_id} does not exist")
    return row


@router.get("/renders/{render_id}", response_model=RenderOut)
def get_render(render_id: int, db: Session = Depends(get_db)) -> Any:
    return _out(_get(render_id, db))


@router.get("/renders/{render_id}/file")
def render_file(render_id: int, db: Session = Depends(get_db)) -> Any:
    """The mp4. Local storage is served from disk; R2 redirects to a signed URL."""
    row = _get(render_id, db)
    if not row.storage_key:
        raise HTTPException(409, f"render {render_id} is {row.status}, not ready")

    storage = get_storage()
    if storage.kind != "local":
        return RedirectResponse(storage.url_for(row.storage_key))

    path = Path(settings.local_storage_dir) / row.storage_key
    if not path.exists():
        raise HTTPException(404, f"file missing on disk: {row.storage_key}")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.post("/renders/{render_id}/approve", response_model=RenderOut)
def approve(render_id: int, db: Session = Depends(get_db)) -> Any:
    """Mark a render as one you would publish. Nothing posts it yet."""
    row = _get(render_id, db)
    if row.status != "ready":
        raise HTTPException(409, f"render {render_id} is {row.status}, not ready")
    row.approved = True
    return _out(row)


@router.post("/renders/{render_id}/tape", response_model=RenderOut)
def attach_tape(
    render_id: int,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Any:
    """Supply the recording by hand and re-render.

    For the archives with nothing fetchable - Air Force One, the Nixon tapes -
    where the audio exists publicly but not behind a URL a worker can take.
    """
    row = _get(render_id, db)

    inbox = Path(settings.work_dir) / "uploads"
    inbox.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "tape.mp3").suffix or ".mp3"
    with tempfile.NamedTemporaryFile(dir=inbox, suffix=suffix, delete=False) as handle:
        shutil.copyfileobj(file.file, handle)
        dest = Path(handle.name)

    options = dict(row.options or {})
    options["tape_path"] = str(dest)
    row.options = options
    row.status = "queued"
    row.error = None
    db.flush()

    if settings.has_redis:
        enqueue("render", produce_run, row.id, job_timeout=1800)
    else:
        background.add_task(produce_run, row.id)
    return _out(row)


@router.delete("/renders/{render_id}")
def delete_render(render_id: int, db: Session = Depends(get_db)) -> dict[str, int]:
    row = _get(render_id, db)
    db.delete(row)
    return {"deleted": render_id}
