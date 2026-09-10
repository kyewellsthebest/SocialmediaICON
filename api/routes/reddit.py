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
import shutil
import subprocess
import time
from pathlib import Path
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


def _fetch_once(post: reddit.Post) -> Path:
    """Download one post to a scratch folder, without recording anything.

    Deliberately not the harvest's own download: that one brands the file and
    writes the queue row. This is a check on the plumbing, so it has to be
    repeatable on the same post.
    """
    import yt_dlp

    from core.ytdlp import base_options, run

    into = Path(settings.work_dir) / "reddit-try"
    into.mkdir(parents=True, exist_ok=True)

    def download(options: dict[str, Any]) -> None:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([post.video_url])

    run(download, base_options(
        format="bestvideo*+bestaudio/best",
        merge_output_format="mp4",
        outtmpl=str(into / f"{post.external_id}.%(ext)s"),
        noplaylist=True,
    ))
    found = sorted(into.glob(f"{post.external_id}.*"))
    if not found:
        raise RuntimeError(f"nothing downloaded for {post.external_id}")
    return found[0]


def _streams(path: Path) -> dict[str, Any]:
    """What ffprobe says is actually inside the file.

    The audio track is the point. Reddit serves video and audio as two
    separate DASH files, so a download that fetched only the video half plays
    perfectly, opens in any player, and is silent - and there is no editor
    downstream to notice before it is posted.
    """
    if not shutil.which("ffprobe"):
        return {"probed": False, "why": "ffprobe is not installed"}
    try:
        found = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height:format=duration",
             "-of", "default=noprint_wrappers=1", str(path)],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"probed": False, "why": "ffprobe timed out"}

    kinds, size, duration = [], None, None
    for line in found.stdout.splitlines():
        key, _, value = line.partition("=")
        if key == "codec_type":
            kinds.append(value)
        elif key == "width":
            size = f"{value}x"
        elif key == "height" and size:
            size += value
        elif key == "duration":
            try:
                duration = round(float(value), 1)
            except ValueError:
                pass
    return {"probed": True, "has_audio": "audio" in kinds,
            "has_video": "video" in kinds, "size": size, "duration_s": duration}


@router.get("/try-one")
def try_one(
    room: str | None = Query(default=None),
    sort: str = Query(default="top"),
    time_filter: str = Query(default="day", alias="time"),
    format: str = Query(default="text"),
) -> Any:
    """Download one real video and say what actually came down.

    A diagnostic, not the harvest: it records nothing, so it can be run again
    and again on the same post. What it is checking is the failure that cannot
    be caught by looking - a silent clip - and it downloads a real file rather
    than trusting that the format string was right.
    """
    room = room or (settings.reddit_rooms[0] if settings.reddit_rooms else "gym")
    began = time.time()

    try:
        posts, route = reddit_routes.listing(room, sort, time_filter, limit=8)
    except reddit.RedditError as exc:
        return _answer(format, {"ok": False, "stage": "listing", "reason": str(exc)},
                       f"could not read r/{room}:\n{exc}")

    keep = [p for p in posts if reddit.postable(p)[0]]
    if not keep:
        why = (f"r/{room} answered by {route}, but none of its {len(posts)} "
               f"videos are postable. Widen the window or try a busier room - "
               f"not necessarily a fault.")
        return _answer(format, {"ok": False, "stage": "filter", "route": route,
                                "video": len(posts), "reason": why}, why)

    best = max(keep, key=lambda p: p.ups)
    try:
        video = _fetch_once(best)
    except Exception as exc:  # noqa: BLE001 - the failure is the measurement
        why = f"found {best.external_id} but could not download it:\n{exc}"
        return _answer(format, {"ok": False, "stage": "download", "route": route,
                                "post": best.external_id, "reason": str(exc)[:400]}, why)

    probe = _streams(video)
    payload = {
        "ok": bool(probe.get("has_audio")),
        "stage": "downloaded", "route": route,
        "post": best.external_id, "url": best.url,
        "caption": best.title,
        "author": f"u/{best.author}" if best.author else None,
        "subreddit": f"r/{best.subreddit}",
        "ups": best.ups if best.ups_known else None,
        "reddit_says_duration_s": best.duration_s,
        "megabytes": round(video.stat().st_size / 1e6, 1),
        "seconds_taken": round(time.time() - began, 1),
        **probe,
    }

    lines = [
        f"r/{room} read by {route}, {len(keep)} of {len(posts)} postable",
        "",
        f"  took       {best.external_id}  ({payload['megabytes']} MB in "
        f"{payload['seconds_taken']}s)",
        f"  caption    {best.title[:88]}",
        f"  by         {payload['author']} in {payload['subreddit']}",
        f"  duration   reddit said {best.duration_s or 0:.0f}s, "
        f"file is {probe.get('duration_s') or 0:.0f}s",
        f"  picture    {probe.get('size') or 'unknown'}",
        "",
    ]
    if not probe.get("probed"):
        lines.append(f"  ?  the audio was not checked: {probe.get('why')}")
    elif probe.get("has_audio"):
        lines.append("  OK it has an audio track. The download path works.")
    else:
        lines += [
            "  NO SILENT. It fetched the video half of the DASH pair only.",
            "     That is the trap: it plays fine and nobody notices until it",
            "     is on the page. The format string in gym_reddit.fetch is",
            "     what to look at.",
        ]
    return _answer(format, payload, "\n".join(lines))


def _answer(format: str, payload: dict[str, Any], report: str) -> Any:
    if format == "json":
        return JSONResponse(payload | {"report": report})
    return PlainTextResponse(report + "\n")
