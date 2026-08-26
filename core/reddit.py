"""Reddit as a source of clippable video.

Reddit's search is site-wide, so one query reaches every subreddit at once -
which matters here, because the good metal detecting video is scattered across
r/metaldetecting, r/Damnthatsinteresting, r/interestingasfuck and a dozen
others rather than sitting in one place.

Two things make it worth the trouble over YouTube:

* It answers from a datacenter. No proxy, no cookies, no bot check.
* The comments say *why* a video is good. A replay curve tells you people
  rewound to 4:12; a comment thread tells you they rewound because of what he
  said when the coil went over it. That is a better instruction for where to
  cut.

Two things are worse: there are no view counts, only votes, and much of what
is posted is a crosspost to YouTube, which is the door we already found shut.
Both are handled by filtering rather than hoping.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from core.config import settings

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
# Reddit serves the same listings as JSON to anyone who asks politely, with
# no app and no token. Lower rate limits, but a handful of searches every few
# hours is nowhere near them - and creating an app is a step that can just
# refuse to work, which should not be the thing that stops the scout.
PUBLIC = "https://www.reddit.com"

# Video Reddit hosts itself. Everything else is a link to somewhere we either
# cannot download from or have no business reposting.
NATIVE_DOMAIN = "v.redd.it"


class RedditError(RuntimeError):
    pass


@dataclass
class Post:
    """One search result, flattened to what the pipeline needs."""

    external_id: str
    title: str
    url: str  # the reddit permalink, for humans
    video_url: str  # what yt-dlp is given
    subreddit: str
    author: str | None
    duration_s: float | None
    ups: int
    upvote_ratio: float | None
    num_comments: int
    created_utc: float
    over_18: bool

    @property
    def age_hours(self) -> float:
        return max((time.time() - self.created_utc) / 3600, 0.01)


def _token(client: httpx.Client) -> str:
    """Application-only OAuth. No user account, no password, read-only."""
    if not (settings.reddit_client_id and settings.reddit_client_secret):
        raise RedditError("REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not set")

    response = client.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(settings.reddit_client_id, settings.reddit_client_secret),
        headers={"User-Agent": settings.reddit_user_agent},
    )
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RedditError(f"could not get a token: {str(payload)[:200]}")
    return str(token)


def _endpoint(client: httpx.Client) -> tuple[str, dict[str, str]]:
    """Where to ask, and with what headers.

    With credentials, the OAuth host: higher limits and a stable contract.
    Without, the public JSON host, which needs no app at all. Reddit rejects
    requests with a default user agent either way, so that header is not
    optional.
    """
    agent = settings.reddit_user_agent
    if settings.has_reddit:
        try:
            return API, {"Authorization": f"Bearer {_token(client)}", "User-Agent": agent}
        except RedditError as exc:
            log.warning("falling back to the public endpoint: %s", exc)
    return PUBLIC, {"User-Agent": agent}


def _post_from(data: dict[str, Any]) -> Post | None:
    """Flatten a listing entry, or None if it is not a video we can use."""
    if data.get("is_self") or data.get("stickied"):
        return None
    if data.get("domain") != NATIVE_DOMAIN:
        return None

    media = (data.get("secure_media") or data.get("media") or {}).get("reddit_video") or {}
    fallback = media.get("fallback_url")
    if not fallback:
        return None

    return Post(
        external_id=str(data.get("id") or ""),
        title=str(data.get("title") or "").strip(),
        url="https://www.reddit.com" + str(data.get("permalink") or ""),
        # yt-dlp is given the permalink, not the fallback: the fallback is
        # video-only, and yt-dlp knows how to pair it with the audio track.
        video_url="https://www.reddit.com" + str(data.get("permalink") or ""),
        subreddit=str(data.get("subreddit") or ""),
        author=data.get("author"),
        duration_s=float(media["duration"]) if media.get("duration") else None,
        ups=int(data.get("ups") or 0),
        upvote_ratio=data.get("upvote_ratio"),
        num_comments=int(data.get("num_comments") or 0),
        created_utc=float(data.get("created_utc") or 0),
        over_18=bool(data.get("over_18")),
    )


def search(
    query: str,
    sort: str = "top",
    time_filter: str = "month",
    limit: int = 100,
    client: httpx.Client | None = None,
) -> list[Post]:
    """Site-wide search for video posts matching `query`.

    `sort` is one of relevance, hot, top, new, comments. `time_filter` is
    hour, day, week, month, year, all - and only applies to top and comments.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        base, headers = _endpoint(client)
        response = client.get(
            f"{base}/search{'' if base == API else '.json'}",
            params={
                "q": query,
                "sort": sort,
                "t": time_filter,
                "limit": min(100, limit),
                "type": "link",
                "include_over_18": "false",
                "raw_json": 1,
            },
            headers=headers,
        )
        if response.status_code >= 400:
            raise RedditError(f"search failed ({response.status_code}): {response.text[:200]}")

        children = (response.json().get("data") or {}).get("children") or []
        posts = [p for p in (_post_from(c.get("data") or {}) for c in children) if p]
        log.info("reddit %r -> %d posts, %d native video", query, len(children), len(posts))
        return posts
    finally:
        if owns_client:
            client.close()


def top_comments(
    post_id: str, limit: int = 25, client: httpx.Client | None = None
) -> list[dict[str, Any]]:
    """The highest-voted comments on a post.

    This is the part a replay curve cannot give you: not where people reacted,
    but what they said about it. Returned oldest-field-first so the caller can
    hand them to a model without reshaping.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        base, headers = _endpoint(client)
        response = client.get(
            f"{base}/comments/{post_id}{'' if base == API else '.json'}",
            params={"sort": "top", "limit": limit, "depth": 1, "raw_json": 1},
            headers=headers,
        )
        if response.status_code >= 400:
            raise RedditError(f"comments failed ({response.status_code})")

        payload = response.json()
        # [0] is the post itself, [1] is the comment tree.
        if not isinstance(payload, list) or len(payload) < 2:
            return []

        out: list[dict[str, Any]] = []
        for child in (payload[1].get("data") or {}).get("children") or []:
            data = child.get("data") or {}
            body = (data.get("body") or "").strip()
            if not body or body in ("[deleted]", "[removed]"):
                continue
            out.append(
                {
                    "body": body,
                    "ups": int(data.get("ups") or 0),
                    "author": data.get("author"),
                }
            )
        out.sort(key=lambda c: c["ups"], reverse=True)
        return out[:limit]
    finally:
        if owns_client:
            client.close()
