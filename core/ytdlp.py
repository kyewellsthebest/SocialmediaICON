"""Shared yt-dlp configuration.

YouTube challenges requests from datacenter IP ranges - which is every cloud
host, Railway included - with "Sign in to confirm you're not a bot". Nothing is
wrong with the request; the address it came from is the problem.

There are three levers, in increasing order of effort:

1. **Player client.** YouTube's clients are checked differently, and the set
   that passes changes every few months. So the client is a list to try in
   order, configurable, rather than a value compiled into the code.
2. **Cookies.** A logged-in session usually passes. Use a throwaway Google
   account, never your real one: signing in from a datacenter IP is exactly
   the pattern account bans are looking for.
3. **A residential proxy.** Reliable, costs money, and is the only one of the
   three that always works.

None of this applies to source video you are licensed to use - a clipping
campaign that hands you the file directly skips the whole problem.
"""

from __future__ import annotations

import base64
import binascii
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.config import settings

log = logging.getLogger(__name__)

# The message YouTube returns when it wants a human. Worth recognising, because
# the raw text sends people looking for a bug that is not there.
BOT_CHECK_MARKERS = (
    "confirm you're not a bot",
    "confirm you are not a bot",
    "sign in to confirm",
)

BOT_CHECK_HELP = (
    "YouTube blocked the download with its bot check. This is about the IP "
    "address, not the video: cloud hosts are challenged by default. Set "
    "YTDLP_COOKIES_B64 (from a throwaway account), or YTDLP_PROXY to a "
    "residential proxy, or try a different YTDLP_PLAYER_CLIENTS order."
)

_cookiefile: Path | None = None


class BotCheck(RuntimeError):
    """YouTube asked for a human. Retrying the same way will not help."""


def is_bot_check(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in BOT_CHECK_MARKERS)


def cookiefile() -> str | None:
    """Path to a cookies.txt, materialised from the environment if needed.

    Cookies are a file, but Railway holds strings - so they travel as base64
    and are written to a temp file once per process.
    """
    global _cookiefile

    if settings.ytdlp_cookiefile:
        return settings.ytdlp_cookiefile
    if not settings.ytdlp_cookies_b64:
        return None
    if _cookiefile and _cookiefile.exists():
        return str(_cookiefile)

    try:
        raw = base64.b64decode(settings.ytdlp_cookies_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        log.error("YTDLP_COOKIES_B64 is not valid base64, ignoring it: %s", exc)
        return None

    handle = tempfile.NamedTemporaryFile(
        prefix="ytdlp-cookies-", suffix=".txt", delete=False, mode="wb"
    )
    handle.write(raw)
    handle.close()
    _cookiefile = Path(handle.name)
    _cookiefile.chmod(0o600)
    log.info("using cookies from YTDLP_COOKIES_B64")
    return str(_cookiefile)


def base_options(**overrides: Any) -> dict[str, Any]:
    """Options common to every yt-dlp call in the codebase."""
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
    }
    if cookies := cookiefile():
        options["cookiefile"] = cookies
    if settings.ytdlp_proxy:
        options["proxy"] = settings.ytdlp_proxy
    options.update(overrides)
    return options


def player_clients() -> list[str]:
    return [c.strip() for c in settings.ytdlp_player_clients.split(",") if c.strip()]


def run(call: Callable[[dict[str, Any]], Any], options: dict[str, Any]) -> Any:
    """Run `call` against each player client until one works.

    A bot check on one client says nothing about the next, so the list is worth
    walking. Any other error is real and raised immediately - retrying a 404
    four times just makes the log longer.
    """
    clients = player_clients()
    if not clients:
        return call(options)

    last: BaseException | None = None
    for client in clients:
        attempt = dict(options)
        extractor_args = dict(attempt.get("extractor_args") or {})
        extractor_args["youtube"] = {
            **extractor_args.get("youtube", {}),
            "player_client": [client],
        }
        attempt["extractor_args"] = extractor_args
        try:
            return call(attempt)
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises a wide range
            if not is_bot_check(exc):
                raise
            log.warning("player client %r was challenged, trying the next", client)
            last = exc

    raise BotCheck(BOT_CHECK_HELP) from last
