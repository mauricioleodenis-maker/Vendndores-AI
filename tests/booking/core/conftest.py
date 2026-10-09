from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.availability import is_holiday
from app.channels.service import get_or_create_contact
from app.core.clock import BOGOTA, utcnow
from app.db.models.booking import WorkingHours
from app.db.models.catalog import Service
from app.db.models.contacts import Contact
from app.db.models.tenants import Tenant


def next_workday(weekday: int = 0, min_days: int = 8) -> date:
    """Proximo ``weekday`` (0=lunes) a >= ``min_days`` dias y que no sea festivo."""
    d = utcnow().astimezone(BOGOTA).date() + timedelta(days=min_days)
    while d.weekday() != weekday or is_holiday(d):
        d += timedelta(days=1)
    return d


def local(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), BOGOTA).astimezone(UTC)


@pytest.fixture
def workday() -> date:
    return next_workday(0)


@pytest_asyncio.fixture
async def hours(session: AsyncSession, tenant: Tenant) -> None:
    for wd in range(5):
        for s, e in (("09:00", "12:00"), ("14:00", "17:00")):
            session.add(
                WorkingHours(
                    tenant_id=tenant.id,
                    weekday=wd,
                    start_time=time.fromisoformat(s),
                    end_time=time.fromisoformat(e),
                )
            )
    await session.commit()


@pytest_asyncio.fixture
async def service(session: AsyncSession, tenant: Tenant) -> Service:
    svc = Service(tenant_id=tenant.id, name="Limpieza", duration_min=60)
    session.add(svc)
    await session.commit()
    return svc


@pytest_asyncio.fixture
async def make_contact(session: AsyncSession, tenant: Tenant) -> Callable[..., Any]:
    n = {"i": 0}

    async def _make(tenant_id: Any = None) -> Contact:
        n["i"] += 1
        c = await get_or_create_contact(
            session, tenant_id or tenant.id, f"+57300000{n['i']:04d}", f"Cliente {n['i']}"
        )
        await session.commit()
        return c

    return _make


@pytest_asyncio.fixture
async def contact(make_contact: Callable[..., Any]) -> Contact:
    return await make_contact()  # type: ignore[no-any-return]
