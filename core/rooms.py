"""Which subreddits get read, and how that list is changed.

The room list is the single biggest lever on what this page posts. A room full
of personal progress videos contributes almost nothing usable; one full of PR
attempts and dropped bars carries the whole queue. Until there is a week of
evidence, nobody knows which is which - not the person who wrote the list, and
certainly not the person who has never seen the subreddit.

So the list lives in the database, editable from the dashboard, and every run
records what each room actually gave back. The environment variable is the
default and the way to reset: no row means "whatever REDDIT_SUBREDDITS says".
"""

from __future__ import annotations

import logging

from core.config import _names, settings
from core.db import session_scope
from core.models import Preference

log = logging.getLogger(__name__)

KEY = "reddit_subreddits"


def current() -> list[str]:
    """The rooms to read: the dashboard's list, or the variable's."""
    if settings.has_db:
        try:
            with session_scope() as session:
                row = session.get(Preference, KEY)
                if row is not None and row.value.strip():
                    return _names(row.value)
        except Exception as exc:  # noqa: BLE001 - a run must not die over this
            log.warning("could not read the room list (%s); using the variable", exc)
    return settings.reddit_rooms


def overridden() -> bool:
    """Whether the dashboard's list is in use rather than the variable's."""
    if not settings.has_db:
        return False
    with session_scope() as session:
        row = session.get(Preference, KEY)
        return row is not None and bool(row.value.strip())


def replace(raw: str) -> list[str]:
    """Set the room list. An empty string hands control back to the variable."""
    names = _names(raw)
    with session_scope() as session:
        row = session.get(Preference, KEY)
        if not names:
            if row is not None:
                session.delete(row)
            return settings.reddit_rooms
        value = ",".join(names)
        if row is None:
            session.add(Preference(name=KEY, value=value))
        else:
            row.value = value
    log.info("room list set to %d rooms", len(names))
    return names
