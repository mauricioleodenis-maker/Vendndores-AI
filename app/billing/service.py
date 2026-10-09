"""Registros de cobro: alta, generacion mensual idempotente, pago, mora, resumen y CSV."""

from __future__ import annotations

import calendar
import csv
import io
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.core.clock import BOGOTA, to_bogota, utcnow
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models.plans import (
    BILLING_KINDS,
    PAYMENT_METHODS,
    BillingRecord,
    Plan,
    Subscription,
)
from app.db.models.tenants import Tenant
from app.db.models.users import User
from app.leads.export import neutralize_cell

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
PAST_DUE_DAYS = 15
LIVE_STATUSES = ("active", "past_due")
NEGATIVE_KINDS = ("reembolso", "descuento")
MAX_AMOUNT_COP = 1_000_000_000
EXPORT_MAX_ROWS = 50_000
PAGE_SIZE = 50

CSV_COLUMNS: tuple[tuple[str, str], ...] = (
    ("tenant", "Empresa"),
    ("kind", "Tipo"),
    ("period", "Periodo"),
    ("amount_cop", "Monto COP"),
    ("iva_cop", "IVA COP"),
    ("status", "Estado"),
    ("due_date", "Vence"),
    ("paid_at", "Pagado"),
    ("method", "Metodo"),
    ("reference", "Referencia"),
    ("external_invoice_no", "Factura"),
)


@dataclass(frozen=True, slots=True)
class BillingRow:
    record: BillingRecord
    tenant_name: str


@dataclass(frozen=True, slots=True)
class Summary:
    mrr_cop: int
    setups_month_cop: int
    overdue_cop: int
    overdue_count: int
    pending_cop: int
    active_subscriptions: int


def valid_period(period: str) -> str:
    if not PERIOD_RE.match(period):
        raise AppError("invalid_period", "Periodo invalido (use AAAA-MM)", 422)
    return period


def current_period(now: datetime | None = None) -> str:
    local = to_bogota(now or utcnow())
    return f"{local.year:04d}-{local.month:02d}"


def month_bounds(period: str) -> tuple[datetime, datetime]:
    """Inicio (incluido) y fin (excluido) del mes en hora de Bogota, expresados en UTC."""
    year, month = (int(p) for p in valid_period(period).split("-"))
    start = datetime(year, month, 1, tzinfo=BOGOTA)
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return start.astimezone(UTC), datetime(ny, nm, 1, tzinfo=BOGOTA).astimezone(UTC)


def effective_monthly_fee(sub: Subscription, plan: Plan) -> int:
    return (
        sub.custom_monthly_fee_cop
        if sub.custom_monthly_fee_cop is not None
        else plan.monthly_fee_cop
    )


def _due_date_for(sub: Subscription, period: str) -> date:
    year, month = (int(p) for p in period.split("-"))
    anchor = to_bogota(sub.started_at).day if sub.started_at else 1
    return date(year, month, min(anchor, calendar.monthrange(year, month)[1]))


async def create_record(
    session: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    kind: str,
    amount_cop: int,
    actor: User | None = None,
    period: str | None = None,
    iva_cop: int = 0,
    due_date: date | None = None,
    notes: str = "",
    reference: str = "",
) -> BillingRecord:
    if kind not in BILLING_KINDS:
        raise AppError("invalid_kind", "Tipo de cobro invalido", 422)
    if abs(amount_cop) > MAX_AMOUNT_COP or abs(iva_cop) > MAX_AMOUNT_COP:
        raise AppError("invalid_amount", "Monto fuera de rango", 422)
    if kind in NEGATIVE_KINDS:
        amount_cop, iva_cop = -abs(amount_cop), -abs(iva_cop)
    elif amount_cop < 0:
        raise AppError("invalid_amount", "El monto no puede ser negativo para este tipo", 422)
    if period is not None:
        valid_period(period)
    sub = await session.get(Subscription, subscription_id)
    if sub is None:
        raise NotFoundError("Suscripción no encontrada")
    record = BillingRecord(
        tenant_id=sub.tenant_id,
        subscription_id=sub.id,
        kind=kind,
        period=period,
        amount_cop=amount_cop,
        iva_cop=iva_cop,
        status="pendiente",
        due_date=due_date or to_bogota(utcnow()).date(),
        notes=notes[:1000],
        reference=reference[:120],
        created_by=actor.id if actor else None,
    )
    session.add(record)
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="billing.create",
        entity_type="billing_record",
        entity_id=record.id,
        tenant_id=sub.tenant_id,
        diff={"kind": kind, "period": period},
    )
    return record


async def generate_monthly_records(
    session: AsyncSession, period: str | None = None, *, now: datetime | None = None
) -> int:
    """Crea la mensualidad del periodo por suscripcion viva (idempotente); devuelve cuantas creo."""
    period = valid_period(period or current_period(now))
    _, end = month_bounds(period)
    rows = (
        await session.execute(
            select(Subscription, Plan)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(Subscription.status.in_(LIVE_STATUSES))
        )
    ).all()
    existing = set(
        (
            await session.execute(
                select(BillingRecord.subscription_id).where(
                    BillingRecord.kind == "mensualidad", BillingRecord.period == period
                )
            )
        )
        .scalars()
        .all()
    )
    created = 0
    for sub, plan in rows:
        if sub.id in existing:
            continue
        if sub.started_at is not None and _aware(sub.started_at) >= end:
            continue
        fee = effective_monthly_fee(sub, plan)
        if fee <= 0:
            continue
        try:
            # Savepoint: una carrera con otra generacion (cron/manual) viola el unico
            # parcial ``uq_billing_monthly``; se ignora esa fila en vez de fallar todo.
            async with session.begin_nested():
                session.add(
                    BillingRecord(
                        tenant_id=sub.tenant_id,
                        subscription_id=sub.id,
                        kind="mensualidad",
                        period=period,
                        amount_cop=fee,
                        status="pendiente",
                        due_date=_due_date_for(sub, period),
                    )
                )
        except IntegrityError:
            continue
        created += 1
    return created


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


async def mark_overdue(session: AsyncSession, *, today: date | None = None) -> int:
    """Pendientes con fecha vencida -> ``vencido``; mora > 15 dias -> suscripcion ``past_due``."""
    today = today or to_bogota(utcnow()).date()
    result = await session.execute(
        update(BillingRecord)
        .where(
            BillingRecord.status == "pendiente",
            BillingRecord.due_date.is_not(None),
            BillingRecord.due_date < today,
            BillingRecord.amount_cop > 0,
        )
        .values(status="vencido")
        .execution_options(synchronize_session="fetch")
    )
    marked = int(result.rowcount or 0)
    late = (
        (
            await session.execute(
                select(BillingRecord.subscription_id)
                .where(BillingRecord.status == "vencido", BillingRecord.due_date.is_not(None))
                .where(
                    BillingRecord.due_date < today.fromordinal(today.toordinal() - PAST_DUE_DAYS)
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    if late:
        await session.execute(
            update(Subscription)
            .where(Subscription.id.in_(list(late)), Subscription.status == "active")
            .values(status="past_due")
        )
    await session.flush()
    return marked


async def _recover_if_clear(session: AsyncSession, subscription_id: uuid.UUID) -> None:
    """Si ya no queda mora en la suscripcion, vuelve a ``active``."""
    sub = await session.get(Subscription, subscription_id)
    if sub is None or sub.status != "past_due":
        return
    left = (
        await session.execute(
            select(func.count())
            .select_from(BillingRecord)
            .where(BillingRecord.subscription_id == sub.id, BillingRecord.status == "vencido")
        )
    ).scalar_one()
    if left == 0:
        sub.status = "active"


async def mark_paid(
    session: AsyncSession,
    record_id: uuid.UUID,
    *,
    actor: User | None,
    method: str | None = None,
    reference: str = "",
    external_invoice_no: str | None = None,
) -> BillingRecord:
    record = await session.get(BillingRecord, record_id)
    if record is None:
        raise NotFoundError("Cobro no encontrado")
    if record.status == "pagado":
        return record  # idempotente
    if record.status == "anulado":
        raise ConflictError("El cobro está anulado")
    if method is not None and method not in PAYMENT_METHODS:
        raise AppError("invalid_method", "Método de pago inválido", 422)
    record.status = "pagado"
    record.paid_at = utcnow()
    record.method = method
    record.reference = reference[:120]
    record.external_invoice_no = (external_invoice_no or None) and external_invoice_no[:60]
    await session.flush()
    await _recover_if_clear(session, record.subscription_id)
    await log_event(
        session,
        actor=actor,
        action="billing.mark_paid",
        entity_type="billing_record",
        entity_id=record.id,
        tenant_id=record.tenant_id,
        diff={"method": method},
    )
    return record


async def void_record(
    session: AsyncSession, record_id: uuid.UUID, *, actor: User | None
) -> BillingRecord:
    record = await session.get(BillingRecord, record_id)
    if record is None:
        raise NotFoundError("Cobro no encontrado")
    if record.status == "pagado":
        raise ConflictError("No se puede anular un cobro pagado")
    was_overdue = record.status == "vencido"
    record.status = "anulado"
    await session.flush()
    if was_overdue:
        await _recover_if_clear(session, record.subscription_id)
    await log_event(
        session,
        actor=actor,
        action="billing.void",
        entity_type="billing_record",
        entity_id=record.id,
        tenant_id=record.tenant_id,
    )
    return record


async def list_records(
    session: AsyncSession,
    *,
    status: str | None = None,
    tenant_id: uuid.UUID | None = None,
    period: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[BillingRow]:
    stmt = (
        select(BillingRecord, Tenant.name)
        .join(Tenant, Tenant.id == BillingRecord.tenant_id)
        .order_by(BillingRecord.due_date.desc(), BillingRecord.created_at.desc())
        .limit(max(1, min(limit, EXPORT_MAX_ROWS)))
        .offset(max(0, offset))
    )
    if status:
        stmt = stmt.where(BillingRecord.status == status)
    if tenant_id:
        stmt = stmt.where(BillingRecord.tenant_id == tenant_id)
    if period:
        stmt = stmt.where(BillingRecord.period == valid_period(period))
    return [BillingRow(r, n) for r, n in (await session.execute(stmt)).all()]


async def count_records(
    session: AsyncSession, *, status: str | None = None, tenant_id: uuid.UUID | None = None
) -> int:
    stmt = select(func.count()).select_from(BillingRecord)
    if status:
        stmt = stmt.where(BillingRecord.status == status)
    if tenant_id:
        stmt = stmt.where(BillingRecord.tenant_id == tenant_id)
    return int((await session.execute(stmt)).scalar_one())


async def summary(session: AsyncSession, *, now: datetime | None = None) -> Summary:
    now = now or utcnow()
    start, end = month_bounds(current_period(now))
    mrr, active = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(
                        func.coalesce(Subscription.custom_monthly_fee_cop, Plan.monthly_fee_cop)
                    ),
                    0,
                ),
                func.count(),
            )
            .select_from(Subscription)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(Subscription.status.in_(LIVE_STATUSES))
        )
    ).one()
    setups = (
        await session.execute(
            select(func.coalesce(func.sum(BillingRecord.amount_cop), 0)).where(
                BillingRecord.kind == "setup",
                BillingRecord.status == "pagado",
                BillingRecord.paid_at >= start,
                BillingRecord.paid_at < end,
            )
        )
    ).scalar_one()
    overdue = (
        await session.execute(
            select(
                func.coalesce(func.sum(BillingRecord.amount_cop + BillingRecord.iva_cop), 0),
                func.count(),
            ).where(BillingRecord.status == "vencido")
        )
    ).one()
    pending = (
        await session.execute(
            select(
                func.coalesce(func.sum(BillingRecord.amount_cop + BillingRecord.iva_cop), 0)
            ).where(BillingRecord.status == "pendiente", BillingRecord.amount_cop > 0)
        )
    ).scalar_one()
    return Summary(
        int(mrr), int(setups), int(overdue[0]), int(overdue[1]), int(pending), int(active)
    )


def export_csv(rows: Sequence[BillingRow]) -> str:
    """CSV (BOM UTF-8) con neutralizacion de formulas en todas las celdas de texto."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([label for _, label in CSV_COLUMNS])
    for row in rows:
        rec = row.record
        values: dict[str, Any] = {
            "tenant": row.tenant_name,
            "kind": rec.kind,
            "period": rec.period,
            "amount_cop": rec.amount_cop,
            "iva_cop": rec.iva_cop,
            "status": rec.status,
            "due_date": rec.due_date.isoformat() if rec.due_date else "",
            "paid_at": rec.paid_at.isoformat() if rec.paid_at else "",
            "method": rec.method,
            "reference": rec.reference,
            "external_invoice_no": rec.external_invoice_no,
        }
        writer.writerow([neutralize_cell(values[key]) for key, _ in CSV_COLUMNS])
    return "﻿" + buf.getvalue()
