"""SQLAlchemy session handling.

The engine is created lazily: importing this module must not require a
DATABASE_URL, because the Phase 1 CLI runs without one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None
#: What the cached engine was built for. In production this never changes; the
#: cache is keyed on it anyway, because an engine held open against a URL that
#: is no longer configured answers queries from the wrong database - and it
#: answers them, so nothing fails and the wrong data just looks like the data.
_engine_url: str | None = None


def get_engine() -> Engine:
    global _engine, _engine_url
    url = settings.sqlalchemy_url
    if _engine is None or _engine_url != url:
        _engine = create_engine(url, pool_pre_ping=True, future=True)
        _engine_url = url
        reset_sessionmaker()
    return _engine


def reset_sessionmaker() -> None:
    global _SessionLocal
    _SessionLocal = None


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    engine = get_engine()
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session
