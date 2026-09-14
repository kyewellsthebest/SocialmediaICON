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

from sqlalchemy import func, select

from core import budget, schedule
from core.config import settings
from core.db import session_scope
from core.models import Reel, ReelPost
from core.publishers import PublishRequest, destinations, get_publisher
from core.publishers.meta import is_rate_limited
from core.storage import get_storage

log = logging.getLogger(__name__)


def publish_one(reel_id: int, only: list[str] | None = None) -> list[ReelPost]:
    """Put one prepared reel on every account. Returns the attempts recorded.

    A platform refusing is recorded, not raised: one refusal is not the others
    refusing, and a run that gives up on the first error posts nothing on a day
    when three of four would have worked.

    `only` names the platforms to ask, and is also what makes a retry possible:
    a reel counts as posted the moment *any* platform takes it, so a reel that
    went to Facebook and was refused by Instagram is "posted" with Instagram
    still owed. Naming the platform says this is that retry rather than a
    second go at the whole thing.
    """
    from worker.tasks.harvest import prepare

    with session_scope() as session:
        reel = session.get(Reel, reel_id)
        if reel is None:
            raise ValueError(f"no reel {reel_id}")
        if reel.state == "posted" and not only:
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
    now = datetime.now(UTC)
    with session_scope() as session:
        reel = session.get(Reel, reel_id)
        for result in results:
            # One row per platform, updated. A row per attempt turns the
            # record of what happened into a record of how often it was
            # retried - which is what forty identical red pills looked like.
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
            row.tried_at = now
            if result.ok:
                row.posted_at = now
            recorded.append(row)
        if reel is not None:
            reel.storage_key = key
            if any(r.ok for r in results):
                # Only on the first success. A retry that finally lands
                # Instagram must not move the reel's posted_at forward, or the
                # carousel's half hour restarts and the day's tally is wrong.
                reel.state = "posted"
                reel.posted_at = reel.posted_at or now
                reel.note = None
            else:
                reel.note = "; ".join(r.error or "refused" for r in results)[:400]
        session.flush()
        for row in recorded:
            session.expunge(row)
    return recorded


def retry_rate_limited(limit: int = 2) -> dict[str, Any]:
    """Ask again on the platforms that said "not now" rather than "no".

    Instagram's publishing limit is 25 posts per account per rolling 24 hours,
    and it is spent by asking, not only by succeeding. When it ran out, every
    reel's Instagram attempt failed - and because Facebook took the same reel,
    the reel was marked posted and Instagram was never asked again. A limit
    that clears by itself cost a day of Instagram posts permanently.

    Only rate limits. A refused token or a broken file fails the same way
    forever, and retrying those is what spends the limit in the first place.
    """
    if not (settings.autopost_enabled and settings.retry_rate_limited):
        return {"retried": 0}
    # A retry is a post flow like any other and costs the same containers.
    # Retrying past the cap is precisely how the cap got spent.
    room, why = budget.allowed("instagram")
    if not room:
        return {"retried": 0, "skipped": why}

    cutoff = datetime.now(UTC) - timedelta(hours=settings.retry_within_hours)
    waited = datetime.now(UTC) - timedelta(minutes=settings.rate_limit_wait_minutes)
    owed: list[tuple[int, str]] = []
    with session_scope() as session:
        rows = session.execute(
            select(ReelPost.reel_id, ReelPost.platform, ReelPost.error,
                   ReelPost.tried_at, ReelPost.created_at)
            .join(Reel, Reel.id == ReelPost.reel_id)
            .where(
                ReelPost.status == "failed",
                # The carousel keeps its own clock, attempts and backoff.
                ReelPost.platform != "instagram_carousel",
                Reel.posted_at.is_not(None),
                Reel.posted_at >= cutoff,
            )
            .order_by(ReelPost.reel_id.asc())
        ).all()
        for reel_id, platform, error, tried, created in rows:
            if not is_rate_limited(error):
                continue
            last = tried or created
            if last is not None:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                if last > waited:
                    continue
            owed.append((reel_id, platform))

    done = failed = 0
    for reel_id, platform in owed[:limit]:
        try:
            results = publish_one(reel_id, only=[platform])
        except Exception as exc:  # noqa: BLE001 - one bad reel is not the rest
            log.warning("retry: reel %s on %s failed (%s)", reel_id, platform, exc)
            failed += 1
            continue
        if any(r.status == "posted" for r in results):
            log.info("retry: reel %s finally went out on %s", reel_id, platform)
            done += 1
        else:
            failed += 1
    return {"retried": done, "failed": failed, "owed": len(owed)}


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
    if "instagram" in destinations():
        # Checked before the schedule, not after: a slot that opens with the
        # cap spent should stay shut rather than spend three containers
        # finding out.
        room, why = budget.allowed("instagram")
        if not room:
            return False, why

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
            # simply never going to work stops costing a request a minute. A
            # rate limit records no attempt, so `attempts` can be zero here -
            # that still waits, and waits long, because asking again is what
            # caused it.
            minutes = (settings.carousel_retry_minutes * attempts
                       if attempts else settings.rate_limit_wait_minutes)
            if now - tried < timedelta(minutes=minutes):
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
        row.tried_at = datetime.now(UTC)
        row.posted_at = datetime.now(UTC) if result.ok else None

        reel = session.get(Reel, reel_id)
        if reel is not None:
            reel.carousel_tried_at = datetime.now(UTC)
            if result.ok:
                reel.carousel_at = datetime.now(UTC)
                reel.carousel_note = None
            elif is_rate_limited(result.error):
                # Not an attempt. Meta is asking for less traffic, which
                # succeeds by itself once the traffic stops - counting it
                # would abandon this reel's second post over something
                # temporary, and the retries are what caused it.
                reel.carousel_note = (
                    f"waiting - Instagram's publishing limit is spent: "
                    f"{result.error}")[:400]
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
            reel.carousel_tried_at = datetime.now(UTC)
            if is_rate_limited(why):
                reel.carousel_note = f"waiting - {why}"[:400]
            else:
                reel.carousel_attempts = (reel.carousel_attempts or 0) + 1
                reel.carousel_note = _note(reel.carousel_attempts, why)
    return {"posted": 0, "failed": 1, "error": why}


def carousels_today(tz) -> int:
    """How many second posts have gone out in the viewer's today.

    The viewer's today, not UTC's. Counting against a UTC day would roll over
    at ten in the morning in Brisbane and put two days' worth in one
    afternoon - the same reason the reels count their own.
    """
    since = schedule.day_of(datetime.now(UTC), tz).astimezone(UTC)
    with session_scope() as session:
        return int(session.execute(
            select(func.count(Reel.id)).where(
                Reel.carousel_at.is_not(None), Reel.carousel_at >= since)
        ).scalar() or 0)


def post_carousels_due(limit: int = 2) -> dict[str, Any]:
    """Send any carousels whose half hour is up.

    Two caps, doing different jobs. `limit` is per tick, because each one
    renders a video and a backlog of ten would hold the heartbeat for a
    quarter of an hour. CAROUSEL_PER_DAY is the real one: only the first few
    reels of the day get a second post, because a carousel costs four requests
    to a reel's two and every reel appearing twice reads as a feed of repeats.
    """
    if not settings.carousel_enabled:
        return {"posted": 0, "skipped": "CAROUSEL_ENABLED is off"}
    if not settings.autopost_enabled:
        return {"posted": 0, "skipped": "AUTOPOST_ENABLED is off"}
    if "instagram" not in destinations():
        return {"posted": 0, "skipped": "Instagram is not a destination"}
    room, why = budget.allowed("instagram")
    if not room:
        return {"posted": 0, "skipped": why}
    if not settings.has_r2:
        # Checked here rather than discovered per reel. Instagram fetches each
        # slide from a URL; local storage hands back a file:// one, which it
        # cannot possibly read - so without R2 every carousel fails at the
        # last step having already rendered two videos.
        return {"posted": 0,
                "skipped": "R2 is not configured, and Instagram fetches each "
                           "carousel slide from a URL rather than an upload"}

    tz = schedule.zone(settings.post_timezone)
    already = carousels_today(tz)
    room_today = settings.carousel_per_day - already
    if room_today <= 0:
        return {"posted": 0,
                "skipped": f"today's {settings.carousel_per_day} second posts "
                           f"have all gone out"}

    posted = failed = 0
    for reel_id in carousel_owed()[:min(limit, room_today)]:
        outcome = post_carousel(reel_id)
        posted += outcome.get("posted", 0)
        failed += outcome.get("failed", 0)
    return {"posted": posted, "failed": failed}
