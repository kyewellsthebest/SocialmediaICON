"""The long-running job: watch, catch, repeat.

One process owns the supervisor, because the buffers are ffmpeg children and
the chat sockets are threads - both belong to whoever started them. Running
two of these would double the bandwidth and race on the caps.
"""

from __future__ import annotations

import logging
import time

from core import livestate
from core.config import settings
from core.supervisor import TICK_S, Supervisor

log = logging.getLogger(__name__)

#: The single instance, so the API can read what it is seeing without
#: starting a second one.
_current: Supervisor | None = None


def current() -> Supervisor | None:
    return _current


def _stalled(hint: str, **extra: object) -> dict:
    """Publish a refusal so it reaches the page instead of dying in a log.

    A job that returns a dict nobody reads is invisible: the button appears to
    do nothing, the dashboard keeps saying "not running", and there is no way
    to tell a refusal from a worker that never picked the job up. Every exit
    from this function has to leave a trace the dashboard can render.
    """
    livestate.publish(
        {
            "running": False,
            "enabled": settings.live_enabled,
            "slots": settings.live_slots,
            "posting_enabled": settings.live_posting_enabled,
            "caps": {
                "per_day": settings.live_clips_per_day,
                "min_gap_minutes": settings.live_min_gap_minutes,
            },
            "streams": [],
            "errors": [],
            "hint": hint,
            **extra,
        }
    )
    log.warning("live_watch: %s", hint)
    return {"ok": False, "reason": hint}


def run(max_seconds: float | None = None) -> dict:
    """Watch until told to stop. Returns a summary of what was caught."""
    global _current

    if not settings.live_enabled:
        return _stalled(
            "The worker picked this up but LIVE_ENABLED is not set on the "
            "worker service. Railway does not share variables between "
            "services - set it there too."
        )
    if _current is not None and _current.running:
        return _stalled("A watcher is already running in this worker.")

    supervisor = Supervisor()
    _current = supervisor
    supervisor.running = True
    livestate.clear()
    started = time.time()
    caught = 0
    stopped_on_purpose = False

    # Say so before doing any work. Resolving three playback URLs and filling
    # three buffers takes the better part of a minute, and a page that shows
    # nothing for that long is indistinguishable from a button that did not
    # work - which is exactly how this looked the first time.
    livestate.publish(
        {
            "running": True,
            "enabled": True,
            "slots": settings.live_slots,
            "posting_enabled": settings.live_posting_enabled,
            "caps": {
                "per_day": settings.live_clips_per_day,
                "min_gap_minutes": settings.live_min_gap_minutes,
            },
            "streams": [],
            "errors": [],
            "hint": "Attaching to streams - the first buffer takes about fifteen seconds.",
        }
    )

    try:
        while supervisor.running:
            if time.time() - supervisor.last_roster_poll >= settings.live_roster_poll_s:
                try:
                    supervisor.poll_roster()
                except Exception as exc:  # noqa: BLE001 - keep the buffers we have
                    supervisor._note(f"roster poll failed ({exc})")

            caught += len(supervisor.tick())

            # The dashboard is served by a different process on a different
            # container, so the only way it can show any of this is if the
            # snapshot is written somewhere both can reach.
            livestate.publish(supervisor.status())

            if livestate.stop_requested() or not livestate.wanted():
                log.info("live_watch: stop requested")
                stopped_on_purpose = True
                break
            if max_seconds is not None and time.time() - started >= max_seconds:
                break
            time.sleep(TICK_S)
    except Exception as exc:  # noqa: BLE001 - the page must learn about this
        supervisor.stop()
        _stalled(
            f"The watcher stopped with an error: {type(exc).__name__}: {exc}",
            errors=supervisor.errors[-6:],
        )
        relaunch("after an error")
        return {"ok": False, "reason": str(exc), "relaunched": True}
    finally:
        supervisor.stop()

    if stopped_on_purpose:
        # Leave a readable final state rather than an empty one: "it ran and
        # stopped" and "it never started" look identical otherwise.
        _stalled(
            f"Stopped after {round(time.time() - started)}s, {caught} clip(s) caught.",
            errors=supervisor.errors[-6:],
        )
    else:
        relaunch("after the run ended")

    return {
        "ok": True,
        "caught": caught,
        "ran_s": round(time.time() - started, 1),
        "errors": supervisor.errors[-6:],
    }


def relaunch(why: str) -> bool:
    """Queue the next run, unless somebody asked it to stop.

    A watcher that has to be started by hand is not a watcher. Deploys,
    crashes and out-of-memory kills all end a run, and none of them are a
    decision to stop watching - only pressing Stop is.
    """
    if not livestate.wanted():
        log.info("live_watch: not relaunching %s - Stop was pressed", why)
        return False
    if not settings.has_redis:
        return False
    try:
        from worker.queue import enqueue

        enqueue("live", "worker.tasks.live_watch.run", job_timeout=24 * 3600)
        log.info("live_watch: relaunching %s", why)
        return True
    except Exception as exc:  # noqa: BLE001 - the next boot will pick it up
        log.warning("live_watch: could not relaunch %s (%s)", why, exc)
        return False


def ensure_running() -> dict:
    """Called when a worker boots. Starts the watch if that is what is wanted."""
    if not settings.live_enabled:
        return {"ok": False, "reason": "LIVE_ENABLED is not set"}
    if not livestate.wanted():
        return {"ok": False, "reason": "Stop was pressed; leaving it stopped"}
    if livestate.read():
        return {"ok": False, "reason": "already running"}
    return {"ok": relaunch("on worker boot")}


def stop() -> dict:
    """Ask the loop to finish - usually from the web process, not this one."""
    livestate.want(False)
    livestate.request_stop()
    if _current is not None:
        _current.running = False
    return {"ok": True, "requested": True}
