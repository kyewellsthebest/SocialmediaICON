"""Which of Kick, YouTube and Twitch actually hand over video from this box?

Every source decision in this project has been made from documentation and
search results, because the machine that reasons about them has no network.
That has been wrong twice.

This settles it with evidence, in the order the sources are wanted: Kick
first, YouTube second, Twitch as the fallback. Per source it reports whether
metadata comes back, whether a real media URL is reachable, and - with
--download - whether bytes actually arrive.

    python -m scripts.probe_sources
    python -m scripts.probe_sources --download
    python -m scripts.probe_sources --only kick --kick-url https://kick.com/x/clips/clip_ABC

One finding is worth stating before it is measured: yt-dlp's Kick extractor
asks for browser TLS impersonation on every request, because Cloudflare
fingerprints the handshake and rejects Python on sight. Without curl_cffi
installed that request cannot be made at all, and the 403 that comes back
looks exactly like Kick having closed its doors. So the first thing the Kick
probe checks is our own dependency, not their server.

Nothing here writes to the database, posts anything, or needs a key beyond
what is already configured. It is a read-only fact-finder.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

TIMEOUT_S = 30.0
#: Enough to prove the transfer is real without pulling a whole stream.
SAMPLE_BYTES = 3 * 1024 * 1024

#: Public, long-lived, and famous enough that "it was deleted" is not the
#: explanation for a failure. Overridable per source on the command line.
YOUTUBE_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
#: Channels large enough to reliably have clips. Tried in order.
KICK_CHANNELS = ("xqc", "trainwreckstv", "adinross")
TWITCH_GAME = "Just Chatting"


@dataclass
class Result:
    source: str
    what: str
    ok: bool = False
    detail: str = ""
    media_url: str | None = None
    bytes_read: int = 0
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def line(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        size = f" {self.bytes_read / 1e6:.1f}MB" if self.bytes_read else ""
        took = f" ({self.elapsed_s:.1f}s)" if self.elapsed_s >= 0.1 else ""
        return f"[{mark}] {self.source:<8} {self.what:<26} {self.detail}{size}{took}"


def _client():  # noqa: ANN202 - httpx type, imported lazily
    import httpx

    return httpx.Client(
        timeout=TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": "clip-engine-probe/1.0 (source feasibility check)"},
    )


def _pull(url: str, limit: int = SAMPLE_BYTES) -> tuple[int, str]:
    """Read up to `limit` bytes. Returns (bytes read, note)."""
    import httpx

    read = 0
    try:
        with _client() as client, client.stream("GET", url) as response:
            if response.status_code >= 400:
                return 0, f"HTTP {response.status_code}"
            for chunk in response.iter_bytes(1 << 16):
                read += len(chunk)
                if read >= limit:
                    break
        return read, "ok"
    except httpx.HTTPError as exc:
        return read, f"{type(exc).__name__}: {exc}"


def _extract(url: str, download: bool, out_dir: str) -> tuple[dict, str]:
    """Ask yt-dlp about a URL through this project's own configuration.

    Deliberately the real code path - base_options, the player-client list,
    the proxy pool - because a probe that bypasses them proves nothing about
    whether the pipeline works.
    """
    import yt_dlp

    from core import ytdlp

    options = ytdlp.base_options(
        skip_download=not download,
        outtmpl=f"{out_dir}/%(extractor)s-%(id)s.%(ext)s",
        format="best[height<=720]/best",
        noplaylist=True,
    )

    def call(opts: dict) -> dict:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=download)

    try:
        return ytdlp.run(call, options) or {}, "ok"
    except Exception as exc:  # noqa: BLE001 - reporting failures is the job
        return {}, f"{type(exc).__name__}: {str(exc)[:220]}"


def _summarise(info: dict) -> str:
    bits = []
    if info.get("title"):
        bits.append(f"{str(info['title'])[:40]!r}")
    if info.get("duration"):
        bits.append(f"{info['duration']:.0f}s")
    if info.get("view_count"):
        bits.append(f"{info['view_count']} views")
    formats = info.get("formats") or []
    if formats:
        bits.append(f"{len(formats)} formats")
    return ", ".join(bits) or "no metadata"


def explain(note: str, ytdlp) -> str:  # noqa: ANN001 - the module, passed to avoid a cycle
    """Say which side of the wire refused, because they look identical.

    An egress proxy denying CONNECT and YouTube denying a datacenter IP both
    arrive as "403 Forbidden". One means the machine running this probe cannot
    reach the internet; the other means the source will not serve this machine.
    Reading the first as the second is how a working source gets written off.
    """
    lowered = note.lower()
    if "tunnel connection failed" in lowered or "connect tunnel failed" in lowered:
        return (
            "this box's own egress proxy refused the CONNECT - the source was "
            "never contacted. Run this where the network is open."
        )
    if any(marker in lowered for marker in ytdlp.BOT_CHECK_MARKERS):
        return (
            "bot check: this is the IP range, not the video. Cloud hosts are "
            "challenged by default - needs cookies (YTDLP_COOKIES) or a "
            "residential proxy (YTDLP_PROXY)."
        )
    if "403" in lowered:
        return "403 from the source after format selection - usually wants a PO token or a proxy"
    return "not a network refusal - read the error above"


# --- Kick: first choice ------------------------------------------------------


def _impersonating_get(url: str):  # noqa: ANN202 - curl_cffi Response
    """A GET that looks like Chrome all the way down to the TLS handshake."""
    from curl_cffi import requests as cffi

    return cffi.get(url, impersonate="chrome", timeout=TIMEOUT_S)


def probe_kick_live(channel: str, out_dir: str, seconds: float = 45.0) -> list[Result]:
    """The one that matters: can we hold a live stream in a rolling buffer?

    Everything about catching moments live rests on this. If the playback URL
    resolves and ffmpeg can segment it, the buffer works and the rest is
    arithmetic we have already tested. If it does not, no amount of chat
    analysis helps, because there is no video to cut.
    """
    import yt_dlp

    from core import ytdlp
    from core.live import RollingBuffer

    out: list[Result] = []
    started = time.time()

    def call(opts: dict) -> dict:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"https://kick.com/{channel}", download=False)

    try:
        info = ytdlp.run(call, ytdlp.base_options(skip_download=True)) or {}
    except Exception as exc:  # noqa: BLE001
        out.append(Result(
            "kick", f"live /{channel}",
            detail=f"{type(exc).__name__}: {str(exc)[:160]}",
            elapsed_s=time.time() - started,
        ))
        return out

    formats = info.get("formats") or []
    playback = next(
        (f["url"] for f in reversed(formats) if f.get("url")), info.get("url")
    )
    out.append(Result(
        "kick", f"live /{channel}",
        ok=bool(playback),
        detail=(
            f"{info.get('concurrent_view_count') or '?'} viewers, "
            f"{len(formats)} formats, {str(info.get('title'))[:34]!r}"
        ),
        media_url=playback,
        elapsed_s=time.time() - started,
    ))
    if not playback:
        return out

    # Buffer it for real, and watch the size stop growing. A number that
    # plateaus is the entire storage claim, measured rather than asserted.
    work = Path(out_dir) / f"live-{channel}"
    buffer = RollingBuffer(
        url=playback, work_dir=work, window_s=20.0, segment_s=2.0, channel=channel
    )
    started = time.time()
    sizes: list[float] = []
    try:
        buffer.start()
        while time.time() - started < seconds:
            time.sleep(5.0)
            status = buffer.status()
            sizes.append(status["megabytes"])
            if not status["running"]:
                break

        status = buffer.status()
        note = buffer.failure()
        out.append(Result(
            "kick", "rolling buffer",
            ok=status["held_s"] > 0,
            detail=(
                f"held {status['held_s']:.0f}s in {status['megabytes']:.1f}MB, "
                f"sizes {'->'.join(f'{s:.1f}' for s in sizes[-4:])}"
                if status["held_s"] else f"nothing buffered: {note[:120]}"
            ),
            elapsed_s=time.time() - started,
        ))

        if status["held_s"] > 12:
            started = time.time()
            clip = buffer.extract(work / "cut.mp4", ago_s=2.0, lead_s=8.0, trail_s=2.0)
            out.append(Result(
                "kick", "cut from the past",
                ok=clip.exists(),
                detail=f"{clip.stat().st_size / 1e6:.1f}MB clip out of the buffer",
                bytes_read=clip.stat().st_size,
                elapsed_s=time.time() - started,
            ))
    except Exception as exc:  # noqa: BLE001
        out.append(Result("kick", "rolling buffer", detail=f"{type(exc).__name__}: {exc}"))
    finally:
        buffer.discard()
        out.append(Result(
            "kick", "buffer discarded", ok=not work.exists(),
            detail="nothing left on disk" if not work.exists() else "FILES REMAIN",
        ))
    return out


def probe_kick(download: bool, out_dir: str, url_override: str | None) -> list[Result]:
    """Kick: wanted first, and the one where our own setup is the suspect."""
    from core import ytdlp

    out: list[Result] = []

    # Before blaming Cloudflare, check whether we are able to knock properly.
    state = ytdlp.impersonation()
    out.append(Result(
        "kick", "TLS impersonation",
        ok=bool(state["available"]),
        detail=(
            f"{state['targets']} browser targets available"
            if state["available"]
            else state["reason"]
        ),
        notes=[] if state["available"] else ["pip install curl_cffi - then re-run"],
    ))

    clip_url = url_override
    if not clip_url:
        # Find a real clip rather than hard-coding one that will rot. The site
        # API is what the browser itself calls, so it needs the same disguise.
        for slug in KICK_CHANNELS:
            started = time.time()
            endpoint = f"https://kick.com/api/v2/channels/{slug}/clips"
            try:
                response = _impersonating_get(endpoint)
                status = response.status_code
                clips = response.json().get("clips", []) if status < 400 else []
            except Exception as exc:  # noqa: BLE001
                out.append(Result(
                    "kick", f"clips /{slug}",
                    detail=f"{type(exc).__name__}: {str(exc)[:110]}",
                    notes=[explain(str(exc), ytdlp)],
                    elapsed_s=time.time() - started,
                ))
                continue

            out.append(Result(
                "kick", f"clips /{slug}",
                ok=bool(clips),
                detail=(
                    f"HTTP {status}, {len(clips)} clips"
                    + (" - Cloudflare" if status in (403, 503) else "")
                ),
                elapsed_s=time.time() - started,
            ))
            if clips:
                clip_id = clips[0].get("id") or clips[0].get("clip_id")
                clip_url = f"https://kick.com/{slug}/clips/{clip_id}"
                break

    if not clip_url:
        out.append(Result("kick", "download", detail="no clip URL to try"))
        return out

    started = time.time()
    info, note = _extract(clip_url, download, out_dir)
    out.append(Result(
        "kick", "yt-dlp clip" + (" + download" if download else ""),
        ok=bool(info),
        detail=_summarise(info) if info else note,
        media_url=clip_url,
        elapsed_s=time.time() - started,
    ))
    return out


# --- YouTube: second choice --------------------------------------------------


def probe_youtube(download: bool, out_dir: str, url_override: str | None) -> list[Result]:
    """YouTube: the question is the IP address, not the extractor."""
    from core import ytdlp

    out: list[Result] = []
    config = ytdlp.describe()
    out.append(Result(
        "youtube", "configuration",
        ok=True,
        detail=(
            f"yt-dlp {config['yt_dlp_version']}, clients "
            f"{','.join(config['player_clients'])}, "
            f"cookies={config['cookies_source']}, proxies={config['proxy_count']}"
        ),
    ))

    started = time.time()
    info, note = _extract(url_override or YOUTUBE_URL, download, out_dir)
    result = Result(
        "youtube", "yt-dlp" + (" + download" if download else ""),
        ok=bool(info),
        detail=_summarise(info) if info else note,
        media_url=url_override or YOUTUBE_URL,
        elapsed_s=time.time() - started,
    )
    if not info:
        result.notes.append(explain(note, ytdlp))
    out.append(result)
    return out


# --- Twitch: the fallback ----------------------------------------------------


def probe_twitch(download: bool, out_dir: str, url_override: str | None) -> list[Result]:
    """Twitch: only worth the keys if the two above do not answer."""
    from core.config import settings

    out: list[Result] = []
    clip_url = url_override
    client_id = getattr(settings, "twitch_client_id", None)
    secret = getattr(settings, "twitch_client_secret", None)

    if not clip_url and not (client_id and secret):
        out.append(Result(
            "twitch", "app token",
            detail="TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set",
            notes=["free at https://dev.twitch.tv/console, or pass --twitch-url"],
        ))
        return out

    if not clip_url:
        started = time.time()
        try:
            with _client() as client:
                token = client.post(
                    "https://id.twitch.tv/oauth2/token",
                    data={
                        "client_id": client_id,
                        "client_secret": secret,
                        "grant_type": "client_credentials",
                    },
                ).json()
            access = token.get("access_token")
            out.append(Result(
                "twitch", "app token",
                ok=bool(access),
                detail="client_credentials accepted" if access else str(token)[:120],
                elapsed_s=time.time() - started,
            ))
            if not access:
                return out

            headers = {"Client-Id": client_id, "Authorization": f"Bearer {access}"}
            started = time.time()
            with _client() as client:
                games = client.get(
                    "https://api.twitch.tv/helix/games",
                    params={"name": TWITCH_GAME}, headers=headers,
                ).json().get("data", [])
                game_id = games[0]["id"] if games else None
                clips = client.get(
                    "https://api.twitch.tv/helix/clips",
                    params={"game_id": game_id, "first": 5}, headers=headers,
                ).json().get("data", []) if game_id else []
            top = clips[0].get("view_count") if clips else 0
            out.append(Result(
                "twitch", "top clips",
                ok=bool(clips),
                detail=f"{len(clips)} clips, top has {top} views",
                elapsed_s=time.time() - started,
            ))
            if clips:
                clip_url = clips[0].get("url")
        except Exception as exc:  # noqa: BLE001
            out.append(Result("twitch", "helix", detail=f"{type(exc).__name__}: {exc}"))
            return out

    if not clip_url:
        return out

    started = time.time()
    info, note = _extract(clip_url, download, out_dir)
    out.append(Result(
        "twitch", "yt-dlp clip" + (" + download" if download else ""),
        ok=bool(info),
        detail=_summarise(info) if info else note,
        media_url=clip_url,
        elapsed_s=time.time() - started,
    ))
    return out


#: Ordered: what the project wants first comes first.
PROBES = {
    "kick": probe_kick,
    "youtube": probe_youtube,
    "twitch": probe_twitch,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="actually pull the media")
    parser.add_argument("--only", help="comma separated: " + ", ".join(PROBES))
    parser.add_argument("--out", default=".work/probe", help="where downloads land")
    parser.add_argument("--kick-url", help="test this Kick clip or VOD instead of discovering one")
    parser.add_argument("--youtube-url", help="test this video instead of the default")
    parser.add_argument("--twitch-url", help="test this clip instead of querying Helix")
    parser.add_argument("--live", metavar="CHANNEL", help="buffer a live Kick channel end to end")
    parser.add_argument("--live-seconds", type=float, default=45.0)
    parser.add_argument("--json", action="store_true", help="machine readable output")
    args = parser.parse_args(argv)

    overrides = {
        "kick": args.kick_url,
        "youtube": args.youtube_url,
        "twitch": args.twitch_url,
    }
    wanted = [n.strip() for n in args.only.split(",")] if args.only else list(PROBES)

    results: list[Result] = []
    if args.live:
        results.extend(probe_kick_live(args.live, args.out, args.live_seconds))
        wanted = [n for n in wanted if n != "kick"]

    for name in wanted:
        probe = PROBES.get(name)
        if probe is None:
            print(f"unknown source {name!r}; expected one of {', '.join(PROBES)}", file=sys.stderr)
            return 2
        results.extend(probe(args.download, args.out, overrides.get(name)))

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
        return 0

    print()
    for result in results:
        print(result.line())
        for note in result.notes:
            print(f"     -> {note}")
    print()

    passed = sum(1 for r in results if r.ok)
    print(f"{passed}/{len(results)} checks passed")
    usable = [
        name for name in wanted
        if any(r.ok and "yt-dlp" in r.what for r in results if r.source == name)
    ]
    print("sources that handed over video: " + (", ".join(usable) if usable else "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
