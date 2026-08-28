"""Which video sources actually work from this server?

Every source decision in this project has been made from documentation and
search results, because the machine that reasons about them has no network.
That has been wrong twice: Reddit was going to be fine and was not; Kick has
an official API that may or may not expose video.

This settles it with evidence. Run it on the box that would actually do the
fetching - Railway, not a laptop, not a sandbox - and it reports, per source,
whether metadata comes back, whether a real media URL is reachable, and
whether bytes actually arrive.

    python -m scripts.probe_sources
    python -m scripts.probe_sources --download   # also pull a few MB

Nothing here writes to the database, posts anything, or needs a key beyond
what is already configured. It is a read-only fact-finder.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field

TIMEOUT_S = 25.0
#: Enough to prove the transfer is real without pulling a whole film.
SAMPLE_BYTES = 2 * 1024 * 1024


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
        return f"[{mark}] {self.source:<16} {self.what:<26} {self.detail}{size}"


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


# --- sources ----------------------------------------------------------------


def probe_archive_org(download: bool) -> list[Result]:
    """archive.org: keyless, and the one source already wired into the studio."""
    out: list[Result] = []
    started = time.time()
    item = "Apollo13Audio"

    try:
        with _client() as client:
            meta = client.get(f"https://archive.org/metadata/{item}").json()
        files = meta.get("files", []) or []
        videos = [f for f in files if str(f.get("name", "")).lower().endswith((".mp4", ".ogv"))]
        result = Result(
            "archive.org", "metadata",
            ok=bool(files),
            detail=f"{len(files)} files, {len(videos)} video",
            elapsed_s=time.time() - started,
        )
        if videos:
            result.media_url = f"https://archive.org/download/{item}/{videos[0]['name']}"
        out.append(result)
    except Exception as exc:  # noqa: BLE001 - the whole point is to report failures
        out.append(Result("archive.org", "metadata", detail=f"{type(exc).__name__}: {exc}"))
        return out

    # A search for moving images proves the discovery half, not just one item.
    try:
        started = time.time()
        with _client() as client:
            response = client.get(
                "https://archive.org/advancedsearch.php",
                params={
                    "q": "mediatype:movies AND collection:prelinger",
                    "fl[]": "identifier", "rows": 5, "output": "json",
                },
            )
            docs = response.json().get("response", {}).get("docs", []) or []
        out.append(Result(
            "archive.org", "search movies",
            ok=bool(docs),
            detail=f"{len(docs)} hits: {', '.join(d['identifier'] for d in docs[:2])}",
            elapsed_s=time.time() - started,
        ))
        if docs and download:
            ident = docs[0]["identifier"]
            with _client() as client:
                files = client.get(f"https://archive.org/metadata/{ident}").json().get("files", [])
            mp4s = [f for f in files if str(f.get("name", "")).lower().endswith(".mp4")]
            if mp4s:
                url = f"https://archive.org/download/{ident}/{mp4s[0]['name']}"
                started = time.time()
                read, note = _pull(url)
                out.append(Result(
                    "archive.org", "download mp4",
                    ok=read > 0, detail=note, media_url=url,
                    bytes_read=read, elapsed_s=time.time() - started,
                ))
    except Exception as exc:  # noqa: BLE001
        out.append(Result("archive.org", "search movies", detail=f"{type(exc).__name__}: {exc}"))

    return out


def probe_dvids(download: bool) -> list[Result]:
    """DVIDS: 1.8M US military assets, public domain, documented JSON API."""
    out: list[Result] = []
    started = time.time()
    try:
        with _client() as client:
            response = client.get(
                "https://api.dvidshub.net/search",
                params={"q": "aircraft", "type": "video", "max_results": 5},
            )
        if response.status_code == 401 or response.status_code == 403:
            out.append(Result(
                "dvids", "search",
                detail=f"HTTP {response.status_code} - needs an API key",
                notes=["register at https://api.dvidshub.net for a free key"],
                elapsed_s=time.time() - started,
            ))
            return out
        payload = response.json()
        results = payload.get("results", []) or []
        first = results[0] if results else {}
        out.append(Result(
            "dvids", "search video",
            ok=bool(results),
            detail=f"{len(results)} hits, keys: {','.join(sorted(first)[:6])}",
            elapsed_s=time.time() - started,
        ))
        url = first.get("video_url") or first.get("url")
        if url and download:
            started = time.time()
            read, note = _pull(url)
            out.append(Result(
                "dvids", "download", ok=read > 0, detail=note,
                media_url=url, bytes_read=read, elapsed_s=time.time() - started,
            ))
    except Exception as exc:  # noqa: BLE001
        out.append(Result("dvids", "search", detail=f"{type(exc).__name__}: {exc}"))
    return out


def probe_twitch(download: bool) -> list[Result]:
    """Twitch: official API, and clips a human already chose to cut."""
    from core.config import settings

    out: list[Result] = []
    client_id = getattr(settings, "twitch_client_id", None)
    secret = getattr(settings, "twitch_client_secret", None)
    if not (client_id and secret):
        out.append(Result(
            "twitch", "app token",
            detail="TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set",
            notes=["free at https://dev.twitch.tv/console - no review needed"],
        ))
        return out

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
                params={"name": "Just Chatting"}, headers=headers,
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

        if clips and download:
            # The mp4 is derivable from the thumbnail URL - the documented
            # trick every Twitch downloader uses, and worth proving here.
            thumb = clips[0].get("thumbnail_url", "")
            url = thumb.split("-preview-")[0] + ".mp4" if "-preview-" in thumb else ""
            if url:
                started = time.time()
                read, note = _pull(url)
                out.append(Result(
                    "twitch", "download clip", ok=read > 0, detail=note,
                    media_url=url, bytes_read=read, elapsed_s=time.time() - started,
                ))
    except Exception as exc:  # noqa: BLE001
        out.append(Result("twitch", "api", detail=f"{type(exc).__name__}: {exc}"))
    return out


def probe_kick(download: bool) -> list[Result]:
    """Kick: does the public API expose video at all? The open question."""
    out: list[Result] = []
    started = time.time()
    try:
        with _client() as client:
            response = client.get("https://api.kick.com/public/v1/categories", params={"q": "irl"})
        out.append(Result(
            "kick", "public API reachable",
            ok=response.status_code < 400,
            detail=f"HTTP {response.status_code}"
            + (" (needs OAuth)" if response.status_code in (401, 403) else ""),
            elapsed_s=time.time() - started,
        ))
    except Exception as exc:  # noqa: BLE001
        out.append(Result("kick", "public API reachable", detail=f"{type(exc).__name__}: {exc}"))

    # Scraping is the route the open yt-dlp issues say is dead. Confirm here
    # rather than take a forum's word for it.
    started = time.time()
    try:
        with _client() as client:
            response = client.get("https://kick.com/api/v2/channels/xqc")
        out.append(Result(
            "kick", "site API (scrape)",
            ok=response.status_code < 400,
            detail=f"HTTP {response.status_code}"
            + (" - Cloudflare" if response.status_code in (403, 503) else ""),
            elapsed_s=time.time() - started,
        ))
    except Exception as exc:  # noqa: BLE001
        out.append(Result("kick", "site API (scrape)", detail=f"{type(exc).__name__}: {exc}"))
    return out


PROBES = {
    "archive.org": probe_archive_org,
    "dvids": probe_dvids,
    "twitch": probe_twitch,
    "kick": probe_kick,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="also pull a few MB of media")
    parser.add_argument("--only", help="comma separated: " + ", ".join(PROBES))
    parser.add_argument("--json", action="store_true", help="machine readable output")
    args = parser.parse_args(argv)

    wanted = (
        [n.strip() for n in args.only.split(",")] if args.only else list(PROBES)
    )
    results: list[Result] = []
    for name in wanted:
        probe = PROBES.get(name)
        if probe is None:
            print(f"unknown source {name!r}; expected one of {', '.join(PROBES)}", file=sys.stderr)
            return 2
        results.extend(probe(args.download))

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
        return 0

    print()
    for result in results:
        print(result.line())
        for note in result.notes:
            print(f"       -> {note}")
    print()

    passed = sum(1 for r in results if r.ok)
    print(f"{passed}/{len(results)} checks passed")
    usable = sorted({r.source for r in results if r.ok})
    print("sources answering: " + (", ".join(usable) if usable else "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
