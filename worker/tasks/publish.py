"""Stage 7 - post an approved clip.

Which backend actually runs is `PUBLISHER` in the environment: manual (default,
nothing is posted), youtube (your own OAuth app, free), meta (your own Meta app
- Instagram Reels, Threads, Facebook Reels, free), or upload_post (a reseller,
the only route to Snapchat and the way past TikTok's audit).

Nothing is posted unless a human approved the clip first, and the approval flow
does not change when you switch backends.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from core.config import settings
from core.db import session_scope
from core.models import Account, Clip, Post
from core.publishers import PublishRequest, get_publisher
from core.storage import get_storage
from worker.tasks.common import work_dir_for

log = logging.getLogger(__name__)


def platforms_for(clip_id: int) -> list[str]:
    """Every active account's platform, deduped, in a stable order."""
    with session_scope() as session:
        rows = (
            session.query(Account.platform)
            .filter(Account.status == "active")
            .distinct()
            .order_by(Account.platform)
            .all()
        )
    return [row[0] for row in rows]


def run(clip_id: int, platforms: list[str] | None = None) -> list[int]:
    """Publish one approved clip. Returns the post ids created."""
    with session_scope() as session:
        clip = session.get(Clip, clip_id)
        if clip is None:
            raise ValueError(f"no clip {clip_id}")
        if clip.status not in ("approved", "posted"):
            raise ValueError(
                f"clip {clip_id} is {clip.status}, not approved - publishing needs a human first"
            )
        storage_key = clip.storage_key
        title = clip.title or f"clip-{clip_id}"
        hashtags = list(clip.hashtags or [])
        candidate_id = clip.candidate_id

    if not storage_key:
        raise ValueError(f"clip {clip_id} has no stored file")

    targets = platforms or platforms_for(clip_id)
    if not targets:
        log.warning("clip %s has no target platforms - add an account first", clip_id)
        return []

    local = work_dir_for(candidate_id) / Path(storage_key).name
    if not local.exists():
        get_storage().get_file(storage_key, local)

    publisher = get_publisher()
    log.info("publishing clip %s to %s via %s", clip_id, ", ".join(targets), publisher.name)

    results = publisher.publish(
        PublishRequest(
            clip_path=local,
            title=title,
            description=title,
            hashtags=hashtags,
            platforms=targets,
            storage_key=storage_key,
        )
    )

    post_ids: list[int] = []
    now = datetime.now(UTC)
    with session_scope() as session:
        for result in results:
            account = (
                session.query(Account)
                .filter(Account.platform == result.platform, Account.status == "active")
                .first()
            )
            post = Post(
                clip_id=clip_id,
                account_id=account.id if account else None,
                platform=result.platform,
                platform_post_id=result.post_id,
                platform_url=result.url,
                posted_at=now if result.ok else None,
                status="posted" if result.ok else "failed",
                error=result.error,
            )
            session.add(post)
            session.flush()
            post_ids.append(post.id)

        if any(r.ok for r in results):
            session.get(Clip, clip_id).status = "posted"

    for result in results:
        if result.ok:
            log.info("posted clip %s to %s (%s)", clip_id, result.platform, result.post_id)
        else:
            log.warning("clip %s failed on %s: %s", clip_id, result.platform, result.error)

    return post_ids


def autopost(limit: int | None = None) -> list[int]:
    """Publish up to `limit` approved clips. Called by the scheduler.

    Off unless AUTOPOST_ENABLED is set: posting on a schedule is a decision about
    account risk, not a default.
    """
    if not settings.autopost_enabled:
        return []
    limit = limit or settings.autopost_per_day

    with session_scope() as session:
        clip_ids = [
            row[0]
            for row in session.query(Clip.id)
            .filter(Clip.status == "approved")
            .order_by(Clip.id.asc())
            .limit(limit)
            .all()
        ]

    published: list[int] = []
    for clip_id in clip_ids:
        try:
            published.extend(run(clip_id))
        except Exception as exc:  # noqa: BLE001 - one clip must not stop the batch
            log.exception("autopost failed for clip %s: %s", clip_id, exc)
    return published
