"""Entorno Alembic async. La URL sale de ``VAI_DATABASE_URL`` (o de ``-x url=...``)."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

import app.db.models  # noqa: F401  (registra todos los modelos)
from alembic import context
from app.core.config import get_settings
from app.db.base import Base, JSONType, UTCDateTime

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return context.get_x_argument(as_dictionary=True).get("url") or get_settings().database_url


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Evita referenciar tipos de la app en las migraciones."""
    if type_ == "type":
        if isinstance(obj, UTCDateTime):
            return "sa.DateTime(timezone=True)"
        if obj is JSONType or (
            isinstance(obj, sa.JSON)
            and getattr(obj, "_variant_mapping", None) is not None
            and "postgresql" in getattr(obj, "_variant_mapping", {})
        ):
            return "sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')"
    return False


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=True,
        render_item=render_item,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Any) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
