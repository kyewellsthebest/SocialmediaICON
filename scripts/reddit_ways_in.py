#!/usr/bin/env python3
"""Try every way in to Reddit and report which ones this host can use.

    python scripts/reddit_ways_in.py
    python scripts/reddit_ways_in.py --room gym --sort top --time day

The point is that "Reddit is blocked" is six different statements, and they
have six different fixes. Running this where the bot runs answers all six at
once, in about a minute, and ends with the line to paste into the environment.

Run it on the host that will do the work. A route that answers from a laptop
says nothing about Railway - that difference *is* the problem being measured.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import reddit, reddit_routes  # noqa: E402
from core.config import settings  # noqa: E402

GREEN, RED, GREY, OFF = "\033[32m", "\033[31m", "\033[90m", "\033[0m"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room", default=None,
                        help="subreddit to test against (default: the first "
                             "of REDDIT_SUBREDDITS, else gym)")
    parser.add_argument("--sort", default="top")
    parser.add_argument("--time", dest="time_filter", default="day")
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()

    room = args.room or (settings.reddit_rooms[0] if settings.reddit_rooms else "gym")

    print(f"\nasking r/{room} for its {args.sort} of the last {args.time_filter}, "
          f"every way there is\n")

    working: list[tuple[str, int, int, float]] = []
    for route in reddit_routes.ROUTES:
        began = time.time()
        try:
            posts = route.fetch(room, args.sort, args.time_filter, args.limit)
        except Exception as exc:  # noqa: BLE001 - the failure is the measurement
            took = time.time() - began
            reason = str(exc) or type(exc).__name__
            print(f"{RED}NO {OFF} {route.name:<9} {took:>5.1f}s  {reason[:88]}")
            continue
        took = time.time() - began

        postable = [p for p in posts if reddit.postable(p)[0]]
        working.append((route.name, len(posts), len(postable), took))
        note = "" if not route.thin else "  (no scores; the sort is the filter)"
        print(f"{GREEN}OK {OFF} {route.name:<9} {took:>5.1f}s  "
              f"{len(posts):>3} video, {len(postable):>3} postable{note}")
        print(f"{GREY}       {route.why}{OFF}")

    print()
    if not working:
        print(f"{RED}Nothing got through.{OFF} Every route failed, which is one of "
              "three things:\n"
              "  - this host has no outbound internet at all (test any other site)\n"
              "  - an egress policy denies reddit.com and every mirror\n"
              "  - all of them are refusing this address, which a proxy in "
              "YTDLP_PROXIES fixes\n")
        return 1

    # Fastest route that actually produced something postable, else fastest
    # that answered at all: a route that answers with nothing usable is still
    # a working route, and the empty result is a bounds problem, not a reach one.
    useful = [w for w in working if w[2]] or working
    best = min(useful, key=lambda w: w[3])
    print(f"{len(working)} of {len(reddit_routes.ROUTES)} work here. "
          f"Fastest that returned something postable: {GREEN}{best[0]}{OFF}\n")
    print("Leave REDDIT_ROUTES unset and it will try them in order and settle on\n"
          "whichever answers - that is the point of having six. Pin it only to\n"
          "skip the failures ahead of the one you know works:\n")
    print(f"    REDDIT_ROUTES={','.join(w[0] for w in sorted(working, key=lambda w: w[3]))}\n")

    if all(dict((w[0], w) for w in working).get(name) is None
           for name in ("oauth", "json", "proxied", "reader")):
        print(f"{GREY}Only feed routes work here, so nothing can report a vote "
              f"count and REDDIT_MIN_UPVOTES is not being applied. The window "
              f"sort is doing that job instead - keep --time short.{OFF}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
