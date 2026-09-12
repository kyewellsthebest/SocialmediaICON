"""The daily run, executed inside the web service.

There used to be three Railway services: a web one, a worker taking jobs off a
Redis queue, and a scheduler putting them on it. Two of them existed to carry
a clipping pipeline that no longer exists, and what they left behind was a
failure mode with no symptom - the dashboard would accept a run, Redis would
accept the job, and nothing would ever pick it up, because the worker was not
deployed. "Queued" and "queued and abandoned" look identical from a browser.

This is one job, once a day, for about ten minutes. A thread is enough, and
one service cannot fail to be running while its own dashboard answers.

Two things keep it honest:

* **One run at a time.** A lock, not a queue. A second request while a run is
  in flight is told so rather than starting a duplicate pass over the same
  rooms - which would double every download and race on the same rows.
* **Every run is recorded**, finished or failed, with the traceback. A run
  that throws inside a thread is otherwise completely silent: nobody is
  waiting on it and there is no response for it to fail.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from core.config import settings
from core.db import session_scope
from core.models import RunLog

log = logging.getLogger(__name__)

#: Held for the whole of a run. Not a queue: two passes over the same rooms
#: would double every download and race each other on the same rows.
_running = threading.Lock()
_started: datetime | None = None

TICK_S = 60.0


def in_flight() -> datetime | None:
    """When the run currently going started, or None if nothing is running."""
    return _started if _running.locked() else None


def last() -> RunLog | None:
    """The most recent run, finished or failed."""
    with session_scope() as session:
        row = session.execute(
            select(RunLog).order_by(RunLog.started_at.desc()).limit(1)
        ).scalar_one_or_none()
        if row is not None:
            session.expunge(row)
        return row


def due() -> bool:
    """Whether enough time has passed since the last run began.

    Measured from when the last run *started*, not when it finished: a pass
    that takes twenty minutes should still go once a day rather than once a
    day plus twenty minutes, drifting later forever.
    """
    previous = last()
    if previous is None:
        return True
    gap = timedelta(minutes=settings.harvest_interval_minutes)
    return datetime.now(UTC) - previous.started_at.replace(tzinfo=UTC) >= gap


def _record_start() -> int:
    with session_scope() as session:
        row = RunLog(started_at=datetime.now(UTC))
        session.add(row)
        session.flush()
        return row.id


def _record_end(run_id: int, summary: dict[str, Any] | None, error: str | None) -> None:
    with session_scope() as session:
        row = session.get(RunLog, run_id)
        if row is None:
            return
        row.finished_at = datetime.now(UTC)
        row.error = error
        if summary:
            row.found = summary.get("found", 0)
            row.added = summary.get("added", 0)
            row.pushed_out = summary.get("pushed_out", 0)
            row.posted = summary.get("posted", 0)
            row.failed = summary.get("failed", 0)


def run(post: bool = True, rooms: int | None = None) -> dict[str, Any]:
    """Do a pass now, here, recording what happened. Blocks until done."""
    if not _running.acquire(blocking=False):
        return {"skipped": "a run is already going", "since": str(in_flight())}

    global _started
    _started = datetime.now(UTC)
    run_id = _record_start()
    try:
        from worker.tasks.harvest import run as harvest

        summary = harvest(post=post, rooms=rooms)
        _record_end(run_id, summary, None)
        return summary
    except Exception as exc:  # noqa: BLE001 - a thread has nobody to raise to
        # Recorded rather than raised. Inside a background thread there is no
        # response to fail and nobody waiting, so an unrecorded exception is a
        # run that simply never appears to have happened.
        detail = "".join(traceback.format_exception(exc))[-2000:]
        log.exception("the run failed")
        _record_end(run_id, None, detail)
        return {"error": str(exc)[:300]}
    finally:
        _started = None
        _running.release()


def start_in_background(post: bool = True, rooms: int | None = None) -> bool:
    """Kick a run off and return immediately. False if one is already going."""
    if _running.locked():
        return False
    threading.Thread(
        target=run, kwargs={"post": post, "rooms": rooms},
        name="putitupp-run", daemon=True,
    ).start()
    return True


_tokens_checked: datetime | None = None


def _maybe_refresh_tokens() -> None:
    """Extend the Meta tokens before they lapse.

    They die at sixty days and cannot be revived afterwards, so this runs at a
    quarter of that: three failed attempts still leave a fortnight of slack.
    It is cheap and it is the one job whose deadline is external.
    """
    global _tokens_checked
    if not settings.has_meta_tokens:
        return
    gap = timedelta(days=settings.token_refresh_interval_days)
    if _tokens_checked is not None and datetime.now(UTC) - _tokens_checked < gap:
        return
    _tokens_checked = datetime.now(UTC)
    try:
        from worker.tasks.refresh_tokens import run as refresh

        log.info("refreshing Meta tokens: %s", refresh())
    except Exception:  # noqa: BLE001 - a dead token is not a dead service
        log.exception("could not refresh the Meta tokens")


def _heartbeat() -> None:
    while True:
        try:
            _maybe_refresh_tokens()
            if settings.harvest_enabled and settings.reddit_rooms and due():
                log.info("the daily run is due")
                run()
        except Exception:  # noqa: BLE001 - the heartbeat must outlive a bad run
            log.exception("the heartbeat stumbled")
        time.sleep(TICK_S)


_heart: threading.Thread | None = None


def start_heartbeat() -> None:
    """Start the once-a-day loop, if it is not already going.

    A daemon thread, so it dies with the process rather than holding a deploy
    open. Idempotent, because a reload in development would otherwise leave
    two of them ticking.
    """
    global _heart
    if _heart is not None and _heart.is_alive():
        return
    if not settings.has_db:
        log.warning("no DATABASE_URL - the daily run cannot record anything, "
                    "so it is not starting")
        return
    _heart = threading.Thread(target=_heartbeat, name="putitupp-heartbeat", daemon=True)
    _heart.start()
    log.info("daily run armed: every %d minutes, %d rooms",
             settings.harvest_interval_minutes, len(settings.reddit_rooms))
