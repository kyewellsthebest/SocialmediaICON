"""Send the top of the queue out.

Where a reel goes is worked out from the credentials that are set, not from a
list of handles kept beside them - INSTAGRAM_USER_ID is the Instagram account,
and a second record of the same fact is one that can disagree with it.

The caption is the Reddit title, verbatim. Not summarised, not rewritten, not
"improved" - it is the author's own words about their own video, and a repost
page that rewrites them is doing something meaningfully worse than one that
copies them.

Which backend actually runs is `PUBLISHER`: manual (default - nothing goes
out), youtube, meta, or upload_post. And nothing goes out at all unless
AUTOPOST_ENABLED is on, whatever else is configured: an accidental deploy that
starts posting is not a mistake you can take back.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from core import schedule
from core.config import settings
from core.db import session_scope
from core.models import Reel, ReelPost
from core.publishers import PublishRequest, destinations, get_publisher
from core.storage import get_storage

log = logging.getLogger(__name__)


def publish_one(reel_id: int, only: list[str] | None = None) -> list[ReelPost]:
    """Put one prepared reel on every account. Returns the attempts recorded.

    A platform refusing is recorded, not raised: one refusal is not the others
    refusing, and a run that gives up on the first error posts nothing on a day
    when three of four would have worked.
    """
    from worker.tasks.harvest import prepare

    with session_scope() as session:
        reel = session.get(Reel, reel_id)
        if reel is None:
            raise ValueError(f"no reel {reel_id}")
        if reel.state == "posted":
            raise ValueError(f"reel {reel_id} has already gone out")
        caption, external_id = reel.caption, reel.external_id
        local = Path(reel.local_path) if reel.local_path else None

    # The download is deliberately late, so a reel beaten before its turn was
    # never fetched at all.
    if local is None or not local.exists():
        local = prepare(reel_id)

    key = f"reels/{external_id}.mp4"
    storage = get_storage()
    try:
        storage.put_file(local, key)
        public_url = storage.url_for(key)
    except Exception as exc:  # noqa: BLE001 - local files still post fine
        log.warning("publish: could not store %s (%s)", key, exc)
        key, public_url = None, None

    request = PublishRequest(
        clip_path=local,
        title=caption[:100],
        # Verbatim. The author wrote this about their own video.
        description=caption,
        hashtags=[],
        platforms=only or destinations(),
        storage_key=key,
        public_url=public_url,
    )
    results = get_publisher().publish(request)

    recorded: list[ReelPost] = []
    with session_scope() as session:
        reel = session.get(Reel, reel_id)
        for result in results:
            row = ReelPost(
                reel_id=reel_id,
                platform=result.platform,
                platform_post_id=result.post_id,
                platform_url=result.url,
                error=result.error,
                status="posted" if result.ok else "failed",
                posted_at=datetime.now(UTC) if result.ok else None,
            )
            session.add(row)
            recorded.append(row)
        if reel is not None:
            reel.storage_key = key
            if any(r.ok for r in results):
                reel.state = "posted"
                reel.posted_at = datetime.now(UTC)
            else:
                reel.note = "; ".join(r.error or "refused" for r in results)[:400]
        session.flush()
        for row in recorded:
            session.expunge(row)
    return recorded


def posted_today(tz) -> tuple[int, datetime | None]:
    """How many have gone out in the viewer's today, and when the last was.

    The viewer's today, not UTC's: counting against a UTC day would reset the
    tally at ten in the morning for an audience in Brisbane, and put ten reels
    out on one afternoon.
    """
    since = schedule.day_of(datetime.now(UTC), tz).astimezone(UTC)
    with session_scope() as session:
        rows = list(
            session.execute(
                select(Reel.posted_at)
                .where(Reel.state == "posted", Reel.posted_at >= since)
                .order_by(Reel.posted_at.desc())
            ).scalars()
        )
    latest = rows[0] if rows else None
    if latest is not None and latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    return len(rows), latest


def window() -> schedule.Window:
    return schedule.Window(
        start_hour=settings.post_start_hour,
        per_day=settings.post_per_day,
        every_minutes=settings.post_every_minutes,
    )


def next_due() -> tuple[bool, str]:
    """Whether a reel should go out now, and why not if it should not."""
    if not settings.autopost_enabled:
        return False, "AUTOPOST_ENABLED is off"
    if not destinations():
        return False, f"PUBLISHER={settings.publisher} has no credentials set"

    tz = schedule.zone(settings.post_timezone)
    count, last = posted_today(tz)
    return schedule.due(datetime.now(UTC), count, last, window(), tz)


def post_one_now() -> dict[str, Any]:
    """Send the single best waiting reel, if a slot is open.

    One per slot rather than a batch. Eight reels arriving at once is a burst
    every platform notices, and it spends a day's queue in a minute.
    """
    ready, why = next_due()
    if not ready:
        return {"posted": 0, "skipped": why}

    from worker.tasks.harvest import queued

    waiting = queued(limit=1)
    if not waiting:
        return {"posted": 0, "skipped": "nothing in the queue"}

    reel = waiting[0]
    try:
        results = publish_one(reel.id)
    except Exception as exc:  # noqa: BLE001 - one bad reel is not the schedule
        log.warning("publish: reel %s failed (%s)", reel.external_id, exc)
        with session_scope() as session:
            row = session.get(Reel, reel.id)
            if row is not None:
                row.note = str(exc)[:400]
        return {"posted": 0, "failed": 1, "error": str(exc)[:300]}

    if any(r.status == "posted" for r in results):
        log.info("publish: %s out (%d ups) %s",
                 reel.external_id, reel.ups, reel.caption[:60])
        return {"posted": 1, "failed": 0, "reel": reel.external_id}
    return {"posted": 0, "failed": 1,
            "error": "; ".join(r.error or "refused" for r in results)[:300]}


def post_due(limit: int | None = None) -> dict[str, Any]:
    """Every slot that is open right now, which is normally one or none.

    `limit` exists for the harvest, which calls this once at the end of a run:
    a queue that has just been filled should not empty itself in one pass.
    """
    posted = failed = 0
    skipped = ""
    for _ in range(limit or settings.post_per_day):
        outcome = post_one_now()
        posted += outcome.get("posted", 0)
        failed += outcome.get("failed", 0)
        if outcome.get("skipped"):
            skipped = outcome["skipped"]
        if not outcome.get("posted"):
            break
    return {"posted": posted, "failed": failed, "skipped": skipped}


def carousel_owed(now: datetime | None = None) -> list[int]:
    """Reels whose carousel is due: posted, delay elapsed, not yet sent.

    A failed one stays owed - a storage blip should not cost a reel its second
    post permanently - but it backs off and eventually gives up. Owed with no
    memory means retried on every heartbeat, once a minute, forever, which is
    how one broken render becomes forty identical failures.

    Oldest first, so a backlog drains in the order it was created rather than
    newest-first, which would leave the oldest owed forever.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(minutes=settings.carousel_delay_minutes)
    with session_scope() as session:
        rows = list(
            session.execute(
                select(Reel.id, Reel.carousel_attempts, Reel.carousel_tried_at)
                .where(
                    Reel.state == "posted",
                    Reel.carousel_at.is_(None),
                    Reel.posted_at.is_not(None),
                    Reel.posted_at <= cutoff,
                    Reel.carousel_attempts < settings.carousel_max_attempts,
                )
                .order_by(Reel.posted_at.asc())
            ).all()
        )

    due = []
    for reel_id, attempts, tried in rows:
        if tried is not None:
            if tried.tzinfo is None:
                tried = tried.replace(tzinfo=UTC)
            # Each failure waits longer than the last, so a render that is
            # simply never going to work stops costing a request a minute.
            wait = timedelta(minutes=settings.carousel_retry_minutes * attempts)
            if now - tried < wait:
                continue
        due.append(reel_id)
    return due


def post_carousel(reel_id: int) -> dict[str, Any]:
    """Build both slides and put them up as one Instagram carousel.

    Needs public URLs: Instagram fetches each slide from one rather than
    accepting an upload, which is why R2 is not optional for this path in the
    way it nearly is for a reel.
    """
    from core import carousel as slides

    with session_scope() as session:
        reel = session.get(Reel, reel_id)
        if reel is None:
            raise ValueError(f"no reel {reel_id}")
        if reel.carousel_at is not None:
            return {"skipped": "already up"}
        caption, external_id = reel.caption, reel.external_id
        key, local = reel.storage_key, Path(reel.local_path) if reel.local_path else None

    storage = get_storage()

    # The branded reel is the source. It may only exist in storage - the
    # service that made it is not necessarily the one running now - so fetch
    # it back rather than assuming a file on this disk.
    work = Path(settings.work_dir) / "carousel"
    work.mkdir(parents=True, exist_ok=True)
    if local is None or not local.exists():
        if not key:
            return _carousel_failed(reel_id, "the reel has no file to build from")
        local = storage.get_file(key, work / f"{external_id}.mp4")

    try:
        cover_path, square_path = slides.build(local, work)
    except Exception as exc:  # noqa: BLE001 - one bad render is not the schedule
        return _carousel_failed(reel_id, f"could not build the slides: {exc}"[:400])

    try:
        cover_key = f"carousel/{external_id}-cover.jpg"
        square_key = f"carousel/{external_id}-square.mp4"
        storage.put_file(cover_path, cover_key)
        storage.put_file(square_path, square_key)
        cover_url = storage.url_for(cover_key, expires_s=6 * 3600)
        square_url = storage.url_for(square_key, expires_s=6 * 3600)
    except Exception as exc:  # noqa: BLE001
        return _carousel_failed(
            reel_id,
            f"could not store the slides ({exc}). Instagram fetches a carousel "
            f"from a URL rather than accepting an upload, so R2 is required "
            f"for this."[:400],
        )

    from core.publishers.meta import MetaPublisher

    result = MetaPublisher().publish_carousel(cover_url, square_url, caption)

    with session_scope() as session:
        # One row per platform, updated. A new row per attempt turns the
        # record of what happened into a record of how often it was retried.
        row = session.execute(
            select(ReelPost).where(
                ReelPost.reel_id == reel_id,
                ReelPost.platform == result.platform,
            )
        ).scalars().first()
        if row is None:
            row = ReelPost(reel_id=reel_id, platform=result.platform)
            session.add(row)
        row.platform_post_id = result.post_id
        row.platform_url = result.url
        row.error = result.error
        row.status = "posted" if result.ok else "failed"
        row.posted_at = datetime.now(UTC) if result.ok else None

        reel = session.get(Reel, reel_id)
        if reel is not None:
            reel.carousel_tried_at = datetime.now(UTC)
            if result.ok:
                reel.carousel_at = datetime.now(UTC)
                reel.carousel_note = None
            else:
                reel.carousel_attempts = (reel.carousel_attempts or 0) + 1
                reel.carousel_note = _note(
                    reel.carousel_attempts, result.error or "refused")

    if result.ok:
        log.info("carousel: %s up (%s)", external_id, result.url or result.post_id)
        return {"posted": 1, "reel": external_id, "url": result.url}
    return {"posted": 0, "failed": 1, "error": result.error}


def _note(attempts: int, why: str) -> str:
    """The failure, with how many goes it has had. Read on the dashboard."""
    if attempts >= settings.carousel_max_attempts:
        return f"gave up after {attempts} attempts: {why}"[:400]
    return f"attempt {attempts} of {settings.carousel_max_attempts}: {why}"[:400]


def _carousel_failed(reel_id: int, why: str) -> dict[str, Any]:
    """Record why, and leave carousel_at unset so it is tried again.

    A render that failed on a bad frame usually fails again, but a storage
    blip does not - and writing the timestamp on failure would mean the one
    that could have worked is never retried. So it stays owed, waits longer
    each time, and gives up rather than retrying for the rest of the week.
    """
    log.warning("carousel: reel %s - %s", reel_id, why)
    with session_scope() as session:
        reel = session.get(Reel, reel_id)
        if reel is not None:
            reel.carousel_attempts = (reel.carousel_attempts or 0) + 1
            reel.carousel_tried_at = datetime.now(UTC)
            reel.carousel_note = _note(reel.carousel_attempts, why)
    return {"posted": 0, "failed": 1, "error": why}


def post_carousels_due(limit: int = 2) -> dict[str, Any]:
    """Send any carousels whose half hour is up.

    Capped per tick: each one renders a video, and a backlog of ten would
    otherwise hold the heartbeat for a quarter of an hour.
    """
    if not settings.carousel_enabled:
        return {"posted": 0, "skipped": "CAROUSEL_ENABLED is off"}
    if not settings.autopost_enabled:
        return {"posted": 0, "skipped": "AUTOPOST_ENABLED is off"}
    if "instagram" not in destinations():
        return {"posted": 0, "skipped": "Instagram is not a destination"}
    if not settings.has_r2:
        # Checked here rather than discovered per reel. Instagram fetches each
        # slide from a URL; local storage hands back a file:// one, which it
        # cannot possibly read - so without R2 every carousel fails at the
        # last step having already rendered two videos.
        return {"posted": 0,
                "skipped": "R2 is not configured, and Instagram fetches each "
                           "carousel slide from a URL rather than an upload"}

    posted = failed = 0
    for reel_id in carousel_owed()[:limit]:
        outcome = post_carousel(reel_id)
        posted += outcome.get("posted", 0)
        failed += outcome.get("failed", 0)
    return {"posted": posted, "failed": failed}
