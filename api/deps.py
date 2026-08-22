"""Shared FastAPI dependencies.

The dashboard is reachable from the public internet the moment it deploys, so
DASHBOARD_TOKEN gates every data route. Leave it unset only for local work - the
app logs a loud warning when it is missing in prod.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import Cookie, Header, HTTPException, Query

from core.config import settings

log = logging.getLogger(__name__)

COOKIE_NAME = "ce_token"


def require_token(
    x_dashboard_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
    ce_token: str | None = Cookie(default=None),
) -> None:
    """Accept the token from a header, a cookie, or a query string."""
    expected = settings.dashboard_token
    if not expected:
        if settings.is_prod:
            log.warning("DASHBOARD_TOKEN is not set - the dashboard is open to anyone")
        return

    supplied = x_dashboard_token or token or ce_token
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="bad or missing dashboard token")
