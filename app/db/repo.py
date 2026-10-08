"""``TenantScopedRepo``: todo acceso a tablas con ``tenant_id`` pasa por aqui."""

from __future__ import annotations

import uuid
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import TenantMixin

T = TypeVar("T", bound=TenantMixin)


def tenant_select(model: type[T], tenant_id: uuid.UUID) -> Select[tuple[T]]:
    """``select(model)`` ya filtrado por tenant."""
    return select(model).where(model.tenant_id == tenant_id)  # type: ignore[arg-type]


class TenantScopedRepo(Generic[T]):
    def __init__(self, session: AsyncSession, model: type[T], tenant_id: uuid.UUID) -> None:
        if tenant_id is None:
            raise ValueError("tenant_id es obligatorio")
        if not issubclass(model, TenantMixin):
            raise TypeError(f"{model.__name__} no tiene tenant_id")
        self.session = session
        self.model = model
        self.tenant_id = tenant_id

    def _select(self) -> Select[tuple[T]]:
        return select(self.model).where(self.model.tenant_id == self.tenant_id)  # type: ignore[arg-type]

    async def get(self, obj_id: Any) -> T | None:
        stmt = self._select().where(self.model.id == obj_id)  # type: ignore[attr-defined]
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(
        self,
        *where: Any,
        order_by: Any = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[T]:
        stmt = self._select().where(*where)
        if order_by is not None:
            stmt = stmt.order_by(*order_by) if isinstance(order_by, list | tuple) else stmt.order_by(order_by)
        if limit is not None:
            stmt = stmt.limit(limit).offset(offset)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count(self, *where: Any) -> int:
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(self.model.tenant_id == self.tenant_id, *where)  # type: ignore[arg-type]
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def add(self, obj: T) -> T:
        existing = getattr(obj, "tenant_id", None)
        if existing is not None and existing != self.tenant_id:
            raise PermissionError("Objeto de otro tenant")
        obj.tenant_id = self.tenant_id  # type: ignore[assignment]
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def delete(self, obj: T) -> None:
        if obj.tenant_id != self.tenant_id:  # type: ignore[comparison-overlap]
            raise PermissionError("Objeto de otro tenant")
        await self.session.delete(obj)
        await self.session.flush()
