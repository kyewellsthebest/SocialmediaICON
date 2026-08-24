"""Publishing adapters.

One interface, four backends: post nothing (manual), post everywhere through a
reseller (upload_post), post to YouTube yourself with your own OAuth app, or
post to Instagram, Threads and Facebook yourself with your own Meta app.
Which one runs is a config switch, so you can start manual, add YouTube and Meta
when the clips are good, and add the reseller when you want TikTok and Snapchat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from core.config import settings


@dataclass
class PublishRequest:
    clip_path: Path
    title: str
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    privacy: str = "public"
    # Meta downloads the file rather than accepting an upload, so it needs a
    # URL. Either is enough: a key to presign, or a URL already in hand.
    storage_key: str | None = None
    public_url: str | None = None

    @property
    def caption(self) -> str:
        tags = " ".join(self.hashtags)
        return f"{self.description}\n\n{tags}".strip()


@dataclass
class PublishResult:
    platform: str
    ok: bool
    post_id: str | None = None
    url: str | None = None
    error: str | None = None


class Publisher(Protocol):
    name: str

    def publish(self, request: PublishRequest) -> list[PublishResult]: ...


def get_publisher(name: str | None = None) -> Publisher:
    choice = (name or settings.publisher or "manual").lower()
    if choice == "upload_post":
        from core.publishers.upload_post import UploadPostPublisher

        return UploadPostPublisher()
    if choice == "youtube":
        from core.publishers.youtube import YouTubePublisher

        return YouTubePublisher()
    if choice == "meta":
        from core.publishers.meta import MetaPublisher

        return MetaPublisher()
    from core.publishers.manual import ManualPublisher

    return ManualPublisher()
