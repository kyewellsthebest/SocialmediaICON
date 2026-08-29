"""Supervisor state, shared between the worker that has it and the web that shows it.

The watcher runs in the worker process: it owns the ffmpeg children and the
chat threads. The dashboard is served by the web process. They are different
containers on Railway and do not share memory, so a status held in a module
global is invisible to the page that wants to display it - the dashboard would
read "not running" forever while three buffers were quietly filling.

So the supervisor publishes a snapshot to Redis on every tick and the API
reads it back. The same channel carries the stop request in the other
direction, because "press Stop in the browser" has to reach a loop running
somewhere else entirely.

Everything here degrades to an in-process fallback when Redis is absent, so
the pipeline still runs end to end on a laptop.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

STATUS_KEY = "clipengine:live:status"
STOP_KEY = "clipengine:live:stop"
#: A snapshot older than this means the worker died mid-run: better to report
#: nothing than to show three streams that stopped existing ten minutes ago.
STATUS_TTL_S = 30

#: Used only when Redis is not configured, so a local run still works.
_fallback: dict[str, Any] = {}


def _redis():  # noqa: ANN202 - redis client, imported lazily
    from core.config import settings

    if not settings.has_redis:
        return None
    try:
        from worker.queue import get_redis

        return get_redis()
    except Exception as exc:  # noqa: BLE001 - a status write must never crash a tick
        log.debug("livestate: no redis (%s)", exc)
        return None


def publish(status: dict[str, Any]) -> None:
    """Record what the supervisor can currently see."""
    status = {**status, "published_at": time.time()}
    client = _redis()
    if client is None:
        _fallback["status"] = status
        return
    try:
        client.set(STATUS_KEY, json.dumps(status), ex=STATUS_TTL_S)
    except Exception as exc:  # noqa: BLE001
        log.debug("livestate: could not publish (%s)", exc)
        _fallback["status"] = status


def read() -> dict[str, Any] | None:
    """The last snapshot, or None if nothing recent was published."""
    client = _redis()
    if client is None:
        found = _fallback.get("status")
    else:
        try:
            raw = client.get(STATUS_KEY)
            found = json.loads(raw) if raw else None
        except Exception as exc:  # noqa: BLE001
            log.debug("livestate: could not read (%s)", exc)
            found = _fallback.get("status")

    if not found:
        return None
    # The key expires in Redis, but the fallback has no TTL of its own.
    if time.time() - float(found.get("published_at", 0)) > STATUS_TTL_S:
        return None
    return found


def request_stop() -> None:
    """Ask the loop to finish, from whichever process the button was pressed in."""
    client = _redis()
    if client is None:
        _fallback["stop"] = True
        return
    try:
        client.set(STOP_KEY, "1", ex=300)
    except Exception as exc:  # noqa: BLE001
        log.debug("livestate: could not request stop (%s)", exc)
        _fallback["stop"] = True


def stop_requested() -> bool:
    client = _redis()
    if client is None:
        return bool(_fallback.get("stop"))
    try:
        return bool(client.get(STOP_KEY))
    except Exception:  # noqa: BLE001
        return bool(_fallback.get("stop"))


def clear() -> None:
    """Forget both flags. Called as a run starts and as it finishes."""
    client = _redis()
    _fallback.pop("stop", None)
    _fallback.pop("status", None)
    if client is None:
        return
    try:
        client.delete(STATUS_KEY, STOP_KEY)
    except Exception:  # noqa: BLE001
        pass
