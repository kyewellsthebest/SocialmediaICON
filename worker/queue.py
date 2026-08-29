"""Redis + RQ wiring.

Queues are per stage so a long render cannot starve ingest, and so worker
counts can be tuned per stage later. `python -m worker.queue` runs a worker
over all of them.
"""

from __future__ import annotations

import logging
import sys
import time
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
    # The live watcher. Its own queue because the job runs for hours and
    # would otherwise block every render behind it.
    "live",
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


MISSING_REDIS = """FATAL: REDIS_URL is not set, and the worker has nothing to take jobs from.

       In Railway: add a Redis database, then set this service's variable to:

           REDIS_URL=${{Redis.REDIS_URL}}

       Set it on every service, not just this one."""


def redis_diagnosis() -> str:
    """Say *why* there is no Redis URL, not merely that there is none.

    "Not set" is three different faults wearing one message: the variable was
    never added, it was added but resolved to an empty string because the
    reference names a service that does not exist, or it is present in the
    environment and something between there and here is dropping it. Each has
    a different fix and they are indistinguishable from the outside, which is
    how an afternoon disappears.

    Names only, never values - these are credentials.
    """
    import os

    raw = os.environ.get("REDIS_URL")
    lines = []

    if raw is None:
        lines.append("REDIS_URL is absent from this process's environment entirely.")
        lines.append("The variable was never added to THIS service - Railway does not")
        lines.append("share variables between services, so adding it to web does not")
        lines.append("give it to worker or scheduler.")
    elif not raw.strip():
        lines.append("REDIS_URL is present but EMPTY.")
        lines.append("That is what a Railway reference looks like when it cannot")
        lines.append("resolve - check the service name in ${{...}} matches exactly,")
        lines.append("including its capitals.")
    elif raw.startswith("${{"):
        lines.append(f"REDIS_URL is the literal text {raw!r}.")
        lines.append("Railway did not expand the reference; retype it in the Railway")
        lines.append("variables editor rather than pasting it as raw text.")
    else:
        lines.append("REDIS_URL *is* set in the environment, so this is our bug, not")
        lines.append("a configuration one. Please send this line to the developer.")

    related = sorted(
        k for k in os.environ if "REDIS" in k.upper() or "DATABASE" in k.upper()
    )
    lines.append("")
    lines.append(f"Related variables this process can see: {', '.join(related) or 'none'}")
    return "\n       ".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    names = argv or list(QUEUE_NAMES)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # Checked before anything else: a missing URL is a config mistake, and a
    # stack trace repeated every restart buries the one line that says so.
    if not settings.has_redis:
        print(MISSING_REDIS, file=sys.stderr)
        print(f"\n       {redis_diagnosis()}\n", file=sys.stderr)
        # Crash-looping every two seconds buries the explanation under a
        # thousand copies of itself. Pause so the message stays readable in
        # the log viewer, and so a restart storm does not bill for nothing.
        time.sleep(15)
        return 1

    from rq import Worker

    log.info("starting worker on queues: %s", ", ".join(names))
    Worker([get_queue(n) for n in names], connection=get_redis()).work(with_scheduler=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
