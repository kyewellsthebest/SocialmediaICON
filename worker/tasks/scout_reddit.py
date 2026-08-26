"""Stage 0b - find clippable video on Reddit.

Reddit's search is site-wide, so a keyword reaches every subreddit at once.
That matters for this niche: the metal detecting video worth clipping is as
likely to be in r/Damnthatsinteresting as in r/metaldetecting, and browsing
subreddits one at a time would miss it.

Unlike YouTube there are no view counts, so ranking runs on votes: how fast a
post gathered them, how one-sided the vote was, and how much argument it
provoked per upvote. The last is the interesting one - a post people merely
approved of has no moment in it, and a post people argued about does.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from core import reddit
from core.config import settings
from core.db import session_scope
from core.language import looks_english
from core.models import Niche, TrackedVideo

log = logging.getLogger(__name__)

PLATFORM = "reddit"


def wanted(post: reddit.Post) -> bool:
    """Whether a post is worth tracking.

    Three gates, each removing something that cannot produce a clip: video too
    short to cut down, too few votes to have demonstrated anything, and titles
    in a language the audience does not read.
    """
    if post.over_18:
        return False
    if post.duration_s is not None and post.duration_s < settings.reddit_min_duration_s:
        return False
    if post.ups < settings.reddit_min_upvotes:
        return False
    if settings.scout_language.strip().lower() == "en" and not looks_english(post.title):
        return False
    return True


def score_post(post: reddit.Post) -> float:
    """0-100, on the same scale as the YouTube score so one table can hold both.

    Reddit gives votes rather than views, so the shape differs but the meaning
    matches: pace, approval, and how much conversation it caused.
    """
    # Upvotes per hour, compressed. 200/hour is a front-page post.
    pace = min(1.0, (post.ups / max(post.age_hours, 1) / 200) ** 0.5)

    # Upvote ratio spans about 0.5 to 1.0 in practice; stretch that to 0-1.
    approval = max(0.0, ((post.upvote_ratio or 0.75) - 0.5) / 0.5)

    # Comments per upvote. 0.1 is a lot of discussion for the votes received,
    # which is the signal that something in the video is worth arguing about.
    discussion = min(1.0, (post.num_comments / max(post.ups, 1)) / 0.1)

    raw = 0.40 * pace + 0.20 * approval + 0.40 * discussion

    # Same taper as YouTube: a month at full marks, then easing off.
    recency = 1.0 if post.age_hours <= 720 else max(0.55, (720 / post.age_hours) ** 0.3)
    return round(100 * raw * recency, 1)


def _niche_id(session, niche_name: str | None) -> int | None:
    if not niche_name:
        return None
    niche = session.execute(select(Niche).where(Niche.name == niche_name)).scalar_one_or_none()
    if niche is None:
        niche = Niche(name=niche_name, config={})
        session.add(niche)
        session.flush()
    return niche.id


def scout(keywords: list[str] | None = None, niche_name: str | None = None) -> list[int]:
    """Search, filter, score and store. Returns the tracked_video ids touched."""
    if not settings.has_reddit:
        log.warning("Reddit credentials are not set - skipping")
        return []

    keywords = keywords or settings.reddit_search_terms
    if not keywords:
        log.warning("no Reddit keywords configured - nothing to look for")
        return []

    niche_name = niche_name or settings.default_niche
    now = datetime.now(UTC)

    found: dict[str, reddit.Post] = {}
    for keyword in keywords:
        try:
            posts = reddit.search(keyword, time_filter=settings.reddit_time_filter)
        except Exception as exc:  # noqa: BLE001 - one keyword must not kill the run
            log.warning("reddit search failed for %r: %s", keyword, exc)
            continue
        for post in posts:
            # The same video surfaces under several keywords; keep it once.
            if post.external_id and post.external_id not in found:
                found[post.external_id] = post

    keepers = [p for p in found.values() if wanted(p)]
    log.info("reddit: %d unique posts, %d passed the gate", len(found), len(keepers))
    if not keepers:
        return []

    touched: list[int] = []
    with session_scope() as session:
        niche_id = _niche_id(session, niche_name)

        for post in keepers:
            video = session.execute(
                select(TrackedVideo).where(
                    TrackedVideo.platform == PLATFORM,
                    TrackedVideo.external_id == post.external_id,
                )
            ).scalar_one_or_none()

            if video is None:
                video = TrackedVideo(platform=PLATFORM, external_id=post.external_id, url=post.url)
                session.add(video)

            video.niche_id = niche_id
            video.title = post.title
            video.channel_title = f"r/{post.subreddit}"
            video.channel_id = post.subreddit
            video.published_at = datetime.fromtimestamp(post.created_utc, tz=UTC)
            video.duration_s = post.duration_s
            # Votes are not views, but they are the closest thing Reddit has
            # and the dashboard column has to hold something comparable.
            video.views = post.ups
            video.comments = post.num_comments
            video.velocity_vph = post.ups / max(post.age_hours, 1)
            video.like_rate = post.upvote_ratio
            video.score = score_post(post)
            video.last_checked_at = now
            if video.status is None:
                video.status = "new"

            session.flush()
            touched.append(video.id)

    log.info("reddit scout stored %d videos", len(touched))
    return touched


def run() -> int:
    """Scheduler entrypoint."""
    return len(scout())
