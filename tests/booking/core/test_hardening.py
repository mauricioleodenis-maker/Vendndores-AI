"""Hardening-C: reschedule con solape del contacto, 3.er tramo y doble envio manual."""

from __future__ import annotations

from datetime import date, time

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.booking import Appointment, WorkingHours
from app.db.models.catalog import Service
from app.db.models.tenants import Tenant

pytestmark = pytest.mark.usefixtures("hours")


async def test_save_hours_keeps_hidden_third_window(
    authenticated_client: httpx.AsyncClient, session: AsyncSession, tenant: Tenant
) -> None:
    for s, e in (("08:00", "10:00"), ("11:00", "12:00"), ("15:00", "17:00")):
        session.add(
            WorkingHours(
                tenant_id=tenant.id,
                weekday=6,
                start_time=time.fromisoformat(s),
                end_time=time.fromisoformat(e),
            )
        )
    await session.commit()
    data = {"tenant_id": str(tenant.id), "d6_ini1": "08:00", "d6_fin1": "10:00"}
    # un tramo nuevo que choca con el oculto (15-17) se rechaza
    bad = {**data, "d6_ini2": "14:00", "d6_fin2": "16:00"}
    r = await authenticated_client.post("/admin/citas/horarios", data=bad)
    assert "error=" in r.headers["location"]
    r = await authenticated_client.post("/admin/citas/horarios", data=data)
    assert "ok=" in r.headers["location"]
    await session.rollback()
    rows = (
        (
            await session.execute(
                select(WorkingHours)
                .where(WorkingHours.weekday == 6)
                .order_by(WorkingHours.start_time)
            )
        )
        .scalars()
        .all()
    )
    assert [w.start_time for w in rows] == [time(8), time(15)]


async def test_manual_double_submit_is_idempotent(
    authenticated_client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    service: Service,
    workday: date,
) -> None:
    form = {
        "tenant_id": str(tenant.id),
        "service_id": str(service.id),
        "fecha": workday.isoformat(),
        "hora": "10:00",
        "telefono": "300 123 4567",
        "clave": "ab" * 16,
    }
    first = await authenticated_client.post("/admin/citas/nueva", data=form)
    second = await authenticated_client.post("/admin/citas/nueva", data=form)
    assert "ok=" in first.headers["location"] and "ok=" in second.headers["location"]
    assert len((await session.execute(select(Appointment))).scalars().all()) == 1
