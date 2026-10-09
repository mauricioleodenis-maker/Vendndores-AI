"""Retencion: borra el contenido de mensajes vencidos (``purge_after``)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.core.config import get_settings
from app.db.models.conversations import Message

_BATCH = 5000
PURGED_BODY = "[purgado por política de retención]"


def default_purge_after(today: date | None = None) -> date:
    """Fecha de purga para un mensaje nuevo segun ``VAI_MSG_RETENTION_DAYS``."""
    base = today or utcnow().date()
    return base + timedelta(days=get_settings().msg_retention_days)


async def backfill_purge_after(session: AsyncSession) -> int:
    """Asigna ``purge_after`` a mensajes que no lo tienen (calculado desde su creacion)."""
    days = get_settings().msg_retention_days
    ids = (
        (
            await session.execute(
                select(Message.id)
                .where(Message.purge_after.is_(None), Message.body_enc.is_not(None))
                .limit(_BATCH)
            )
        )
        .scalars()
        .all()
    )
    if not ids:
        return 0
    # Un UPDATE por dia de creacion (no por fila): agrupa en lotes.
    rows = (
        await session.execute(select(Message.id, Message.created_at).where(Message.id.in_(ids)))
    ).all()
    by_date: dict[date, list[Any]] = {}
    for mid, created in rows:
        by_date.setdefault(created.date() + timedelta(days=days), []).append(mid)
    for due, mids in by_date.items():
        await session.execute(update(Message).where(Message.id.in_(mids)).values(purge_after=due))
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
