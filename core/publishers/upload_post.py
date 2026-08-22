"""Reseller backend — one call, several platforms.

This is the only way to reach Snapchat Spotlight, and it skips TikTok's app
audit because the reseller has already passed one.

The request shape below follows Upload-Post's documented multipart form. If they
change a field name, this single file is the only thing that needs editing —
nothing else in the codebase knows how publishing works. Run
`python scripts/check_publisher.py` after setting your key to confirm the
contract before you trust it with a queue.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from core.config import settings
from core.publishers import PublishRequest, PublishResult

log = logging.getLogger(__name__)

# Platform names as the API expects them, keyed by ours.
PLATFORM_NAMES = {
    "tiktok": "tiktok",
    "instagram": "instagram",
    "youtube": "youtube",
    "facebook": "facebook",
    "threads": "threads",
    "x": "x",
    "linkedin": "linkedin",
    "pinterest": "pinterest",
}


class UploadPostPublisher:
    name = "upload_post"

    def __init__(self) -> None:
        if not settings.upload_post_api_key:
            raise RuntimeError("UPLOAD_POST_API_KEY is not set")
        if not settings.upload_post_user:
            raise RuntimeError("UPLOAD_POST_USER is not set (the profile name in their dashboard)")
        self.base_url = settings.upload_post_base_url.rstrip("/")

    def publish(self, request: PublishRequest) -> list[PublishResult]:
        platforms = [PLATFORM_NAMES.get(p, p) for p in request.platforms]
        if not platforms:
            return [PublishResult(platform="none", ok=False, error="no platforms configured")]

        clip = Path(request.clip_path)
        data = [("user", settings.upload_post_user), ("title", request.title[:150])]
        data += [("platform[]", platform) for platform in platforms]
        # Per-platform caption fields; harmless where a platform ignores them.
        data += [
            ("description", request.caption[:2000]),
            ("caption", request.caption[:2000]),
        ]

        try:
            with clip.open("rb") as fh, httpx.Client(
                timeout=httpx.Timeout(900.0, connect=30.0)
            ) as client:
                response = client.post(
                    f"{self.base_url}/api/upload",
                    headers={"Authorization": f"Apikey {settings.upload_post_api_key}"},
                    data=data,
                    files={"video": (clip.name, fh, "video/mp4")},
                )
        except httpx.HTTPError as exc:
            return [
                PublishResult(platform=p, ok=False, error=f"request failed: {exc}")
                for p in platforms
            ]

        if response.status_code >= 400:
            detail = response.text[:400]
            log.error("upload-post rejected the post (%s): %s", response.status_code, detail)
            return [
                PublishResult(platform=p, ok=False, error=f"HTTP {response.status_code}: {detail}")
                for p in platforms
            ]

        return self._parse(response.json(), platforms)

    def _parse(self, payload: dict, platforms: list[str]) -> list[PublishResult]:
        """Read per-platform outcomes, tolerating a few response shapes."""
        results: list[PublishResult] = []
        per_platform = payload.get("results") or payload.get("platforms") or {}

        for platform in platforms:
            entry = per_platform.get(platform) if isinstance(per_platform, dict) else None
            if entry is None:
                # No per-platform detail: fall back to the top-level status.
                ok = bool(payload.get("success", True))
                results.append(
                    PublishResult(
                        platform=platform,
                        ok=ok,
                        post_id=str(payload.get("id") or "") or None,
                        error=None if ok else str(payload)[:300],
                    )
                )
                continue

            if isinstance(entry, dict):
                ok = bool(entry.get("success", True)) and not entry.get("error")
                results.append(
                    PublishResult(
                        platform=platform,
                        ok=ok,
                        post_id=str(
                            entry.get("post_id") or entry.get("id") or entry.get("share_id") or ""
                        )
                        or None,
                        url=entry.get("url") or entry.get("permalink"),
                        error=str(entry.get("error"))[:300] if entry.get("error") else None,
                    )
                )
            else:
                results.append(PublishResult(platform=platform, ok=bool(entry)))
        return results
