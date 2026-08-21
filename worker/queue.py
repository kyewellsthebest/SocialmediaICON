"""Redis + RQ wiring.

Queues are per stage so a long render cannot starve ingest, and so worker
counts can be tuned per stage later. `python -m worker.queue` runs a worker
over all of them.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

from core.config import settings

log = logging.getLogger(__name__)

QUEUE_NAMES = (
    "ingest",
    "transcribe",
    "detect",
    "rank",
    "render",
    "publish",
    "metrics",
)

_redis: Any = None


def get_redis() -> Any:
    global _redis
    if _redis is None:
        if not settings.has_redis:
            raise RuntimeError("REDIS_URL is not set")
        from redis import Redis

        _redis = Redis.from_url(settings.redis_url)
    return _redis


def get_queue(name: str) -> Any:
    if name not in QUEUE_NAMES:
        raise ValueError(f"unknown queue {name!r}; expected one of {QUEUE_NAMES}")
    from rq import Queue

    return Queue(name, connection=get_redis())


def enqueue(name: str, func: Callable[..., Any] | str, *args: Any, **kwargs: Any) -> Any:
    """Enqueue a stage, or run nothing if Redis is not configured.

    The Phase 1 CLI drives the stages itself, so a missing Redis is a warning,
    not a crash.
    """
    if not settings.has_redis:
        log.warning("REDIS_URL not set - skipping enqueue of %s on %s", func, name)
        return None
    timeout = kwargs.pop("job_timeout", 3600)
    return get_queue(name).enqueue(func, *args, job_timeout=timeout, **kwargs)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    names = argv or list(QUEUE_NAMES)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from rq import Worker

    log.info("starting worker on queues: %s", ", ".join(names))
    Worker([get_queue(n) for n in names], connection=get_redis()).work(with_scheduler=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
