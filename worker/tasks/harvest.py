"""The daily run: read the rooms, keep the best fifteen, post the top eight.

The queue is a leaderboard, not a pipeline. Every run reads every room, and
anything better than the weakest thing waiting takes its place - so a video
found on Tuesday can still be beaten on Thursday and never go out. That is the
point: the fifteen slots are always holding the fifteen best videos anyone has
seen, rather than the fifteen oldest.

Nothing is ever deleted from the table. A posted reel stays as a posted reel
and a beaten one stays as a beaten one, because both answer the same question -
"have we already dealt with this?" - and a queue that forgets reposts itself
the first time a video comes round again.

One economy worth knowing about: a reel is downloaded when it is about to be
posted, not when it joins the queue. Most of what enters the queue is beaten
before its turn comes, and downloading fifteen videos a day to post eight is
paying twice for the privilege of throwing half away.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from core import brand, reddit, reddit_routes
from core.config import settings
from core.db import session_scope
from core.models import Reel
from core.storage import get_storage

log = logging.getLogger(__name__)


def known_ids() -> set[str]:
    """Every Reddit post this queue has already made a decision about.

    Handed to the feed routes so they do not look up what we already know. A
    queue that has been running a week recognises most of what a feed shows
    it, and re-reading those is the difference between a one-minute run and a
    ten-minute one.
    """
    with session_scope() as session:
        return set(session.execute(select(Reel.external_id)).scalars())


def discover(rooms: list[str] | None = None) -> list[reddit.Post]:
    """Every postable video the rooms are showing that we have not seen.

    One room failing is not the run failing. By the time `listing` gives up it
    has already tried every way in, so a room that still cannot be read is a
    name that no longer exists - a typo in configuration, not an outage.
    """
    rooms = rooms if rooms is not None else settings.reddit_rooms
    skip = known_ids()
    seen: dict[str, reddit.Post] = {}
    refused: dict[str, int] = {}
    routes: set[str] = set()
    looked = 0

    for room in rooms:
        try:
            found, route = reddit_routes.listing(
                room, "top", settings.reddit_time_filter,
                limit=settings.reddit_per_room, skip=skip,
            )
        except reddit.RedditError as exc:
            log.warning("harvest: r/%s could not be read (%s)", room, exc)
            continue
        routes.add(route)
        looked += len(found)
        for post in found:
            if post.external_id in seen:
                continue
            ok, why = reddit.postable(post)
            if not ok:
                key = why.split()[-1] if why else "?"
                refused[key] = refused.get(key, 0) + 1
                continue
            seen[post.external_id] = post

    log.info(
        "harvest: %d new postable from %d rooms via %s (looked at %d, refused: %s)",
        len(seen), len(rooms), ", ".join(sorted(routes)) or "nothing", looked,
        ", ".join(f"{n} {k}" for k, n in refused.items()) or "none",
    )
    return sorted(seen.values(), key=lambda p: p.ups, reverse=True)


def admit(posts: list[reddit.Post]) -> int:
    """Put newly found video into the queue. Returns how many were added."""
    if not posts:
        return 0
    added = 0
    with session_scope() as session:
        already = set(
            session.execute(
                select(Reel.external_id).where(
                    Reel.external_id.in_([p.external_id for p in posts])
                )
            ).scalars()
        )
        for post in posts:
            if post.external_id in already:
                continue
            session.add(
                Reel(
                    external_id=post.external_id,
                    permalink=post.url,
                    subreddit=post.subreddit,
                    author=post.author,
                    caption=post.title,
                    ups=post.ups,
                    duration_s=post.duration_s,
                    posted_to_reddit_at=(
                        datetime.fromtimestamp(post.created_utc, UTC)
                        if post.created_utc else None
                    ),
                    state="found",
                )
            )
            added += 1
    log.info("harvest: %d joined the queue", added)
    return added


def trim(size: int | None = None) -> int:
    """Cut the queue back to its best `size`. Returns how many were pushed out.

    This is the rule in one place: a better video arriving pushes the weakest
    one waiting out of the queue, and the one that leaves is marked rather
    than deleted so it is never picked up again on a later run.
    """
    size = settings.queue_size if size is None else size
    pushed = 0
    with session_scope() as session:
        waiting = list(
            session.execute(
                select(Reel)
                .where(Reel.state.in_(("found", "ready")))
                .order_by(Reel.ups.desc(), Reel.id.asc())
            ).scalars()
        )
        for reel in waiting[size:]:
            reel.state = "dropped"
            reel.note = f"beaten - {reel.ups} upvotes did not make the top {size}"
            pushed += 1
    if pushed:
        log.info("harvest: %d pushed out of the queue", pushed)
    return pushed


def queued(limit: int | None = None) -> list[Reel]:
    """What is waiting, best first."""
    with session_scope() as session:
        query = (
            select(Reel)
            .where(Reel.state.in_(("found", "ready")))
            .order_by(Reel.ups.desc(), Reel.id.asc())
        )
        if limit:
            query = query.limit(limit)
        rows = list(session.execute(query).scalars())
        for row in rows:
            session.expunge(row)
        return rows


def prepare(reel_id: int) -> Path:
    """Download one reel and put the badge on it. Returns the finished file.

    Done when the reel is about to go out rather than when it joins the queue:
    most of what enters the queue is beaten before its turn comes, and
    downloading fifteen a day to post eight pays twice to throw half away.
    """
    import yt_dlp

    from core.ytdlp import base_options, run

    with session_scope() as session:
        reel = session.get(Reel, reel_id)
        if reel is None:
            raise ValueError(f"no reel {reel_id}")
        permalink, external_id = reel.permalink, reel.external_id

    raw_dir = Path(settings.work_dir) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    def download(options: dict[str, Any]) -> None:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([permalink])

    run(download, base_options(
        # bestvideo+bestaudio, merged. Reddit serves the two as separate DASH
        # files, and anything that resolves to a video-only stream is the
        # silent-clip trap: it plays fine and nobody notices until it is up.
        format="bestvideo*+bestaudio/best",
        merge_output_format="mp4",
        outtmpl=str(raw_dir / f"{external_id}.%(ext)s"),
        noplaylist=True,
    ))

    found = sorted(raw_dir.glob(f"{external_id}.*"))
    if not found:
        raise RuntimeError(f"nothing downloaded for {external_id}")

    branded = brand.apply(found[0], Path(settings.work_dir) / "branded")
    # The raw copy has done its job and is half the disk usage.
    for stray in found:
        stray.unlink(missing_ok=True)

    # Into storage, not just onto this disk. web, worker and scheduler are
    # three separate containers with three separate filesystems, so a file the
    # worker branded is a file the dashboard cannot open - it looks up
    # local_path, finds nothing at it, and reports a video that was never
    # downloaded. Storage is the only place all three can see.
    key: str | None = f"reels/{external_id}.mp4"
    try:
        get_storage().put_file(branded, key)
    except Exception as exc:  # noqa: BLE001 - the local copy still posts fine
        log.warning("harvest: could not store %s (%s)", key, exc)
        key = None

    with session_scope() as session:
        reel = session.get(Reel, reel_id)
        if reel is not None:
            reel.local_path = str(branded)
            reel.storage_key = key
            reel.state = "ready"
    return branded


LAST_RUN_KEY = "putitupp:last-run"


def note_run(summary: dict[str, Any]) -> None:
    """Leave a record of the last pass where the dashboard can read it.

    A full pass reads twenty-five rooms and looks each candidate up
    individually, so it takes minutes. Without this, "still running" and "did
    nothing at all" look exactly the same from the browser - which is the
    state the whole thing was in when it was first switched on.

    Redis rather than a table: it is a status line, not a record, and it is
    already there for the queue.
    """
    if not settings.has_redis:
        return
    try:
        import json

        from worker.queue import get_redis

        get_redis().set(LAST_RUN_KEY, json.dumps(
            summary | {"at": datetime.now(UTC).isoformat()}))
    except Exception as exc:  # noqa: BLE001 - a status line is not worth a run
        log.warning("harvest: could not record the run (%s)", exc)


def last_run() -> dict[str, Any] | None:
    """What the last pass did, or None if none has finished here."""
    if not settings.has_redis:
        return None
    try:
        import json

        from worker.queue import get_redis

        raw = get_redis().get(LAST_RUN_KEY)
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def run(post: bool = True) -> dict[str, Any]:
    """One daily pass: read the rooms, re-rank, then send the top out."""
    began = datetime.now(UTC)
    found = discover()
    added = admit(found)
    pushed = trim()

    summary: dict[str, Any] = {
        "found": len(found), "added": added, "pushed_out": pushed,
        "queue": len(queued()), "posted": 0, "failed": 0,
    }

    if post:
        from worker.tasks.publish import post_due

        result = post_due()
        summary["posted"] = result["posted"]
        summary["failed"] = result["failed"]
        summary["queue"] = len(queued())

    summary["seconds"] = round((datetime.now(UTC) - began).total_seconds(), 1)
    note_run(summary)
    log.info("harvest: %s", summary)
    return summary
