"""Retencion: borra el contenido de mensajes vencidos (``purge_after``)."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.core.config import get_settings
from app.db.models.conversations import Message

PURGED_BODY = "[purgado por política de retención]"


def default_purge_after(today: date | None = None) -> date:
    """Fecha de purga para un mensaje nuevo segun ``VAI_MSG_RETENTION_DAYS``."""
    base = today or utcnow().date()
    return base + timedelta(days=get_settings().msg_retention_days)


async def backfill_purge_after(session: AsyncSession) -> int:
    """Asigna ``purge_after`` a mensajes que no lo tienen (calculado desde su creacion)."""
    days = get_settings().msg_retention_days
    rows = (
        await session.execute(
            select(Message.id, Message.created_at).where(
                Message.purge_after.is_(None), Message.body_enc.is_not(None)
            )
        )
    ).all()
    for mid, created in rows:
        await session.execute(
            update(Message)
            .where(Message.id == mid)
            .values(purge_after=created.date() + timedelta(days=days))
        )
    await session.flush()
    return len(rows)


async def purge_expired_messages(session: AsyncSession, *, today: date | None = None) -> int:
    """Borra cuerpo cifrado y redactado de mensajes con ``purge_after <= hoy``. Conserva la fila
    (idempotencia de webhooks, metricas). Devuelve cuantos mensajes se purgaron."""
    cutoff = today or utcnow().date()
    result = await session.execute(
        update(Message)
        .where(
            Message.purge_after.is_not(None),
            Message.purge_after <= cutoff,
            (Message.body_enc.is_not(None)) | (Message.body_redacted != PURGED_BODY),
        )
        .values(body_enc=None, body_redacted=PURGED_BODY)
    )
    await session.flush()
    return int(result.rowcount)  # type: ignore[attr-defined]
