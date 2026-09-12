"""Everything the dashboard reads and every button it has.

One module, because there is one job. The old API had six routers for six
pipeline stages; this has a queue, a record of what went out, and the accounts
it goes out to.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func, select

from core import credentials, jobs
from core.config import settings
from core.db import session_scope
from core.models import Reel, ReelPost
from core.publishers import destinations
from core.storage import get_storage

log = logging.getLogger(__name__)

router = APIRouter(tags=["app"])


def _reel(reel: Reel, place: int | None = None) -> dict[str, Any]:
    return {
        "id": reel.id,
        "place": place,
        "external_id": reel.external_id,
        "caption": reel.caption,
        "ups": reel.ups,
        "duration_s": reel.duration_s,
        "subreddit": reel.subreddit,
        "author": reel.author,
        "permalink": reel.permalink,
        "state": reel.state,
        "note": reel.note,
        # Either place counts: the dashboard only needs to know
        # whether there is a file to watch somewhere.
        "ready": bool(reel.storage_key or reel.local_path),
        "found_at": reel.created_at.isoformat() if reel.created_at else None,
        "posted_at": reel.posted_at.isoformat() if reel.posted_at else None,
    }


@router.get("/overview")
def overview() -> dict[str, Any]:
    """The numbers on the front page."""
    with session_scope() as session:
        counts = dict(
            session.execute(
                select(Reel.state, func.count(Reel.id)).group_by(Reel.state)
            ).all()
        )
        weakest = session.execute(
            select(func.min(Reel.ups)).where(Reel.state.in_(("found", "ready")))
        ).scalar()
    waiting = int(counts.get("found", 0)) + int(counts.get("ready", 0))
    return {
        "waiting": waiting,
        "slots": settings.queue_size,
        # What a new video has to beat to get in. Once the queue is full this
        # is the real quality bar, and it is not a number anyone configured.
        "bar": weakest if waiting >= settings.queue_size else None,
        "posted": int(counts.get("posted", 0)),
        "beaten": int(counts.get("dropped", 0)),
        # Derived from the credentials, not from a table of handles typed
        # in beside them. An empty list means nothing is configured to
        # receive a post, which is a different problem from autopost off.
        "destinations": destinations(),
        "posts_per_run": settings.post_per_run,
        "rooms": len(settings.reddit_rooms),
        "window": settings.reddit_time_filter,
        "publisher": settings.publisher,
        "autopost": settings.autopost_enabled,
        "routes": settings.reddit_route_names or ["all six, in order"],
        "storage": "r2" if settings.has_storage else "local disk",

    }


@router.get("/queue")
def queue() -> dict[str, Any]:
    """What is waiting, best first. Position 1 goes out next."""
    with session_scope() as session:
        rows = list(
            session.execute(
                select(Reel)
                .where(Reel.state.in_(("found", "ready")))
                .order_by(Reel.ups.desc(), Reel.id.asc())
            ).scalars()
        )
        items = [_reel(r, place=i + 1) for i, r in enumerate(rows)]
    return {"items": items, "slots": settings.queue_size,
            "goes_out_next": settings.post_per_run}


@router.get("/posted")
def posted(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    with session_scope() as session:
        rows = list(
            session.execute(
                select(Reel).where(Reel.state == "posted")
                .order_by(Reel.posted_at.desc().nullslast(), Reel.id.desc())
                .limit(limit)
            ).scalars()
        )
        items = []
        for reel in rows:
            attempts = list(
                session.execute(
                    select(ReelPost).where(ReelPost.reel_id == reel.id)
                ).scalars()
            )
            items.append(_reel(reel) | {
                "went_to": [
                    {"platform": a.platform, "status": a.status,
                     "url": a.platform_url, "error": a.error}
                    for a in attempts
                ],
            })
    return {"items": items}


@router.get("/beaten")
def beaten(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    """Pushed out of the queue by something better. Kept so it is never
    picked up again on a later run."""
    with session_scope() as session:
        rows = list(
            session.execute(
                select(Reel).where(Reel.state == "dropped")
                .order_by(Reel.ups.desc()).limit(limit)
            ).scalars()
        )
        return {"items": [_reel(r) for r in rows]}


@router.get("/reels/{reel_id}/video")
def video(reel_id: int) -> Any:
    """The branded file, so the dashboard can play it before it goes out.

    Storage first, disk second. web, worker and scheduler are three separate
    containers with three separate filesystems, so a file the worker branded
    is not on the disk this request is being served from - and reading
    local_path here would report "never downloaded" for a video that exists.
    """
    with session_scope() as session:
        reel = session.get(Reel, reel_id)
        if reel is None:
            raise HTTPException(404, "no such reel")
        key = reel.storage_key
        path = Path(reel.local_path) if reel.local_path else None

    if key and settings.has_r2:
        # A redirect rather than a proxy: the browser asks R2 for byte ranges
        # directly, which is what makes scrubbing work, and the web service
        # does not spend its memory relaying video.
        return RedirectResponse(get_storage().url_for(key, expires_s=3600))

    if path is not None and path.exists():
        return FileResponse(path, media_type="video/mp4")

    raise HTTPException(
        404,
        "no file for this reel on this service. A reel is downloaded when it "
        "is about to go out, so most of the queue has none - and without R2 "
        "configured, one the worker prepared is on the worker's disk, which "
        "the dashboard cannot read." if not settings.has_r2 else
        "no file for this reel yet - it is downloaded when it is about to go out.",
    )


@router.post("/run")
def run_now(
    post: bool = Query(default=True),
    inline: bool = Query(default=False),
    rooms: int = Query(default=0, ge=0, le=25),
) -> dict[str, Any]:
    """Do a run now instead of waiting for the daily one.

    In the background by default, because a full pass reads every room and
    takes minutes - longer than a browser will wait, and a request that times
    out invites a second press, which is how you get two passes downloading
    the same videos at once.

    `inline` waits for it, which only makes sense with `rooms` small. That
    pair answers a question the background version cannot: does the chain work
    at all, right now, with the answer on this screen?
    """
    if inline:
        return {"waited": True, "result": jobs.run(post=post, rooms=rooms or 3)}

    started = jobs.start_in_background(post=post, rooms=rooms or None)
    return {
        "waited": False,
        "started": started,
        "since": str(jobs.in_flight()) if not started else None,
    }


@router.get("/run/status")
def run_status() -> dict[str, Any]:
    """Whether a run is going, and what the last one did.

    A run happens on a thread inside this service, so there is no queue to be
    stuck in and no worker to be missing. What can still go wrong is that it
    threw - which leaves no trace anywhere else, since nobody was waiting on
    it and there was no response for it to fail.
    """
    going = jobs.in_flight()
    previous = jobs.last()
    return {
        "running": going is not None,
        "since": going.isoformat() if going else None,
        "armed": settings.harvest_enabled and bool(settings.reddit_rooms),
        "every_minutes": settings.harvest_interval_minutes,
        "last": None if previous is None else {
            "started_at": previous.started_at.isoformat(),
            "finished_at": previous.finished_at.isoformat() if previous.finished_at else None,
            "found": previous.found,
            "added": previous.added,
            "pushed_out": previous.pushed_out,
            "posted": previous.posted,
            "failed": previous.failed,
            "error": previous.error,
        },
    }


@router.post("/reels/{reel_id}/drop")
def drop(reel_id: int) -> dict[str, Any]:
    """Kick one out by hand. It stays on record, so it cannot come back."""
    with session_scope() as session:
        reel = session.get(Reel, reel_id)
        if reel is None:
            raise HTTPException(404, "no such reel")
        if reel.state == "posted":
            raise HTTPException(409, "already posted - that cannot be undone here")
        reel.state = "dropped"
        reel.note = "dropped by hand"
    return {"ok": True}


@router.post("/reels/{reel_id}/post")
def post_one(reel_id: int) -> dict[str, Any]:
    """Send one out now, ahead of its turn."""
    from worker.tasks.publish import publish_one

    if not settings.autopost_enabled:
        raise HTTPException(409, "AUTOPOST_ENABLED is off - nothing will go out")
    attempts = publish_one(reel_id)
    return {"attempts": [
        {"platform": a.platform, "status": a.status,
         "url": a.platform_url, "error": a.error} for a in attempts
    ]}


@router.post("/reels/{reel_id}/prepare")
def prepare_one(reel_id: int) -> dict[str, Any]:
    """Download and brand one now, so it can be watched before it goes out."""
    from worker.tasks.harvest import prepare

    path = prepare(reel_id)
    return {"ok": True, "megabytes": round(path.stat().st_size / 1e6, 1)}


@router.get("/services/meta")
def meta_status() -> dict[str, Any]:
    """Which Meta accounts the configured ids actually resolve to.

    Setup is a chain of ids and tokens that all look alike, and the failure this
    catches is posting to the wrong Instagram account - which nothing else will
    tell you until it has already happened. So this asks Meta to name each
    account rather than reporting that a variable is non-empty.

    Never returns a token, only what one resolves to.
    """
    configured = {
        "instagram": settings.has_instagram,
        "threads": settings.has_threads,
        "facebook": settings.has_facebook,
    }
    # Naming the empty variables turns "it does not work" into a checklist. The
    # usual cause is a shared variable that was never applied to this service,
    # and that looks identical to a typo until you can see which name is blank.
    present = {
        "META_APP_ID": bool(settings.meta_app_id),
        "META_APP_SECRET": bool(settings.meta_app_secret),
        "META_ACCESS_TOKEN": bool(settings.meta_access_token),
        "INSTAGRAM_USER_ID": bool(settings.instagram_user_id),
        "INSTAGRAM_ACCESS_TOKEN": bool(settings.instagram_access_token),
        "THREADS_USER_ID": bool(settings.threads_user_id),
        "THREADS_ACCESS_TOKEN": bool(settings.threads_access_token),
        "FACEBOOK_PAGE_ID": bool(settings.facebook_page_id),
        "FACEBOOK_PAGE_TOKEN": bool(settings.facebook_page_token),
        "R2_ACCOUNT_ID": bool(settings.r2_account_id),
        "R2_ACCESS_KEY_ID": bool(settings.r2_access_key_id),
        "R2_SECRET_ACCESS_KEY": bool(settings.r2_secret_access_key),
        "R2_BUCKET": bool(settings.r2_bucket),
    }
    payload: dict[str, Any] = {
        "publisher": settings.publisher,
        "graph_version": settings.meta_graph_version,
        "instagram_route": (
            "instagram_login" if settings.instagram_via_instagram_login else "facebook_login"
        ),
        "configured": configured,
        # Meta fetches the clip from a URL, so publishing needs public storage.
        "storage_ready": settings.has_r2,
        "accounts": {},
        "tokens": [],
        "variables_set": sorted(name for name, ok in present.items() if ok),
        "variables_missing": sorted(name for name, ok in present.items() if not ok),
    }

    if not any(configured.values()):
        payload["hint"] = (
            "No Meta credentials reached this service. If you set them as Railway "
            "shared variables, they must also be applied to each service - check "
            "the web service's own Variables tab, then redeploy."
        )
        return payload

    from core.publishers.meta import describe_accounts

    try:
        payload["accounts"] = describe_accounts()
    except Exception as exc:  # noqa: BLE001 - a dead token must not 500 the page
        payload["accounts"] = {}
        payload["error"] = str(exc)[:300]

    payload["tokens"] = [
        {
            "name": row["name"],
            "days_left": row["days_left"],
            "refreshed_at": row["refreshed_at"].isoformat() if row["refreshed_at"] else None,
            "last_error": row["last_error"],
        }
        for row in credentials.status()
    ]
    if not settings.has_r2:
        payload["hint"] = (
            "Meta downloads the clip from a URL, so R2 must be configured before "
            "PUBLISHER=meta can post."
        )
    return payload


@router.get("/services")
def services() -> dict[str, Any]:
    """Where reels will go, and whether those credentials actually work.

    Not a list of handles somebody typed: the ids in the environment are the
    accounts, and this asks Meta to name each one rather than reporting that a
    variable is non-empty. Posting to the wrong Instagram account is the
    failure nothing else catches until after it has happened.
    """
    where = destinations()
    payload: dict[str, Any] = {
        "publisher": settings.publisher,
        "autopost": settings.autopost_enabled,
        "destinations": where,
        "resolved": {},
        "blocked": [],
    }

    if not where:
        payload["blocked"].append(
            f"PUBLISHER={settings.publisher} has no credentials set, so there "
            f"is nowhere for a reel to go."
        )
    if not settings.autopost_enabled:
        payload["blocked"].append("AUTOPOST_ENABLED is off, so nothing will be sent.")
    if settings.publisher == "meta" and not settings.has_r2:
        payload["blocked"].append(
            "Meta downloads the file from a URL rather than accepting an "
            "upload, so R2 must be configured before it can post."
        )

    if settings.publisher in ("meta", "manual") and where:
        from core.publishers.meta import describe_accounts

        try:
            payload["resolved"] = describe_accounts()
        except Exception as exc:  # noqa: BLE001 - a dead token must not 500 the page
            payload["error"] = str(exc)[:300]

    payload["tokens"] = [
        {
            "name": row["name"],
            "days_left": row["days_left"],
            "last_error": row["last_error"],
        }
        for row in credentials.status()
    ]
    return payload
