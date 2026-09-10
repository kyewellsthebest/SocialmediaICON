"""Every way of getting a Reddit listing, tried in order until one answers.

The API is not the problem people say it is - a script app is still free and
still instant - but it is a single point of failure, and a page that stops
posting because one endpoint started refusing is not worth running. So this
module holds several genuinely independent ways to ask Reddit what its top gym
video of the day is, and `listing` walks them until something comes back.

They are independent in the sense that matters: each one fails for a different
reason, so one failing says nothing about the next.

    oauth     the supported route. Needs a free script app. Highest limits,
              complete metadata, and the only one that can search site-wide.
    json      the same listings with no app at all, from www.reddit.com.
              Reddit refuses these from datacenter ranges, so this is the one
              that works on a laptop and 403s on Railway.
    proxied   json again, leaving from the downloader's proxy pool - a
              different address, which is the entire reason json refused.
    reader    json again, fetched by a public reader service instead of by us.
              Their address asks, not ours; free, no card, no key.
    mirror    a Redlib front-end. A different domain entirely, so a block on
              reddit.com does not apply. Serves RSS, not JSON, so the metadata
              is thin and has to be filled in.
    rss       Reddit's own Atom feed. Different content type, served by
              different infrastructure, and refused less often than .json.
              Thin metadata, same as mirror.

The last two return titles and links and nothing else - no duration, no votes,
no adult flag - and none of those may be guessed. They are filled in by asking
yt-dlp about each link, which is the same tool that has to download the video
anyway: if it cannot read the post, the post was never postable.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

import httpx

from core import reddit
from core.config import settings

log = logging.getLogger(__name__)

ATOM = "{http://www.w3.org/2005/Atom}"

#: A reddit post id as it appears in a permalink: /comments/<id>/slug/
PERMALINK_ID = re.compile(r"/comments/([a-z0-9]+)", re.I)
#: ...and the room it was posted in, from the same permalink.
PERMALINK_ROOM = re.compile(r"/r/([A-Za-z0-9_]+)/")


class RouteFailed(RuntimeError):
    """This way in did not work. Says nothing about the next one."""


@dataclass
class Entry:
    """What a feed gives you before anything has been looked up."""

    external_id: str
    title: str
    permalink: str
    author: str | None = None
    subreddit: str = ""


# --------------------------------------------------------------------------
# shared pieces


def _path(url: str) -> str:
    """The path of a URL, so a mirror's link can be re-pointed at Reddit.

    A Redlib entry links to the Redlib instance. The path is the same path
    Reddit uses, which is the whole reason the mirror is usable as a source of
    real permalinks rather than only as a reader.
    """
    without_scheme = url.split("://", 1)[-1]
    slash = without_scheme.find("/")
    return without_scheme[slash:] if slash >= 0 else "/"


def parse_atom(text: str) -> list[Entry]:
    """Entries from a Reddit or Redlib Atom feed, links pointed back at Reddit."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise RouteFailed(f"not a feed: {exc}") from exc

    # A block page is often well-formed enough to parse, and parsing it
    # yields no entries - which reads as "the room posted nothing today"
    # rather than "we were refused", and sends nobody to look at anything.
    if not root.tag.endswith(("feed", "rss", "RDF")):
        raise RouteFailed(f"answered with <{root.tag}>, not a feed")

    out: list[Entry] = []
    for node in root.iter(f"{ATOM}entry"):
        link = node.find(f"{ATOM}link")
        href = (link.get("href") if link is not None else None) or ""
        found = PERMALINK_ID.search(href)
        if not found:
            continue
        room = PERMALINK_ROOM.search(href)
        title = (node.findtext(f"{ATOM}title") or "").strip()
        author = node.findtext(f"{ATOM}author/{ATOM}name")
        if author:
            author = author.strip().lstrip("/").removeprefix("u/")
        out.append(
            Entry(
                external_id=found.group(1),
                title=title,
                # Always Reddit's own permalink: a mirror URL handed to yt-dlp
                # is a URL yt-dlp has no extractor for.
                permalink="https://www.reddit.com" + _path(href),
                author=author or None,
                subreddit=room.group(1) if room else "",
            )
        )
    return out


def _children(payload: Any) -> list[dict[str, Any]]:
    """The t3 data dicts out of a listing payload, whatever wrapped it."""
    if isinstance(payload, list):  # a comments-shaped response
        payload = payload[0] if payload else {}
    children = ((payload or {}).get("data") or {}).get("children") or []
    return [c.get("data") or {} for c in children if isinstance(c, dict)]


def _listing_path(room: str | None, sort: str, time_filter: str, limit: int) -> tuple[str, dict]:
    """Where the listing lives and what to ask it for.

    `top` over a window is the important one: it is the vote filter expressed
    as a URL, which is what makes the thin routes usable at all. They cannot
    report scores, but "the top twenty of the last day" is already sorted by
    the thing the score would have been used to check.
    """
    where = f"/r/{room}" if room else ""
    sort = sort if sort in ("top", "hot", "new", "rising", "controversial") else "top"
    params: dict[str, Any] = {"limit": min(100, limit), "raw_json": 1}
    if sort in ("top", "controversial"):
        params["t"] = time_filter
    return f"{where}/{sort}", params


def _q(params: dict[str, Any]) -> str:
    from urllib.parse import urlencode

    return urlencode(params)


# --------------------------------------------------------------------------
# the routes themselves


def by_oauth(room: str | None, sort: str, time_filter: str, limit: int) -> list[reddit.Post]:
    """The supported route: a free script app, over oauth.reddit.com."""
    if not settings.has_reddit:
        raise RouteFailed("no REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET")
    path, params = _listing_path(room, sort, time_filter, limit)
    with reddit.make_client() as client:
        try:
            token = reddit._token(client)
        except reddit.RedditError as exc:
            raise RouteFailed(str(exc)) from exc
        response = client.get(
            reddit.API + path,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": settings.reddit_user_agent,
            },
        )
    if response.status_code >= 400:
        raise RouteFailed(f"{response.status_code} from oauth.reddit.com")
    return _posts(response.json())


def _plain_json(room, sort, time_filter, limit, *, proxy: str | None) -> list[reddit.Post]:
    path, params = _listing_path(room, sort, time_filter, limit)
    kwargs: dict[str, Any] = {"timeout": 30.0, "follow_redirects": True}
    if proxy:
        kwargs["proxy"] = proxy
    with httpx.Client(**kwargs) as client:
        response = client.get(
            f"{reddit.PUBLIC}{path}.json",
            params=params,
            headers={"User-Agent": settings.reddit_user_agent},
        )
    if response.status_code == 403:
        raise RouteFailed("403 - Reddit refuses anonymous reads from this address")
    if response.status_code >= 400:
        raise RouteFailed(f"{response.status_code} from www.reddit.com")
    try:
        payload = response.json()
    except ValueError:
        raise RouteFailed("answered with a page, not JSON (usually a block page)") from None
    return _posts(payload)


def by_json(room: str | None, sort: str, time_filter: str, limit: int) -> list[reddit.Post]:
    """No app at all. Works from a laptop, 403s from most clouds."""
    return _plain_json(room, sort, time_filter, limit, proxy=None)


def by_proxied_json(room: str | None, sort: str, time_filter: str, limit: int) -> list[reddit.Post]:
    """The same request from a different address, which is what it objected to."""
    proxy = settings.reddit_proxy
    if not proxy:
        from core.ytdlp import proxies

        pool = [p for p in proxies() if p]
        if not pool:
            raise RouteFailed("no proxy configured (REDDIT_PROXY / YTDLP_PROXIES)")
        proxy = pool[int(time.time() // 600) % len(pool)]
    return _plain_json(room, sort, time_filter, limit, proxy=proxy)


def by_reader(room: str | None, sort: str, time_filter: str, limit: int) -> list[reddit.Post]:
    """Have a public reader service fetch the JSON, so their address asks.

    These exist to make pages readable by machines and are free at this
    volume without an account. The response is the page's text, so it is the
    JSON itself - it only has to be found inside whatever wrapping came back.
    """
    prefix = (settings.reddit_reader or "").strip()
    if not prefix:
        raise RouteFailed("no reader service configured (REDDIT_READER)")
    path, params = _listing_path(room, sort, time_filter, limit)
    target = f"{reddit.PUBLIC}{path}.json?{_q(params)}"
    headers = {
        "User-Agent": settings.reddit_user_agent,
        # Ask for the page as it is rather than as prose about it.
        "X-Return-Format": "text",
        "Accept": "application/json, text/plain",
    }
    if key := settings.reddit_reader_key:
        headers["Authorization"] = f"Bearer {key}"
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        response = client.get(prefix.rstrip("/") + "/" + target, headers=headers)
    if response.status_code >= 400:
        raise RouteFailed(f"{response.status_code} from the reader service")
    return _posts(_json_inside(response.text))


def _json_inside(text: str) -> Any:
    """The JSON document in `text`, which may be wrapped in markdown."""
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise RouteFailed("the reader returned no JSON - probably a block page")
    try:
        return json.loads(text[start : end + 1])
    except ValueError as exc:
        raise RouteFailed(f"the reader's JSON did not parse: {exc}") from exc


def _feed(url: str, headers: dict[str, str], timeout: float = 30.0) -> list[Entry]:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url, headers=headers)
    if response.status_code >= 400:
        raise RouteFailed(f"{response.status_code}")
    return parse_atom(response.text)


def by_mirror(room: str | None, sort: str, time_filter: str, limit: int) -> list[reddit.Post]:
    """A Redlib front-end: a different domain, so Reddit's block is not in play.

    Public instances come and go, which is why the list is configuration with
    defaults rather than a constant. Each one is given a single try; a mirror
    that is down is not a reason to sit on it.
    """
    if not room:
        raise RouteFailed("a mirror can only serve a named subreddit")
    instances = settings.reddit_mirror_list
    if not instances:
        raise RouteFailed("no mirrors configured (REDDIT_MIRRORS)")
    path, params = _listing_path(room, sort, time_filter, limit)
    problems = []
    for base in instances:
        base = base.rstrip("/")
        # Two shapes, because Redlib serves RSS off two different routes
        # depending on version, and RSS is opt-in on an instance rather than
        # always on - so a 404 here means "not enabled", not "wrong URL", and
        # both shapes are worth one try before moving to the next instance.
        for url in (
            f"{base}{path}.rss?{_q(params)}",
            f"{base}/r/{room}.rss?{_q(dict(params, sort=sort))}",
        ):
            try:
                entries = _feed(
                    url, {"User-Agent": settings.reddit_user_agent}, timeout=20.0)
            except (RouteFailed, httpx.HTTPError) as exc:
                problems.append(f"{base}: {exc}")
                continue
            if entries:
                log.info("reddit: mirror %s answered with %d entries", base, len(entries))
                return hydrate(entries, limit)
            problems.append(f"{base}: empty feed")
    raise RouteFailed("; ".join(problems[:4]))


def by_rss(room: str | None, sort: str, time_filter: str, limit: int) -> list[reddit.Post]:
    """Reddit's own Atom feed. Same host, different door, refused less often."""
    if not room:
        raise RouteFailed("the feed route needs a named subreddit")
    path, params = _listing_path(room, sort, time_filter, limit)
    try:
        entries = _feed(
            f"{reddit.PUBLIC}{path}.rss?{_q(params)}",
            {"User-Agent": settings.reddit_user_agent, "Accept": "application/atom+xml"},
        )
    except httpx.HTTPError as exc:
        raise RouteFailed(str(exc)) from exc
    if not entries:
        raise RouteFailed("empty feed")
    return hydrate(entries, limit)


# --------------------------------------------------------------------------


def _posts(payload: Any) -> list[reddit.Post]:
    found = [reddit._post_from(data) for data in _children(payload)]
    posts = [p for p in found if p]
    if not posts and not _children(payload):
        raise RouteFailed("answered, but with an empty listing")
    return posts


def hydrate(entries: list[Entry], limit: int) -> list[reddit.Post]:
    """Fill in what a feed cannot say, by asking yt-dlp about each post.

    A feed gives a title and a link. Duration, the adult flag and whether the
    video is even hosted by Reddit are all missing, and every one of them is a
    thing that must not be guessed - a guessed adult flag publishes porn to a
    gym page. yt-dlp knows all three, and it is the tool that has to open the
    post to download it regardless, so a post it cannot read was never
    postable in the first place.

    The cost is one network round trip per candidate, which is why this only
    runs on the routes that have no cheaper way to know.
    """
    import yt_dlp

    from core.ytdlp import base_options

    options = base_options(skip_download=True, extract_flat=False)
    out: list[reddit.Post] = []
    with yt_dlp.YoutubeDL(options) as ydl:
        for entry in entries[: max(limit, 0)]:
            try:
                info = ydl.extract_info(entry.permalink, download=False)
            except Exception as exc:  # noqa: BLE001 - one dead post is not a run
                log.debug("reddit: could not read %s (%s)", entry.external_id, exc)
                continue
            if not isinstance(info, dict):
                continue
            if (info.get("extractor_key") or info.get("extractor") or "").lower() != "reddit":
                # A link post to YouTube or Streamable. Not ours to repost, and
                # not ours to download either.
                continue
            duration = info.get("duration")
            out.append(
                reddit.Post(
                    external_id=entry.external_id,
                    title=entry.title or str(info.get("title") or "").strip(),
                    url=entry.permalink,
                    video_url=entry.permalink,
                    # From the permalink, not from yt-dlp. Reddit's extractor
                    # leaves `channel` unset, and falling through to `uploader`
                    # put the *author's* name in the subreddit field - so a
                    # post from r/gym filed itself under r/<whoever posted it>.
                    # That value goes into the attribution sidecar, which is
                    # the file that answers "which post was this?" when someone
                    # asks for their video to be taken down.
                    subreddit=entry.subreddit or str(
                        info.get("channel_id") or "").removeprefix("r/"),
                    author=entry.author or info.get("uploader_id"),
                    duration_s=float(duration) if duration else None,
                    ups=int(info.get("like_count") or 0),
                    upvote_ratio=None,
                    num_comments=int(info.get("comment_count") or 0),
                    created_utc=float(info.get("timestamp") or 0),
                    over_18=int(info.get("age_limit") or 0) >= 18,
                    # Nothing in a feed carries a score. The listing was
                    # already sorted by it, so the ranking survives even though
                    # the number does not - but a threshold cannot be applied
                    # to a number nobody reported.
                    ups_known=bool(info.get("like_count")),
                )
            )
    log.info("reddit: read %d of %d feed entries", len(out), len(entries[:limit]))
    return out


@dataclass
class Route:
    name: str
    why: str
    fetch: Callable[[str | None, str, str, int], list[reddit.Post]]
    #: Feed routes pay a round trip per candidate, so they go last and are
    #: worth marking rather than leaving the ordering to carry the meaning.
    thin: bool = field(default=False)


ROUTES: tuple[Route, ...] = (
    Route("oauth", "a free script app over oauth.reddit.com", by_oauth),
    Route("json", "www.reddit.com/....json, no app", by_json),
    Route("proxied", "the same JSON from a different address", by_proxied_json),
    Route("reader", "a public reader service fetches it for us", by_reader),
    Route("mirror", "a Redlib front-end, different domain", by_mirror, thin=True),
    Route("rss", "Reddit's own Atom feed", by_rss, thin=True),
)

#: The route that worked last, tried first next time. One process that has
#: found a working way in should not re-pay four failures per search.
_preferred: str | None = None


def routes() -> list[Route]:
    """The routes to try, in order, honouring REDDIT_ROUTES and stickiness."""
    allowed = settings.reddit_route_names
    chosen = [r for r in ROUTES if not allowed or r.name in allowed]
    if allowed:
        # An explicit list is an ordering as well as a filter.
        chosen.sort(key=lambda r: allowed.index(r.name))
    if _preferred:
        chosen.sort(key=lambda r: r.name != _preferred)
    return chosen


def listing(
    room: str | None,
    sort: str = "top",
    time_filter: str | None = None,
    limit: int = 50,
) -> tuple[list[reddit.Post], str]:
    """Posts from `room`, by whichever route answers. Also says which one did.

    Raises RedditError only when every route has been tried and none worked,
    with all their reasons - because "Reddit failed" with one reason sends you
    to fix the wrong thing.
    """
    global _preferred
    time_filter = time_filter or settings.reddit_time_filter
    problems: list[str] = []

    for route in routes():
        try:
            posts = route.fetch(room, sort, time_filter, limit)
        except RouteFailed as exc:
            problems.append(f"{route.name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - any failure is the next route's cue
            problems.append(f"{route.name}: {type(exc).__name__}: {exc}")
            continue
        # An empty result here is not a failed route: it answered, and the
        # listing simply held no Reddit-hosted video. Moving on would try five
        # more ways to be told the same thing, then report a reach problem for
        # what is a bounds problem.
        if _preferred != route.name:
            log.info("reddit: reaching Reddit by %s (%s)", route.name, route.why)
            _preferred = route.name
        return posts, route.name

    _preferred = None
    raise reddit.RedditError(
        "no route to Reddit worked - " + "; ".join(problems)
    )
