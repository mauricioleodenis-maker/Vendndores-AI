"""Contrato de proveedores de calendario. Dueno: B8/B9 (puede ampliarse sin romper)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.booking import Appointment


class CalendarProvider(Protocol):
    async def busy_intervals(
        self, session: AsyncSession, tenant_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]: ...

    async def create_event(
        self, session: AsyncSession, tenant_id: uuid.UUID, appointment: Appointment
    ) -> str | None: ...

    async def update_event(
        self, session: AsyncSession, tenant_id: uuid.UUID, appointment: Appointment
    ) -> None: ...

    async def delete_event(
        self, session: AsyncSession, tenant_id: uuid.UUID, appointment: Appointment
    ) -> None: ...
