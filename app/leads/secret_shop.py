"""Prueba secreta: registro del mensaje, medicion del tiempo de respuesta y evidencia para el pitch.

El sistema NO lee el WhatsApp del negocio: el operador registra la respuesta a mano.
Etica: mensaje de cliente potencial real, una sola vez por negocio, sin datos de salud.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.core.clock import ensure_utc, to_bogota, utcnow
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.jobs import register_job
from app.core.logging import get_logger
from app.db.models.leads import Lead, ReviewSignal, SecretShopTest
from app.db.models.users import User
from app.leads import pipeline
from app.leads.scoring import score_lead
from app.privacy.redaction import redact_pii

log = get_logger(__name__)

SCENARIOS = ("precio", "cita", "horario", "urgencia")
CHANNELS = ("whatsapp", "instagram", "llamada", "web_form")
TIMEOUT_HOURS = 24
SLOW_SECONDS = 15 * 60
BUSINESS_OPEN_HOUR = 8
BUSINESS_CLOSE_HOUR = 18
EXCERPT_MAX = 280

_SERVICE_BY_NICHE = {
    "dentista": "una limpieza dental",
    "clinica_estetica": "una valoración estética",
    "taller": "una revisión del carro",
    "restaurante": "una mesa para cuatro personas",
}

_SCRIPTS = {
    "precio": "Hola, ¿cuánto vale {servicio} y tienen disponibilidad esta semana?",
    "cita": "Hola, quisiera agendar {servicio}. ¿Qué día y hora tienen disponible?",
    "horario": "Hola, ¿cuál es su horario de atención y si atienden {servicio} los sábados?",
    "urgencia": "Hola, necesito {servicio} lo antes posible, ¿tienen espacio hoy o mañana?",
}


def suggested_script(scenario: str, niche: str) -> str:
    """Guion sugerido (plantilla interna, sin datos sensibles) por escenario y nicho."""
    if scenario not in _SCRIPTS:
        raise AppError("invalid_scenario", "Escenario desconocido", 422)
    servicio = _SERVICE_BY_NICHE.get(niche, "sus servicios")
    return _SCRIPTS[scenario].format(servicio=servicio)


def is_after_hours(moment: datetime, hours: Any = None) -> bool:
    """True si ``moment`` cae fuera del horario del negocio.

    ``hours`` admite el formato de Places (``{"periods": [{"open": {"day","hour","minute"},
    "close": {...}}]}``, domingo=0). Sin horario: lun-sab 08:00-18:00 America/Bogota.
    """
    local = to_bogota(moment)
    periods = hours.get("periods") if isinstance(hours, Mapping) else None
    if periods:
        day = (local.weekday() + 1) % 7  # Places: domingo = 0
        minute_of_week = day * 1440 + local.hour * 60 + local.minute
        for p in periods:
            try:
                o, c = p["open"], p.get("close")
                start = int(o["day"]) * 1440 + int(o.get("hour", 0)) * 60 + int(o.get("minute", 0))
                if c is None:  # abierto 24h
                    return False
                end = int(c["day"]) * 1440 + int(c.get("hour", 0)) * 60 + int(c.get("minute", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if end < start:  # cruza el domingo -> lunes
                end += 7 * 1440
            for shift in (0, 7 * 1440):
                if start <= minute_of_week + shift < end:
                    return False
        return True
    if local.weekday() == 6:
        return True
    return not (BUSINESS_OPEN_HOUR <= local.hour < BUSINESS_CLOSE_HOUR)


def classify_outcome(response_seconds: int | None, *, closed_booking: bool = False) -> str:
    if response_seconds is None:
        return "sin_respuesta"
    if response_seconds > SLOW_SECONDS:
        return "respuesta_lenta"
    return "respuesta_con_cierre" if closed_booking else "respuesta_ok"


async def get_test(session: AsyncSession, test_id: uuid.UUID) -> SecretShopTest:
    test = await session.get(SecretShopTest, test_id)
    if test is None:
        raise NotFoundError("Prueba secreta no encontrada")
    return test


async def latest_test(session: AsyncSession, lead_id: uuid.UUID) -> SecretShopTest | None:
    stmt = (
        select(SecretShopTest)
        .where(SecretShopTest.lead_id == lead_id)
        .order_by(SecretShopTest.sent_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def rescore(session: AsyncSession, lead: Lead, test: SecretShopTest | None = None) -> int:
    """Recalcula el score del lead con la prueba mas reciente."""
    signals = list(
        (
            await session.execute(select(ReviewSignal).where(ReviewSignal.lead_id == lead.id))
        ).scalars()
    )
    result = score_lead(lead, signals, test or await latest_test(session, lead.id))
    lead.score = result.score
    lead.score_breakdown = result.breakdown
    return result.score


async def create_test(
    session: AsyncSession,
    lead_id: uuid.UUID,
    *,
    actor: User | None,
    scenario: str = "precio",
    channel: str = "whatsapp",
    sent_at: datetime | None = None,
    message_text: str = "",
) -> SecretShopTest:
    """Registra el envio de la prueba y mueve el lead a ``prueba_secreta``."""
    if scenario not in SCENARIOS:
        raise AppError("invalid_scenario", "Escenario desconocido", 422)
    if channel not in CHANNELS:
        raise AppError("invalid_channel", "Canal desconocido", 422)
    lead = await pipeline.get_lead(session, lead_id, for_update=True)
    if lead.disposition != "activo":
        raise ConflictError("No se puede hacer la prueba a un lead perdido o «no contactar»")
    if await latest_test(session, lead.id) is not None:
        raise ConflictError("Ya existe una prueba secreta para este negocio (solo una vez)")
    now = utcnow()
    moment = ensure_utc(sent_at) if sent_at else now
    if moment > now + timedelta(minutes=5):
        raise AppError("invalid_sent_at", "La fecha de envío no puede estar en el futuro", 422)
    text = (message_text or "").strip() or suggested_script(scenario, lead.niche)
    test = SecretShopTest(
        lead_id=lead.id,
        created_by=actor.id if actor else None,
        channel=channel,
        sent_at=moment,
        scenario=scenario,
        message_text=text[:1000],
        after_hours=is_after_hours(moment, lead.hours),
    )
    session.add(test)
    await session.flush()
    if lead.stage == "nuevo":
        await pipeline.transition_stage(session, lead, "prueba_secreta", actor=actor)
    await pipeline.add_event(
        session,
        lead,
        "secret_shop_sent",
        {"test_id": str(test.id), "scenario": scenario, "channel": channel},
        actor=actor,
    )
    await log_event(
        session,
        actor=actor,
        action="secret_shop.create",
        entity_type="secret_shop_test",
        entity_id=test.id,
        diff={"lead_id": str(lead.id), "scenario": scenario},
    )
    return test


async def record_reply(
    session: AsyncSession,
    test_id: uuid.UUID,
    *,
    first_reply_at: datetime,
    excerpt: str = "",
    closed_booking: bool = False,
    actor: User | None,
) -> SecretShopTest:
    """Registra la primera respuesta: calcula ``response_seconds``/``outcome`` y re-puntua."""
    test = await get_test(session, test_id)
    if test.first_reply_at is not None:
        raise ConflictError("Esta prueba ya tiene una respuesta registrada")
    reply_at = ensure_utc(first_reply_at)
    sent = ensure_utc(test.sent_at)
    if reply_at < sent:
        raise AppError("invalid_reply_at", "La respuesta no puede ser anterior al envío", 422)
    if reply_at > utcnow() + timedelta(minutes=5):
        raise AppError("invalid_reply_at", "La respuesta no puede estar en el futuro", 422)
    seconds = int((reply_at - sent).total_seconds())
    test.first_reply_at = reply_at
    test.first_reply_excerpt = redact_pii((excerpt or "").strip())[:EXCERPT_MAX]
    test.response_seconds = seconds
    test.outcome = classify_outcome(seconds, closed_booking=closed_booking)
    lead = await pipeline.get_lead(session, test.lead_id)
    old = lead.score
    new = await rescore(session, lead, test)
    await pipeline.add_event(
        session,
        lead,
        "secret_shop_replied",
        {"test_id": str(test.id), "seconds": seconds, "outcome": test.outcome},
        actor=actor,
    )
    if old != new:
        await pipeline.add_event(session, lead, "scored", {"from": old, "to": new}, actor=actor)
    await log_event(
        session,
        actor=actor,
        action="secret_shop.reply",
        entity_type="secret_shop_test",
        entity_id=test.id,
        diff={"seconds": seconds, "outcome": test.outcome},
    )
    return test


async def expire_unanswered(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Marca ``sin_respuesta`` las pruebas sin respuesta tras 24 h. Devuelve cuantas."""
    moment = now or utcnow()
    cutoff = moment - timedelta(hours=TIMEOUT_HOURS)
    stmt = select(SecretShopTest).where(
        SecretShopTest.first_reply_at.is_(None),
        SecretShopTest.outcome.is_(None),
        SecretShopTest.sent_at <= cutoff,
    )
    tests = list((await session.execute(stmt.limit(500))).scalars())
    if not tests:
        return 0
    # Batch: un SELECT para leads y otro para senales (evita N+1).
    lead_ids = {t.lead_id for t in tests}
    leads = {
        lead.id: lead
        for lead in (await session.execute(select(Lead).where(Lead.id.in_(lead_ids)))).scalars()
    }
    signals: dict[uuid.UUID, list[ReviewSignal]] = {}
    for sig in (
        await session.execute(select(ReviewSignal).where(ReviewSignal.lead_id.in_(lead_ids)))
    ).scalars():
        signals.setdefault(sig.lead_id, []).append(sig)
    for test in tests:
        test.outcome = "sin_respuesta"
        lead = leads.get(test.lead_id)
        if lead is None:
            continue
        result = score_lead(lead, signals.get(lead.id, []), test)
        lead.score = result.score
        lead.score_breakdown = result.breakdown
        await pipeline.add_event(
            session,
            lead,
            "secret_shop_replied",
            {"test_id": str(test.id), "outcome": "sin_respuesta", "timeout_hours": TIMEOUT_HOURS},
        )
    await session.flush()
    return len(tests)


@register_job("leads.secret_shop_timeout")
async def secret_shop_timeout_job(ctx: dict[str, Any] | None = None) -> int:
    from app.db.session import session_scope

    async with session_scope() as session:
        count = await expire_unanswered(session)
    if count:
        log.info("secret_shop.timeout", count=count)
    return count


async def pending_reviews(
    session: AsyncSession, *, now: datetime | None = None, minutes: int = 15
) -> list[SecretShopTest]:
    """Pruebas sin respuesta con mas de ``minutes`` minutos (recordatorio al operador)."""
    cutoff = (now or utcnow()) - timedelta(minutes=minutes)
    stmt = (
        select(SecretShopTest)
        .where(
            SecretShopTest.first_reply_at.is_(None),
            SecretShopTest.outcome.is_(None),
            SecretShopTest.sent_at <= cutoff,
        )
        .order_by(SecretShopTest.sent_at)
    )
    return list((await session.execute(stmt)).scalars())


async def pitch_evidence(session: AsyncSession, lead_id: uuid.UUID) -> dict[str, Any]:
    """Variables para el pitch: solo se cita al propio negocio, nunca a terceros."""
    await pipeline.get_lead(session, lead_id)
    test = await latest_test(session, lead_id)
    if test is None:
        return {"available": False}
    sent_local = to_bogota(test.sent_at)
    minutes = None if test.response_seconds is None else round(test.response_seconds / 60)
    hour12 = sent_local.strftime("%I:%M %p").lstrip("0").lower()
    return {
        "available": True,
        "scenario": test.scenario,
        "channel": test.channel,
        "sent_at": ensure_utc(test.sent_at).isoformat(),
        "sent_at_local": hour12,
        "response_minutes": minutes,
        "outcome": test.outcome,
        "after_hours": test.after_hours,
        "answered": test.first_reply_at is not None,
    }
