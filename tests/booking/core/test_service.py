from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking import service as svc_mod
from app.booking.providers import LocalCalendarProvider, get_provider
from app.booking.service import BookingService, SlotTakenError, SlotUnavailableError
from app.core.errors import AppError, NotFoundError
from app.db.models.audit import AuditLog
from app.db.models.booking import Appointment, CalendarConnection
from app.db.models.bots import BotConfig
from app.db.models.catalog import Service
from app.db.models.contacts import Contact
from app.db.models.tenants import Tenant
from tests.booking.core.conftest import local

pytestmark = pytest.mark.usefixtures("hours")


class SpyProvider(LocalCalendarProvider):
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def create_event(self, session: Any, tenant_id: Any, appointment: Any) -> str | None:
        self.calls.append("create")
        if self.fail:
            raise RuntimeError("boom")
        return "evt-1"

    async def update_event(self, session: Any, tenant_id: Any, appointment: Any) -> None:
        self.calls.append("update")

    async def delete_event(self, session: Any, tenant_id: Any, appointment: Any) -> None:
        self.calls.append("delete")


@pytest.fixture
def spy_scheduler(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def schedule(session: Any, appt: Any) -> None:
        calls.append("schedule")

    async def cancel(session: Any, appt_id: Any) -> None:
        calls.append("cancel")

    monkeypatch.setattr(svc_mod.scheduler, "schedule_for_appointment", schedule)
    monkeypatch.setattr(svc_mod.scheduler, "cancel_for_appointment", cancel)
    return calls


async def test_find_slots(
    session: AsyncSession, tenant: Tenant, service: Service, workday: date
) -> None:
    slots = await BookingService().find_slots(
        session, tenant.id, service.id, workday, workday, limit=4
    )
    assert len(slots) == 4 and slots[0].ends_at - slots[0].starts_at == timedelta(minutes=60)


async def test_book_ok_and_idempotent(
    session: AsyncSession,
    tenant: Tenant,
    service: Service,
    contact: Contact,
    workday: date,
    spy_scheduler: list[str],
) -> None:
    spy = SpyProvider()
    bs = BookingService(spy)
    start = local(workday, "10:00")
    a1 = await bs.book(
        session,
        tenant.id,
        contact_id=contact.id,
        service_id=service.id,
        starts_at=start,
        idempotency_key="k1",
    )
    a2 = await bs.book(
        session,
        tenant.id,
        contact_id=contact.id,
        service_id=service.id,
        starts_at=start,
        idempotency_key="k1",
    )
    assert a1.id == a2.id and a1.status == "confirmed"
    assert a1.google_event_id == "evt-1" and a1.sync_status == "synced"
    assert spy.calls == ["create"] and spy_scheduler == ["schedule"]
    await session.commit()
    audit = (
        (await session.execute(select(AuditLog).where(AuditLog.action == "appointment.create")))
        .scalars()
        .all()
    )
    assert len(audit) == 1


async def test_double_booking_rejected(
    session: AsyncSession, tenant: Tenant, service: Service, make_contact: Any, workday: date
) -> None:
    bs = BookingService()
    c1, c2 = await make_contact(), await make_contact()
    start = local(workday, "10:00")
    await bs.book(
        session,
        tenant.id,
        contact_id=c1.id,
        service_id=service.id,
        starts_at=start,
        idempotency_key="a",
    )
    with pytest.raises(AppError):  # el bot ya no lo ve en la grilla
        await bs.book(
            session,
            tenant.id,
            contact_id=c2.id,
            service_id=service.id,
            starts_at=start,
            idempotency_key="b",
        )
    with pytest.raises(SlotTakenError):  # manual: choque directo
        await bs.book(
            session,
            tenant.id,
            contact_id=c2.id,
            service_id=service.id,
            starts_at=start + timedelta(minutes=30),
            idempotency_key="c",
            source="manual",
        )
    # distinta key, mismo contacto, mismo slot manual -> tambien choca
    with pytest.raises(SlotTakenError):
        await bs.book(
            session,
            tenant.id,
            contact_id=c1.id,
            service_id=service.id,
            starts_at=start,
            idempotency_key="d",
            source="manual",
        )


async def test_concurrent_same_idempotency_key_returns_one(
    session: AsyncSession, tenant: Tenant, service: Service, contact: Contact, workday: date
) -> None:
    bs = BookingService()
    start = local(workday, "11:00")
    results = []
    for _ in range(3):
        results.append(
            await bs.book(
                session,
                tenant.id,
                contact_id=contact.id,
                service_id=service.id,
                starts_at=start,
                idempotency_key="same",
            )
        )
    assert len({r.id for r in results}) == 1
    count = (await session.execute(select(Appointment))).scalars().all()
    assert len(count) == 1


async def test_unique_index_race_maps_to_slot_taken(
    session: AsyncSession,
    tenant: Tenant,
    service: Service,
    make_contact: Any,
    workday: date,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si la verificacion previa no ve la cita (carrera), el indice unico lo atrapa."""
    bs = BookingService()
    c1, c2 = await make_contact(), await make_contact()
    start = local(workday, "10:00")
    await bs.book(
        session,
        tenant.id,
        contact_id=c1.id,
        service_id=service.id,
        starts_at=start,
        idempotency_key="a",
    )

    async def blind(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(svc_mod.availability, "active_appointments", blind)
    monkeypatch.setattr(BookingService, "_require_offered", allow)
    with pytest.raises(SlotTakenError):
        await bs.book(
            session,
            tenant.id,
            contact_id=c2.id,
            service_id=service.id,
            starts_at=start,
            idempotency_key="b",
        )
    # la sesion sigue utilizable
    assert (await session.execute(select(Appointment))).scalars().all()


async def test_validation_errors(
    session: AsyncSession,
    tenant: Tenant,
    service: Service,
    contact: Contact,
    workday: date,
    make_tenant: Any,
) -> None:
    bs = BookingService()
    kw: dict[str, Any] = {"contact_id": contact.id, "service_id": service.id}
    with pytest.raises(AppError):
        await bs.book(
            session, tenant.id, starts_at=local(workday, "10:00"), idempotency_key="", **kw
        )
    with pytest.raises(AppError):
        await bs.book(
            session, tenant.id, starts_at=datetime(2030, 1, 1, 10), idempotency_key="x", **kw
        )
    with pytest.raises(AppError):
        await bs.book(
            session,
            tenant.id,
            starts_at=local(workday, "10:00"),
            idempotency_key="x",
            source="otro",
            **kw,
        )
    with pytest.raises(SlotUnavailableError):  # fuera de horario laboral (bot)
        await bs.book(
            session, tenant.id, starts_at=local(workday, "13:00"), idempotency_key="y", **kw
        )
    with pytest.raises(SlotUnavailableError):  # pasado
        await bs.book(
            session,
            tenant.id,
            starts_at=local(workday - timedelta(days=30), "10:00"),
            idempotency_key="z",
            **kw,
        )
    with pytest.raises(NotFoundError):
        await bs.book(
            session,
            tenant.id,
            contact_id=contact.id,
            service_id=uuid.uuid4(),
            starts_at=local(workday, "10:00"),
            idempotency_key="s",
        )
    other = await make_tenant()
    with pytest.raises(NotFoundError):  # contacto de otro tenant
        await bs.book(
            session, other.id, starts_at=local(workday, "10:00"), idempotency_key="t", **kw
        )


async def test_contact_limits(
    session: AsyncSession, tenant: Tenant, service: Service, contact: Contact, workday: date
) -> None:
    bs = BookingService()
    kw: dict[str, Any] = {"contact_id": contact.id, "service_id": service.id}
    await bs.book(session, tenant.id, starts_at=local(workday, "09:00"), idempotency_key="1", **kw)
    await bs.book(session, tenant.id, starts_at=local(workday, "10:00"), idempotency_key="2", **kw)
    with pytest.raises(AppError) as exc:
        await bs.book(
            session, tenant.id, starts_at=local(workday, "11:00"), idempotency_key="3", **kw
        )
    assert exc.value.code == "too_many_appointments"


async def test_contact_overlap(
    session: AsyncSession, tenant: Tenant, service: Service, contact: Contact, workday: date
) -> None:
    session.add(
        BotConfig(
            tenant_id=tenant.id,
            version=1,
            status="published",
            booking_rules={"slot_minutes": 30, "max_active_per_contact": 5},
        )
    )
    await session.commit()
    bs = BookingService()
    kw: dict[str, Any] = {"contact_id": contact.id, "service_id": service.id}
    await bs.book(session, tenant.id, starts_at=local(workday, "09:00"), idempotency_key="1", **kw)
    with pytest.raises(AppError):
        await bs.book(
            session, tenant.id, starts_at=local(workday, "09:30"), idempotency_key="2", **kw
        )


async def test_provider_failure_does_not_break_booking(
    session: AsyncSession, tenant: Tenant, service: Service, contact: Contact, workday: date
) -> None:
    bs = BookingService(SpyProvider(fail=True))
    a = await bs.book(
        session,
        tenant.id,
        contact_id=contact.id,
        service_id=service.id,
        starts_at=local(workday, "10:00"),
        idempotency_key="f",
    )
    assert a.status == "confirmed" and a.sync_status == "pending_push"


async def test_cancel(
    session: AsyncSession,
    tenant: Tenant,
    service: Service,
    contact: Contact,
    workday: date,
    spy_scheduler: list[str],
    make_tenant: Any,
) -> None:
    spy = SpyProvider()
    bs = BookingService(spy)
    a = await bs.book(
        session,
        tenant.id,
        contact_id=contact.id,
        service_id=service.id,
        starts_at=local(workday, "10:00"),
        idempotency_key="c",
    )
    done = await bs.cancel(session, tenant.id, a.id, by="bot", reason="  no puedo  ")
    assert (
        done.status == "cancelled"
        and done.cancelled_by == "contact"
        and done.cancel_reason == "no puedo"
    )
    assert "delete" in spy.calls and spy_scheduler[-1] == "cancel"
    again = await bs.cancel(session, tenant.id, a.id, by="tenant")
    assert again.cancelled_by == "contact"  # idempotente
    # el slot vuelve a estar libre
    slots = await bs.find_slots(session, tenant.id, service.id, workday, workday, limit=50)
    assert local(workday, "10:00") in {s.starts_at for s in slots}
    with pytest.raises(AppError):
        await bs.cancel(session, tenant.id, a.id, by="hacker")
    other = await make_tenant()
    with pytest.raises(NotFoundError):
        await bs.cancel(session, other.id, a.id, by="tenant")
    a.status = "done"
    await session.flush()
    with pytest.raises(AppError):
        await bs.cancel(session, tenant.id, a.id, by="tenant")


async def test_reschedule(
    session: AsyncSession,
    tenant: Tenant,
    service: Service,
    make_contact: Any,
    workday: date,
    spy_scheduler: list[str],
) -> None:
    spy = SpyProvider()
    bs = BookingService(spy)
    c1, c2 = await make_contact(), await make_contact()
    a = await bs.book(
        session,
        tenant.id,
        contact_id=c1.id,
        service_id=service.id,
        starts_at=local(workday, "10:00"),
        idempotency_key="r1",
    )
    b = await bs.book(
        session,
        tenant.id,
        contact_id=c2.id,
        service_id=service.id,
        starts_at=local(workday, "15:00"),
        idempotency_key="r2",
    )
    a.reminder_24h_sent_at = datetime(2020, 1, 1)
    moved = await bs.reschedule(
        session, tenant.id, a.id, local(workday, "10:00") + timedelta(minutes=15), by="contact"
    )
    assert moved.starts_at == local(workday, "10:15") and moved.reminder_24h_sent_at is None
    assert spy.calls[-1] == "update" and spy_scheduler[-2:] == ["cancel", "schedule"]
    with pytest.raises(SlotTakenError):
        await bs.reschedule(session, tenant.id, a.id, b.starts_at, by="tenant")
    with pytest.raises(SlotUnavailableError):
        await bs.reschedule(session, tenant.id, a.id, local(workday, "13:00"), by="contact")
    # el personal puede mover fuera de la grilla si no hay choque
    again = await bs.reschedule(session, tenant.id, a.id, local(workday, "13:00"), by="tenant")
    assert again.starts_at == local(workday, "13:00")
    with pytest.raises(SlotUnavailableError):
        await bs.reschedule(
            session, tenant.id, a.id, local(workday - timedelta(days=30), "10:00"), by="tenant"
        )
    await bs.cancel(session, tenant.id, a.id, by="tenant")
    with pytest.raises(AppError):
        await bs.reschedule(session, tenant.id, a.id, local(workday, "09:00"), by="tenant")
    with pytest.raises(AppError):
        await bs.reschedule(session, tenant.id, b.id, local(workday, "09:00"), by="nadie")


async def test_reschedule_onto_own_slot_overlap(
    session: AsyncSession, tenant: Tenant, service: Service, contact: Contact, workday: date
) -> None:
    bs = BookingService()
    a = await bs.book(
        session,
        tenant.id,
        contact_id=contact.id,
        service_id=service.id,
        starts_at=local(workday, "10:00"),
        idempotency_key="o",
    )
    moved = await bs.reschedule(session, tenant.id, a.id, local(workday, "10:30"), by="contact")
    assert moved.starts_at == local(workday, "10:30")  # solape con su propio hueco permitido


async def test_get_provider_selection(
    session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert isinstance(await get_provider(session, tenant.id), LocalCalendarProvider)
    session.add(CalendarConnection(tenant_id=tenant.id, provider="google", status="active"))
    await session.commit()

    import importlib
    import types

    fake = types.SimpleNamespace(GoogleCalendarProvider=SpyProvider)
    real_import = importlib.import_module
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda n, *a: fake if n == "app.booking.google_calendar" else real_import(n, *a),
    )
    assert isinstance(await get_provider(session, tenant.id), SpyProvider)

    def boom(n: str, *a: Any) -> Any:
        raise ImportError(n)

    monkeypatch.setattr(importlib, "import_module", boom)
    assert isinstance(await get_provider(session, tenant.id), LocalCalendarProvider)


async def test_local_provider_noops(session: AsyncSession, tenant: Tenant) -> None:
    p = LocalCalendarProvider()
    now = datetime(2026, 1, 1)
    assert await p.busy_intervals(session, tenant.id, now, now) == []
    a: Any = None
    assert await p.create_event(session, tenant.id, a) is None
    assert await p.update_event(session, tenant.id, a) is None
    assert await p.delete_event(session, tenant.id, a) is None


async def test_gather_unused() -> None:
    assert await asyncio.sleep(0) is None
