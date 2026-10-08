"""Engine y sesiones async."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def make_engine(url: str | None = None, **kwargs: Any) -> AsyncEngine:
    url = url or get_settings().database_url
    if url.startswith("sqlite"):
        if ":memory:" in url or url.endswith("///"):
            kwargs.setdefault("poolclass", StaticPool)
        kwargs.setdefault("connect_args", {"check_same_thread": False})
        engine = create_async_engine(url, **kwargs)

        @event.listens_for(engine.sync_engine, "connect")
        def _fk_on(dbapi_conn: Any, _rec: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    kwargs.setdefault("pool_pre_ping", True)
    return create_async_engine(url, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = make_sessionmaker(get_engine())
    return _sessionmaker


def configure_database(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Fija engine/sessionmaker globales (tests, arranque)."""
    global _engine, _sessionmaker
    _engine = engine
    _sessionmaker = make_sessionmaker(engine)
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Sesion con commit al salir y rollback ante error (jobs, CLI)."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def set_tenant_context(session: AsyncSession, tenant_id: object) -> None:
    """Fija ``app.tenant_id`` para RLS en Postgres (no-op en SQLite)."""
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        from sqlalchemy import text

        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
        )
