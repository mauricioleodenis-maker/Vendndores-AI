"""Pipeline de ventas: transiciones de etapa validas, disposicion y timeline (``lead_events``)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.core.clock import utcnow
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models.leads import LEAD_DISPOSITIONS, LEAD_STAGES, Lead, LeadEvent
from app.db.models.users import User

STAGES: tuple[str, ...] = LEAD_STAGES  # nuevo, prueba_secreta, contactado, demo, cerrado

# Avance de un paso, salto de prueba_secreta (nuevo -> contactado) y retroceso de un paso.
ALLOWED: dict[str, frozenset[str]] = {
    "nuevo": frozenset({"prueba_secreta", "contactado"}),
    "prueba_secreta": frozenset({"nuevo", "contactado"}),
    "contactado": frozenset({"prueba_secreta", "demo"}),
    "demo": frozenset({"contactado", "cerrado"}),
    "cerrado": frozenset({"demo"}),
}


class InvalidTransitionError(AppError):
    def __init__(self, src: str, dst: str) -> None:
        super().__init__(
            "invalid_transition", f"No se puede pasar de «{src}» a «{dst}» directamente", 409
        )


def can_transition(src: str, dst: str) -> bool:
    return dst in ALLOWED.get(src, frozenset())


async def get_lead(session: AsyncSession, lead_id: uuid.UUID, *, for_update: bool = False) -> Lead:
    """Carga el lead; ``for_update`` bloquea la fila (evita doble conversion/demo concurrente)."""
    if for_update:
        lead = (
            await session.execute(
                select(Lead)
                .where(Lead.id == lead_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    else:
        lead = await session.get(Lead, lead_id)
    if lead is None:
        raise NotFoundError("Lead no encontrado")
    return lead


async def add_event(
    session: AsyncSession,
    lead: Lead,
    kind: str,
    data: dict[str, Any] | None = None,
    *,
    actor: User | None = None,
) -> LeadEvent:
    """Agrega un evento append-only al timeline del lead."""
    event = LeadEvent(
        lead_id=lead.id,
        ts=utcnow(),
        actor_user_id=actor.id if actor else None,
        kind=kind,
        data=data or {},
    )
    session.add(event)
    await session.flush()
    return event


async def list_events(
    session: AsyncSession, lead_id: uuid.UUID, *, limit: int = 100
) -> list[LeadEvent]:
    stmt = (
        select(LeadEvent)
        .where(LeadEvent.lead_id == lead_id)
        .order_by(LeadEvent.ts.desc(), LeadEvent.id)
        .limit(min(limit, 500))
    )
    return list((await session.execute(stmt)).scalars())


def _ensure_active(lead: Lead) -> None:
    if lead.disposition != "activo":
        raise ConflictError("El lead no está activo (perdido o no contactar)")


async def transition_stage(
    session: AsyncSession,
    lead: Lead | uuid.UUID,
    to_stage: str,
    *,
    actor: User | None,
    note: str = "",
    system: bool = False,
) -> Lead:
    """Mueve el lead de etapa validando ``ALLOWED``.

    ``cerrado`` exige ``converted_tenant_id``. ``system=True`` (conversion) omite la
    adyacencia pero no el resto de reglas.
    """
    if to_stage not in STAGES:
        raise AppError("invalid_stage", "Etapa desconocida", 422)
    if isinstance(lead, uuid.UUID):
        lead = await get_lead(session, lead)
    _ensure_active(lead)
    src = lead.stage
    if src == to_stage:
        return lead
    if not system and not can_transition(src, to_stage):
        raise InvalidTransitionError(src, to_stage)
    if to_stage == "cerrado" and lead.converted_tenant_id is None:
        raise ConflictError("Para cerrar el lead primero conviértelo en cliente")
    lead.stage = to_stage
    clean_note = note.strip()[:500]
    await add_event(
        session,
        lead,
        "stage_changed",
        {"from": src, "to": to_stage, "note": clean_note},
        actor=actor,
    )
    await log_event(
        session,
        actor=actor,
        action="lead.stage_change",
        entity_type="lead",
        entity_id=lead.id,
        diff={"from": src, "to": to_stage},
    )
    return lead


async def set_disposition(
    session: AsyncSession,
    lead: Lead | uuid.UUID,
    disposition: str,
    *,
    actor: User | None,
    reason: str = "",
) -> Lead:
    """Marca perdido / no_contactar / activo (``no_contactar`` solo lo revierte el owner)."""
    if disposition not in LEAD_DISPOSITIONS:
        raise AppError("invalid_disposition", "Estado desconocido", 422)
    if isinstance(lead, uuid.UUID):
        lead = await get_lead(session, lead)
    prev = lead.disposition
    if prev == disposition:
        return lead
    if prev == "no_contactar" and not (actor is not None and actor.role == "owner"):
        raise ConflictError("Un lead «no contactar» solo puede reactivarlo el owner")
    lead.disposition = disposition
    kind = "opt_out" if disposition == "no_contactar" else "note"
    await add_event(
        session,
        lead,
        kind,
        {"disposition": disposition, "from": prev, "reason": reason.strip()[:300]},
        actor=actor,
    )
    await log_event(
        session,
        actor=actor,
        action="lead.disposition",
        entity_type="lead",
        entity_id=lead.id,
        diff={"from": prev, "to": disposition},
    )
    return lead
