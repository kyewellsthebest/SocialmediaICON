"""Send the top of the queue out.

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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from core.config import settings
from core.db import session_scope
from core.models import Account, Reel, ReelPost
from core.publishers import PublishRequest, get_publisher
from core.storage import get_storage

log = logging.getLogger(__name__)


def platforms() -> list[str]:
    """Every active account's platform, deduped, in a stable order."""
    with session_scope() as session:
        rows = session.execute(
            select(Account.platform)
            .where(Account.status == "active")
            .distinct()
            .order_by(Account.platform)
        ).scalars()
        return list(rows)


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
        platforms=only or platforms(),
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


def post_due(limit: int | None = None) -> dict[str, Any]:
    """Send out the best `limit` waiting reels, strongest first."""
    limit = settings.post_per_run if limit is None else limit
    if not settings.autopost_enabled:
        log.info("publish: AUTOPOST_ENABLED is off, nothing sent")
        return {"posted": 0, "failed": 0, "skipped": "autopost disabled"}

    from worker.tasks.harvest import queued

    posted = failed = 0
    for reel in queued(limit=limit):
        try:
            results = publish_one(reel.id)
        except Exception as exc:  # noqa: BLE001 - one bad reel is not the run
            log.warning("publish: reel %s failed (%s)", reel.external_id, exc)
            with session_scope() as session:
                row = session.get(Reel, reel.id)
                if row is not None:
                    row.note = str(exc)[:400]
            failed += 1
            continue
        if any(r.status == "posted" for r in results):
            posted += 1
            log.info("publish: %s out (%d ups) %s",
                     reel.external_id, reel.ups, reel.caption[:60])
        else:
            failed += 1

    return {"posted": posted, "failed": failed}
