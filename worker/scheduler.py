"""The 24/7 heartbeat.

A third Railway process next to `web` and `worker`. It owns nothing except
timing: every minute it asks which periodic jobs are due and puts them on the
queue for the worker to run.

Last-run times live in Redis, so a redeploy does not re-fire everything, and two
schedulers cannot double-fire the same job.
"""

from __future__ import annotations

import logging
import math
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from core.config import settings
from worker.queue import MISSING_REDIS, enqueue, get_redis

log = logging.getLogger("scheduler")

TICK_S = 60
KEY_PREFIX = "clipengine:sched:"


@dataclass
class Job:
    name: str
    queue: str
    every_minutes: int
    func: Callable[..., object]
    enabled: bool = True


def _jobs() -> list[Job]:
    from worker.tasks.collect_metrics import collect_due
    from worker.tasks.publish import autopost
    from worker.tasks.refresh_tokens import run as refresh_tokens
    from worker.tasks.scout import run as scout_run
    from worker.tasks.scout_reddit import run as scout_reddit

    return [
        Job(
            name="scout",
            queue="metrics",
            every_minutes=settings.scout_interval_minutes,
            func=scout_run,
            enabled=settings.scout_enabled and settings.has_youtube_read,
        ),
        Job(
            name="scout_reddit",
            queue="metrics",
            every_minutes=settings.scout_interval_minutes,
            func=scout_reddit,
            # No credentials needed: the public endpoint answers without an app.
            enabled=settings.scout_enabled and bool(settings.reddit_search_terms),
        ),
        Job(
            name="metrics",
            queue="metrics",
            every_minutes=settings.metrics_interval_minutes,
            func=collect_due,
        ),
        Job(
            name="refresh_tokens",
            queue="metrics",
            # Meta tokens die at 60 days and cannot be revived afterwards, so
            # refresh at a quarter of that: three failed runs still leave a
            # fortnight of slack.
            every_minutes=settings.token_refresh_interval_days * 24 * 60,
            func=refresh_tokens,
            enabled=settings.has_meta_tokens,
        ),
        Job(
            name="autopost",
            queue="publish",
            # Spread the daily allowance across the day rather than dumping it.
            every_minutes=max(30, math.floor(24 * 60 / max(1, settings.autopost_per_day))),
            func=autopost,
            enabled=settings.autopost_enabled,
        ),
    ]


def _due(job: Job, now: float) -> bool:
    """True if the job has not run inside its interval. Claims the slot."""
    if not settings.has_redis:
        return True
    redis = get_redis()
    key = f"{KEY_PREFIX}{job.name}"
    last = redis.get(key)
    if last is not None and now - float(last) < job.every_minutes * 60:
        return False
    # Set before enqueueing: a double-fire is worse than a skipped tick.
    redis.set(key, now)
    return True


def tick(now: float | None = None) -> list[str]:
    """Run one scheduling pass. Returns the names of the jobs fired."""
    now = now or time.time()
    fired: list[str] = []

    for job in _jobs():
        if not job.enabled:
            continue
        if not _due(job, now):
            continue
        try:
            if settings.has_redis:
                enqueue(job.queue, job.func)
            else:
                log.info("no redis - running %s inline", job.name)
                job.func()
            fired.append(job.name)
            log.info("fired %s (every %d min)", job.name, job.every_minutes)
        except Exception as exc:  # noqa: BLE001 - a bad job must not kill the loop
            log.exception("failed to fire %s: %s", job.name, exc)

    return fired


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    running = True

    def stop(signum, frame):  # noqa: ANN001, ARG001 - signal handler signature
        nonlocal running
        log.info("shutting down")
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # Without Redis the scheduler runs jobs inline, which is fine on a laptop
    # and wrong in production: renders would run inside the scheduler process
    # and one slow job would stall every other schedule.
    if not settings.has_redis:
        if settings.is_prod:
            print(MISSING_REDIS, file=sys.stderr)
            return 1
        log.warning("REDIS_URL is not set - running jobs inline (development only)")

    enabled = [j.name for j in _jobs() if j.enabled]
    log.info("scheduler up. active jobs: %s", ", ".join(enabled) or "none")

    while running:
        tick()
        for _ in range(TICK_S):
            if not running:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
