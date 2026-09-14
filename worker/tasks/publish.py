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
                # day's tally counts it as a fresh post.
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
