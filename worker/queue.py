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

# Two queues, because there are two jobs and they have different shapes: one
# is a long download-and-encode pass, the other is a handful of API calls.
QUEUE_NAMES = (
    "harvest",
    "publish",
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
    """Why there is no Redis URL. See core.envcheck for the reasoning."""
    from core.envcheck import explain

    return explain("REDIS_URL", service_hint="Redis").replace("\n", "\n       ")


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




def status() -> dict[str, Any]:
    """What the queue is actually doing, and whether anything is listening.

    The failure this exists for: a job is accepted by Redis, nothing is
    running to take it, and the dashboard says "queued, give it a few
    minutes" forever. Redis accepting work is not the same as work happening,
    and from a browser those look identical.

    So this reports the one number that settles it - how many workers are
    listening - alongside what is waiting, running and failed, and the
    traceback of the last failure. A job that died on the worker is otherwise
    invisible: RQ files it in a registry nobody reads.
    """
    if not settings.has_redis:
        return {"redis": False, "why": "REDIS_URL is not set on this service"}

    try:
        from rq import Queue, Worker

        connection = get_redis()
        connection.ping()
    except Exception as exc:  # noqa: BLE001 - the point is to report, not raise
        return {"redis": False, "why": f"{type(exc).__name__}: {exc}"}

    listening: set[str] = set()
    workers = 0
    try:
        for worker in Worker.all(connection=connection):
            workers += 1
            listening.update(q.name for q in worker.queues)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not list workers (%s)", exc)

    out: dict[str, Any] = {
        "redis": True,
        "workers": workers,
        "listening_to": sorted(listening),
        "queues": {},
        "last_error": None,
    }

    for name in QUEUE_NAMES:
        queue = Queue(name, connection=connection)
        failed = queue.failed_job_registry
        out["queues"][name] = {
            "waiting": queue.count,
            "running": len(queue.started_job_registry),
            "failed": len(failed),
        }
        if out["last_error"] is None and len(failed):
            try:
                from rq.job import Job

                job = Job.fetch(failed.get_job_ids()[-1], connection=connection)
                out["last_error"] = {
                    "queue": name,
                    "at": job.ended_at.isoformat() if job.ended_at else None,
                    # The last lines are the ones that say what happened; the
                    # top of a traceback is RQ's own plumbing.
                    "traceback": "\n".join(
                        (job.exc_info or "no traceback recorded").strip().splitlines()[-12:]
                    ),
                }
            except Exception as exc:  # noqa: BLE001
                out["last_error"] = {"queue": name, "traceback": f"unreadable: {exc}"}

    if workers == 0:
        out["why"] = (
            "Redis is up and accepting jobs, but no worker is listening to "
            "them. Nothing will ever run. Check that the `worker` service is "
            "deployed and not crash-looping - its deploy log says why."
        )
    elif not (set(QUEUE_NAMES) & listening):
        out["why"] = (
            f"{workers} worker(s) are running but listening to "
            f"{sorted(listening) or 'nothing'} rather than "
            f"{list(QUEUE_NAMES)}. That is an old deploy: the queue names "
            f"changed, so redeploy the worker service."
        )
    return out
