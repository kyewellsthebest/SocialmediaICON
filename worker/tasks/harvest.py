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

    with session_scope() as session:
        reel = session.get(Reel, reel_id)
        if reel is not None:
            reel.local_path = str(branded)
            reel.state = "ready"
    return branded


def run(post: bool = True) -> dict[str, Any]:
    """One daily pass: read the rooms, re-rank, then send the top out."""
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

    log.info("harvest: %s", summary)
    return summary
