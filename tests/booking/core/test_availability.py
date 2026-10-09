from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.availability import (
    BookingRules,
    compute_slots,
    holiday_name,
    is_holiday,
    merge_intervals,
    overlaps,
    subtract_intervals,
)
from app.core.clock import BOGOTA
from app.db.models.booking import Appointment, TimeOff
from app.db.models.catalog import Service
from app.db.models.tenants import Tenant
from tests.booking.core.conftest import local

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def h(n: int) -> datetime:
    return T0 + timedelta(hours=n)


def test_merge_intervals() -> None:
    assert merge_intervals([(h(3), h(5)), (h(1), h(2)), (h(2), h(4)), (h(9), h(9))]) == [
        (h(1), h(5))
    ]


def test_subtract_intervals() -> None:
    assert subtract_intervals([(h(0), h(10))], [(h(2), h(3)), (h(8), h(12))]) == [
        (h(0), h(2)),
        (h(3), h(8)),
    ]
    assert subtract_intervals([(h(0), h(2))], [(h(0), h(5))]) == []
    assert subtract_intervals([(h(0), h(2))], [(h(5), h(6))]) == [(h(0), h(2))]


def test_overlaps() -> None:
    assert overlaps((h(0), h(2)), (h(1), h(3)))
    assert not overlaps((h(0), h(1)), (h(1), h(2)))


def test_colombian_holidays() -> None:
    assert is_holiday(date(2026, 12, 25))
    assert holiday_name(date(2026, 12, 25))
    assert not is_holiday(date(2026, 12, 23))


def test_rules_clamp_and_defaults() -> None:
    r = BookingRules.from_dict({"slot_minutes": "x", "buffer_min": 9999, "min_notice_hours": -3})
    assert r.slot_minutes == 15 and r.buffer_min == 240 and r.min_notice_hours == 0
    assert BookingRules.from_dict(None) == BookingRules()


async def slots_for(
    session: AsyncSession, tenant: Tenant, service: Service, day: date, **kw: Any
) -> list[datetime]:
    out = await compute_slots(
        session, tenant.id, service.id, day, kw.pop("to", day), max_slots=500, **kw
    )
    return [s for s, _ in out]


async def test_basic_grid_and_timezone(
    session: AsyncSession, tenant: Tenant, service: Service, hours: None, workday: date
) -> None:
    rules = BookingRules(slot_minutes=60)
    slots = await slots_for(session, tenant, service, workday, rules=rules)
    local_times = [s.astimezone(BOGOTA).strftime("%H:%M") for s in slots]
    assert local_times == ["09:00", "10:00", "11:00", "14:00", "15:00", "16:00"]
    assert slots[0] == local(workday, "09:00")


async def test_duration_must_fit_window(
    session: AsyncSession, tenant: Tenant, service: Service, hours: None, workday: date
) -> None:
    service.duration_min = 90
    await session.commit()
    slots = await slots_for(session, tenant, service, workday, rules=BookingRules(slot_minutes=30))
    times = [s.astimezone(BOGOTA).strftime("%H:%M") for s in slots]
    assert "10:30" in times and "11:00" not in times and "15:30" in times and "16:00" not in times


async def test_weekend_and_holiday_closed(
    session: AsyncSession, tenant: Tenant, service: Service, hours: None
) -> None:
    saturday = date(2026, 11, 7)
    assert (
        await slots_for(session, tenant, service, saturday, now=datetime(2026, 10, 1, tzinfo=UTC))
        == []
    )
    # 2026-12-08 (martes) es festivo; 12-09 no
    now = datetime(2026, 12, 1, tzinfo=UTC)
    assert await slots_for(session, tenant, service, date(2026, 12, 8), now=now) == []
    assert await slots_for(session, tenant, service, date(2026, 12, 9), now=now)
    rules = BookingRules(closed_on_holidays=False)
    assert await slots_for(session, tenant, service, date(2026, 12, 8), now=now, rules=rules)


async def test_existing_appointment_and_buffer(
    session: AsyncSession, tenant: Tenant, service: Service, hours: None, workday: date
) -> None:
    session.add(
        Appointment(
            tenant_id=tenant.id,
            service_id=service.id,
            starts_at=local(workday, "10:00"),
            ends_at=local(workday, "11:00"),
            status="confirmed",
            source="manual",
            idempotency_key="a",
        )
    )
    await session.commit()
    plain = await slots_for(session, tenant, service, workday, rules=BookingRules(slot_minutes=60))
    assert [s.astimezone(BOGOTA).strftime("%H:%M") for s in plain] == [
        "09:00",
        "11:00",
        "14:00",
        "15:00",
        "16:00",
    ]
    buffered = await slots_for(
        session, tenant, service, workday, rules=BookingRules(slot_minutes=60, buffer_min=15)
    )
    # con 15 min de colchon ni 09:00 (termina 10:00 pegado) ni 11:00 caben
    assert [s.astimezone(BOGOTA).strftime("%H:%M") for s in buffered] == ["14:00", "15:00", "16:00"]


async def test_expired_hold_does_not_block(
    session: AsyncSession, tenant: Tenant, service: Service, hours: None, workday: date
) -> None:
    session.add(
        Appointment(
            tenant_id=tenant.id,
            service_id=service.id,
            starts_at=local(workday, "09:00"),
            ends_at=local(workday, "10:00"),
            status="pending",
            hold_expires_at=datetime(2020, 1, 1, tzinfo=UTC),
            source="bot",
            idempotency_key="h",
        )
    )
    await session.commit()
    slots = await slots_for(session, tenant, service, workday, rules=BookingRules(slot_minutes=60))
    assert local(workday, "09:00") in slots


async def test_time_off_blocks(
    session: AsyncSession, tenant: Tenant, service: Service, hours: None, workday: date
) -> None:
    session.add(
        TimeOff(
            tenant_id=tenant.id, starts_at=local(workday, "00:00"), ends_at=local(workday, "13:00")
        )
    )
    await session.commit()
    slots = await slots_for(session, tenant, service, workday, rules=BookingRules(slot_minutes=60))
    assert [s.astimezone(BOGOTA).strftime("%H:%M") for s in slots] == ["14:00", "15:00", "16:00"]


async def test_min_notice_and_max_days(
    session: AsyncSession, tenant: Tenant, service: Service, hours: None
) -> None:
    day = date(2026, 11, 3)  # martes (el 2 es festivo)
    now = local(day, "08:00")
    rules = BookingRules(slot_minutes=60, min_notice_hours=2)
    slots = await slots_for(session, tenant, service, day, now=now, rules=rules)
    assert slots[0] == local(day, "10:00")
    far = date(2026, 12, 14)
    assert (
        await slots_for(
            session, tenant, service, far, now=now, rules=BookingRules(max_days_ahead=10)
        )
        == []
    )
    assert await slots_for(session, tenant, service, date(2026, 10, 1), now=now) == []


async def test_external_busy_and_provider_failure(
    session: AsyncSession, tenant: Tenant, service: Service, hours: None, workday: date
) -> None:
    class Busy:
        async def busy_intervals(self, *_a: Any) -> list[tuple[datetime, datetime]]:
            return [(local(workday, "09:00"), local(workday, "12:00"))]

    class Broken:
        async def busy_intervals(self, *_a: Any) -> list[tuple[datetime, datetime]]:
            raise RuntimeError("google caido")

    rules = BookingRules(slot_minutes=60)
    got = await slots_for(session, tenant, service, workday, rules=rules, provider=Busy())  # type: ignore[arg-type]
    assert len(got) == 3
    got = await slots_for(session, tenant, service, workday, rules=rules, provider=Broken())  # type: ignore[arg-type]
    assert len(got) == 6


async def test_spread_across_days_and_limit(
    session: AsyncSession, tenant: Tenant, service: Service, hours: None, workday: date
) -> None:
    out = await compute_slots(
        session,
        tenant.id,
        service.id,
        workday,
        workday + timedelta(days=4),
        max_slots=5,
        rules=BookingRules(slot_minutes=30),
    )
    assert len(out) == 5
    assert len({s.astimezone(BOGOTA).date() for s, _ in out}) == 5
    assert out == sorted(out)


async def test_tenant_isolation_and_unknown_service(
    session: AsyncSession,
    tenant: Tenant,
    service: Service,
    hours: None,
    workday: date,
    make_tenant: Any,
) -> None:
    other = await make_tenant()
    assert await compute_slots(session, other.id, service.id, workday, workday) == []
    import uuid

    assert await compute_slots(session, tenant.id, uuid.uuid4(), workday, workday) == []
    assert (
        await compute_slots(session, tenant.id, service.id, workday, workday - timedelta(days=1))
        == []
    )
    # citas de otro tenant no bloquean
    session.add(
        Appointment(
            tenant_id=other.id,
            starts_at=local(workday, "09:00"),
            ends_at=local(workday, "10:00"),
            status="confirmed",
            source="manual",
            idempotency_key="z",
        )
    )
    await session.commit()
    assert local(workday, "09:00") in await slots_for(session, tenant, service, workday)


async def test_time_from_working_hours_unused_import() -> None:
    assert time(9, 0) < time(10, 0)
