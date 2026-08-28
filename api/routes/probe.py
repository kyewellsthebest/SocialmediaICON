"""Run the source probes from the box that actually has a network.

Every source decision in this project has been made from documentation,
because the machine reasoning about them has no egress. The probe script
fixes that but needs a shell, and a shell on the deployment is exactly the
thing that is awkward to get at.

So the probe is exposed here too. Open the URL, read the JSON. It answers
one question: does Kick hand a playback URL to a datacenter IP, and can
ffmpeg hold it in a buffer.

Read-only. Nothing is written to the database, nothing is posted, and the
buffer it opens is discarded before the response returns.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query

log = logging.getLogger(__name__)

router = APIRouter(prefix="/probe", tags=["probe"])

#: A probe that runs longer than this is not telling us anything new, and a
#: request that hangs for minutes looks like a broken deployment.
MAX_SECONDS = 90.0


@router.get("/kick")
def probe_kick(
    channel: str = Query(..., description="a channel that is live right now"),
    seconds: float = Query(30.0, ge=5.0, le=MAX_SECONDS),
    buffer: bool = Query(True, description="also hold it in a rolling buffer"),
) -> dict[str, Any]:
    """Does Kick serve this machine, and can we buffer what it serves?

    `channel` has to be live at the moment you call this - an offline channel
    returns a clean "not live", which answers nothing about whether Kick would
    have served us.
    """
    from scripts.probe_sources import probe_kick_live

    try:
        results = probe_kick_live(channel, "/tmp/probe", seconds if buffer else 0.0)
    except Exception as exc:  # noqa: BLE001 - a probe reports failures, never raises
        log.exception("probe: kick/%s failed", channel)
        return {
            "channel": channel,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "checks": [],
        }

    checks = [
        {
            "what": r.what,
            "ok": r.ok,
            "detail": r.detail,
            "elapsed_s": round(r.elapsed_s, 1),
            "notes": r.notes,
        }
        for r in results
    ]
    # The single question this endpoint exists to answer, stated plainly, so
    # the answer does not have to be inferred from a list of sub-results.
    served = any(r.ok and r.what.startswith("live /") for r in results)
    buffered = any(r.ok and r.what == "rolling buffer" for r in results)

    # An egress proxy refusing CONNECT and Kick refusing a datacenter IP both
    # arrive as "403 Forbidden". Reporting the first as the second is how a
    # working source gets written off, so it gets its own verdict.
    blocked = not served and any(
        marker in r.detail.lower()
        for r in results
        for marker in ("tunnel connection failed", "connect tunnel failed")
    )

    if blocked:
        verdict = (
            "inconclusive - this machine's own network refused the connection, "
            "so Kick was never contacted"
        )
    elif served and buffered:
        verdict = "Kick serves this datacenter IP and the buffer holds it"
    elif served:
        verdict = "Kick serves this datacenter IP, but the buffer did not fill"
    else:
        verdict = "Kick did not hand over a playback URL"

    return {
        "channel": channel,
        "playback_url_served": served,
        "buffered": buffered,
        "network_blocked_locally": blocked,
        "verdict": verdict,
        "checks": checks,
    }


@router.get("/ladder")
def probe_ladder(channel: str = Query(...)) -> dict[str, Any]:
    """What renditions this channel offers, and the bandwidth each would cost.

    This is where the monitoring bill is actually decided: if the ladder has a
    160p rung the detection feed is cheap, and if its smallest picture is 720p
    it is not.
    """
    import yt_dlp

    from core import ytdlp
    from core.live import DELIVER, DETECT, Variant, choose_variant

    def call(opts: dict) -> dict:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"https://kick.com/{channel}", download=False)

    try:
        info = ytdlp.run(call, ytdlp.base_options(skip_download=True)) or {}
    except Exception as exc:  # noqa: BLE001
        return {"channel": channel, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # yt-dlp has already parsed the master playlist, so read the ladder off its
    # formats rather than fetching and parsing the m3u8 a second time.
    variants = [
        Variant(
            url=f.get("url", ""),
            bandwidth_bps=int(f.get("tbr") or 0) * 1000,
            width=int(f.get("width") or 0),
            height=int(f.get("height") or 0),
            audio_only=(f.get("vcodec") == "none"),
        )
        for f in (info.get("formats") or [])
        if f.get("url")
    ]
    if not variants:
        return {"channel": channel, "ok": False, "error": "no formats offered"}

    detect = choose_variant(variants, DETECT)
    deliver = choose_variant(variants, DELIVER)
    return {
        "channel": channel,
        "ok": True,
        "live": bool(info.get("is_live")),
        "viewers": info.get("concurrent_view_count"),
        "ladder": [
            {
                "label": v.label(),
                "mbps": round(v.mbps, 3),
                "gb_per_day_x10": round(v.gb_per_day(10), 1),
            }
            for v in variants
        ],
        "detect": {"label": detect.label(), "gb_per_day_x10": round(detect.gb_per_day(10), 1)},
        "deliver": {"label": deliver.label(), "gb_per_day_x10": round(deliver.gb_per_day(10), 1)},
    }
