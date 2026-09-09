#!/usr/bin/env python3
"""Prove the Reddit path works, one step at a time, and say where it stopped.

    python scripts/reddit_check.py
    python scripts/reddit_check.py --download

Written because the sandbox this was built in cannot reach Reddit at all - its
egress policy denies the host - so every test behind it runs against recorded
payload shapes rather than a live call. This is the part that has to be run
somewhere with a real connection, and it is deliberately chatty: a check that
only says "failed" moves the mystery rather than solving it.

The four things that can go wrong are separated, because they have four
different fixes:

    credentials   -> the app is not a script app, or the id and secret are swapped
    reach         -> the host is refusing this address, so a proxy is needed
    filter        -> it answers, but nothing survives the bounds
    audio         -> it downloads, and it is silent

The last one is the reason --download exists. Reddit serves video and audio as
separate DASH files, so a silent clip downloads cleanly, plays cleanly, and is
noticed only after it has been posted.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import reddit  # noqa: E402
from core.config import settings  # noqa: E402


def tick(ok: bool) -> str:
    return "\033[32mOK\033[0m  " if ok else "\033[31mNO\033[0m  "


def has_audio(path: Path) -> bool | None:
    """Whether the file carries a real audio stream. None if ffprobe is absent."""
    try:
        found = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
            capture_output=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return bool(found.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--term", default="fail")
    parser.add_argument("--download", action="store_true",
                        help="also take the top result and check it has sound")
    args = parser.parse_args()

    rooms = settings.reddit_rooms
    print(f"\nsearching for {args.term!r} in "
          f"{', '.join('r/' + r for r in rooms) if rooms else 'all of Reddit'}")
    print(f"bounds: {settings.reddit_floor_duration_s:.0f}"
          f"-{settings.reddit_max_duration_s:.0f}s, "
          f"{settings.reddit_min_upvotes}+ upvotes, "
          f"sorted top over the last {settings.reddit_time_filter}\n")

    # 1. credentials -------------------------------------------------------
    print(tick(settings.has_reddit) + (
        "credentials are set" if settings.has_reddit else
        "no credentials - REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are unset. "
        "Reddit refuses anonymous reads from a datacenter, so this will "
        "probably 403 next."))

    # 2. reach -------------------------------------------------------------
    try:
        posts = reddit.search(
            args.term, sort="top", time_filter=settings.reddit_time_filter,
            subreddit=rooms[0] if rooms else None,
        )
    except reddit.RedditError as exc:
        print(tick(False) + f"Reddit refused it: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(tick(False) + f"could not reach Reddit: {type(exc).__name__}: {exc}")
        return 1
    print(tick(True) + f"Reddit answered - {len(posts)} native video posts")

    # 3. filter ------------------------------------------------------------
    keep, refused = [], {}
    for post in posts:
        ok, why = reddit.postable(post)
        if ok:
            keep.append(post)
        else:
            refused[why] = refused.get(why, 0) + 1
    print(tick(bool(keep)) + f"{len(keep)} of {len(posts)} are postable")
    for why, n in sorted(refused.items(), key=lambda kv: -kv[1])[:6]:
        print(f"       {n:>3} refused: {why}")

    if not keep:
        print("\nNothing survived the bounds. Widen them, or search a busier "
              "room or a longer window - not necessarily a fault.")
        return 1

    print("\nthe ones it would take:\n")
    for post in sorted(keep, key=lambda p: p.ups, reverse=True)[:8]:
        print(f"  {post.ups:>6} ups  {post.duration_s or 0:>4.0f}s  "
              f"r/{post.subreddit:<16} {post.title[:58]}")

    # 4. audio -------------------------------------------------------------
    if args.download:
        from worker.tasks.gym_reddit import fetch

        best = max(keep, key=lambda p: p.ups)
        into = Path(settings.work_dir) / "reddit-check"
        print(f"\ndownloading {best.external_id} to check it has sound...")
        try:
            video = fetch(best, into)
        except Exception as exc:  # noqa: BLE001
            print(tick(False) + f"download failed: {type(exc).__name__}: {exc}")
            return 1

        sound = has_audio(video)
        size_mb = video.stat().st_size / 1e6
        print(tick(True) + f"downloaded {video.name} ({size_mb:.1f} MB)")
        if sound is None:
            print("       (ffprobe is not installed, so the audio was not checked)")
        else:
            print(tick(sound) + (
                "it has an audio track" if sound else
                "SILENT - it fetched the video half of the DASH pair only. "
                "That is the trap: it plays fine and nobody notices."))
            if not sound:
                return 1

    print("\nThe path works. Set REDDIT_SUBREDDITS to the rooms you want and "
          "the harvest can run.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
