"""Keep the Meta tokens alive.

Meta's long-lived tokens last 60 days and can be extended indefinitely - but
only while they are still valid. Miss the window and there is no recovery
path: you go back to the app dashboard and generate a new one by hand.

So this runs on a fortnightly timer rather than close to the deadline. Three
missed runs in a row still leaves a fortnight of slack, which is what you want
from something whose failure mode is silent until the day posting stops.
"""

from __future__ import annotations

import logging

import httpx

from core import credentials
from core.config import settings
from core.publishers.meta import FACEBOOK_HOST, INSTAGRAM_HOST, THREADS_HOST

log = logging.getLogger(__name__)

# Meta hands back 60 days; treat anything it reports as authoritative.
DEFAULT_LIFETIME_S = 60 * 24 * 3600


def _refresh_one(client: httpx.Client, name: str) -> tuple[bool, str]:
    token = credentials.get(name)
    if not token:
        return False, "not configured"

    if name == "META_ACCESS_TOKEN":
        if not (settings.meta_app_id and settings.meta_app_secret):
            return False, "META_APP_ID / META_APP_SECRET are not set"
        url = f"{FACEBOOK_HOST}/{settings.meta_graph_version}/oauth/access_token"
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "fb_exchange_token": token,
        }
    elif name == "INSTAGRAM_ACCESS_TOKEN":
        url = f"{INSTAGRAM_HOST}/refresh_access_token"
        params = {"grant_type": "ig_refresh_token", "access_token": token}
    else:
        url = f"{THREADS_HOST}/refresh_access_token"
        params = {"grant_type": "th_refresh_token", "access_token": token}

    try:
        payload = client.get(url, params=params).json()
    except (httpx.HTTPError, ValueError) as exc:
        return False, f"request failed: {exc}"

    fresh = payload.get("access_token")
    if not fresh:
        error = payload.get("error") or payload
        return False, str(error)[:300]

    lifetime = payload.get("expires_in")
    credentials.put(name, str(fresh), int(lifetime) if lifetime else DEFAULT_LIFETIME_S)
    return True, "refreshed"


def run(client: httpx.Client | None = None) -> dict[str, str]:
    """Extend every managed token. Returns what happened to each."""
    outcome: dict[str, str] = {}
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        for name in credentials.MANAGED:
            ok, detail = _refresh_one(client, name)
            outcome[name] = detail
            if ok:
                log.info("%s refreshed", name)
            elif detail == "not configured":
                log.debug("%s is not configured, skipping", name)
            else:
                # Loud, because the consequence is posting stopping in weeks.
                log.error("could not refresh %s: %s", name, detail)
                credentials.record_failure(name, detail)
    finally:
        if owns_client:
            client.close()
    return outcome
