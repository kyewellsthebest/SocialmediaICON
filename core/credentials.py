"""Tokens that outlive the environment they started in.

Meta's tokens expire after 60 days and can be extended indefinitely - but only
by calling an endpoint before they lapse, and a running process cannot rewrite
its own environment variables. So the live value lives in the database: seeded
from the environment the first time it is read, then replaced in place by the
refresh job.

The environment variable stays authoritative for *rotation*. Paste a new value
into Railway and it wins on the next read, because a hand-set token is always a
deliberate act and the stored one may be the thing you are replacing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from core.config import settings
from core.models import Credential

log = logging.getLogger(__name__)

# Names this module manages. Anything else falls straight through to settings.
MANAGED = ("META_ACCESS_TOKEN", "INSTAGRAM_ACCESS_TOKEN", "THREADS_ACCESS_TOKEN")

SETTING_FOR = {
    "META_ACCESS_TOKEN": "meta_access_token",
    "INSTAGRAM_ACCESS_TOKEN": "instagram_access_token",
    "THREADS_ACCESS_TOKEN": "threads_access_token",
}


def from_env(name: str) -> str | None:
    return getattr(settings, SETTING_FOR[name], None) if name in SETTING_FOR else None


def get(name: str) -> str | None:
    """The token to use right now.

    Falls back to the environment whenever the database is absent or empty, so
    nothing here is required for the CLI or a laptop run.
    """
    env = from_env(name)
    if not settings.has_db:
        return env

    from core.db import session_scope

    try:
        with session_scope() as session:
            row = session.query(Credential).filter(Credential.name == name).one_or_none()
            stored = row.value if row else None
            stored_at = row.refreshed_at if row else None
    except Exception as exc:  # a missing table should not stop a publish
        log.warning("could not read credential %s: %s", name, exc)
        return env

    if stored is None:
        return env
    if env and env != stored and _env_is_newer(env, stored, stored_at):
        return env
    return stored


def _env_is_newer(env: str, stored: str, stored_at: datetime | None) -> bool:
    """A hand-set variable that differs from the stored one wins.

    There is no timestamp on an environment variable, so this cannot be decided
    by age. It is decided by intent: someone typing a token into Railway is
    replacing what is there, and the refresh job only ever writes values it
    derived from the token it already had.
    """
    del stored, stored_at
    return bool(env)


def put(name: str, value: str, lifetime_s: int | None = None, error: str | None = None) -> None:
    """Store a refreshed token. Silent no-op without a database."""
    if not settings.has_db:
        log.warning("no DATABASE_URL - %s was refreshed but cannot be stored", name)
        return

    from core.db import session_scope

    now = datetime.now(UTC)
    expires = now + timedelta(seconds=lifetime_s) if lifetime_s else None
    with session_scope() as session:
        row = session.query(Credential).filter(Credential.name == name).one_or_none()
        if row is None:
            row = Credential(name=name)
            session.add(row)
        row.value = value
        row.refreshed_at = now
        row.expires_at = expires
        row.last_error = error


def record_failure(name: str, error: str) -> None:
    """Note why a refresh failed, without touching the token itself."""
    if not settings.has_db:
        return

    from core.db import session_scope

    with session_scope() as session:
        row = session.query(Credential).filter(Credential.name == name).one_or_none()
        if row is not None:
            row.last_error = error[:500]


def status() -> list[dict]:
    """What is stored, and how long each token has left."""
    if not settings.has_db:
        return []

    from core.db import session_scope

    now = datetime.now(UTC)
    out = []
    with session_scope() as session:
        for row in session.query(Credential).order_by(Credential.name).all():
            days = None
            if row.expires_at:
                days = round((row.expires_at - now).total_seconds() / 86400, 1)
            out.append(
                {
                    "name": row.name,
                    "refreshed_at": row.refreshed_at,
                    "expires_at": row.expires_at,
                    "days_left": days,
                    "last_error": row.last_error,
                }
            )
    return out
