"""Stage 7 (Phase 3) - post an approved clip via an official publishing API.

Deliberately not implemented yet. Phases 1-2 are human-in-the-loop: the bot
renders the clip and writes the metadata, a human approves and posts.

When this is built, the order is YouTube Shorts first (standard OAuth, quota
limited), then IG Reels (Business/Creator account + Meta app review). TikTok's
Content Posting API requires an app audit - unaudited apps can only create
drafts, so do not design the queue assuming auto-post works there on day one.
Snapchat has no practical public posting API for this: manual only.
"""

from __future__ import annotations

PHASE_3_MESSAGE = (
    "Publishing is Phase 3. Approve clips in the review queue and post them "
    "manually until the platform apps are approved."
)


def run(clip_id: int, account_id: int) -> None:
    raise NotImplementedError(PHASE_3_MESSAGE)
