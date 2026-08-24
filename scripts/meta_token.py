#!/usr/bin/env python3
"""Check and extend the Meta tokens.

    python scripts/meta_token.py             # what is configured, and who it is
    python scripts/meta_token.py --exchange instagram=IGQ...  # 1 hour -> 60 days
    python scripts/meta_token.py --refresh   # extend by another 60 days

The tokens Meta's dashboard generates last about an hour. Run --exchange on
each one first, or the queue works this afternoon and is dead by morning.

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
from core.publishers.meta import (  # noqa: E402
    FACEBOOK_HOST,
    THREADS_HOST,
    MetaError,
    describe_accounts,
    exchange_token,
    list_page_tokens,
    refresh_tokens,
)


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

    # The part worth checking before you trust the queue: an id is just digits,
    # so confirm each one is the account you meant.
    print("\nPosting as:")
    for platform, who in describe_accounts().items():
        print(f"  {platform:<10} {who}")

    if settings.instagram_via_instagram_login:
        print("\nInstagram: using Instagram Login (no Facebook Page involved).")

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


ENV_VAR = {
    "instagram": "INSTAGRAM_ACCESS_TOKEN",
    "threads": "THREADS_ACCESS_TOKEN",
    "facebook": "META_ACCESS_TOKEN",
}


def _exchange(pairs: list[str]) -> int:
    failed = False
    out: dict[str, str] = {}
    for pair in pairs:
        platform, _, short = pair.partition("=")
        platform = platform.strip().lower()
        if not short or platform not in ENV_VAR:
            print(f"skipping {pair!r} - expected one of {', '.join(ENV_VAR)}=<token>")
            failed = True
            continue
        try:
            out[ENV_VAR[platform]] = exchange_token(platform, short.strip())
        except MetaError as exc:
            print(f"{platform}: {exc}")
            failed = True

    if out:
        print("Long-lived tokens - put these in Railway -> Variables:\n")
        for key, value in out.items():
            print(f"{key}={value}\n")
        print("They expire in 60 days. Run --refresh before then.")
    return 1 if failed else 0


def _page_tokens() -> int:
    try:
        pages = list_page_tokens()
    except MetaError as exc:
        print(exc)
        return 1

    if not pages:
        print(
            "No Pages came back. The token needs pages_show_list, and you must have\n"
            "granted access to the Page itself in the login dialog."
        )
        return 1

    print("Pages this token can post to:\n")
    for page in pages:
        print(f"  {page['name']} ({page['id']})")
    print("\nFor the one you post from, set:\n")
    for page in pages:
        print(f"# {page['name']}")
        print(f"FACEBOOK_PAGE_ID={page['id']}")
        print(f"FACEBOOK_PAGE_TOKEN={page['access_token']}\n")
    print("Page tokens derived this way do not expire on a timer.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="extend the tokens by 60 days")
    parser.add_argument(
        "--page-token",
        action="store_true",
        help="derive a non-expiring Page token from the long-lived user token",
    )
    parser.add_argument(
        "--exchange",
        nargs="+",
        metavar="PLATFORM=TOKEN",
        help="swap short-lived dashboard tokens for 60-day ones "
        "(instagram=... threads=... facebook=...)",
    )
    args = parser.parse_args()

    if args.exchange:
        return _exchange(args.exchange)

    if args.page_token:
        return _page_tokens()

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
