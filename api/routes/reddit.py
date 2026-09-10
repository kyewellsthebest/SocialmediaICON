"""Which ways in to Reddit this deployment can actually use.

The same measurement as scripts/reddit_ways_in.py, reachable from a browser,
because the machine that needs measuring has no terminal on it. That is the
whole point of the endpoint: a route that answers from a laptop says nothing
about whether it answers from Railway, and the difference between those two
addresses is the entire problem. Asking the deployment itself is the only way
to get a true answer.

It makes real outbound requests and is therefore slow - up to a minute or two
if several routes are hanging. It is a diagnostic run by hand, not something
the dashboard polls.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from core import reddit, reddit_routes
from core.config import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/reddit", tags=["reddit"])


def _try(route: reddit_routes.Route, room: str, sort: str,
         time_filter: str, limit: int) -> dict[str, Any]:
    began = time.time()
    try:
        posts = route.fetch(room, sort, time_filter, limit)
    except Exception as exc:  # noqa: BLE001 - the failure is the measurement
        return {
            "route": route.name, "ok": False,
            "seconds": round(time.time() - began, 1),
            "reason": (str(exc) or type(exc).__name__)[:300],
        }
    postable = [p for p in posts if reddit.postable(p)[0]]
    return {
        "route": route.name, "ok": True,
        "seconds": round(time.time() - began, 1),
        "video": len(posts), "postable": len(postable),
        # Feed routes cannot report a score, so the upvote bound is not being
        # applied on them. Worth saying out loud rather than leaving to be
        # discovered when the page starts posting 20-upvote videos.
        "sees_scores": bool(posts and posts[0].ups_known),
        "sample": [
            {"id": p.external_id, "ups": p.ups if p.ups_known else None,
             "duration_s": p.duration_s, "title": p.title[:70]}
            for p in postable[:3]
        ],
    }


def _report(rows: list[dict[str, Any]], room: str, time_filter: str) -> str:
    lines = [f"asking r/{room} for its top of the last {time_filter}, "
             f"every way there is", ""]
    for row in rows:
        if row["ok"]:
            note = "" if row["sees_scores"] else "   (cannot see vote counts)"
            lines.append(f"OK   {row['route']:<9} {row['seconds']:>5.1f}s  "
                         f"{row['video']:>3} video, {row['postable']:>3} postable{note}")
        else:
            lines.append(f"NO   {row['route']:<9} {row['seconds']:>5.1f}s  {row['reason'][:90]}")

    working = [r for r in rows if r["ok"]]
    lines.append("")
    if not working:
        lines += [
            "Nothing got through. Every way in was refused, which is one of:",
            "  - this host has no outbound internet at all",
            "  - an egress policy denies reddit.com and every mirror",
            "  - all of them are refusing this address; a proxy in "
            "YTDLP_PROXIES fixes that",
        ]
        return "\n".join(lines)

    useful = [r for r in working if r["postable"]] or working
    best = min(useful, key=lambda r: r["seconds"])
    order = ",".join(r["route"] for r in sorted(working, key=lambda r: r["seconds"]))
    lines += [
        f"{len(working)} of {len(rows)} work here. Fastest that returned "
        f"something postable: {best['route']}",
        "",
        "Leave REDDIT_ROUTES unset and it tries them in order and settles on",
        "whichever answers. Pin it only to skip the failures ahead of the one",
        "that works:",
        "",
        f"    REDDIT_ROUTES={order}",
    ]
    if not any(r["sees_scores"] for r in working):
        lines += [
            "",
            "Only feed routes work here, so nothing can report a vote count and",
            "REDDIT_MIN_UPVOTES is not being applied. The window sort is doing",
            "that job instead - keep the window short.",
        ]
    return "\n".join(lines)


@router.get("/ways-in")
def ways_in(
    room: str | None = Query(default=None),
    sort: str = Query(default="top"),
    time_filter: str = Query(default="day", alias="time"),
    # Small on purpose. The feed routes look each candidate up with yt-dlp,
    # one round trip apiece, so a large limit turns a diagnostic into a
    # request that times out before it can tell you anything.
    limit: int = Query(default=5, ge=1, le=25),
    format: str = Query(default="text"),
) -> Any:
    """Try every way in to Reddit from *this* host and report what worked."""
    room = room or (settings.reddit_rooms[0] if settings.reddit_rooms else "gym")
    rows = [_try(route, room, sort, time_filter, limit)
            for route in reddit_routes.ROUTES]
    log.info("reddit ways-in on r/%s: %s", room,
             ", ".join(f"{r['route']}={'ok' if r['ok'] else 'no'}" for r in rows))

    if format == "json":
        return JSONResponse({"room": room, "sort": sort, "time": time_filter,
                             "routes": rows, "report": _report(rows, room, time_filter)})
    return PlainTextResponse(_report(rows, room, time_filter) + "\n")
