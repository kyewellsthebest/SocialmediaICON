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


def run(max_seconds: float | None = None) -> dict:
    """Watch until told to stop. Returns a summary of what was caught."""
    global _current

    if not settings.live_enabled:
        return {"ok": False, "reason": "LIVE_ENABLED is not set"}
    if _current is not None and _current.running:
        return {"ok": False, "reason": "a supervisor is already running"}

    supervisor = Supervisor()
    _current = supervisor
    supervisor.running = True
    livestate.clear()
    started = time.time()
    caught = 0

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

            if livestate.stop_requested():
                log.info("live_watch: stop requested")
                break
            if max_seconds is not None and time.time() - started >= max_seconds:
                break
            time.sleep(TICK_S)
    finally:
        supervisor.stop()
        livestate.clear()

    return {
        "ok": True,
        "caught": caught,
        "ran_s": round(time.time() - started, 1),
        "errors": supervisor.errors[-6:],
    }


def stop() -> dict:
    """Ask the loop to finish - usually from the web process, not this one."""
    livestate.request_stop()
    if _current is not None:
        _current.running = False
    return {"ok": True, "requested": True}
