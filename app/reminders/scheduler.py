"""Programacion de recordatorios y seguimientos (contrato MAESTRO §5, B10).

``scheduled_jobs.dedupe_key`` es unico: reprogramar la misma cita al mismo horario no duplica.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import ensure_utc, utcnow
from app.core.logging import get_logger
from app.db.models.booking import Appointment
from app.db.models.contacts import Contact
from app.db.models.scheduling import ScheduledJob
from app.plans.entitlements import get_entitlements
from app.reminders import policies as pol

log = get_logger(__name__)

_GRACE = timedelta(minutes=1)


def dedupe_key(kind: str, appointment: Appointment) -> str:
    return f"{kind}:{appointment.id}:{ensure_utc(appointment.starts_at).isoformat()}"


async def _upsert_job(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    run_at: datetime,
    key: str,
    contact_id: uuid.UUID | None,
    appointment_id: uuid.UUID | None,
    payload: dict[str, str],
) -> ScheduledJob | None:
    existing = (
        await session.execute(select(ScheduledJob).where(ScheduledJob.dedupe_key == key))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.tenant_id != tenant_id or existing.status != "cancelled":
            return None  # ya programado/ejecutado: idempotente
        existing.status, existing.run_at = (
            "pending",
            run_at,
        )  # revive (reprogramada al mismo horario)
        existing.attempts, existing.last_error, existing.next_attempt_at = 0, None, None
        existing.locked_at = existing.locked_by = None
        await session.flush()
        return existing
    job = ScheduledJob(
        tenant_id=tenant_id,
        kind=kind,
        run_at=run_at,
        dedupe_key=key,
        contact_id=contact_id,
        appointment_id=appointment_id,
        payload=payload,
    )
    session.add(job)
    await session.flush()
    return job


async def _contact_ok(session: AsyncSession, appointment: Appointment) -> bool:
    if appointment.contact_id is None:
        return False
    contact = await session.get(Contact, appointment.contact_id)
    return contact is not None and not contact.opted_out and contact.erased_at is None


async def schedule_for_appointment(session: AsyncSession, appointment: Appointment) -> None:
    """Crea recordatorios 24 h (todos los planes) y 2 h (planes con seguimientos).

    Solo para citas activas con contacto que no se dio de baja. Los que ya quedaron en el pasado
    se omiten (una cita agendada con 5 h de antelacion no recibe el de 24 h).
    """
    if appointment.status not in ("pending", "confirmed") or not await _contact_ok(
        session, appointment
    ):
        return
    now = utcnow()
    starts = ensure_utc(appointment.starts_at)
    ent = await get_entitlements(session, appointment.tenant_id)
    kinds = [pol.REMINDER_24H]
    if ent.has_feature("followups"):
        kinds.append(pol.REMINDER_2H)
    for kind in kinds:
        run_at = starts - pol.OFFSETS[kind]
        if run_at < now - _GRACE:
            continue
        await _upsert_job(
            session,
            tenant_id=appointment.tenant_id,
            kind=kind,
            run_at=run_at,
            key=dedupe_key(kind, appointment),
            contact_id=appointment.contact_id,
            appointment_id=appointment.id,
            payload={"appointment_id": str(appointment.id)},
        )


async def cancel_for_appointment(session: AsyncSession, appointment_id: uuid.UUID) -> None:
    """Cancela los jobs pendientes de la cita (cancelacion o reprogramacion)."""
    await session.execute(
        update(ScheduledJob)
        .where(
            ScheduledJob.appointment_id == appointment_id,
            ScheduledJob.status.in_(("pending", "running")),
        )
        .values(status="cancelled", last_error="cita_cancelada_o_reprogramada")
    )
    await session.flush()


async def schedule_noshow_followup(session: AsyncSession, appointment: Appointment) -> None:
    """Seguimiento 2 h despues de marcar la cita como no-show (planes con seguimientos)."""
    if appointment.status != "no_show" or not await _contact_ok(session, appointment):
        return
    ent = await get_entitlements(session, appointment.tenant_id)
    if not ent.has_feature("followups"):
        return
    await _upsert_job(
        session,
        tenant_id=appointment.tenant_id,
        kind=pol.FOLLOWUP_NOSHOW,
        run_at=utcnow() + pol.NOSHOW_DELAY,
        key=f"{pol.FOLLOWUP_NOSHOW}:{appointment.id}",
        contact_id=appointment.contact_id,
        appointment_id=appointment.id,
        payload={"appointment_id": str(appointment.id)},
    )


async def schedule_unbooked_followup(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> ScheduledJob | None:
    """Reactivacion de una conversacion sin cita: +24 h y +3 dias, maximo 2 por conversacion."""
    contact = await session.get(Contact, contact_id)
    if contact is None or contact.tenant_id != tenant_id or contact.opted_out or contact.erased_at:
        return None
    ent = await get_entitlements(session, tenant_id)
    if not ent.has_feature("followups"):
        return None
    prefix = f"{pol.FOLLOWUP_UNBOOKED}:{conversation_id}:"
    existing = (
        await session.execute(
            select(ScheduledJob.dedupe_key).where(
                ScheduledJob.dedupe_key.like(prefix + "%"), ScheduledJob.tenant_id == tenant_id
            )
        )
    ).all()
    step = len(existing)
    if step >= pol.MAX_UNBOOKED_FOLLOWUPS:
        return None
    return await _upsert_job(
        session,
        tenant_id=tenant_id,
        kind=pol.FOLLOWUP_UNBOOKED,
        run_at=utcnow() + pol.UNBOOKED_DELAYS[step],
        key=f"{prefix}{step + 1}",
        contact_id=contact_id,
        appointment_id=None,
        payload={"conversation_id": str(conversation_id)},
    )
