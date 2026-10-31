from __future__ import annotations

from functools import lru_cache
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings


def _is_in_memory_sqlite(database_url: str) -> bool:
    try:
        url = make_url(database_url)
    except Exception:
        return False

    if not url.drivername.startswith("sqlite"):
        return False

    return url.database in (None, "", ":memory:")


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    connect_args = {}
    engine_kwargs = {}

    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    if _is_in_memory_sqlite(settings.database_url):
        engine_kwargs["poolclass"] = StaticPool

    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args=connect_args,
        **engine_kwargs,
    )


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def reset_session_state() -> None:
    get_session_factory.cache_clear()
    get_engine.cache_clear()


def get_db_session() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
