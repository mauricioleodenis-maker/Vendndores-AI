"""ComplianceGate: decide si un mensaje de outreach puede salir AHORA. Fail-closed.

Cada regla es dura (en codigo, no solo configuracion). Ante cualquier error inesperado se deniega.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

import holidays
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import BOGOTA, ensure_utc, to_bogota
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.leads import Lead
from app.db.models.outreach import Campaign, MessageTemplate, OutreachMessage
from app.outreach.templates import is_sendable
from app.privacy.service import is_suppressed

log = get_logger(__name__)

WINDOW_START_HOUR = 8
WINDOW_END_HOUR = 19  # exclusivo: ultimo envio 18:59
MAX_DAILY = 80  # tope duro por dia (todas las campanas) para proteger la calidad del numero
RAMP_START = 20
RAMP_STEP = 10
MAX_STEPS = 2
MIN_GAP = timedelta(hours=72)
QUALITY_WINDOW = 100
QUALITY_MIN_SAMPLE = 20
QUALITY_MAX_BAD_RATIO = 0.05
BLOCK_ERROR_CODES = frozenset({"63049", "21610", "63032"})
_COUNTED = ("queued", "sent", "delivered", "read", "failed", "replied")

Action = Literal["allow", "deny", "defer"]


@dataclass(frozen=True, slots=True)
class GateDecision:
    action: Action
    reason: str = ""
    until: datetime | None = None

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


def allow() -> GateDecision:
    return GateDecision("allow")


def deny(reason: str) -> GateDecision:
    return GateDecision("deny", reason)


def defer(reason: str, until: datetime | None) -> GateDecision:
    return GateDecision("defer", reason, until)


# --------------------------------------------------------------------------- calendario
def _is_business_day(d: date) -> bool:
    if d.weekday() == 6:  # domingo
        return False
    return d not in holidays.CO(years=d.year)


def in_send_window(now: datetime) -> bool:
    local = to_bogota(now)
    return _is_business_day(local.date()) and WINDOW_START_HOUR <= local.hour < WINDOW_END_HOUR


def next_window_start(now: datetime) -> datetime:
    """Proximo instante (UTC) en que se abre la ventana de envio."""
    local = to_bogota(now)
    day = local.date()
    if _is_business_day(day) and local.hour < WINDOW_START_HOUR:
        pass  # hoy mismo a las 8
    else:
        day += timedelta(days=1)
    while not _is_business_day(day):
        day += timedelta(days=1)
    return datetime.combine(day, time(WINDOW_START_HOUR), tzinfo=BOGOTA).astimezone(UTC)


def _start_of_local_day_utc(now: datetime) -> datetime:
    local = to_bogota(now)
    return datetime.combine(local.date(), time(0), tzinfo=BOGOTA).astimezone(UTC)


# --------------------------------------------------------------------------- limites
def effective_daily_limit(campaign: Campaign, now: datetime) -> int:
    """Rampa +10/dia desde 20, acotada por ``daily_limit`` de la campana y el tope global."""
    started_raw = (campaign.stats or {}).get("started_at")
    days = 0
    if started_raw:
        started = ensure_utc(datetime.fromisoformat(started_raw))
        days = max(0, (to_bogota(now).date() - to_bogota(started).date()).days)
    ramp = RAMP_START + RAMP_STEP * days
    return max(0, min(int(campaign.daily_limit), ramp, MAX_DAILY))


async def _sent_today(
    session: AsyncSession, now: datetime, campaign_id: object | None = None
) -> int:
    stmt = select(func.count(OutreachMessage.id)).where(
        OutreachMessage.sent_at.is_not(None),
        OutreachMessage.sent_at >= _start_of_local_day_utc(now),
    )
    if campaign_id is not None:
        stmt = stmt.where(OutreachMessage.campaign_id == campaign_id)
    return int((await session.execute(stmt)).scalar_one())


async def quality_degraded(session: AsyncSession, campaign: Campaign) -> bool:
    """``True`` si fallos/bloqueos superan 5% de los ultimos 100 envios de la campana."""
    rows = (
        await session.execute(
            select(OutreachMessage.status, OutreachMessage.delivery_error_code)
            .where(
                OutreachMessage.campaign_id == campaign.id,
                OutreachMessage.status.in_(_COUNTED),
                OutreachMessage.status != "queued",
            )
            .order_by(OutreachMessage.created_at.desc())
            .limit(QUALITY_WINDOW)
        )
    ).all()
    if len(rows) < QUALITY_MIN_SAMPLE:
        return False
    bad = sum(1 for status, code in rows if status == "failed" or code in BLOCK_ERROR_CODES)
    return bad / len(rows) > QUALITY_MAX_BAD_RATIO


# --------------------------------------------------------------------------- gate
class ComplianceGate:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def check(
        self,
        lead: Lead,
        campaign: Campaign,
        now: datetime,
        *,
        template: MessageTemplate | None = None,
        step: int = 1,
    ) -> GateDecision:
        try:
            return await self._check(lead, campaign, now, template, step)
        except Exception as exc:  # noqa: BLE001 - fail-closed
            log.error("outreach.gate_error", error_type=type(exc).__name__)
            return deny("error_interno")

    async def _check(
        self,
        lead: Lead,
        campaign: Campaign,
        now: datetime,
        template: MessageTemplate | None,
        step: int,
    ) -> GateDecision:
        if not get_settings().outreach_enabled:
            return deny("kill_switch")
        if campaign.status != "running":
            return deny("campana_no_activa")
        if lead.disposition != "activo":
            return deny("lead_no_activo")
        if lead.phone_type != "mobile" or not lead.phone_e164:
            return deny("no_movil")
        if await is_suppressed(self.session, lead.phone_e164):
            return deny("suprimido")
        if template is None or not is_sendable(template):
            return deny("plantilla_no_aprobada")
        if step < 1 or step > MAX_STEPS:
            return deny("max_pasos")

        prior = (
            (
                await self.session.execute(
                    select(OutreachMessage).where(
                        OutreachMessage.lead_id == lead.id,
                        OutreachMessage.status.in_(("sent", "delivered", "read", "replied")),
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(prior) >= MAX_STEPS or len(prior) >= step:
            return deny("max_pasos")
        if any(m.status == "replied" or m.replied_at for m in prior):
            return deny("ya_respondio")
        if prior:
            last = max(ensure_utc(m.sent_at) for m in prior if m.sent_at)
            if now - last < MIN_GAP:
                return defer("espera_72h", last + MIN_GAP)

        if not in_send_window(now):
            return defer("fuera_de_ventana", next_window_start(now))

        if await quality_degraded(self.session, campaign):
            return deny("calidad")

        if await _sent_today(self.session, now) >= MAX_DAILY:
            return defer("tope_global_diario", next_window_start(now))
        if await _sent_today(self.session, now, campaign.id) >= effective_daily_limit(
            campaign, now
        ):
            return defer("limite_diario", next_window_start(now))
        return allow()
