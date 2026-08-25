"""Shared yt-dlp configuration.

YouTube challenges requests from datacenter IP ranges - which is every cloud
host, Railway included - with "Sign in to confirm you're not a bot". Nothing is
wrong with the request; the address it came from is the problem.

There are three levers, in increasing order of effort:

1. **Player client.** YouTube's clients are checked differently, and the set
   that passes changes every few months. So the client is a list to try in
   order, configurable, rather than a value compiled into the code.
2. **Cookies.** A logged-in session usually passes. Paste the contents of a
   cookies.txt into YTDLP_COOKIES - tabs lost in transit are repaired here.
   Use a throwaway Google account, never your real one: signing in from a
   datacenter IP is exactly the pattern account bans are looking for.
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

# The web client authenticates with cookies but is increasingly served
# SABR-only streams, whose formats yt-dlp skips for having no usable URL. The
# result is a client that gets past the bot check and then offers nothing to
# download - so this is a reason to try the next client, not to fail.
NO_FORMAT_MARKERS = (
    "requested format is not available",
    "no video formats found",
    "no formats found",
)

# Other ways a client can fail that say nothing about the video itself.
# YouTube phrases its refusals differently per client and changes the wording,
# so this list grows; what they share is that a different client may well
# succeed on the same URL a second later.
CLIENT_FAILURE_MARKERS = (
    "the page needs to be reloaded",
    "unable to extract player response",
    "failed to extract any player response",
    "please sign in",
    "unable to extract yt initial data",
    "player response is invalid",
)

BOT_CHECK_HELP = (
    "YouTube blocked the download with its bot check. This is about the IP "
    "address, not the video: cloud hosts are challenged by default. Paste a "
    "cookies.txt into YTDLP_COOKIES (from a throwaway account), or point "
    "YTDLP_PROXY at a residential proxy. Check /api/settings/ytdlp to see "
    "whether cookies actually reached the worker."
)

_cookiefile: Path | None = None


class BotCheck(RuntimeError):
    """YouTube asked for a human. Retrying the same way will not help."""


def is_bot_check(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in BOT_CHECK_MARKERS)


def is_no_usable_format(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in NO_FORMAT_MARKERS)


def is_client_failure(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in CLIENT_FAILURE_MARKERS)


def is_worth_another_client(error: BaseException) -> bool:
    """Whether a different player client might succeed where this one did not.

    Three families qualify, for one underlying reason: what YouTube hands back
    depends on which client asked, and it refuses each of them differently.
    Everything that is genuinely about the video - deleted, private, region
    locked, members only - fails identically on all five and raises at once.
    """
    return is_bot_check(error) or is_no_usable_format(error) or is_client_failure(error)


COOKIE_HEADER = "# Netscape HTTP Cookie File"


def normalise_cookie_text(text: str) -> bytes:
    """Repair a cookies.txt that lost its tabs on the way through a text box.

    The Netscape format is tab-separated, and pasting one through a web form
    routinely turns those tabs into spaces - after which yt-dlp reads the file
    as empty and the challenge looks unchanged. Fields cannot contain tabs, so
    re-splitting on whitespace and rejoining recovers the original exactly,
    with any spaces in the value folded back into the last field.
    """
    lines = [COOKIE_HEADER]
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "\t" in line:
            lines.append(line.rstrip())
            continue
        fields = stripped.split()
        if len(fields) < 7:
            continue  # not a cookie line; dropping it beats a parse error
        lines.append("\t".join([*fields[:6], " ".join(fields[6:])]))
    return ("\n".join(lines) + "\n").encode()


def cookiefile() -> str | None:
    """Path to a cookies.txt, materialised from the environment if needed.

    Cookies are a file but Railway holds strings, so they arrive either as the
    file's text pasted straight in or as base64, and are written to a temp file
    once per process.
    """
    global _cookiefile

    if settings.ytdlp_cookiefile:
        return settings.ytdlp_cookiefile
    if not (settings.ytdlp_cookies or settings.ytdlp_cookies_b64):
        return None
    if _cookiefile and _cookiefile.exists():
        return str(_cookiefile)

    if settings.ytdlp_cookies:
        raw = normalise_cookie_text(settings.ytdlp_cookies)
        if raw.count(b"\n") < 2:
            log.error("YTDLP_COOKIES has no usable cookie lines, ignoring it")
            return None
    else:
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
    log.info("using cookies from the environment (%d bytes)", len(raw))
    return str(_cookiefile)


def describe() -> dict[str, Any]:
    """What yt-dlp is actually configured with, for the dashboard.

    "I set the cookies" and "the worker is using the cookies" are different
    claims, and the gap between them is invisible from the outside - a variable
    that was never shared with the service looks exactly like a variable that
    did not help. Returns counts and flags, never cookie contents.
    """
    path = cookiefile()
    source = "none"
    if settings.ytdlp_cookiefile:
        source = "file"
    elif settings.ytdlp_cookies:
        source = "pasted text"
    elif settings.ytdlp_cookies_b64:
        source = "base64"

    lines = 0
    if path:
        try:
            lines = sum(
                1
                for line in Path(path).read_text(errors="replace").splitlines()
                if line.strip() and not line.startswith("#")
            )
        except OSError:
            lines = -1

    try:
        import yt_dlp

        version = yt_dlp.version.__version__
    except Exception:  # noqa: BLE001 - a version read must never break the page
        version = "unknown"

    return {
        "yt_dlp_version": version,
        "cookies_source": source,
        "cookies_loaded": bool(path),
        "cookie_lines": lines,
        "player_clients": player_clients(),
        "proxy_set": bool(settings.ytdlp_proxy),
    }


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
    """Clients to try, in order.

    A cookie jar is a *web* session, so the web client is the one that can
    actually present it. Without this, cookies get set and then handed to
    clients that ignore them, and the challenge looks unchanged - which reads
    as "cookies did not work" when they were never used.
    """
    clients = [c.strip() for c in settings.ytdlp_player_clients.split(",") if c.strip()]
    if cookiefile() and "web" not in clients:
        clients.insert(0, "web")
    return clients


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
        youtube_args = {
            **extractor_args.get("youtube", {}),
            "player_client": [client],
        }
        if settings.ytdlp_allow_missing_pot:
            youtube_args.setdefault("formats", ["missing_pot"])
        extractor_args["youtube"] = youtube_args
        attempt["extractor_args"] = extractor_args
        try:
            return call(attempt)
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises a wide range
            if not is_worth_another_client(exc):
                raise
            if is_bot_check(exc):
                reason = "was challenged"
            elif is_no_usable_format(exc):
                reason = "offered no usable format"
            else:
                reason = f"failed with {str(exc).strip()[:80]!r}"
            log.warning("player client %r %s, trying the next", client, reason)
            last = exc

    state = describe()
    if last is not None and is_no_usable_format(last):
        raise BotCheck(
            "Every player client got through but none offered a downloadable "
            "format. YouTube serves some clients streams yt-dlp cannot use. Try "
            "reordering YTDLP_PLAYER_CLIENTS, or update yt-dlp. "
            f"[tried={','.join(clients)}, yt-dlp={state['yt_dlp_version']}]"
        ) from last

    if state["cookies_loaded"] and not state["proxy_set"]:
        raise BotCheck(
            "Cookies got one client past the bot check but it had no usable "
            "format, and every other client was blocked by IP. That combination "
            "needs either a residential proxy (YTDLP_PROXY) or a proof-of-origin "
            "token provider - cookies alone cannot resolve it. "
            f"[cookies={state['cookie_lines']} lines, tried={','.join(clients)}]"
        ) from last

    raise BotCheck(
        f"{BOT_CHECK_HELP} "
        f"[this worker: cookies={'yes' if state['cookies_loaded'] else 'NO'}"
        f" ({state['cookie_lines']} lines, {state['cookies_source']}),"
        f" proxy={'yes' if state['proxy_set'] else 'no'},"
        f" tried={','.join(clients)}]"
    ) from last
