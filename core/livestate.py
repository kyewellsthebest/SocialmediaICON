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
IMAGE_KEY = "clipengine:live:image:{name}"
STOP_KEY = "clipengine:live:stop"
WANTED_KEY = "clipengine:live:wanted"
NOTE_KEY = "clipengine:live:note"
CLAIM_KEY = "clipengine:live:claim:{name}"
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


# --- images the worker draws and the web serves ------------------------------
#
# The spectrogram is rendered on the worker, from a buffer on the worker's own
# disk, and has to reach a browser talking to the web service. Those are
# different containers with different filesystems, so the picture travels the
# same way the numbers do. It is kept out of the status snapshot deliberately:
# that is rewritten every few seconds and a couple of hundred kilobytes of PNG
# riding along with it would be rewritten every few seconds too.


def put_image(name: str, data: bytes, *, ttl_s: int = 120) -> None:
    client = _redis()
    if client is None:
        _fallback.setdefault("images", {})[name] = (time.time(), data)
        return
    try:
        client.set(IMAGE_KEY.format(name=name), data, ex=ttl_s)
    except Exception as exc:  # noqa: BLE001 - a missing graph is not an outage
        log.debug("livestate: could not store image %s (%s)", name, exc)


def get_image(name: str, *, max_age_s: int = 120) -> bytes | None:
    client = _redis()
    if client is None:
        found = (_fallback.get("images") or {}).get(name)
        if not found:
            return None
        when, data = found
        return data if time.time() - when <= max_age_s else None
    try:
        return client.get(IMAGE_KEY.format(name=name))
    except Exception:  # noqa: BLE001
        return None


# --- what it should be doing, as opposed to what it is doing -----------------
#
# Running is a fact about right now; wanted is an intention that outlives a
# deploy, a crash and an out-of-memory kill. Keeping them apart is what lets
# the watcher restart itself without also overriding somebody who deliberately
# pressed Stop. It has no expiry for the same reason - an intention that times
# out is not an intention.


def want(watching: bool) -> None:
    client = _redis()
    if client is None:
        _fallback["wanted"] = watching
        return
    try:
        client.set(WANTED_KEY, "1" if watching else "0")
    except Exception as exc:  # noqa: BLE001
        log.debug("livestate: could not record intent (%s)", exc)
        _fallback["wanted"] = watching


def wanted(default: bool = True) -> bool:
    """Whether it should be watching. Unset means yes - it is a watcher."""
    client = _redis()
    if client is None:
        return bool(_fallback.get("wanted", default))
    try:
        raw = client.get(WANTED_KEY)
    except Exception:  # noqa: BLE001
        return bool(_fallback.get("wanted", default))
    if raw is None:
        return default
    return raw in (b"1", "1")


# --- the last thing that went wrong, kept ------------------------------------
#
# The status snapshot expires after thirty seconds, which is right for a live
# reading and wrong for an explanation. A worker that refuses to start says so
# once - "LIVE_ENABLED is not set on the worker service" - and thirty seconds
# later the page is back to saying "restarting", which is the least useful
# true statement available. The reason outlives the reading.


def note(message: str, **extra: Any) -> None:
    """Remember why it is not running, for as long as it is not running."""
    payload = json.dumps({"message": message, "at": time.time(), **extra})
    client = _redis()
    if client is None:
        _fallback["note"] = payload
        return
    try:
        client.set(NOTE_KEY, payload, ex=7 * 24 * 3600)
    except Exception as exc:  # noqa: BLE001
        log.debug("livestate: could not record note (%s)", exc)
        _fallback["note"] = payload


def last_note() -> dict[str, Any] | None:
    client = _redis()
    raw = _fallback.get("note")
    if client is not None:
        try:
            raw = client.get(NOTE_KEY) or raw
        except Exception:  # noqa: BLE001
            pass
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def clear_note() -> None:
    _fallback.pop("note", None)
    client = _redis()
    if client is None:
        return
    try:
        client.delete(NOTE_KEY)
    except Exception:  # noqa: BLE001
        pass


# --- doing a thing at most once every so often -------------------------------


def claim(name: str, *, seconds: float) -> bool:
    """True for the first caller in the window, False for everyone after.

    The dashboard polls every five seconds and there may be more than one web
    container, so anything the *page* triggers - re-queueing a watcher that
    died, for instance - has to be claimed before it is done or a stalled
    watch turns into a queue full of identical jobs.
    """
    client = _redis()
    if client is None:
        held = _fallback.setdefault("claims", {})
        if time.time() - held.get(name, 0) < seconds:
            return False
        held[name] = time.time()
        return True
    try:
        return bool(client.set(CLAIM_KEY.format(name=name), "1", nx=True, ex=int(seconds)))
    except Exception:  # noqa: BLE001 - if Redis is unreachable nothing can be queued anyway
        return False
