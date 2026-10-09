from __future__ import annotations

from datetime import date, time
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.booking import Appointment, TimeOff, WorkingHours
from app.db.models.catalog import Service
from app.db.models.tenants import Tenant
from tests.booking.core.conftest import local


async def test_requires_login(client: httpx.AsyncClient) -> None:
    r = await client.get("/admin/citas", headers={"accept": "text/html"})
    assert r.status_code in (303, 401)


async def test_empty_state(authenticated_client: httpx.AsyncClient) -> None:
    r = await authenticated_client.get("/admin/citas")
    assert r.status_code == 200 and "Sin empresas" in r.text


@pytest.mark.usefixtures("hours")
async def test_manual_create_list_cancel(
    authenticated_client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    service: Service,
    workday: date,
) -> None:
    c = authenticated_client
    form = {
        "tenant_id": str(tenant.id),
        "service_id": str(service.id),
        "fecha": workday.isoformat(),
        "hora": "10:00",
        "telefono": "300 123 4567",
        "nombre": "Ana Perez",
    }
    r = await c.post("/admin/citas/nueva", data=form)
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    page = await c.get(f"/admin/citas?tenant_id={tenant.id}&dia={workday.isoformat()}")
    assert "Ana Perez" in page.text and "10:00" in page.text and "Limpieza" in page.text
    assert "3001234567" not in page.text

    dup = await c.post("/admin/citas/nueva", data=form)
    assert "error=" in dup.headers["location"]
    bad = await c.post("/admin/citas/nueva", data={**form, "telefono": "123"})
    assert "error=" in bad.headers["location"]
    bad = await c.post("/admin/citas/nueva", data={**form, "hora": "25:99"})
    assert "error=" in bad.headers["location"]

    appt = (await session.execute(select(Appointment))).scalar_one()
    r = await c.post(
        f"/admin/citas/{appt.id}/cancelar", data={"tenant_id": str(tenant.id), "motivo": "x"}
    )
    assert "ok=" in r.headers["location"]
    r = await c.post(f"/admin/citas/{appt.id}/cancelar", data={"tenant_id": str(tenant.id)})
    assert r.status_code == 303
    page = await c.get(f"/admin/citas?tenant_id={tenant.id}&dia={workday.isoformat()}")
    assert "Cancelada" in page.text


async def test_cancel_unknown_and_csrf(
    authenticated_client: httpx.AsyncClient, tenant: Tenant
) -> None:
    import uuid

    r = await authenticated_client.post(
        f"/admin/citas/{uuid.uuid4()}/cancelar", data={"tenant_id": str(tenant.id)}
    )
    assert r.status_code == 404
    r = await authenticated_client.post("/admin/citas/nueva", data={}, headers={"X-CSRF-Token": ""})
    assert r.status_code in (403, 422)


async def test_unknown_tenant_404(authenticated_client: httpx.AsyncClient) -> None:
    import uuid

    r = await authenticated_client.get(f"/admin/citas?tenant_id={uuid.uuid4()}")
    assert r.status_code == 404


async def test_holiday_banner_and_bad_day(
    authenticated_client: httpx.AsyncClient, tenant: Tenant
) -> None:
    r = await authenticated_client.get(f"/admin/citas?tenant_id={tenant.id}&dia=2026-12-25")
    assert "Festivo" in r.text
    r = await authenticated_client.get(f"/admin/citas?tenant_id={tenant.id}&dia=basura")
    assert r.status_code == 200


async def test_working_hours_editor(
    authenticated_client: httpx.AsyncClient, session: AsyncSession, tenant: Tenant
) -> None:
    c = authenticated_client
    tid = str(tenant.id)
    r = await c.get(f"/admin/citas/horarios?tenant_id={tid}")
    assert r.status_code == 200 and "Lunes" in r.text
    data = {
        "tenant_id": tid,
        "d0_ini1": "08:00",
        "d0_fin1": "12:00",
        "d0_ini2": "14:00",
        "d0_fin2": "18:00",
        "d5_ini1": "09:00",
        "d5_fin1": "13:00",
    }
    r = await c.post("/admin/citas/horarios", data=data)
    assert "ok=" in r.headers["location"]
    rows = (
        (
            await session.execute(
                select(WorkingHours).order_by(WorkingHours.weekday, WorkingHours.start_time)
            )
        )
        .scalars()
        .all()
    )
    assert [(w.weekday, w.start_time) for w in rows] == [(0, time(8)), (0, time(14)), (5, time(9))]
    r = await c.get(f"/admin/citas/horarios?tenant_id={tid}")
    assert 'value="08:00"' in r.text

    for bad in (
        {**data, "d1_ini1": "10:00", "d1_fin1": "09:00"},
        {**data, "d1_ini1": "10:00"},
        {**data, "d1_ini1": "xx", "d1_fin1": "11:00"},
        {**data, "d1_ini1": "09:00", "d1_fin1": "12:00", "d1_ini2": "11:00", "d1_fin2": "13:00"},
    ):
        r = await c.post("/admin/citas/horarios", data=bad)
        assert "error=" in r.headers["location"]
    await session.rollback()
    assert len((await session.execute(select(WorkingHours))).scalars().all()) == 3

    r = await c.post("/admin/citas/horarios", data={"tenant_id": tid})
    assert "ok=" in r.headers["location"]
    await session.rollback()
    assert (await session.execute(select(WorkingHours))).scalars().all() == []


async def test_time_off_crud(
    authenticated_client: httpx.AsyncClient, session: AsyncSession, tenant: Tenant, workday: date
) -> None:
    c = authenticated_client
    base = {"tenant_id": str(tenant.id)}
    r = await c.post(
        "/admin/citas/ausencias",
        data={
            **base,
            "desde": workday.isoformat(),
            "hasta": workday.isoformat(),
            "motivo": "Vacaciones",
        },
    )
    assert "ok=" in r.headers["location"]
    off = (await session.execute(select(TimeOff))).scalar_one()
    assert off.starts_at == local(workday, "00:00")
    page = await c.get(f"/admin/citas/horarios?tenant_id={tenant.id}")
    assert "Vacaciones" in page.text and workday.strftime("%d/%m/%Y") in page.text
    r = await c.post(
        "/admin/citas/ausencias", data={**base, "desde": "2026-02-02", "hasta": "2026-02-01"}
    )
    assert "error=" in r.headers["location"]
    r = await c.post("/admin/citas/ausencias", data={**base, "desde": "mal", "hasta": "mal"})
    assert "error=" in r.headers["location"]
    r = await c.post(f"/admin/citas/ausencias/{off.id}/eliminar", data=base)
    assert "ok=" in r.headers["location"]
    import uuid

    r = await c.post(f"/admin/citas/ausencias/{uuid.uuid4()}/eliminar", data=base)
    assert r.status_code == 404


async def test_operator_cannot_edit_hours(
    client: httpx.AsyncClient, make_user: Any, login: Any, tenant: Tenant
) -> None:
    op = await make_user("operator")
    await login(client, op)
    assert (await client.get("/admin/citas")).status_code == 200
    assert (await client.get(f"/admin/citas/horarios?tenant_id={tenant.id}")).status_code == 403
