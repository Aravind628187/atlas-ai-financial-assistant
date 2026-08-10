"""
Database bootstrap. Uses SQLAlchemy so swapping SQLite -> Postgres/MySQL
in production is a one-line change to DATABASE_URL in .env.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import settings

def normalize_database_url(database_url: str) -> str:
    """Accept standard PostgreSQL URLs and Render's legacy postgres alias."""
    value = str(database_url or "").strip()
    if value.startswith("postgres://"):
        return "postgresql://" + value.removeprefix("postgres://")
    return value


def database_engine_options(database_url: str) -> dict[str, Any]:
    backend = make_url(normalize_database_url(database_url)).get_backend_name()
    if backend == "sqlite":
        return {"connect_args": {"check_same_thread": False, "timeout": 30}, "future": True}
    return {"pool_pre_ping": True, "pool_recycle": 300, "future": True}


def create_database_engine(database_url: str) -> Engine:
    url = normalize_database_url(database_url)
    if not url:
        raise ValueError("DATABASE_URL must not be empty")
    return create_engine(url, **database_engine_options(url))


database_url = normalize_database_url(settings.database_url)
if make_url(database_url).get_backend_name() == "sqlite":
    os.makedirs("data", exist_ok=True)

engine = create_database_engine(database_url)

if make_url(database_url).get_backend_name() == "sqlite":
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Create all tables. Safe to call repeatedly."""
    from app import models  # noqa: F401  (ensures models are registered on Base)

    Base.metadata.create_all(bind=engine)


@contextmanager
def get_session():
    """Context-managed DB session for use outside of FastAPI dependency injection."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
