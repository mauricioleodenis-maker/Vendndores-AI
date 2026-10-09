"""Jobs de outreach (descubiertos por ``app.worker``)."""

from __future__ import annotations

from typing import Any

from arq.cron import cron

from app.core.jobs import register_job
from app.core.logging import get_logger
from app.db.session import get_sessionmaker
from app.outreach import dispatch

log = get_logger(__name__)


@register_job("outreach.dispatch")
async def dispatch_campaign_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Cron cada minuto: despacha un lote por campana en curso (respeta el gate)."""
    async with get_sessionmaker()() as session:
        try:
            result = await dispatch.dispatch_all(session, pace=float(ctx.get("pace", 1.0)))
            await session.commit()
        except Exception:
            await session.rollback()
            log.error("outreach.dispatch_crashed")
            raise
    return result


CRON_JOBS = [cron(dispatch_campaign_job, minute=set(range(60)), name="outreach.dispatch")]
