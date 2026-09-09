"""Find short gym video on Reddit and download it, ready to post as it is.

The clipping pipeline this replaces asked "where in this hour is the moment".
This asks nothing of the kind: somebody already decided the moment was worth
posting, and several thousand people already agreed by upvoting it. The video
arrives finished, under a minute, with a title its author wrote. Nothing is
cut, nothing is judged, nothing is captioned - which is why this path needs no
model, no transcript and no API key beyond a free Reddit app.

What it does need is discipline about two things, because there is no editor
downstream to catch either: nothing adult, and nothing longer than short-form.
Both live in core.reddit.postable, on the one function every path goes through.

Attribution is written beside every download rather than left implicit. A
repost page that offers to take a video down on request has to be able to
answer "which post was this?" months later, and the answer has to survive the
video being renamed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from core import reddit
from core.config import settings
from core.db import session_scope
from core.models import TrackedVideo

log = logging.getLogger(__name__)

PLATFORM = "reddit"

#: Searched inside each configured subreddit. Deliberately plain: the room is
#: doing the topic filtering, so the term only has to find the video posts
#: within it rather than describe the niche.
TERMS = ("fail", "PR", "lift", "form check", "gym")


def find(
    terms: list[str] | None = None,
    rooms: list[str] | None = None,
    *,
    sort: str | None = None,
    time_filter: str | None = None,
) -> list[reddit.Post]:
    """Every postable video the search turns up, best first, no duplicates.

    A post can match several terms and sit in several rooms, so the same video
    arrives more than once; the first sighting wins and the rest are dropped
    before anything is downloaded.
    """
    terms = terms or list(TERMS)
    rooms = rooms if rooms is not None else settings.reddit_rooms
    sort = sort or "top"
    time_filter = time_filter or settings.reddit_time_filter

    seen: dict[str, reddit.Post] = {}
    refused: dict[str, int] = {}

    client = reddit.make_client()
    try:
        for room in rooms or [None]:
            for term in terms:
                try:
                    found = reddit.search(
                        term, sort=sort, time_filter=time_filter,
                        client=client, subreddit=room,
                    )
                except reddit.RedditError as exc:
                    # One bad room or term is not the whole run. A dead
                    # subreddit name is a typo in config, not an outage.
                    log.warning("reddit: %s in r/%s failed (%s)", term, room, exc)
                    continue
                for post in found:
                    if post.external_id in seen:
                        continue
                    ok, why = reddit.postable(post)
                    if not ok:
                        refused[why.split()[-1] if why else "?"] = (
                            refused.get(why.split()[-1] if why else "?", 0) + 1)
                        continue
                    seen[post.external_id] = post
    finally:
        client.close()

    log.info(
        "reddit: %d postable from %d rooms x %d terms (refused: %s)",
        len(seen), len(rooms or [1]), len(terms),
        ", ".join(f"{n} {k}" for k, n in refused.items()) or "none",
    )
    return sorted(seen.values(), key=lambda p: p.ups, reverse=True)


def unseen(posts: list[reddit.Post]) -> list[reddit.Post]:
    """The ones not downloaded before.

    Against the database rather than the disk: Railway's filesystem does not
    survive a redeploy, and a page that reposts its own back catalogue every
    time it restarts is worse than one that posts nothing.
    """
    if not posts:
        return []
    ids = [p.external_id for p in posts]
    with session_scope() as session:
        known = set(
            session.execute(
                select(TrackedVideo.external_id).where(
                    TrackedVideo.platform == PLATFORM,
                    TrackedVideo.external_id.in_(ids),
                )
            ).scalars()
        )
    fresh = [p for p in posts if p.external_id not in known]
    log.info("reddit: %d of %d are new", len(fresh), len(posts))
    return fresh


def remember(post: reddit.Post) -> None:
    """Note that this one has been taken, so it is not taken again."""
    with session_scope() as session:
        session.add(
            TrackedVideo(
                platform=PLATFORM,
                external_id=post.external_id,
                url=post.url,
                title=post.title,
                channel_title=post.author,
                duration_s=post.duration_s,
                likes=post.ups,
                comments=post.num_comments,
                published_at=datetime.fromtimestamp(post.created_utc, UTC)
                if post.created_utc else None,
            )
        )


def fetch(post: reddit.Post, into: Path) -> Path:
    """Download the video, with its sound.

    yt-dlp is handed the permalink rather than the fallback_url because Reddit
    serves video and audio as separate DASH files. The fallback is the video
    track alone, and a page of silent gym videos is the classic way this goes
    wrong - it downloads, it plays, and nobody notices until it is posted.
    """
    import yt_dlp

    from core.ytdlp import base_options, run

    into.mkdir(parents=True, exist_ok=True)
    target = into / f"{post.external_id}.mp4"

    def download(options: dict[str, Any]) -> None:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([post.video_url])

    run(download, base_options(
        # bestvideo+bestaudio, merged. Anything that resolves to a
        # video-only stream is the silent-clip trap.
        format="bestvideo*+bestaudio/best",
        merge_output_format="mp4",
        outtmpl=str(into / f"{post.external_id}.%(ext)s"),
        noplaylist=True,
    ))

    if not target.exists():
        found = sorted(into.glob(f"{post.external_id}.*"))
        if not found:
            raise RuntimeError(f"nothing downloaded for {post.external_id}")
        target = found[0]
    return target


def credit(post: reddit.Post, video: Path) -> Path:
    """Write who made it and where it came from, beside the file.

    A page that offers to remove a video on request has to be able to answer
    "which post was this?" long after the fact, and a filename cannot carry
    that. The caption is stored here too, verbatim, because it is the author's
    own words about their own video and rewriting it would be worse.
    """
    beside = video.with_suffix(".json")
    beside.write_text(
        json.dumps(
            {
                "caption": post.title,
                "source": post.url,
                "author": f"u/{post.author}" if post.author else None,
                "subreddit": f"r/{post.subreddit}",
                "taken_at": datetime.now(UTC).isoformat(),
                "post": asdict(post),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return beside


def harvest(limit: int = 10) -> list[dict[str, Any]]:
    """Find, download and hold. Returns what was taken."""
    into = Path(settings.work_dir) / "reddit"
    taken: list[dict[str, Any]] = []

    for post in unseen(find())[:limit]:
        try:
            video = fetch(post, into)
        except Exception as exc:  # noqa: BLE001 - one bad download is not a run
            log.warning("reddit: could not download %s (%s)", post.external_id, exc)
            continue
        credit(post, video)
        remember(post)
        taken.append(
            {
                "id": post.external_id,
                "caption": post.title,
                "source": post.url,
                "author": post.author,
                "subreddit": post.subreddit,
                "duration_s": post.duration_s,
                "ups": post.ups,
                "path": str(video),
            }
        )
        log.info("reddit: took %s (%.0fs, %d ups) %s",
                 post.external_id, post.duration_s or 0, post.ups, post.title[:60])

    return taken
