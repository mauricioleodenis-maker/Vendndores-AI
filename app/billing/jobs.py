"""Jobs de facturacion (descubiertos por ``app.worker``)."""

from __future__ import annotations

from typing import Any

from arq.cron import cron

from app.billing import service
from app.core.jobs import register_job
from app.core.logging import get_logger
from app.db.session import get_sessionmaker

log = get_logger(__name__)


@register_job("billing.generate_monthly_records")
async def generate_monthly_records(
    ctx: dict[str, Any], period: str | None = None
) -> dict[str, int]:
    """Cron diario: genera mensualidades del mes (idempotente) y marca cobros vencidos."""
    async with get_sessionmaker()() as session:
        created = await service.generate_monthly_records(session, period)
        overdue = await service.mark_overdue(session)
        await session.commit()
    log.info("billing.monthly", created=created, overdue=overdue)
    return {"created": created, "overdue": overdue}


CRON_JOBS = [
    cron(generate_monthly_records, hour={11}, minute={5}, name="billing.generate_monthly_records")
]
