"""Catalogo comercial: planes, oferta Fundador y garantia (MAESTRO §7 + support/05).

MAESTRO gana sobre support/05 cuando difieren (mensualidad Pro 390.000, topes de
conversaciones 500/1.500/4.000, calendarios 1/3/10, voz en Premium).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.plans import Offer, Plan

IVA_PCT = 19
GUARANTEE_DAYS = 30
FOUNDER_CODE = "fundador"
CURRENCY = "COP"


@dataclass(frozen=True, slots=True)
class PlanSpec:
    code: str
    name: str
    description: str
    setup_fee_cop: int
    monthly_fee_cop: int
    limits: dict[str, Any]
    features: tuple[str, ...]
    sort: int
    is_public: bool = True


@dataclass(frozen=True, slots=True)
class OfferSpec:
    code: str
    name: str
    discount_type: str
    value: int
    max_redemptions: int | None
    applies_to_plan_codes: tuple[str, ...]
    conditions: str
    extra: dict[str, Any] = field(default_factory=dict)


def _limits(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "max_conversations_month": 500,
        "max_calendars": 1,
        "max_resources": 1,
        "max_services": 10,
        "max_panel_users": 2,
        "max_whatsapp_templates": 3,
        "max_bot_regenerations_month": 5,
        "reminders": ["24h"],
        "followups_enabled": False,
        "followup_sequences": 0,
        "voice_enabled": False,
        "voice_minutes_month": 0,
        "sandbox_enabled": True,
        "export_csv": False,
        "support": {"channel": "email", "sla_business_days": 2},
    }
    base.update(overrides)
    return base


PLAN_CATALOG: tuple[PlanSpec, ...] = (
    PlanSpec(
        code="basico",
        name="Básico",
        description="Consultorio o taller de una sede con una persona en recepción.",
        setup_fee_cop=800_000,
        monthly_fee_cop=250_000,
        limits=_limits(),
        features=(
            "Recepcionista IA en WhatsApp 24/7",
            "Hasta 500 conversaciones al mes",
            "1 calendario conectado",
            "Recordatorio de cita 24 h antes",
            "Soporte por correo (2 días hábiles)",
        ),
        sort=1,
    ),
    PlanSpec(
        code="pro",
        name="Pro",
        description="Clínica o taller con varios servicios y agenda compartida.",
        setup_fee_cop=1_200_000,
        monthly_fee_cop=390_000,
        limits=_limits(
            max_conversations_month=1500,
            max_calendars=3,
            max_resources=3,
            max_services=30,
            max_panel_users=5,
            max_whatsapp_templates=10,
            reminders=["24h", "2h"],
            followups_enabled=True,
            followup_sequences=1,
            support={"channel": "whatsapp", "sla_business_days": 1},
        ),
        features=(
            "Todo lo del plan Básico",
            "Hasta 1.500 conversaciones al mes",
            "Hasta 3 calendarios",
            "Recordatorios 24 h y 2 h antes",
            "Seguimiento de inasistencias y reactivación",
            "Soporte por WhatsApp (1 día hábil)",
        ),
        sort=2,
    ),
    PlanSpec(
        code="premium",
        name="Premium",
        description="Restaurante con reservas o clínica multi-sede con más volumen.",
        setup_fee_cop=2_000_000,
        monthly_fee_cop=600_000,
        limits=_limits(
            max_conversations_month=4000,
            max_calendars=10,
            max_resources=10,
            max_services=None,
            max_panel_users=15,
            max_whatsapp_templates=25,
            reminders=["24h", "2h", "custom"],
            followups_enabled=True,
            followup_sequences=3,
            voice_enabled=True,
            export_csv=True,
            support={"channel": "whatsapp", "sla_business_days": 0},
        ),
        features=(
            "Todo lo del plan Pro",
            "Hasta 4.000 conversaciones al mes",
            "Hasta 10 calendarios",
            "Servicios ilimitados y varias secuencias de seguimiento",
            "Canal de voz (cuando esté disponible)",
            "Exportación CSV y soporte prioritario",
        ),
        sort=3,
    ),
)

OFFER_CATALOG: tuple[OfferSpec, ...] = (
    OfferSpec(
        code=FOUNDER_CODE,
        name="Oferta Fundador",
        discount_type="setup_pct",
        value=100,
        max_redemptions=5,
        applies_to_plan_codes=("basico", "pro", "premium"),
        conditions=(
            "Primeros 5 clientes: instalación sin costo a cambio de un testimonio en video "
            "y autorización de caso de estudio. No acumulable con otros descuentos."
        ),
    ),
)

PLAN_BY_CODE: dict[str, PlanSpec] = {p.code: p for p in PLAN_CATALOG}


def default_limits(plan_code: str) -> dict[str, Any]:
    """Limites del catalogo para ``plan_code`` (basico si no existe)."""
    spec = PLAN_BY_CODE.get(plan_code, PLAN_BY_CODE["basico"])
    return dict(spec.limits)


async def seed(session: AsyncSession) -> dict[str, int]:
    """Upsert idempotente por ``code`` (hook de ``app.cli seed``). Conserva ``redeemed``."""
    created = {"plans": 0, "offers": 0}
    for spec in PLAN_CATALOG:
        plan = (
            await session.execute(select(Plan).where(Plan.code == spec.code))
        ).scalar_one_or_none()
        values: dict[str, Any] = {
            "name": spec.name,
            "description": spec.description,
            "setup_fee_cop": spec.setup_fee_cop,
            "monthly_fee_cop": spec.monthly_fee_cop,
            "limits": dict(spec.limits),
            "features": list(spec.features),
            "sort": spec.sort,
            "is_public": spec.is_public,
        }
        if plan is None:
            session.add(Plan(code=spec.code, **values))
            created["plans"] += 1
        else:
            for key, value in values.items():
                setattr(plan, key, value)
    for ospec in OFFER_CATALOG:
        offer = (
            await session.execute(select(Offer).where(Offer.code == ospec.code))
        ).scalar_one_or_none()
        ovalues: dict[str, Any] = {
            "name": ospec.name,
            "discount_type": ospec.discount_type,
            "value": ospec.value,
            "max_redemptions": ospec.max_redemptions,
            "applies_to_plan_codes": list(ospec.applies_to_plan_codes),
            "conditions": ospec.conditions,
        }
        if offer is None:
            session.add(Offer(code=ospec.code, **ovalues))
            created["offers"] += 1
        else:
            for key, value in ovalues.items():
                setattr(offer, key, value)
    await session.flush()
    return created
