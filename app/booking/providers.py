"""Proveedores de calendario: local (por defecto) y seleccion por tenant."""

from __future__ import annotations

import importlib
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.calendar_base import CalendarProvider
from app.core.logging import get_logger
from app.db.models.booking import Appointment, CalendarConnection

log = get_logger(__name__)


class LocalCalendarProvider:
    """Calendario propio: la DB es la fuente de verdad; no hay ocupado externo ni espejo."""

    async def busy_intervals(
        self, session: AsyncSession, tenant_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        return []

    async def create_event(
        self, session: AsyncSession, tenant_id: uuid.UUID, appointment: Appointment
    ) -> str | None:
        return None

    async def update_event(
        self, session: AsyncSession, tenant_id: uuid.UUID, appointment: Appointment
    ) -> None:
        return None

    async def delete_event(
        self, session: AsyncSession, tenant_id: uuid.UUID, appointment: Appointment
    ) -> None:
        return None


LOCAL_PROVIDER = LocalCalendarProvider()


async def get_provider(session: AsyncSession, tenant_id: uuid.UUID) -> CalendarProvider:
    """Google si el tenant tiene una conexion activa y el modulo existe; si no, local.

    ``app.booking.google_calendar`` (B9) debe exponer ``GoogleCalendarProvider`` con
    constructor sin argumentos que cumpla ``CalendarProvider``.
    """
    conn = (
        await session.execute(
            select(CalendarConnection.provider).where(
                CalendarConnection.tenant_id == tenant_id,
                CalendarConnection.provider == "google",
                CalendarConnection.status == "active",
            )
        )
    ).first()
    if conn is None:
        return LOCAL_PROVIDER
    try:
        module = importlib.import_module("app.booking.google_calendar")
        provider: CalendarProvider = module.GoogleCalendarProvider()
    except (ImportError, AttributeError, TypeError):
        log.warning("booking.google_provider_unavailable", tenant_id=str(tenant_id))
        return LOCAL_PROVIDER
    return provider
