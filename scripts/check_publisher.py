#!/usr/bin/env python3
"""Confirm the configured publisher works before trusting it with a queue.

    python scripts/check_publisher.py            # dry run: config only
    python scripts/check_publisher.py --post out/01-clip.mp4 --platforms tiktok

The dry run checks credentials are present. The --post run actually publishes,
so point it at a throwaway clip and expect it to appear on your account.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import settings  # noqa: E402
from core.publishers import PublishRequest, get_publisher  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--post", help="path to a clip to actually publish")
    parser.add_argument("--platforms", default="youtube", help="comma separated")
    parser.add_argument("--title", default="clip-engine test")
    parser.add_argument("--privacy", default="private", help="public|unlisted|private")
    args = parser.parse_args()

    print(f"PUBLISHER={settings.publisher}")
    try:
        publisher = get_publisher()
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(f"[ok]   backend ready: {publisher.name}")

    if not args.post:
        print("\nDry run only. Re-run with --post <file> to publish a real clip.")
        return 0

    clip = Path(args.post)
    if not clip.exists():
        print(f"[FAIL] no such file: {clip}")
        return 1

    results = publisher.publish(
        PublishRequest(
            clip_path=clip,
            title=args.title,
            description=args.title,
            hashtags=["#test"],
            platforms=[p.strip() for p in args.platforms.split(",") if p.strip()],
            privacy=args.privacy,
        )
    )
    failures = 0
    for result in results:
        if result.ok:
            print(f"[ok]   {result.platform}: {result.post_id} {result.url or ''}")
        else:
            failures += 1
            print(f"[FAIL] {result.platform}: {result.error}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
