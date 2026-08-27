"""Pexels stock footage.

Free, documented, licensed for commercial use with no attribution, and the only
stock source with an API a bot can use. The free key allows 200 requests an
hour and 20,000 a month, which is far more than a few videos a day will touch.

Clips are cached on disk by (term, index) so the twentieth render of an Apollo
video reuses the same handful of downloads. Variety comes from the grade and
the recombination, not from fetching a fresh file every time.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import settings

log = logging.getLogger(__name__)

API = "https://api.pexels.com/videos/search"
TIMEOUT_S = 30.0
#: Downloading a 4K master to crop it to 1080x1920 wastes bandwidth and time.
MAX_WIDTH = 2200


class PexelsError(RuntimeError):
    pass


@dataclass
class Clip:
    id: int
    url: str
    width: int
    height: int
    duration_s: float
    file_url: str

    @property
    def vertical(self) -> bool:
        return self.height >= self.width


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "clip"


def _pick_file(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Largest mp4 that is still under MAX_WIDTH.

    Pexels returns every rendition of a clip from 640px up to the original.
    The render crops to 1080x1920 either way, so anything past roughly 2K is
    bandwidth spent on pixels that get thrown away.
    """
    usable = [
        f
        for f in files
        if str(f.get("file_type", "")).endswith("mp4") and f.get("link") and f.get("width")
    ]
    if not usable:
        return None
    under = [f for f in usable if int(f["width"]) <= MAX_WIDTH]
    pool = under or usable
    return max(pool, key=lambda f: int(f["width"]))


def search(term: str, *, per_page: int = 12, orientation: str = "portrait") -> list[Clip]:
    """Clips matching `term`, best first.

    Portrait is asked for but not required: the library is overwhelmingly
    landscape, and a landscape clip cropped to 9:16 is fine when the overlay
    sits on top of it. An empty portrait result falls back to any orientation
    rather than returning nothing.
    """
    if not settings.pexels_api_key:
        raise PexelsError("PEXELS_API_KEY is not set")

    import httpx

    def _query(orient: str | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": term, "per_page": per_page, "size": "medium"}
        if orient:
            params["orientation"] = orient
        response = httpx.get(
            API,
            params=params,
            headers={"Authorization": settings.pexels_api_key},
            timeout=TIMEOUT_S,
        )
        if response.status_code == 401:
            raise PexelsError("Pexels rejected the key - check PEXELS_API_KEY")
        if response.status_code == 429:
            raise PexelsError("Pexels rate limit reached (200/hour) - try again shortly")
        response.raise_for_status()
        return response.json().get("videos", []) or []

    raw = _query(orientation) or _query(None)

    clips: list[Clip] = []
    for video in raw:
        chosen = _pick_file(video.get("video_files", []) or [])
        if chosen is None:
            continue
        clips.append(
            Clip(
                id=int(video.get("id", 0)),
                url=str(video.get("url", "")),
                width=int(chosen["width"]),
                height=int(chosen.get("height") or 0),
                duration_s=float(video.get("duration") or 0.0),
                file_url=str(chosen["link"]),
            )
        )
    return clips


def cache_dir() -> Path:
    path = Path(settings.work_dir) / "stock"
    path.mkdir(parents=True, exist_ok=True)
    return path


def download(clip: Clip, term: str, index: int) -> Path:
    """Fetch a clip to the cache, or return the copy already there."""
    import httpx

    dest = cache_dir() / f"{_slug(term)}-{index:02d}-{clip.id}.mp4"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    tmp = dest.with_suffix(".part")
    with httpx.stream("GET", clip.file_url, timeout=TIMEOUT_S, follow_redirects=True) as response:
        response.raise_for_status()
        with tmp.open("wb") as handle:
            for chunk in response.iter_bytes(1 << 16):
                handle.write(chunk)
    tmp.replace(dest)
    log.info("stock: cached %s (%.1fs, %dx%d)", dest.name, clip.duration_s, clip.width, clip.height)
    return dest


def fetch_for(
    terms: tuple[str, ...] | list[str],
    *,
    want: int = 1,
    min_s: float = 4.0,
) -> list[Path]:
    """Local paths to `want` usable clips, trying each term in turn.

    Returns fewer than asked - possibly none - rather than raising: the studio
    treats footage as an improvement on its own drawn plate, not a requirement,
    so a rate limit or a thin search result should soften the video, not fail
    the render.
    """
    out: list[Path] = []
    for term in terms:
        if len(out) >= want:
            break
        try:
            found = [c for c in search(term) if c.duration_s >= min_s]
        except Exception as exc:  # noqa: BLE001 - footage is optional
            log.warning("stock: search for %r failed: %s", term, exc)
            continue
        # Prefer portrait, then longest: a long clip can be re-entered at a
        # different offset in a later video and read as different footage.
        found.sort(key=lambda c: (not c.vertical, -c.duration_s))
        for index, clip in enumerate(found[: max(1, want - len(out))]):
            try:
                out.append(download(clip, term, index))
            except Exception as exc:  # noqa: BLE001
                log.warning("stock: download of %s failed: %s", clip.id, exc)
    return out
