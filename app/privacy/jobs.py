"""Jobs de privacidad (descubiertos por ``app.worker``)."""

from __future__ import annotations

from typing import Any

from arq.cron import cron

from app.core.jobs import register_job
from app.core.logging import get_logger
from app.db.session import get_sessionmaker
from app.privacy.retention import backfill_purge_after, purge_expired_messages

log = get_logger(__name__)


@register_job("privacy.purge_retention")
async def purge_retention_job(ctx: dict[str, Any]) -> int:
    async with get_sessionmaker()() as session:
        while await backfill_purge_after(session):
            await session.commit()
        purged = await purge_expired_messages(session)
        await session.commit()
    log.info("privacy.purged", messages=purged)
    return purged


CRON_JOBS = [cron(purge_retention_job, hour={8}, minute={30}, name="privacy.purge_retention")]
