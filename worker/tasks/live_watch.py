"""The long-running job: watch, catch, repeat.

One process owns the supervisor, because the buffers are ffmpeg children and
the chat sockets are threads - both belong to whoever started them. Running
two of these would double the bandwidth and race on the caps.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
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


def _holder() -> str:
    """Who this watcher is, for the lease. Unique per process."""
    return f"{socket.gethostname()}:{os.getpid()}"


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
    # ...and again where it will still be readable tomorrow. The snapshot
    # above expires in thirty seconds, which is right for a live reading and
    # useless for an explanation: "LIVE_ENABLED is not set on the worker" was
    # visible for half a minute and then the page went back to saying
    # "restarting" forever.
    livestate.note(hint, **{k: v for k, v in extra.items() if k == "errors"})
    log.warning("live_watch: %s", hint)
    return {"ok": False, "reason": hint}


#: How long a starting watcher waits for a lease somebody else holds.
#:
#: Slightly longer than the lease itself, so a lease left behind by a killed
#: container is always outlived rather than sometimes.
LEASE_WAIT_S = livestate.LEASE_S + 30.0
#: How often it checks while waiting.
LEASE_POLL_S = 5.0
#: How many consecutive "no snapshot" readings mean the lease holder is gone
#: rather than merely starting up.
DEAD_READINGS = 2


def _claim(holder: str) -> bool:
    """Take the lease, waiting out one that is only still there because the
    last container was killed before it could hand it back.

    Railway stops a container with SIGKILL on deploy, so the release in the
    `finally` below does not always run and the lease sits in Redis until its
    TTL. This used to return immediately, and the watchdog re-queued the job
    every sixty seconds until the TTL passed - so a deploy cost five minutes
    of not watching, and the page said "Another watcher already holds the
    lease; leaving it alone", which reads as a permanent refusal rather than
    a wait. It was neither obvious that it would recover nor true that it
    already had.

    Waiting here instead means a deploy costs however long is actually left on
    the old lease, and the page can say so while it counts down.
    """
    if livestate.take_lease(holder):
        return True

    deadline = time.time() + LEASE_WAIT_S
    dead_readings = 0
    while time.time() < deadline:
        # A lease says somebody claims to be watching; the snapshot says
        # somebody is. A lease with no snapshot behind it belongs to a process
        # that is gone, and waiting out its five minutes helps nobody. Two
        # readings apart, because a watcher that has this second taken the
        # lease has not published yet and stealing from it would give us the
        # two watchers the lease exists to prevent.
        if livestate.read() is None:
            dead_readings += 1
            if dead_readings >= DEAD_READINGS:
                log.info("live_watch: the lease holder stopped publishing; taking over")
                if livestate.steal_lease(holder):
                    return True
        else:
            dead_readings = 0

        left = livestate.lease_left_s()
        livestate.note(
            "Waiting for the previous watcher's lease to expire"
            + (f" - about {left:.0f}s left." if left else ".")
        )
        log.info("live_watch: waiting for the lease (%s left)",
                 f"{left:.0f}s" if left else "unknown")
        time.sleep(LEASE_POLL_S)
        if livestate.take_lease(holder):
            log.info("live_watch: took the lease after waiting")
            return True
    return False


def _hand_back_on_signal(holder: str) -> None:
    """Release the lease when the platform asks the process to stop.

    Railway sends SIGTERM and then SIGKILL a grace period later. Nothing here
    listened, so every deploy left the lease behind to expire on its own and
    the next watcher waited minutes for a container that had been gone the
    whole time. Handing it back on the way out makes a deploy cost seconds.

    Only ever called on the way out, and it re-raises the default behaviour
    afterwards so the process still dies when it is told to.
    """
    def handler(signum, frame):  # noqa: ANN001, ARG001
        log.info("live_watch: signal %s - handing the lease back", signum)
        livestate.request_stop()
        livestate.release_lease(holder)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # not the main thread, or not supported
            log.debug("live_watch: could not catch %s", sig)


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

    # One watcher, enforced across processes rather than inside one. The
    # watchdog can queue a run while a healthy one is mid-tick, and two
    # watchers would double the bandwidth and race on the caps.
    holder = _holder()
    if not _claim(holder):
        return _stalled(
            "Another watcher still holds the lease and it did not expire in "
            f"{LEASE_WAIT_S:.0f}s. If nothing else is running, the previous "
            "container was killed without handing it back and it will clear "
            "itself shortly."
        )

    _hand_back_on_signal(holder)

    supervisor = Supervisor()
    _current = supervisor
    supervisor.running = True
    livestate.clear()
    # Whatever went wrong last time did not go wrong this time.
    livestate.clear_note()
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

            # Before the work, not after: a tick that throws must still have
            # said the watcher was alive, or the watchdog starts a second one
            # on top of a watcher that is merely having a bad minute.
            livestate.renew_lease(holder)

            # Published before the work as well as after. A pass over ten
            # streams is seconds of ffmpeg per stream, and publishing only on
            # the way out left the page with nothing to read for most of every
            # pass - which it reported as "RESTARTING", because from outside a
            # watcher that is busy and a watcher that has died look identical.
            livestate.publish(supervisor.status())

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
        # Hand it back rather than waiting out the TTL: a deploy should cost
        # seconds of not watching, not five minutes of it.
        livestate.release_lease(holder)

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


def watchdog() -> dict:
    """Is a watcher alive? If not, start one. Runs every minute, forever.

    `ensure_running` covers a worker booting, which covers deploys and any
    crash that takes the container with it. It does not cover the rest: a job
    lost by the queue, a relaunch that failed because Redis blinked at the
    wrong moment, an out-of-memory kill of the job but not the worker. Each of
    those leaves a live worker process with nothing watching, and nothing to
    notice - which is how a night with no clips happens without a single line
    in the log saying anything is wrong.

    The lease is the test, and it is a fact rather than an inference: the
    running watcher renews it every tick, so its absence means no watcher
    ticked in the last five minutes.
    """
    livestate.watchdog_ran()
    if not settings.live_enabled:
        return {"ok": False, "reason": "LIVE_ENABLED is not set on the worker"}
    if not livestate.wanted():
        return {"ok": False, "reason": "Stop was pressed; leaving it stopped"}
    # `is not False` on purpose: unknown means leave it alone. A watchdog that
    # acts on a guess queues a relaunch a minute forever, and with Redis
    # unreachable none of them could run anyway.
    if livestate.watcher_alive() is not False:
        return {"ok": True, "reason": "a watcher is alive, or cannot be asked"}

    log.warning("live_watch: no watcher has ticked in %ss - starting one",
                livestate.LEASE_S)

    livestate.note(
        "The watcher stopped without saying so and the watchdog restarted it. "
        "If this keeps happening, the last errors above say why."
    )
    return {"ok": relaunch("watchdog found no live watcher"), "restarted": True}


#: How often the in-process watchdog looks. The same minute the scheduler job
#: uses, and it does the same thing - two of them is not a problem, because
#: what they queue is refused by the lease if a watcher is already alive.
WATCHDOG_EVERY_S = 60.0


def start_watchdog() -> None:
    """Run the watchdog inside this process, forever.

    The scheduler runs it too, and this exists because that is not something
    to rely on. The scheduler is a separate Railway service with its own copy
    of the environment, and Railway does not share variables between services:
    with LIVE_ENABLED set on the worker and not on the scheduler, the
    scheduler's watchdog job is disabled and nothing says so. The whole 24/7
    guarantee would quietly not exist.

    Here it cannot be misconfigured that way, because this process is the one
    that runs the watch - if it is running at all, the environment is right.
    """
    import threading

    def loop() -> None:
        while True:
            time.sleep(WATCHDOG_EVERY_S)
            try:
                watchdog()
            except Exception as exc:  # noqa: BLE001 - a watchdog must not die
                log.warning("live_watch: watchdog raised (%s)", exc)

    threading.Thread(target=loop, daemon=True, name="live-watchdog").start()
    log.info("live_watch: watchdog running in this process every %.0fs",
             WATCHDOG_EVERY_S)


def ensure_running() -> dict:
    """Called when a worker boots. Starts the watch if that is what is wanted."""
    if not settings.live_enabled:
        # The worker's own log is the wrong place for this: it is the one
        # thing about the worker the dashboard cannot otherwise see, and it is
        # the most common reason the page sits on "restarting" forever.
        reason = (
            "The worker is running but LIVE_ENABLED is not set on it. Railway "
            "does not share variables between services - set LIVE_ENABLED=true "
            "on the worker service too, then redeploy it."
        )
        livestate.note(reason)
        return {"ok": False, "reason": reason}
    if not livestate.wanted():
        return {"ok": False, "reason": "Stop was pressed; leaving it stopped"}
    # The lease, not the snapshot. The snapshot is a *reading* and it is
    # deliberately allowed to go stale for a minute and a half while a pass
    # over ten streams runs; the lease is the answer to "is a watcher alive",
    # and run() refuses to start a second one beside it regardless.
    # `is True` on purpose, the opposite of the watchdog: unknown means try.
    # The alternative is a worker that boots next to a dead watcher and decides
    # not to start one. run() takes the lease, so trying is safe.
    if livestate.watcher_alive() is True:
        return {"ok": False, "reason": "already running"}
    return {"ok": relaunch("on worker boot")}


def stop() -> dict:
    """Ask the loop to finish - usually from the web process, not this one."""
    livestate.want(False)
    livestate.request_stop()
    if _current is not None:
        _current.running = False
    return {"ok": True, "requested": True}
