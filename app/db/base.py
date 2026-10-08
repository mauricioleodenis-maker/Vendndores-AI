"""Base declarativa, convenciones de nombres, mixins y tipos portables SQLite/Postgres."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, MetaData, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JSONType = sa.JSON().with_variant(JSONB(), "postgresql")
BigIntPK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


class UTCDateTime(TypeDecorator[datetime]):
    """timestamptz que siempre devuelve datetimes aware en UTC (SQLite los pierde)."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Se requieren datetimes con zona horaria")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: UTCDateTime}


def utcnow() -> datetime:
    return datetime.now(UTC)


def enum_check(column: str, values: Sequence[str], *, nullable: bool = False) -> CheckConstraint:
    """CHECK portable que reemplaza a un enum nativo."""
    items = ", ".join(f"'{v}'" for v in values)
    cond = f"{column} IN ({items})"
    if nullable:
        cond = f"{column} IS NULL OR {cond}"
    return CheckConstraint(cond, name=column)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def json_col(default: Any = dict, nullable: bool = False) -> Mapped[Any]:
    return mapped_column(JSONType, default=default, nullable=nullable)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class TenantMixin:
    """Agrega ``tenant_id`` NOT NULL con FK e indice. Los repos filtran por esta columna."""

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(
            Uuid, ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
        )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
