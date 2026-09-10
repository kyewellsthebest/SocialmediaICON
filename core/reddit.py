"""Reddit as the source of finished video.

Nothing here is edited. Somebody already decided the moment was worth posting,
framed it, chose where it starts and stops, and several thousand people agreed
by upvoting it. The video arrives under a minute long with a title its author
wrote - which is why this path needs no model, no transcript, and no key beyond
a free Reddit app it can also do without.

What it does need is discipline about two things, because there is no editor
downstream to catch either: nothing adult, and nothing longer than short-form.
Both live in `postable`, the one function every path goes through.

Reddit refuses unauthenticated reads from datacenter ranges with a 403.
Credentials avoid that, and failing those, core.reddit_routes has five other
ways in - the feeds among them, which is what a cloud host usually ends up on.
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
    #: Whether `ups` is a number Reddit reported or a placeholder. The feed
    #: routes in core.reddit_routes cannot see scores at all, and a zero that
    #: means "not reported" must not be read as a zero that means "nobody
    #: voted" - that would refuse every post those routes ever find.
    ups_known: bool = True

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
    try:
        payload = response.json()
    except ValueError:
        # Reddit answers a refused request with an HTML page, and parsing that
        # as JSON raises something that says nothing about what went wrong.
        raise RedditError(
            f"the token request was refused ({response.status_code}). Check the "
            "client id and secret, and that the app is a 'script' app."
        ) from None

    token = payload.get("access_token")
    if not token:
        raise RedditError(f"could not get a token: {str(payload)[:200]}")
    return str(token)


def _proxy() -> str | None:
    """A proxy for Reddit, reusing whatever the downloader was given.

    Reddit blocks the public endpoint from datacenter ranges, so on a cloud
    host an unauthenticated request needs to leave from somewhere else.
    """
    if settings.reddit_proxy:
        return settings.reddit_proxy
    if settings.has_reddit:
        return None  # credentials are the better answer; no proxy needed
    from core.ytdlp import proxies

    pool = [p for p in proxies() if p]
    if not pool:
        return None
    # Walk the pool with the clock so one address does not carry every request.
    return pool[int(time.time() // 600) % len(pool)]


def make_client(timeout: float = 30.0) -> httpx.Client:
    """An httpx client configured the way Reddit needs to be asked."""
    kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
    if proxy := _proxy():
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


def _endpoint(client: httpx.Client) -> tuple[str, dict[str, str]]:
    """Where to ask, and with what headers.

    With credentials, the OAuth host: higher limits and a stable contract.
    Without, the public JSON host, which needs no app at all. Reddit rejects
    requests with a default user agent either way, so that header is not
    optional.
    """
    agent = settings.reddit_user_agent
    if settings.has_reddit:
        # Credentials that do not work are a problem to report, not to route
        # around: the public endpoint fails too, and its message would tell
        # you to set the credentials you have already set.
        return API, {"Authorization": f"Bearer {_token(client)}", "User-Agent": agent}
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


def postable(post: Post) -> tuple[bool, str]:
    """Whether this video can go on a repost page as it stands, and why not.

    Different question from the scout's `wanted`, which looked for long video
    worth cutting down. Nothing here gets edited: what is found is what is
    posted, so the bounds are the platform's rather than an editor's.

    The adult check is here rather than in the caller on purpose. `search`
    already sends include_over_18=false and that is not enough to rely on -
    it is a preference on a listing, it does not cover a crosspost out of a
    quarantined room, and a caller that forgets it publishes the result. One
    refusal in the module every path goes through is worth more than the same
    line copied into three tasks.

    The vote threshold is the one bound that can be skipped, and only when the
    route that found the post could not see votes. Sorting by top over a window
    has already applied the same ranking the threshold was standing in for; the
    adult and duration checks are never skipped, because nothing else in the
    system is looking.
    """
    if post.over_18:
        return False, "adult"
    if post.duration_s is None:
        # v.redd.it always reports a duration. Missing means the payload was
        # not what it claimed, and guessing costs a download to find out.
        return False, "no duration"
    if post.duration_s > settings.reddit_max_duration_s:
        return False, f"{post.duration_s:.0f}s is longer than short-form"
    if post.duration_s < settings.reddit_floor_duration_s:
        return False, f"{post.duration_s:.0f}s is not a post"
    if post.ups_known and post.ups < settings.reddit_min_upvotes:
        return False, f"{post.ups} upvotes"
    return True, ""


def search(
    query: str,
    sort: str = "top",
    time_filter: str = "month",
    limit: int = 100,
    client: httpx.Client | None = None,
    subreddit: str | None = None,
) -> list[Post]:
    """Search for video posts matching `query`.

    `sort` is one of relevance, hot, top, new, comments. `time_filter` is
    hour, day, week, month, year, all - and only applies to top and comments.

    Site-wide by default, which is what a scout hunting one good video wants.
    Pass `subreddit` to search inside one room instead: a niche feed needs the
    rooms named, because "gym" across all of Reddit returns memes, screenshots
    of texts, and photographs of actual gymnasium buildings.
    """
    owns_client = client is None
    client = client or make_client()
    try:
        base, headers = _endpoint(client)
        where = f"/r/{subreddit}" if subreddit else ""
        params: dict[str, Any] = {
            "q": query,
            "sort": sort,
            "t": time_filter,
            "limit": min(100, limit),
            "type": "link",
            "include_over_18": "false",
            "raw_json": 1,
        }
        if subreddit:
            # Without this Reddit widens a subreddit search back out to the
            # whole site and answers with everything, which looks like the
            # filter silently doing nothing.
            params["restrict_sr"] = 1
        response = client.get(
            f"{base}{where}/search{'' if base == API else '.json'}",
            params=params,
            headers=headers,
        )
        if response.status_code == 403:
            raise RedditError(
                "Reddit refused the request (403). It blocks unauthenticated "
                "reads from datacenter ranges: set REDDIT_CLIENT_ID and "
                "REDDIT_CLIENT_SECRET, or REDDIT_PROXY to a residential proxy."
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
