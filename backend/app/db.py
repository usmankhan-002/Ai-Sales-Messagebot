"""Neon (Postgres) connection for the bot's own `ai_bot` database.

Optional: when DATABASE_URL is empty the service runs exactly as before,
nothing is stored, and the database is reported as "not_configured". Use
Neon's *pooled* connection string (host contains "-pooler").
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _normalize_url(url: str) -> str:
    """Neon hands out postgres:// / postgresql:// URLs; point SQLAlchemy at psycopg 3."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def get_engine() -> Engine | None:
    global _engine, _SessionLocal
    if not settings.db_configured:
        return None
    if _engine is None:
        url = _normalize_url(settings.database_url.strip())
        kwargs: dict = {"pool_pre_ping": True}
        if not url.startswith("sqlite"):
            # Neon suspends idle compute; recycle and ping so dead connections are replaced.
            # pool_timeout/connect_timeout: if Neon is unreachable, fail in seconds so AI
            # calls (which only record best-effort) don't hang waiting for the database.
            # prepare_threshold=None: no server-side prepared statements through the pooler.
            kwargs.update(pool_recycle=300, pool_size=5, max_overflow=5, pool_timeout=5,
                          connect_args={"connect_timeout": 5, "prepare_threshold": None})
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            # SQLite ignores foreign keys unless asked; tests rely on cascades like Postgres.
            @event.listens_for(_engine, "connect")
            def _fk_on(dbapi_conn, _record):
                dbapi_conn.execute("PRAGMA foreign_keys=ON")
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine (tests, or after changing DATABASE_URL)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def init_db() -> bool:
    """Create the bot's tables if they don't exist. Never touches other tables."""
    engine = get_engine()
    if engine is None:
        return False
    from .models import Base

    try:
        Base.metadata.create_all(engine)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not create database tables: %s", type(exc).__name__)
        return False


@contextmanager
def session_scope() -> Iterator[Session]:
    """Commit on success, roll back on error."""
    if get_engine() is None or _SessionLocal is None:
        raise RuntimeError("DATABASE_URL is not configured.")
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def db_status() -> str:
    """"not_configured", "ok" or "error". Never raises, never leaks the URL."""
    engine = get_engine()
    if engine is None:
        return "not_configured"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Database check failed: %s", type(exc).__name__)
        return "error"
