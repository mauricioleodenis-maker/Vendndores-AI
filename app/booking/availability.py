"""Calculo de disponibilidad: funciones puras sobre intervalos + ``compute_slots``.

Todo se guarda en UTC; las reglas de horario se interpretan en la zona del tenant
(por defecto ``America/Bogota``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import holidays
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.calendar_base import CalendarProvider
from app.core.clock import BOGOTA, ensure_utc, utcnow
from app.core.logging import get_logger
from app.db.models.booking import Appointment, TimeOff, WorkingHours
from app.db.models.bots import BotConfig
from app.db.models.catalog import Service
from app.db.models.tenants import Tenant

log = get_logger(__name__)

Interval = tuple[datetime, datetime]

MAX_RANGE_DAYS = 62
ACTIVE_STATUSES = ("pending", "confirmed")


@dataclass(frozen=True, slots=True)
class BookingRules:
    slot_minutes: int = 15
    buffer_min: int = 0
    min_notice_hours: float = 2
    max_days_ahead: int = 30
    closed_on_holidays: bool = True
    max_active_per_contact: int = 2

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> BookingRules:
        raw = raw or {}
        d = cls()

        def _num(key: str, default: float, lo: float, hi: float) -> float:
            try:
                val = float(raw.get(key, default))
            except (TypeError, ValueError):
                return default
            return min(max(val, lo), hi)

        return cls(
            slot_minutes=int(_num("slot_minutes", d.slot_minutes, 5, 240)),
            buffer_min=int(_num("buffer_min", d.buffer_min, 0, 240)),
            min_notice_hours=_num("min_notice_hours", d.min_notice_hours, 0, 24 * 30),
            max_days_ahead=int(_num("max_days_ahead", d.max_days_ahead, 1, 365)),
            closed_on_holidays=bool(raw.get("closed_on_holidays", d.closed_on_holidays)),
            max_active_per_contact=int(
                _num("max_active_per_contact", d.max_active_per_contact, 1, 20)
            ),
        )


# --------------------------------------------------------------------------- intervalos puros
def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Une intervalos solapados o contiguos. Ignora los vacios."""
    ordered = sorted((s, e) for s, e in intervals if e > s)
    merged: list[Interval] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def subtract_intervals(base: Sequence[Interval], busy: Sequence[Interval]) -> list[Interval]:
    """Resta ``busy`` de ``base`` y devuelve los huecos libres."""
    free: list[Interval] = []
    blocked = merge_intervals(busy)
    for start, end in merge_intervals(base):
        cursor = start
        for b_start, b_end in blocked:
            if b_end <= cursor:
                continue
            if b_start >= end:
                break
            if b_start > cursor:
                free.append((cursor, b_start))
            cursor = max(cursor, b_end)
            if cursor >= end:
                break
        if cursor < end:
            free.append((cursor, end))
    return free


def overlaps(a: Interval, b: Interval) -> bool:
    return a[0] < b[1] and b[0] < a[1]


# --------------------------------------------------------------------------- festivos
@lru_cache(maxsize=16)
def _co_holidays(year: int) -> Any:
    return holidays.country_holidays("CO", years=year)


def is_holiday(day: date) -> bool:
    return day in _co_holidays(day.year)


def holiday_name(day: date) -> str | None:
    return _co_holidays(day.year).get(day)


# --------------------------------------------------------------------------- reglas y datos
@lru_cache(maxsize=32)
def zone_for(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - zona invalida => zona de negocio
        return BOGOTA


async def load_rules(session: AsyncSession, tenant_id: uuid.UUID) -> BookingRules:
    raw = (
        await session.execute(
            select(BotConfig.booking_rules).where(
                BotConfig.tenant_id == tenant_id, BotConfig.status == "published"
            )
        )
    ).scalar_one_or_none()
    return BookingRules.from_dict(raw if isinstance(raw, dict) else None)


_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


async def _bot_hours(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict[int, list[tuple[time, time]]]:
    """Horario semanal del bot publicado, usado mientras el negocio no tenga ``working_hours``."""
    cfg = (
        await session.execute(
            select(BotConfig.config).where(
                BotConfig.tenant_id == tenant_id, BotConfig.status == "published"
            )
        )
    ).scalar_one_or_none()
    weekly = ((cfg or {}).get("hours") or {}).get("weekly") or {}
    out: dict[int, list[tuple[time, time]]] = {}
    for idx, key in enumerate(_DAY_KEYS):
        for win in weekly.get(key) or []:
            try:
                out.setdefault(idx, []).append(
                    (time.fromisoformat(win["open"]), time.fromisoformat(win["close"]))
                )
            except (KeyError, ValueError, TypeError):
                continue
    return out


async def active_appointments(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    start: datetime,
    end: datetime,
    *,
    resource_key: str = "default",
    exclude_id: uuid.UUID | None = None,
    now: datetime | None = None,
    for_update: bool = False,
) -> list[Appointment]:
    """Citas activas que tocan ``[start, end)``. Un hold vencido no bloquea."""
    now = now or utcnow()
    stmt = select(Appointment).where(
        Appointment.tenant_id == tenant_id,
        Appointment.resource_key == resource_key,
        Appointment.status.in_(ACTIVE_STATUSES),
        Appointment.starts_at < end,
        Appointment.ends_at > start,
    )
    if exclude_id is not None:
        stmt = stmt.where(Appointment.id != exclude_id)
    if for_update:
        stmt = stmt.with_for_update()
    rows = (await session.execute(stmt)).scalars().all()
    return [
        a
        for a in rows
        if not (
            a.status == "pending"
            and a.hold_expires_at is not None
            and ensure_utc(a.hold_expires_at) <= now
        )
    ]


async def compute_slots(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    service_id: uuid.UUID,
    date_from: date,
    date_to: date,
    *,
    provider: CalendarProvider | None = None,
    rules: BookingRules | None = None,
    now: datetime | None = None,
    max_slots: int = 12,
    resource_key: str = "default",
    exclude_id: uuid.UUID | None = None,
) -> list[tuple[datetime, datetime]]:
    """Horarios libres (UTC) para ``service_id`` entre ``date_from`` y ``date_to`` (inclusive).

    Considera horario laboral, ausencias, festivos CO, citas activas, ocupado externo del
    proveedor, buffer, duracion, antelacion minima y maximo de dias. Devuelve como maximo
    ``max_slots`` repartidos entre dias y franjas, ordenados por hora.
    """
    now = now or utcnow()
    if date_to < date_from:
        return []
    date_to = min(date_to, date_from + timedelta(days=MAX_RANGE_DAYS - 1))

    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    service = (
        await session.execute(
            select(Service).where(
                Service.id == service_id,
                Service.tenant_id == tenant_id,
                Service.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if tenant is None or service is None:
        return []

    rules = rules or await load_rules(session, tenant_id)
    tz = zone_for(tenant.timezone)
    duration = timedelta(minutes=max(int(service.duration_min), 5))
    buffer = timedelta(minutes=rules.buffer_min)
    step = timedelta(minutes=rules.slot_minutes)
    earliest = now + timedelta(hours=rules.min_notice_hours)
    today = now.astimezone(tz).date()
    last_day = min(date_to, today + timedelta(days=rules.max_days_ahead))
    first_day = max(date_from, today)
    if last_day < first_day:
        return []

    hours = (
        (
            await session.execute(
                select(WorkingHours).where(
                    WorkingHours.tenant_id == tenant_id, WorkingHours.resource_id.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    by_weekday: dict[int, list[tuple[time, time]]] = {}
    for h in hours:
        by_weekday.setdefault(int(h.weekday), []).append((h.start_time, h.end_time))
    if not by_weekday:
        by_weekday = await _bot_hours(session, tenant_id)

    range_start = datetime.combine(first_day, time.min, tz).astimezone(UTC)
    range_end = datetime.combine(last_day + timedelta(days=1), time.min, tz).astimezone(UTC)

    busy: list[Interval] = []
    for off in (
        (
            await session.execute(
                select(TimeOff).where(
                    TimeOff.tenant_id == tenant_id,
                    TimeOff.resource_id.is_(None),
                    TimeOff.starts_at < range_end,
                    TimeOff.ends_at > range_start,
                )
            )
        )
        .scalars()
        .all()
    ):
        busy.append((ensure_utc(off.starts_at), ensure_utc(off.ends_at)))
    for appt in await active_appointments(
        session,
        tenant_id,
        range_start - buffer,
        range_end + buffer,
        resource_key=resource_key,
        now=now,
        exclude_id=exclude_id,
    ):
        busy.append((ensure_utc(appt.starts_at), ensure_utc(appt.ends_at)))
    if provider is not None:
        try:
            external = await provider.busy_intervals(session, tenant_id, range_start, range_end)
        except Exception as exc:  # noqa: BLE001 - si el calendario externo cae, se sigue con la DB
            log.warning(
                "booking.external_busy_failed",
                tenant_id=str(tenant_id),
                error=type(exc).__name__,
            )
            external = []
        busy.extend((ensure_utc(s), ensure_utc(e)) for s, e in external)

    # El buffer separa citas: se amplia cada ocupado y se exige que el slot quepa en el hueco.
    blocked = merge_intervals((s - buffer, e + buffer) for s, e in busy)

    per_day: list[list[tuple[datetime, datetime]]] = []
    day = first_day
    while day <= last_day:
        windows = by_weekday.get(day.weekday(), [])
        if windows and not (rules.closed_on_holidays and is_holiday(day)):
            base = [
                (
                    datetime.combine(day, w_start, tz).astimezone(UTC),
                    datetime.combine(day, w_end, tz).astimezone(UTC),
                )
                for w_start, w_end in windows
                if w_end > w_start
            ]
            day_slots: list[tuple[datetime, datetime]] = []
            for free_start, free_end in subtract_intervals(base, blocked):
                cursor = free_start
                # Alinear al paso desde el inicio del hueco laboral (no del hueco recortado).
                cursor = _align(cursor, base, step)
                while cursor + duration <= free_end:
                    if cursor >= earliest:
                        day_slots.append((cursor, cursor + duration))
                    cursor += step
            if day_slots:
                per_day.append(sorted(set(day_slots)))
        day += timedelta(days=1)

    return _spread(per_day, max_slots)


def _align(cursor: datetime, base: Sequence[Interval], step: timedelta) -> datetime:
    """Redondea ``cursor`` hacia arriba a la grilla que arranca en el inicio de su ventana."""
    for w_start, w_end in base:
        if w_start <= cursor < w_end:
            delta = cursor - w_start
            steps = -(-delta // step)  # techo
            return w_start + steps * step
    return cursor


def _bisect_order(items: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Ordena para repartir: primero, ultimo, medio... (manana y tarde)."""
    if len(items) <= 2:
        return list(items)
    out: list[tuple[datetime, datetime]] = []
    queue: list[tuple[int, int]] = [(0, len(items) - 1)]
    seen: set[int] = set()
    while queue:
        lo, hi = queue.pop(0)
        for idx in (lo, hi):
            if idx not in seen:
                seen.add(idx)
                out.append(items[idx])
        if hi - lo > 1:
            mid = (lo + hi) // 2
            if mid not in seen:
                seen.add(mid)
                out.append(items[mid])
            queue.append((lo + 1, mid))
            queue.append((mid, hi - 1))
    return out


def _spread(
    per_day: list[list[tuple[datetime, datetime]]], limit: int
) -> list[tuple[datetime, datetime]]:
    if limit <= 0:
        return []
    total = sum(len(d) for d in per_day)
    if total <= limit:
        return sorted(s for d in per_day for s in d)
    queues = [_bisect_order(d) for d in per_day]
    chosen: list[tuple[datetime, datetime]] = []
    depth = 0
    while len(chosen) < limit and any(depth < len(q) for q in queues):
        for q in queues:
            if depth < len(q) and len(chosen) < limit:
                chosen.append(q[depth])
        depth += 1
    return sorted(chosen)
