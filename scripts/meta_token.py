#!/usr/bin/env python3
"""Check and extend the Meta tokens.

    python scripts/meta_token.py             # what is configured, and who it is
    python scripts/meta_token.py --refresh   # extend by another 60 days

Meta's long-lived tokens last 60 days and cannot be refreshed once expired, so
this is a diary entry, not a fire-and-forget. Run it monthly and paste the new
values into Railway - the process cannot rewrite its own environment.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from core.config import settings  # noqa: E402
from core.publishers.meta import FACEBOOK_HOST, THREADS_HOST, refresh_tokens  # noqa: E402


def _describe() -> int:
    rows = [
        ("Instagram", settings.has_instagram, settings.instagram_user_id),
        ("Threads", settings.has_threads, settings.threads_user_id),
        ("Facebook", settings.has_facebook, settings.facebook_page_id),
    ]
    for name, configured, account in rows:
        state = f"ready (account {account})" if configured else "not configured"
        print(f"{name:<10} {state}")

    if not any(configured for _, configured, _ in rows):
        print("\nNothing to check. See docs/DEPLOY.md for which keys to set.")
        return 1

    # Ask Meta who the token belongs to and when it dies. `debug_token` needs
    # the app credentials as well, so skip it when those are absent.
    if settings.meta_access_token and settings.meta_app_id and settings.meta_app_secret:
        response = httpx.get(
            f"{FACEBOOK_HOST}/{settings.meta_graph_version}/debug_token",
            params={
                "input_token": settings.meta_access_token,
                "access_token": f"{settings.meta_app_id}|{settings.meta_app_secret}",
            },
            timeout=30.0,
        )
        data = response.json().get("data", {})
        expires = data.get("expires_at")
        print(f"\nMeta token: valid={data.get('is_valid')} expires_at={expires or 'never'}")
        scopes = data.get("scopes") or []
        if scopes:
            print(f"scopes: {', '.join(scopes)}")

    if settings.threads_access_token:
        response = httpx.get(
            f"{THREADS_HOST}/v1.0/me",
            params={"fields": "id,username", "access_token": settings.threads_access_token},
            timeout=30.0,
        )
        print(f"Threads token: {response.json()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="extend the tokens by 60 days")
    args = parser.parse_args()

    if not args.refresh:
        return _describe()

    fresh = refresh_tokens()
    if not fresh:
        print("Nothing was refreshed - check the log above for why.")
        return 1

    print("Put these back into your environment (Railway -> Variables):\n")
    for key, value in fresh.items():
        print(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
