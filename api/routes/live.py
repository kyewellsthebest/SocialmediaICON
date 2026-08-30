"""What the bot is watching, and what it has caught.

Two views onto the same run: `/live` is what it can see right now - the
streams, their chat, every signal it is scoring - and `/catches` is what it
decided was worth keeping.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from core.config import settings
from core.db import get_db
from core.models import Catch
from worker.queue import enqueue

log = logging.getLogger(__name__)

router = APIRouter(prefix="/live", tags=["live"])


def _idle(hint: str) -> dict[str, Any]:
    return {
        "running": False,
        "enabled": settings.live_enabled,
        "slots": settings.live_slots,
        "posting_enabled": settings.live_posting_enabled,
        "caps": {
            "per_day": settings.live_clips_per_day,
            "min_gap_minutes": settings.live_min_gap_minutes,
        },
        "streams": [],
        "errors": [],
        "wanted": _wanted(),
        "hint": hint,
    }


def _wanted() -> bool:
    from core import livestate

    return livestate.wanted() if settings.has_redis else False


@router.get("")
def status() -> dict[str, Any]:
    """Everything the watcher can see, for the Live view.

    Read from the shared snapshot rather than from memory: the supervisor
    lives in the worker process and this one is the web. A status held in a
    module global here would read "not running" forever while three buffers
    were quietly filling in another container.
    """
    from core import livestate

    found = livestate.read()
    if found:
        found.setdefault("wanted", livestate.wanted())
        return found
    if not settings.live_enabled:
        return _idle("Set LIVE_ENABLED=true on the web and worker, then press Start.")
    if not settings.has_redis:
        return _idle("REDIS_URL is not set, so the watcher cannot be started or read.")
    return _stuck()


def _stuck() -> dict[str, Any]:
    """Nothing is publishing. Say why, and put it back on the queue.

    "Restarting..." was the only thing this page could say once the snapshot
    expired, and it said it forever - for a job sitting in a queue nobody is
    listening to, for a worker missing LIVE_ENABLED, and for a run that died
    without relaunching itself. All three look the same from here and only
    reading the queue tells them apart.
    """
    from core import livestate

    out = _idle("")
    queue = _queue_view(has_snapshot=False)
    out["diagnosis"] = queue.get("verdict", "")
    out["queue"] = queue.get("queue")

    # An explanation the worker left behind outlives its snapshot, so it is
    # still here long after the thirty seconds in which it was readable.
    note = livestate.last_note()
    if note:
        out["hint"] = note.get("message", "")
        out["noted_at"] = note.get("at")
    else:
        out["hint"] = out["diagnosis"]

    # A watch that is wanted but is neither queued nor running has fallen
    # through a crack - an out-of-memory kill takes the process without
    # running the relaunch. Put it back, at most once a minute, and only when
    # there is genuinely nothing in flight so a stall cannot become a queue
    # full of identical jobs.
    counts = queue.get("queue") or {}
    idle = counts.get("waiting") == 0 and counts.get("started") == 0
    if out["wanted"] and idle and counts.get("workers_listening_on_live"):
        if livestate.claim("relaunch", seconds=60):
            job = enqueue("live", "worker.tasks.live_watch.run", job_timeout=24 * 3600)
            out["requeued"] = job is not None
            if job is not None:
                out["hint"] = "Nothing was running, so it has been put back on the queue."
    return out


@router.post("/start")
def start() -> dict[str, Any]:
    if not settings.live_enabled:
        raise HTTPException(400, "LIVE_ENABLED is not set on this service")
    if not settings.has_redis:
        raise HTTPException(503, "REDIS_URL is not set, so there is no worker to run this")

    from core import livestate

    if livestate.read():
        return {"ok": True, "already_running": True}

    # The queue name comes first; the watcher has its own so a run lasting
    # hours cannot sit in front of every other job. job_timeout has to be
    # long for the same reason - the default hour would kill it mid-stream.
    # Record the intent before queueing anything: this is what makes the
    # watch survive the next deploy without being pressed again.
    livestate.want(True)
    livestate.clear()
    job = enqueue("live", "worker.tasks.live_watch.run", job_timeout=24 * 3600)
    if job is None:
        raise HTTPException(503, "the job could not be queued - is Redis reachable?")

    # Claim the state immediately. Until a worker picks the job up nothing else
    # writes here, so without this the page shows "not running" and there is no
    # way to tell a queued job from a button that did nothing.
    livestate.publish(
        {
            "running": False,
            "queued": True,
            "enabled": True,
            "slots": settings.live_slots,
            "posting_enabled": settings.live_posting_enabled,
            "caps": {
                "per_day": settings.live_clips_per_day,
                "min_gap_minutes": settings.live_min_gap_minutes,
            },
            "streams": [],
            "errors": [],
            "hint": (
                "Queued. Waiting for the worker to pick it up - if this does not "
                "change within a minute, the worker is not listening on the "
                "'live' queue, or LIVE_ENABLED is not set on the worker service."
            ),
        }
    )
    return {"ok": True, "queued": True, "job": getattr(job, "id", None)}


@router.post("/stop")
def stop() -> dict[str, Any]:
    from worker.tasks.live_watch import stop as stop_watching

    return stop_watching()


@router.get("/streams/{channel}")
def stream(channel: str) -> dict[str, Any]:
    """Everything known about one stream, for its own page.

    Read out of the same snapshot the Live view uses rather than asking the
    supervisor again: there is one writer and it is in another process, so a
    second source here could only ever disagree with the first.
    """
    from core import livestate

    found = livestate.read() or {}
    for entry in found.get("streams", []):
        if entry.get("channel", "").lower() == channel.lower():
            return entry
    raise HTTPException(404, f"{channel} is not being watched right now")


@router.get("/streams/{channel}/spectrogram")
def spectrogram(channel: str) -> Response:
    """The sound of the last half minute, drawn."""
    from core import livestate

    data = livestate.get_image(f"spectrogram:{channel}")
    if not data:
        raise HTTPException(404, "no spectrogram yet - it is drawn every twenty seconds")
    # No caching: the page asks for this on a timer and a stale picture of a
    # live stream is worse than none.
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/catches")
def catches(
    limit: int = Query(30, ge=1, le=200), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    """What has been caught, newest first."""
    rows = db.query(Catch).order_by(Catch.id.desc()).limit(limit).all()
    return [_row(c) for c in rows]


@router.get("/catches/{catch_id}/video")
def video(catch_id: int, db: Session = Depends(get_db)):  # noqa: ANN201 - several shapes
    """The clip itself, wherever the worker managed to put it.

    Three places, in the order they are worth having: object storage, which
    the browser can fetch directly; Redis, which holds it when there is no R2
    configured; and this service's own disk, which only works when the worker
    and the web happen to be the same machine.
    """
    from core import livestate

    row = db.get(Catch, catch_id)
    if row is None or not row.storage_key:
        raise HTTPException(404, "no such clip")
    key = row.storage_key

    if key.startswith("redis:"):
        data = livestate.get_image(f"clip:{key.split(':', 1)[1]}", max_age_s=48 * 3600)
        if not data:
            raise HTTPException(410, "the clip has expired from the review queue")
        return Response(content=data, media_type="video/mp4")

    if not key.startswith("/") and settings.has_r2:
        from core.storage import get_storage

        return RedirectResponse(get_storage().url_for(key), status_code=307)

    path = Path(key)
    if not path.exists():
        raise HTTPException(
            410,
            "the clip is not on this service's disk. Clips are cut on the worker; "
            "configure R2 so both services can reach them.",
        )
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.post("/catches/{catch_id}/keep")
def keep(catch_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(Catch, catch_id)
    if row is None:
        raise HTTPException(404, "no such clip")
    row.approved = True
    row.status = "kept"
    db.flush()
    return _row(row)


@router.delete("/catches/{catch_id}")
def discard(catch_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(Catch, catch_id)
    if row is None:
        raise HTTPException(404, "no such clip")
    # Delete the file too: a rejected clip is the one thing here with no
    # reason to occupy disk.
    if row.storage_key:
        Path(row.storage_key).unlink(missing_ok=True)
    db.delete(row)
    return {"ok": True}


def _row(c: Catch) -> dict[str, Any]:
    return {
        "id": c.id,
        "channel": c.channel,
        "source_url": c.source_url,
        "at_s": c.at_s,
        "duration_s": c.duration_s,
        "score": c.score,
        "why": c.why or {},
        "mood": c.mood or {},
        "quotes": c.quotes or [],
        "peak_viewers": c.peak_viewers,
        "status": c.status,
        "approved": c.approved,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "has_video": _video_state(c)[0],
        "video_note": _video_state(c)[1],
    }


@router.get("/debug")
def debug() -> dict[str, Any]:
    """Why is nothing happening? Answered from the queue itself.

    Start enqueues a job in one process and a worker in another runs it, so
    when nothing happens there are four separate places it can have gone: the
    variable is missing on the web side, the job never reached Redis, no worker
    is listening on the queue, or the job ran and raised. From the dashboard
    all four look identical, and each round of guessing costs a deploy.

    This reads the queue directly and says which one it is - including the
    traceback of the last failure, which is the thing there was previously no
    way to see at all.
    """
    from core import livestate

    out: dict[str, Any] = {
        "web": {
            "live_enabled": settings.live_enabled,
            "has_redis": settings.has_redis,
            "has_db": settings.has_db,
            "slots": settings.live_slots,
        },
        "snapshot": livestate.read(),
    }

    # Migrations only run on web, so a web service that spent the morning
    # crash-looping may never have created this. The watcher counts the day's
    # clips against it on every tick, so a missing table stops every cut.
    try:
        from sqlalchemy import inspect

        from core.db import get_engine

        out["catches_table"] = "catches" in inspect(get_engine()).get_table_names()
    except Exception as exc:  # noqa: BLE001
        out["catches_table"] = f"could not check: {type(exc).__name__}: {exc}"

    found = _queue_view(has_snapshot=bool(out["snapshot"]),
                        catches_table=out.get("catches_table"))
    out.update(found)
    return out


def _queue_view(*, has_snapshot: bool, catches_table: Any = True) -> dict[str, Any]:
    """Read the live queue and say, in one sentence, what is wrong.

    Start enqueues a job in one process and a worker in another runs it, so
    when nothing happens there are five separate places it can have gone: the
    variable is missing on the web side, the job never reached Redis, no
    worker is listening on the queue, the job ran and raised, or it returned
    without ever publishing. From the dashboard all five look identical, and
    each round of guessing costs a deploy.

    Split out of the debug endpoint because the Live view needs the same
    answer: a page that says "restarting" while a job sits in a queue nobody
    is listening to is worse than a page that says so.
    """
    if not settings.has_redis:
        return {"verdict": "No Redis on the web service, so nothing can be queued."}

    out: dict[str, Any] = {}
    try:
        from rq import Queue, Worker

        from worker.queue import get_redis

        redis = get_redis()
        redis.ping()
        queue = Queue("live", connection=redis)

        workers = Worker.all(connection=redis)
        listening = [
            {"name": w.name, "state": str(w.get_state()), "job": w.get_current_job_id()}
            for w in workers
            if "live" in [q.name for q in w.queues]
        ]

        failed = queue.failed_job_registry
        recent: list[dict[str, Any]] = []
        for job_id in failed.get_job_ids()[-3:]:
            try:
                job = queue.fetch_job(job_id)
                recent.append(
                    {
                        "id": job_id,
                        "ended_at": str(getattr(job, "ended_at", "")),
                        # The traceback is the whole point of this endpoint.
                        "error": (getattr(job, "exc_info", "") or "")[-1500:],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                recent.append({"id": job_id, "error": f"could not fetch: {exc}"})

        waiting, started = len(queue), len(queue.started_job_registry)
        out["queue"] = {
            "waiting": waiting,
            "started": started,
            "failed": len(failed),
            "workers_listening_on_live": listening,
        }
        out["recent_failures"] = recent

        if catches_table is False:
            out["verdict"] = (
                "The 'catches' table does not exist - migrations have not run. "
                "Redeploy web (it runs alembic upgrade head at boot), or the "
                "watcher will run but never be allowed to cut."
            )
        elif not listening:
            out["verdict"] = (
                "No worker is listening on the 'live' queue. The worker service "
                "is either not running this build, or has a custom start command "
                "that bypasses scripts/start.sh."
            )
        elif has_snapshot:
            out["verdict"] = "A snapshot exists; the watcher is reporting state."
        elif started:
            out["verdict"] = (
                "A worker has the job and has not published a snapshot yet - "
                "the first buffer takes about fifteen seconds."
            )
        elif waiting:
            out["verdict"] = "A job is queued and no worker has taken it yet."
        elif recent:
            out["verdict"] = "A job ran and raised - see recent_failures[].error."
        else:
            out["verdict"] = (
                "Nothing queued, nothing running, no snapshot: the job finished "
                "without publishing. Check LIVE_ENABLED on the worker."
            )
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never 500
        out["verdict"] = f"Could not read the queue: {type(exc).__name__}: {exc}"

    return out


def _video_state(c: Catch) -> tuple[bool, str]:
    """Whether the video will play, and if not, which of the reasons it is.

    "No video" is three different things and only one of them is worth acting
    on: a clip cut before clips were stored anywhere the web service could
    read them, one that has aged out of the review queue, and one whose file
    is simply gone. Saying which is the difference between a row you delete
    and a row that tells you to configure R2.
    """
    key = c.storage_key or ""
    if not key:
        return False, "Nothing was stored for this one."

    if key.startswith("redis:"):
        from core import livestate

        held = livestate.get_image(f"clip:{key.split(':', 1)[1]}", max_age_s=48 * 3600)
        if held:
            return True, ""
        return False, "Held for review for two days, then expired. Configure R2 to keep clips."

    if key.startswith("/") or key.startswith("."):
        # A bare path only ever worked when the worker and the web service
        # happened to be the same machine, which on Railway they are not.
        if Path(key).exists():
            return True, ""
        return False, (
            "Cut before clips were stored where this page can reach them, "
            "so the file only ever existed on the worker. Newer clips are fine."
        )

    if settings.has_r2:
        return True, ""
    return False, "Stored in R2, but R2 is not configured on this service."


def _playable(c: Catch) -> bool:
    return _video_state(c)[0]
