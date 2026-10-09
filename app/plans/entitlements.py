"""Entitlements por plan, uso mensual y limites (dueno: B2, firmas fijas MAESTRO §5)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import to_bogota, utcnow
from app.core.errors import AppError
from app.db.models.booking import CalendarConnection
from app.db.models.catalog import Service
from app.db.models.plans import Plan, Subscription, UsageCounter
from app.plans.catalog import default_limits

SOFT_LIMIT_PCT = 80

# metrica -> clave de limite en ``plans.limits``
_LIMIT_KEYS: dict[str, str] = {
    "conversations": "max_conversations_month",
    "services": "max_services",
    "calendars": "max_calendars",
}
_FEATURE_KEYS: dict[str, str] = {
    "followups": "followups_enabled",
    "voice": "voice_enabled",
    "export_csv": "export_csv",
    "sandbox": "sandbox_enabled",
}


class PlanLimitExceeded(AppError):
    def __init__(self, metric: str = "") -> None:
        super().__init__("plan_limit_exceeded", f"Limite del plan alcanzado: {metric}", 402)
        self.metric = metric


@dataclass(slots=True)
class Entitlements:
    plan_code: str = "basico"
    limits: dict[str, Any] = field(default_factory=dict)
    status: str = "trial"  # trial|active|past_due|cancelled|none
    active: bool = True  # False solo si la ultima suscripcion esta cancelada
    has_subscription: bool = False
    period: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    def limit(self, metric: str) -> int | None:
        """Tope de la metrica; ``None`` = ilimitado o sin tope definido."""
        value = self.limits.get(_LIMIT_KEYS.get(metric, metric))
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def has_feature(self, feature: str) -> bool:
        return bool(self.limits.get(_FEATURE_KEYS.get(feature, feature), False))

    def usage_pct(self, metric: str) -> int | None:
        cap = self.limit(metric)
        if cap is None or cap <= 0:
            return None
        return min(100 * self.usage.get(metric, 0) // cap, 999)

    def near_limit(self, metric: str) -> bool:
        pct = self.usage_pct(metric)
        return pct is not None and pct >= SOFT_LIMIT_PCT


def current_period() -> str:
    """Mes de facturacion ``YYYY-MM`` en hora de Bogota."""
    return to_bogota(utcnow()).strftime("%Y-%m")


async def get_counter(
    session: AsyncSession, tenant_id: uuid.UUID, period: str
) -> UsageCounter | None:
    return (
        await session.execute(
            select(UsageCounter).where(
                UsageCounter.tenant_id == tenant_id, UsageCounter.period == period
            )
        )
    ).scalar_one_or_none()


async def _latest_subscription(session: AsyncSession, tenant_id: uuid.UUID) -> Subscription | None:
    live = (
        await session.execute(
            select(Subscription)
            .where(Subscription.tenant_id == tenant_id, Subscription.status != "cancelled")
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if live is not None:
        return live
    return (
        await session.execute(
            select(Subscription)
            .where(Subscription.tenant_id == tenant_id)
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_entitlements(session: AsyncSession, tenant_id: uuid.UUID) -> Entitlements:
    """Plan efectivo del tenant. Sin suscripcion rige el plan Basico (prueba)."""
    period = current_period()
    sub = await _latest_subscription(session, tenant_id)
    plan_code, limits, status, active = "basico", default_limits("basico"), "none", True
    if sub is not None:
        plan = await session.get(Plan, sub.plan_id)
        if plan is not None:
            plan_code = plan.code
            limits = {**default_limits(plan.code), **(plan.limits or {})}
        status = sub.status
        active = sub.status != "cancelled"
    counter = await get_counter(session, tenant_id, period)
    usage = {
        "conversations": counter.conversations if counter else 0,
        "messages_in": counter.messages_in if counter else 0,
        "messages_out": counter.messages_out if counter else 0,
        "tokens_in": counter.llm_tokens_in if counter else 0,
        "tokens_out": counter.llm_tokens_out if counter else 0,
    }
    return Entitlements(
        plan_code=plan_code,
        limits=limits,
        status=status,
        active=active,
        has_subscription=sub is not None,
        period=period,
        usage=usage,
    )


async def _count(session: AsyncSession, metric: str, tenant_id: uuid.UUID) -> int:
    if metric == "services":
        stmt = (
            select(func.count())
            .select_from(Service)
            .where(Service.tenant_id == tenant_id, Service.is_active.is_(True))
        )
    else:  # calendars
        stmt = (
            select(func.count())
            .select_from(CalendarConnection)
            .where(
                CalendarConnection.tenant_id == tenant_id, CalendarConnection.status != "revoked"
            )
        )
    return int((await session.execute(stmt)).scalar_one())


async def assert_within_limit(session: AsyncSession, tenant_id: uuid.UUID, metric: str) -> None:
    """Falla con ``PlanLimitExceeded`` si NO cabe una unidad mas de ``metric``.

    Metricas: ``conversations`` (mes en curso), ``services``, ``calendars``.
    """
    if metric not in _LIMIT_KEYS:
        raise ValueError(f"Metrica desconocida: {metric}")
    ent = await get_entitlements(session, tenant_id)
    if not ent.active:
        raise PlanLimitExceeded("suscripcion_cancelada")
    cap = ent.limit(metric)
    if cap is None:
        return
    used = (
        ent.usage["conversations"]
        if metric == "conversations"
        else await _count(session, metric, tenant_id)
    )
    if used >= cap:
        raise PlanLimitExceeded(metric)


async def record_usage(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    conversations: int = 0,
    messages_in: int = 0,
    messages_out: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> None:
    """Suma al contador del mes (upsert sin carreras: UPDATE, si no existe INSERT)."""
    deltas = {
        "conversations": conversations,
        "messages_in": messages_in,
        "messages_out": messages_out,
        "llm_tokens_in": tokens_in,
        "llm_tokens_out": tokens_out,
    }
    if any(v < 0 for v in deltas.values()):
        raise ValueError("El uso no puede ser negativo")
    if not any(deltas.values()):
        return
    period = current_period()

    async def _bump() -> int:
        result = await session.execute(
            update(UsageCounter)
            .where(UsageCounter.tenant_id == tenant_id, UsageCounter.period == period)
            .values(
                {getattr(UsageCounter, k): getattr(UsageCounter, k) + v for k, v in deltas.items()}
            )
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    if await _bump():
        return
    try:
        async with session.begin_nested():
            session.add(
                UsageCounter(
                    tenant_id=tenant_id,
                    period=period,
                    conversations=conversations,
                    messages_in=messages_in,
                    messages_out=messages_out,
                    llm_tokens_in=tokens_in,
                    llm_tokens_out=tokens_out,
                )
            )
    except IntegrityError:  # otro proceso lo creo primero
        await _bump()
