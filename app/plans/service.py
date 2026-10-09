"""Servicio de planes y suscripciones (transaccion de la sesion del llamador)."""

from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.core.clock import utcnow
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models.plans import BillingRecord, Offer, Plan, Subscription
from app.db.models.tenants import Tenant
from app.db.models.users import User
from app.plans import catalog
from app.plans.offers import get_offer, is_applicable, redeem_offer, release_offer
from app.plans.quote import Quote, pct_of, quote


@dataclass(slots=True)
class SubscriptionOutcome:
    subscription: Subscription
    plan: Plan
    offer: Offer | None
    quote: Quote


def add_months(moment: datetime, months: int) -> datetime:
    """Suma meses calendario recortando el dia al fin de mes (31 ene -> 28/29 feb)."""
    index = moment.month - 1 + months
    year, month = moment.year + index // 12, index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


async def ensure_catalog(session: AsyncSession) -> None:
    """Siembra el catalogo si la tabla esta vacia (primer arranque)."""
    first = (await session.execute(select(Plan.id).limit(1))).first()
    if first is None:
        await catalog.seed(session)


async def list_public_plans(session: AsyncSession) -> list[Plan]:
    await ensure_catalog(session)
    stmt = (
        select(Plan)
        .where(Plan.is_public.is_(True), Plan.is_active.is_(True))
        .order_by(Plan.sort, Plan.code)
    )
    return list((await session.execute(stmt)).scalars())


async def get_plan(session: AsyncSession, code: str) -> Plan:
    stmt = select(Plan).where(Plan.code == code, Plan.is_active.is_(True))
    plan = (await session.execute(stmt)).scalar_one_or_none()
    if plan is None:  # solo si falta se comprueba la siembra (evita una consulta por llamada)
        await ensure_catalog(session)
        plan = (await session.execute(stmt)).scalar_one_or_none()
    if plan is None:
        raise NotFoundError("Plan no encontrado")
    return plan


async def quote_for(
    session: AsyncSession,
    plan_code: str,
    offer_code: str | None = None,
    *,
    include_iva: bool = False,
    custom_monthly_fee_cop: int | None = None,
) -> Quote:
    """Cotiza validando que la oferta aplique (sin consumir cupo)."""
    plan = await get_plan(session, plan_code)
    offer: Offer | None = None
    if offer_code:
        offer = await get_offer(session, offer_code)
        reason = is_applicable(offer, plan.code)
        if reason is not None:
            raise AppError("offer_unavailable", reason, 409)
    return quote(plan, offer, include_iva, custom_monthly_fee_cop=custom_monthly_fee_cop)


async def _require_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    # FOR UPDATE serializa altas concurrentes de suscripcion del mismo tenant (no-op en SQLite)
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    ).scalar_one_or_none()
    if tenant is None or tenant.deleted_at is not None:
        raise NotFoundError("Empresa no encontrada")
    return tenant


async def get_live_subscription(session: AsyncSession, tenant_id: uuid.UUID) -> Subscription | None:
    """Suscripcion vigente (no cancelada) mas reciente."""
    return (
        await session.execute(
            select(Subscription)
            .where(Subscription.tenant_id == tenant_id, Subscription.status != "cancelled")
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_subscription(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    plan_code: str,
    *,
    offer_code: str | None = None,
    custom_monthly_fee_cop: int | None = None,
    include_iva: bool = False,
    actor: User | None = None,
) -> SubscriptionOutcome:
    """Crea la suscripcion, reclama el cupo de la oferta y deja el cobro de setup pendiente."""
    await _require_tenant(session, tenant_id)
    if await get_live_subscription(session, tenant_id) is not None:
        raise ConflictError("La empresa ya tiene una suscripción vigente")
    plan = await get_plan(session, plan_code)
    offer: Offer | None = None
    if offer_code:
        offer = await redeem_offer(session, offer_code, plan.code)  # lanza 409 sin cupo
    q = quote(plan, offer, include_iva, custom_monthly_fee_cop=custom_monthly_fee_cop)

    now = utcnow()
    sub = Subscription(
        tenant_id=tenant_id,
        plan_id=plan.id,
        offer_id=offer.id if offer else None,
        status="active",
        started_at=now,
        current_period_end=add_months(now, 1),
        custom_monthly_fee_cop=custom_monthly_fee_cop,
        guarantee_until=(now + timedelta(days=catalog.GUARANTEE_DAYS)).astimezone(UTC).date(),
        guarantee_status="vigente",
        notes=catalog.FOUNDER_CODE if offer and offer.code == catalog.FOUNDER_CODE else "",
    )
    session.add(sub)
    await session.flush()
    if q.setup_net_cop > 0:
        session.add(
            BillingRecord(
                tenant_id=tenant_id,
                subscription_id=sub.id,
                kind="setup",
                amount_cop=q.setup_net_cop,
                iva_cop=pct_of(q.setup_net_cop, catalog.IVA_PCT) if include_iva else 0,
                status="pendiente",
                due_date=now.date(),
                created_by=actor.id if actor else None,
            )
        )
    await log_event(
        session,
        actor=actor,
        action="subscription.create",
        entity_type="subscription",
        entity_id=sub.id,
        tenant_id=tenant_id,
        diff={"plan": plan.code, "offer": offer.code if offer else None},
    )
    await session.flush()
    return SubscriptionOutcome(sub, plan, offer, q)


async def change_plan(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    plan_code: str | None = None,
    *,
    custom_monthly_fee_cop: int | None = None,
    actor: User | None = None,
) -> Subscription:
    """Upgrade/downgrade (el prorrateo es manual). Solo cambia el plan de la suscripcion."""
    sub = await get_live_subscription(session, tenant_id)
    if sub is None:
        raise NotFoundError("La empresa no tiene suscripción vigente")
    before = {"plan_id": str(sub.plan_id), "custom_fee": sub.custom_monthly_fee_cop}
    if plan_code is not None:
        plan = await get_plan(session, plan_code)
        if plan.id == sub.plan_id and custom_monthly_fee_cop is None:
            raise ConflictError("La empresa ya está en ese plan")
        sub.plan_id = plan.id
    if custom_monthly_fee_cop is not None:
        sub.custom_monthly_fee_cop = custom_monthly_fee_cop
    await log_event(
        session,
        actor=actor,
        action="subscription.change",
        entity_type="subscription",
        entity_id=sub.id,
        tenant_id=tenant_id,
        diff={"before": before, "plan": plan_code, "custom_fee": custom_monthly_fee_cop},
    )
    await session.flush()
    return sub


async def set_status(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    status: str,
    *,
    actor: User | None = None,
) -> Subscription:
    """Cambia el estado; cancelar dentro de la garantia la marca reclamada."""
    if status not in ("active", "past_due", "cancelled"):
        raise AppError("invalid_status", "Estado de suscripción inválido", 422)
    sub = await get_live_subscription(session, tenant_id)
    if sub is None:
        raise NotFoundError("La empresa no tiene suscripción vigente")
    sub.status = status
    if status == "cancelled" and sub.guarantee_status == "vigente":
        today: date = utcnow().date()
        in_window = sub.guarantee_until is not None and today <= sub.guarantee_until
        sub.guarantee_status = "reclamada" if in_window else "vencida"
        if in_window and sub.offer_id is not None:
            offer = await session.get(Offer, sub.offer_id)
            if offer is not None:
                await release_offer(session, offer.code)
    await log_event(
        session,
        actor=actor,
        action=f"subscription.{status}",
        entity_type="subscription",
        entity_id=sub.id,
        tenant_id=tenant_id,
    )
    await session.flush()
    return sub
