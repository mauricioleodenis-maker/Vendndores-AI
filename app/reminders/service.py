"""Ejecucion de recordatorios/seguimientos: re-verifica estado, horario de silencio y envia."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.sender import (
    SendResult,
    send_whatsapp_template,
    send_whatsapp_text,
    window_is_open,
)
from app.channels.service import decrypt_phone
from app.core.clock import ensure_utc, utcnow
from app.core.crypto import get_crypto, make_aad, unpack_blob
from app.core.logging import get_logger
from app.db.models.booking import Appointment
from app.db.models.bots import BotConfig
from app.db.models.contacts import Consent, Contact
from app.db.models.conversations import Conversation
from app.db.models.outreach import MessageTemplate
from app.db.models.scheduling import ScheduledJob
from app.db.models.tenants import Tenant
from app.plans.entitlements import get_entitlements
from app.reminders import policies as pol

log = get_logger(__name__)

TERMINAL_OK = "done"


async def claim_due(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 200, worker: str = "cron"
) -> list[uuid.UUID]:
    """Libera locks vencidos, reclama los jobs pendientes ya vencidos y devuelve sus ids."""
    now = now or utcnow()
    stale = (
        (
            await session.execute(
                select(ScheduledJob)
                .where(
                    ScheduledJob.status == "running",
                    ScheduledJob.locked_at < now - pol.STALE_LOCK,
                    ScheduledJob.kind.in_(_KINDS),
                )
                # un job cuyo envio sigue en curso (fila bloqueada por process_job) no se reclama
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for job in stale:
        job.status, job.locked_at, job.locked_by = "pending", None, None
    await session.flush()
    rows = (
        (
            await session.execute(
                select(ScheduledJob)
                .where(
                    ScheduledJob.status == "pending",
                    ScheduledJob.kind.in_(_KINDS),
                    ScheduledJob.run_at <= now,
                    or_(
                        ScheduledJob.next_attempt_at.is_(None), ScheduledJob.next_attempt_at <= now
                    ),
                )
                .order_by(ScheduledJob.run_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for job in rows:
        job.status, job.locked_at, job.locked_by = "running", now, worker
    await session.flush()
    return [j.id for j in rows]


async def release_claims(session: AsyncSession, ids: list[uuid.UUID]) -> None:
    """Devuelve a ``pending`` jobs reclamados que no pudieron encolarse (se reintentan al ciclo)."""
    if not ids:
        return
    await session.execute(
        update(ScheduledJob)
        .where(ScheduledJob.id.in_(ids), ScheduledJob.status == "running")
        .values(status="pending", locked_at=None, locked_by=None)
    )
    await session.flush()


_KINDS = (pol.REMINDER_24H, pol.REMINDER_2H, pol.FOLLOWUP_NOSHOW, pol.FOLLOWUP_UNBOOKED)


def _finish(job: ScheduledJob, status: str, reason: str | None = None) -> str:
    job.status, job.last_error = status, reason
    job.locked_at = job.locked_by = None
    return reason or status


def _contact_name(tenant_id: uuid.UUID, contact: Contact) -> str:
    if contact.display_name_enc:
        try:
            aad = make_aad("contacts", tenant_id, "display_name_enc")
            full = get_crypto().decrypt_str(unpack_blob(contact.display_name_enc), aad=aad)
            return full.split()[0] if full.strip() else "cliente"
        except Exception:  # noqa: BLE001 - clave rotada/corrupta
            return "cliente"
    return "cliente"


async def _recordatorios_revoked(
    session: AsyncSession, tenant_id: uuid.UUID, contact_id: uuid.UUID
) -> bool:
    revoked, active = (
        await session.execute(
            select(
                func.count().filter(Consent.revoked_at.is_not(None)),
                func.count().filter(Consent.revoked_at.is_(None)),
            ).where(
                Consent.tenant_id == tenant_id,
                Consent.contact_id == contact_id,
                Consent.purpose == "recordatorios",
            )
        )
    ).one()
    return bool(revoked) and not active


async def _window_open(session: AsyncSession, tenant_id: uuid.UUID, contact_id: uuid.UUID) -> bool:
    last = (
        await session.execute(
            select(func.max(Conversation.last_inbound_at)).where(
                Conversation.tenant_id == tenant_id, Conversation.contact_id == contact_id
            )
        )
    ).scalar_one_or_none()
    return window_is_open(ensure_utc(last) if last else None)


async def _bot_template(session: AsyncSession, tenant_id: uuid.UUID, kind: str) -> str | None:
    cfg = (
        await session.execute(
            select(BotConfig.templates).where(
                BotConfig.tenant_id == tenant_id, BotConfig.status == "published"
            )
        )
    ).scalar_one_or_none()
    value = (cfg or {}).get(pol.BOT_TEMPLATE_KEYS[kind]) if isinstance(cfg, dict) else None
    return value if isinstance(value, str) and value.strip() else None


async def _content_sid(session: AsyncSession, tenant_id: uuid.UUID, kind: str) -> str | None:
    name = pol.TEMPLATE_NAMES.get(kind)
    if name is None:
        return None
    rows = (
        await session.execute(
            select(MessageTemplate.tenant_id, MessageTemplate.twilio_content_sid)
            .where(
                MessageTemplate.name == name,
                MessageTemplate.approval_status == "approved",
                MessageTemplate.twilio_content_sid.is_not(None),
                or_(MessageTemplate.tenant_id == tenant_id, MessageTemplate.tenant_id.is_(None)),
            )
            .order_by(MessageTemplate.version.desc())
        )
    ).all()
    for tid, sid in rows:  # la plantilla propia del tenant gana sobre la global
        if tid == tenant_id and sid:
            return str(sid)
    return str(rows[0][1]) if rows else None


async def _send(
    session: AsyncSession,
    job: ScheduledJob,
    *,
    phone: str,
    values: dict[str, str],
    can_text: bool,
) -> SendResult | str:
    """Texto libre en la ventana de 24 h; si no, plantilla aprobada. ``str`` = motivo de skip."""
    kind = job.kind
    if can_text:
        custom = await _bot_template(session, job.tenant_id, kind)
        body = pol.render_text(custom or pol.DEFAULT_TEXTS[kind], values)
        return await send_whatsapp_text(session, tenant_id=job.tenant_id, to_e164=phone, body=body)
    sid = await _content_sid(session, job.tenant_id, kind)
    if sid is None:
        return "skipped_no_template"
    variables = {str(i): values[k] for i, k in enumerate(pol.TEMPLATE_VARS[kind], start=1)}
    return await send_whatsapp_template(
        session, tenant_id=job.tenant_id, to_e164=phone, content_sid=sid, variables=variables
    )


async def process_job(
    session: AsyncSession, job_id: uuid.UUID, *, now: datetime | None = None
) -> str:
    """Ejecuta un job reclamado. Devuelve 'done' o el motivo ('skipped_*', 'retry', 'failed')."""
    now = now or utcnow()
    # Bloqueo de fila: una segunda ejecucion del mismo job espera a que la primera confirme y
    # entonces ve un estado final (no vuelve a enviar); cancelaciones concurrentes tambien esperan.
    job = (
        await session.execute(
            select(ScheduledJob)
            .where(ScheduledJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if job is None or job.status != "running" or job.kind not in _KINDS:
        return "ignored"
    tenant = await session.get(Tenant, job.tenant_id)
    contact = await session.get(Contact, job.contact_id) if job.contact_id else None
    if tenant is None or tenant.deleted_at is not None or tenant.status not in ("active", "trial"):
        return _finish(job, "cancelled", "skipped_tenant_inactivo")
    if contact is None or contact.tenant_id != job.tenant_id or contact.erased_at is not None:
        return _finish(job, "cancelled", "skipped_sin_contacto")
    if contact.opted_out or await _recordatorios_revoked(session, job.tenant_id, contact.id):
        return _finish(job, "cancelled", "skipped_opt_out")

    tz = pol.tz_of(tenant.timezone)
    appt: Appointment | None = None
    if job.appointment_id:
        appt = (
            await session.execute(
                select(Appointment)
                .where(Appointment.id == job.appointment_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if appt is None or appt.tenant_id != job.tenant_id:
            return _finish(job, "cancelled", "skipped_sin_cita")

    if job.kind in (pol.REMINDER_24H, pol.REMINDER_2H):
        assert appt is not None
        starts = ensure_utc(appt.starts_at)
        sent_col = "reminder_24h_sent_at" if job.kind == pol.REMINDER_24H else "reminder_2h_sent_at"
        if appt.status not in ("pending", "confirmed"):
            return _finish(job, "cancelled", "skipped_cita_no_activa")
        if starts <= now:
            return _finish(job, "cancelled", "skipped_cita_pasada")
        if getattr(appt, sent_col) is not None:
            return _finish(job, "cancelled", "skipped_ya_enviado")
        # La cita pudo moverse sin pasar por el scheduler: el job queda obsoleto.
        if job.dedupe_key and not job.dedupe_key.endswith(starts.isoformat()):
            return _finish(job, "cancelled", "skipped_cita_reprogramada")
    elif job.kind == pol.FOLLOWUP_NOSHOW:
        assert appt is not None
        if appt.status != "no_show":
            return _finish(job, "cancelled", "skipped_cita_no_activa")
    else:  # followup_unbooked
        has_active = (
            await session.execute(
                select(Appointment.id)
                .where(
                    Appointment.tenant_id == job.tenant_id,
                    Appointment.contact_id == contact.id,
                    Appointment.status.in_(("pending", "confirmed")),
                    Appointment.starts_at > now,
                )
                .limit(1)
            )
        ).first()
        if has_active:
            return _finish(job, "cancelled", "skipped_ya_agendo")

    if job.kind in (pol.FOLLOWUP_NOSHOW, pol.FOLLOWUP_UNBOOKED):
        ent = await get_entitlements(session, job.tenant_id)
        if not ent.has_feature("followups"):
            return _finish(job, "cancelled", "skipped_plan_sin_seguimientos")

    if pol.outside_send_window(now, tz):
        nxt = pol.next_allowed(now, tz)
        is_reminder = job.kind in (pol.REMINDER_24H, pol.REMINDER_2H)
        if appt is not None and is_reminder and nxt >= ensure_utc(appt.starts_at):
            return _finish(job, "cancelled", "skipped_horario_silencio")
        job.status, job.run_at, job.locked_at, job.locked_by = "pending", nxt, None, None
        return "postponed"

    phone = decrypt_phone(job.tenant_id, contact)
    if phone is None:
        return _finish(job, "cancelled", "skipped_telefono_ilegible")

    can_text = await _window_open(session, job.tenant_id, contact.id)
    if job.kind == pol.FOLLOWUP_UNBOOKED and not can_text:
        return _finish(
            job, "cancelled", "skipped_ventana_cerrada"
        )  # sin plantilla utility para esto
    values = {
        "nombre": _contact_name(job.tenant_id, contact),
        "negocio": tenant.name,
        "fecha": pol.format_date_es(appt.starts_at, tz) if appt else "",
        "hora": pol.format_time_es(appt.starts_at, tz) if appt else "",
        # frases relativas calculadas al momento del envio (el job pudo posponerse por silencio)
        "cuando": pol.when_phrase(appt.starts_at, now, tz) if appt else "",
        "faltan": pol.time_left_phrase(appt.starts_at, now) if appt else "",
    }
    result = await _send(session, job, phone=phone, values=values, can_text=can_text)
    if isinstance(result, str):
        log.warning("reminders.skipped", kind=job.kind, reason=result, tenant_id=str(job.tenant_id))
        return _finish(job, "cancelled", result)
    if result.status == "suppressed":
        return _finish(job, "cancelled", "skipped_suprimido")
    if not result.ok:
        job.attempts += 1
        job.last_error = (result.error or "error")[:200]
        if job.attempts >= pol.MAX_ATTEMPTS:
            log.error("reminders.failed", kind=job.kind, tenant_id=str(job.tenant_id))
            return _finish(job, "failed", job.last_error)
        job.status, job.locked_at, job.locked_by = "pending", None, None
        job.next_attempt_at = now + pol.RETRY_BACKOFF[job.attempts - 1]
        return "retry"
    if appt is not None and job.kind in (pol.REMINDER_24H, pol.REMINDER_2H):
        if job.kind == pol.REMINDER_24H:
            appt.reminder_24h_sent_at = now
        else:
            appt.reminder_2h_sent_at = now
        appt.reminder_sent_at = now
    return _finish(job, TERMINAL_OK)


async def run_batch(session: AsyncSession, ids: list[uuid.UUID]) -> dict[str, Any]:
    """Procesa varios jobs (uso local/tests); devuelve un conteo por resultado."""
    counts: dict[str, int] = {}
    for jid in ids:
        res = await process_job(session, jid)
        counts[res] = counts.get(res, 0) + 1
    return counts
