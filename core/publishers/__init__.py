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


def destinations() -> list[str]:
    """Where a reel goes, worked out from the credentials that are set.

    There used to be a table of handles to fill in by hand, and it was the
    thing stopping anything from posting: the credentials named the accounts
    perfectly well, and the table sat empty beside them saying there was
    nowhere to post to.

    Two records of the same fact is one too many. INSTAGRAM_USER_ID *is* the
    Instagram account; a handle typed next to it adds nothing and can
    disagree with it. So the destinations are derived, and the only way to
    change them is to change the credentials - which is also the only way to
    change where a post can actually land.
    """
    choice = (settings.publisher or "manual").lower()

    if choice == "youtube":
        return ["youtube"] if settings.has_youtube_write else []

    if choice == "upload_post":
        # The reseller can reach a dozen platforms with one call, and which
        # ones is a decision rather than a credential - the same key posts
        # everywhere. So this one is named explicitly.
        from core.publishers.upload_post import PLATFORM_NAMES

        if not settings.has_upload_post:
            return []
        wanted = [p.strip().lower() for p in settings.upload_post_platforms.split(",")]
        return [p for p in wanted if p in PLATFORM_NAMES]

    # meta, and manual, which reports what *would* be reachable rather than
    # an empty list - "nothing configured" and "configured but switched off"
    # are different problems and should not read the same.
    reachable = []
    if settings.has_instagram:
        reachable.append("instagram")
    if settings.has_threads:
        reachable.append("threads")
    if settings.has_facebook:
        reachable.append("facebook")
    if choice == "manual" and settings.has_youtube_write:
        reachable.append("youtube")
    return reachable
