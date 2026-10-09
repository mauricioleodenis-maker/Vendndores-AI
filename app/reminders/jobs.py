"""Jobs de recordatorios (descubiertos por ``app.worker``)."""

from __future__ import annotations

import uuid
from typing import Any

from arq.cron import cron

from app.core.clock import utcnow
from app.core.jobs import enqueue, register_job
from app.core.logging import get_logger
from app.db.session import get_sessionmaker
from app.reminders import service

log = get_logger(__name__)


@register_job("reminders.enqueue_due")
async def enqueue_due(ctx: dict[str, Any]) -> int:
    """Cron cada 5 min: reclama los jobs vencidos y encola uno ``reminders.send`` por cada uno."""
    async with get_sessionmaker()() as session:
        ids = await service.claim_due(session)
        await session.commit()
    for jid in ids:
        await enqueue(
            "reminders.send", str(jid), _job_id=f"reminders:{jid}:{int(utcnow().timestamp())}"
        )
    if ids:
        log.info("reminders.enqueued", count=len(ids))
    return len(ids)


@register_job("reminders.send")
async def send(ctx: dict[str, Any], job_id: str) -> str:
    async with get_sessionmaker()() as session:
        try:
            result = await service.process_job(session, uuid.UUID(job_id))
            await session.commit()
        except Exception:
            await session.rollback()
            log.error("reminders.send_crashed", job_id=job_id)
            raise
    return result


CRON_JOBS = [
    cron(
        enqueue_due, minute=set(range(0, 60, 5)), name="reminders.enqueue_due", run_at_startup=True
    )
]
