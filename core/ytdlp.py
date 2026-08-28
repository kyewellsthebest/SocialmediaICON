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
import time
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

# Refusals that are about *where the request came from*, not the video and not
# the client. A different country's exit answers differently, so these move to
# the next proxy rather than trying more clients through the same one.
# YouTube writes the apostrophe as U+2019, so match on the halves around it.
GEO_MARKERS = (
    "this content isn",
    "not available in your country",
    "uploader has not made this video available",
    "video is unavailable in your",
    "who has blocked it in your country",
)

# Other ways a client can fail that say nothing about the video itself.
# YouTube phrases its refusals differently per client and changes the wording,
# so this list grows; what they share is that a different client may well
# succeed on the same URL a second later.
CLIENT_FAILURE_MARKERS = (
    "the page needs to be reloaded",
    # 403 on the media itself, after a format was chosen: the URL is bound to
    # something this request no longer matches - a rotating proxy that moved
    # IP, or a format that wanted a proof-of-origin token. Another client
    # issues different URLs, so it is worth one.
    "http error 403",
    "unable to download video data",
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


def is_geo_blocked(error: BaseException) -> bool:
    """The content exists; this exit is in the wrong place to be shown it."""
    message = str(error).lower()
    return any(marker in message for marker in GEO_MARKERS)


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


def is_worth_another_proxy(error: BaseException) -> bool:
    """Whether a different exit might succeed where this one did not."""
    return is_worth_another_client(error) or is_geo_blocked(error)


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
        "proxy_set": proxies() != [None],
        "proxy_count": len([p for p in proxies() if p]),
        "impersonation": impersonation(),
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
    first = proxies()[0]
    if first:
        options["proxy"] = first
    options.update(overrides)
    return options


def _as_proxy_url(entry: str) -> str | None:
    """Accept either a proxy URL or the ip:port:user:pass line dashboards export.

    Webshare and friends hand you a downloadable list in the second form, and
    retyping twenty of them into URLs is exactly the kind of transcription
    people get wrong once and then debug for an hour.
    """
    entry = entry.strip()
    if not entry:
        return None
    if "://" in entry:
        return entry
    parts = entry.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{entry}"
    log.warning("could not read proxy entry %r, skipping it", entry)
    return None


def proxies() -> list[str | None]:
    """Every proxy to try, in order. `[None]` means a direct connection."""
    raw = settings.ytdlp_proxies or ""
    entries = [e for chunk in raw.split("\n") for e in chunk.split(",")]
    found = [url for url in (_as_proxy_url(e) for e in entries) if url]
    if not found and settings.ytdlp_proxy:
        found = [settings.ytdlp_proxy]
    return found or [None]


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
    """Run `call` against each proxy and player client until one works.

    Two dimensions, because two different things get refused: an address the
    platform distrusts, and a client it will not serve. Proxies are the outer
    loop - a burned IP fails on every client, so moving on beats exhausting the
    clients against an address that will never answer.

    The starting proxy walks with the clock, so runs do not all begin on the
    same address and wear it out while the rest sit idle.
    """
    clients = player_clients() or [None]
    pool = proxies()
    budget = max(1, settings.ytdlp_max_proxies_per_run)
    start = int(time.time() // 600) % len(pool) if len(pool) > 1 else 0
    ordered = [pool[(start + i) % len(pool)] for i in range(len(pool))][:budget]

    last: BaseException | None = None
    tried_clients: list[str] = []

    for index, proxy in enumerate(ordered):
        for client in clients:
            attempt = dict(options)
            if proxy:
                attempt["proxy"] = proxy
            elif "proxy" in attempt:
                del attempt["proxy"]

            if client:
                extractor_args = dict(attempt.get("extractor_args") or {})
                youtube_args = {
                    **extractor_args.get("youtube", {}),
                    "player_client": [client],
                }
                if settings.ytdlp_allow_missing_pot:
                    youtube_args.setdefault("formats", ["missing_pot"])
                extractor_args["youtube"] = youtube_args
                attempt["extractor_args"] = extractor_args
                if client not in tried_clients:
                    tried_clients.append(client)

            try:
                return call(attempt)
            except Exception as exc:  # noqa: BLE001 - yt-dlp raises a wide range
                if is_geo_blocked(exc):
                    # Every client through this exit will be told the same
                    # thing, so stop asking and change country.
                    log.warning(
                        "proxy %d/%d cannot see this video from where it is",
                        index + 1,
                        len(ordered),
                    )
                    last = exc
                    break
                if not is_worth_another_client(exc):
                    raise
                if is_bot_check(exc):
                    reason = "was challenged"
                elif is_no_usable_format(exc):
                    reason = "offered no usable format"
                else:
                    reason = f"failed with {str(exc).strip()[:80]!r}"
                log.warning("proxy %d/%d, client %r %s", index + 1, len(ordered), client, reason)
                last = exc

    state = describe()
    prefix = (
        f"[proxies={len(ordered)}of{len(pool)} "
        f"cookies={'yes' if state['cookies_loaded'] else 'NO'}"
        f"({state['cookie_lines']}) "
        f"tried={','.join(tried_clients)}] "
    )

    if last is not None and is_geo_blocked(last):
        raise BotCheck(
            prefix + "The video is not available from any exit tried - it is "
            "region locked. Proxies in the uploader's country would see it; "
            "these cannot."
        ) from last

    if last is not None and is_no_usable_format(last):
        raise BotCheck(
            prefix + "Every client got through but none offered a downloadable "
            "format. Try reordering YTDLP_PLAYER_CLIENTS, or update yt-dlp."
        ) from last

    if len(pool) > len(ordered):
        raise BotCheck(
            prefix + f"{len(ordered)} of your {len(pool)} proxies were refused. "
            "The rest are untried - raise YTDLP_MAX_PROXIES_PER_RUN, or the whole "
            "block may be flagged."
        ) from last

    if state["cookies_loaded"] and pool == [None]:
        raise BotCheck(
            prefix + "Cookies got one client past the bot check but it had no "
            "usable format, and every other client was blocked by IP. That needs "
            "a residential proxy; cookies alone cannot resolve it."
        ) from last

    raise BotCheck(prefix + BOT_CHECK_HELP) from last


def impersonation() -> dict[str, Any]:
    """Can yt-dlp pretend to be a browser, and which ones?

    Cloudflare fingerprints the TLS handshake, so a request that looks like
    Python is refused before anything reads the URL. yt-dlp can imitate a real
    browser's handshake, but only when curl_cffi is installed - and when it is
    not, the failure surfaces as a 403 from the site, which reads exactly like
    the site having closed its doors. It has not. The dependency is missing.

    Kick's extractor asks for impersonation on every request, so for Kick this
    is not a tuning knob, it is the difference between working and not.
    """
    try:
        import yt_dlp
    except ImportError:
        return {"available": False, "targets": 0, "reason": "yt-dlp is not installed"}

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            targets = list(ydl._get_available_impersonate_targets())
    except Exception as exc:  # noqa: BLE001 - a status read must never break the page
        return {"available": False, "targets": 0, "reason": f"{type(exc).__name__}: {exc}"}

    return {
        "available": bool(targets),
        "targets": len(targets),
        "reason": "" if targets else "curl_cffi is not installed - Kick will 403",
    }
