"""Shared yt-dlp configuration.

Reddit is the only source, and it is a friendly one: no bot check, no player
clients, no cookies. What is left is the one thing that can still go wrong on a
cloud host - the address the request comes from - so this is a proxy pool and a
loop that walks it.

Most deployments will have no proxies at all, in which case the pool is a
single direct connection and the loop runs once. That is the intended state;
the pool exists so that the day Reddit starts refusing a datacenter range, the
fix is a variable rather than a rewrite.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from core.config import settings

log = logging.getLogger(__name__)


def _as_proxy_url(entry: str) -> str | None:
    """Accept either a proxy URL or the ip:port:user:pass line dashboards export.

    Webshare and friends hand you a downloadable list in the second form, and
    retyping twenty of them into URLs is exactly the kind of transcription
    people get wrong once and then debug for an hour.
    """
    entry = (entry or "").strip()
    if not entry:
        return None
    if "://" in entry:
        return entry
    bits = entry.split(":")
    if len(bits) == 4:
        host, port, user, password = bits
        return f"http://{user}:{password}@{host}:{port}"
    if len(bits) == 2:
        return f"http://{entry}"
    return None


def proxies() -> list[str | None]:
    """Every proxy to try, in order. `[None]` means a direct connection."""
    raw = settings.ytdlp_proxies or ""
    entries = [e for chunk in raw.split("\n") for e in chunk.split(",")]
    found = [url for url in (_as_proxy_url(e) for e in entries) if url]
    if not found and settings.ytdlp_proxy:
        found = [settings.ytdlp_proxy]
    return found or [None]


def base_options(**overrides: Any) -> dict[str, Any]:
    """Options common to every yt-dlp call in the codebase."""
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
    }
    first = proxies()[0]
    if first:
        options["proxy"] = first
    options.update(overrides)
    return options


def run(call: Callable[[dict[str, Any]], Any], options: dict[str, Any]) -> Any:
    """Run `call` against each proxy until one works.

    The starting proxy walks with the clock, so runs do not all begin on the
    same address and wear it out while the rest sit idle.
    """
    pool = proxies()
    budget = max(1, settings.ytdlp_max_proxies_per_run)
    start = int(time.time() // 600) % len(pool) if len(pool) > 1 else 0
    ordered = [pool[(start + i) % len(pool)] for i in range(len(pool))][:budget]

    last: BaseException | None = None
    for index, proxy in enumerate(ordered):
        attempt = dict(options)
        if proxy:
            attempt["proxy"] = proxy
        else:
            attempt.pop("proxy", None)
        try:
            return call(attempt)
        except Exception as exc:  # noqa: BLE001 - the next address may well work
            last = exc
            if index + 1 < len(ordered):
                log.warning("yt-dlp failed on proxy %d of %d (%s); trying the next",
                            index + 1, len(ordered), exc)

    assert last is not None
    raise last
