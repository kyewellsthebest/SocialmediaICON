"""How often this app is allowed to touch Meta's publisher, counted here.

The failure this exists for: Instagram refused everything with "Application
request limit reached" on a day four things had been posted. Four is not
twenty-five, and the gap was never visible from this side, because the only
number anyone had was Meta's - reported after the fact and never itemised.

Two things were happening, and they need separating.

*Posts* are what the schedule decides: five reels an hour apart from seven,
and five carousels half an hour behind each. Ten a day, which is well under
Meta's twenty-five and always was.

*Calls* are what those posts cost, and they are not the same number. A reel is
a container and a publish. A carousel is three containers and a publish,
because each slide is fetched separately and the pair is then tied together.
And a failed attempt costs its containers whether or not anything is published
- so a carousel retried four times has asked Meta to hold twelve files to show
nothing for it. That is the arithmetic that turned four posts into a spent
limit, and no cap on posts would have caught it, because the posts were never
the problem.

So the budget is counted in attempts - one per post flow, successful or not -
and it is checked before the first request rather than discovered at the last.
Every call is recorded either way, so "Meta says 25 and we say 9" is a fact to
act on rather than a theory.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from core.config import settings
from core.db import session_scope
from core.models import GraphCall

log = logging.getLogger(__name__)

#: A publish is one per post flow, so counting publishes counts attempts -
#: including the ones that failed, which is the whole point.
ATTEMPT_KINDS = ("publish",)


def record(platform: str, kind: str, ok: bool = True, detail: str | None = None) -> None:
    """Note one call. Never raises: a ledger must not break the thing it counts."""
    try:
        with session_scope() as session:
            session.add(GraphCall(
                at=datetime.now(UTC), platform=platform, kind=kind, ok=ok,
                detail=(detail or "")[:400] or None,
            ))
    except Exception as exc:  # noqa: BLE001 - counting is not the job
        log.debug("could not record a %s call: %s", kind, exc)


def _since() -> datetime:
    """The start of the rolling window Meta measures, which is 24 hours."""
    return datetime.now(UTC) - timedelta(hours=24)


def spent(platform: str = "instagram") -> dict[str, int]:
    """What the last 24 hours cost, by kind. Zeros if nothing is recorded."""
    out = {"container": 0, "publish": 0, "poll": 0, "read": 0}
    try:
        with session_scope() as session:
            rows = session.execute(
                select(GraphCall.kind, func.count(GraphCall.id))
                .where(GraphCall.at >= _since(), GraphCall.platform.startswith(platform))
                .group_by(GraphCall.kind)
            ).all()
    except Exception as exc:  # noqa: BLE001
        log.debug("could not read the ledger: %s", exc)
        return out
    for kind, count in rows:
        out[kind] = int(count)
    out["total"] = sum(v for k, v in out.items() if k != "total")
    return out


def attempts_today(platform: str = "instagram") -> int:
    return spent(platform).get("publish", 0)


def cap() -> int:
    """How many post flows a day, before this stops asking.

    Derived from the schedule rather than set beside it, so raising POST_PER_DAY
    does not silently leave the cap behind and quietly stop posting in the
    afternoon. The headroom is for genuine retries - a storage blip on one clip
    should not eat the evening's slots.
    """
    if settings.instagram_daily_attempts:
        return settings.instagram_daily_attempts
    posts = settings.post_per_day * (2 if settings.carousel_enabled else 1)
    return posts + settings.publish_headroom


def allowed(platform: str = "instagram") -> tuple[bool, str]:
    """Whether one more post flow may start, and why not if it may not.

    Checked before the first container, not after the last refusal. A limit
    discovered from Meta has already been spent finding out.
    """
    if platform not in ("instagram", "instagram_carousel"):
        return True, ""
    used, limit = attempts_today(), cap()
    if used < limit:
        return True, ""
    return False, (
        f"this app's own daily cap is spent: {used} of {limit} post attempts in "
        f"the last 24 hours. It refills as the oldest ones age out. Raise "
        f"INSTAGRAM_DAILY_ATTEMPTS if the schedule genuinely needs more."
    )


def ledger(platform: str = "instagram") -> dict[str, object]:
    """The readout: what we spent, what we allow, and what it bought."""
    used = spent(platform)
    posted = 0
    try:
        with session_scope() as session:
            posted = int(session.execute(
                select(func.count(GraphCall.id)).where(
                    GraphCall.at >= _since(),
                    GraphCall.platform.startswith(platform),
                    GraphCall.kind == "publish",
                    GraphCall.ok.is_(True),
                )
            ).scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not read the ledger: %s", exc)
    return {
        "window_hours": 24,
        "posted": posted,
        "attempts": used.get("publish", 0),
        "cap": cap(),
        "calls": used,
    }
