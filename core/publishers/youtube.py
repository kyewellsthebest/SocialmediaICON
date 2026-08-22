"""Native YouTube Shorts upload with your own OAuth app.

Free, no reseller in the middle, and since the December 2025 quota change an
upload costs roughly 100 units against a 10,000/day allowance. A vertical video
under three minutes is treated as a Short automatically.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from core.config import settings
from core.publishers import PublishRequest, PublishResult
from core.youtube import COST_UPLOAD, _spend

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
WATCH_URL = "https://www.youtube.com/watch?v="

CATEGORY_ENTERTAINMENT = "24"


class YouTubePublisher:
    name = "youtube"

    def _access_token(self) -> str:
        missing = [
            var
            for var, value in (
                ("YOUTUBE_CLIENT_ID", settings.youtube_client_id),
                ("YOUTUBE_CLIENT_SECRET", settings.youtube_client_secret),
                ("YOUTUBE_REFRESH_TOKEN", settings.youtube_refresh_token),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"missing YouTube OAuth config: {', '.join(missing)}")

        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                TOKEN_URL,
                data={
                    "client_id": settings.youtube_client_id,
                    "client_secret": settings.youtube_client_secret,
                    "refresh_token": settings.youtube_refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        response.raise_for_status()
        return response.json()["access_token"]

    def publish(self, request: PublishRequest) -> list[PublishResult]:
        try:
            post_id = self._upload(request)
        except Exception as exc:  # noqa: BLE001 - reported, not raised, per platform
            log.exception("youtube upload failed")
            return [PublishResult(platform="youtube", ok=False, error=str(exc)[:500])]
        return [
            PublishResult(
                platform="youtube", ok=True, post_id=post_id, url=f"{WATCH_URL}{post_id}"
            )
        ]

    def _upload(self, request: PublishRequest) -> str:
        clip = Path(request.clip_path)
        size = clip.stat().st_size
        token = self._access_token()

        # Tags travel in the tags array, not the title; the hashtags still go in
        # the description because that is what surfaces under a Short.
        metadata = {
            "snippet": {
                "title": request.title[:100] or clip.stem,
                "description": request.caption[:4900],
                "tags": [tag.lstrip("#") for tag in request.hashtags][:15],
                "categoryId": CATEGORY_ENTERTAINMENT,
            },
            "status": {
                "privacyStatus": request.privacy,
                "selfDeclaredMadeForKids": False,
            },
        }

        _spend(COST_UPLOAD)

        with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
            start = client.post(
                UPLOAD_URL,
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Upload-Content-Type": "video/mp4",
                    "X-Upload-Content-Length": str(size),
                },
                json=metadata,
            )
            start.raise_for_status()
            session_uri = start.headers.get("location") or start.headers.get("Location")
            if not session_uri:
                raise RuntimeError("YouTube did not return a resumable upload URL")

            with clip.open("rb") as fh:
                finish = client.put(
                    session_uri,
                    headers={"Content-Type": "video/mp4", "Content-Length": str(size)},
                    content=fh.read(),
                )
            finish.raise_for_status()

        video_id = finish.json().get("id")
        if not video_id:
            raise RuntimeError(f"upload succeeded but no video id came back: {finish.text[:300]}")
        log.info("uploaded %s to youtube as %s", clip.name, video_id)
        return video_id
