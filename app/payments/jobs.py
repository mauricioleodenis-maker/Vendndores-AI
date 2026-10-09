"""Jobs de cobro Wompi (descubiertos por ``app.worker`` via ``CRON_JOBS``)."""

from __future__ import annotations

from typing import Any

from arq.cron import cron

from app.core.jobs import register_job
from app.core.logging import get_logger
from app.db.session import get_sessionmaker
from app.payments import service
from app.payments.client import WompiClient
from app.payments.settings import get_wompi_settings

log = get_logger(__name__)


@register_job("payments.monthly_links")
async def monthly_links(ctx: dict[str, Any], period: str | None = None) -> dict[str, int]:
    """Crea links de pago de mensualidades nuevas y los envia por WhatsApp (idempotente)."""
    s = get_wompi_settings()
    if not s.enabled:
        return {"created": 0, "sent": 0, "failed": 0}
    cfg = service.PaymentJobSettings()
    async with get_sessionmaker()() as session:
        out = await service.create_links_for_new_monthly(
            session,
            WompiClient.from_settings(s),
            redirect_url=cfg.redirect_url,
            content_sid=cfg.link_template_sid,
            period=period,
        )
        await session.commit()
    log.info("payments.monthly_links", **out)
    return out


@register_job("payments.reconcile_pending")
async def reconcile_pending(ctx: dict[str, Any]) -> dict[str, int]:
    s = get_wompi_settings()
    if not s.enabled:
        return {"checked": 0, "approved": 0, "updated": 0, "errors": 0}
    async with get_sessionmaker()() as session:
        out = await service.reconcile_pending(session, WompiClient.from_settings(s))
        await session.commit()
    log.info("payments.reconcile", **out)
    return out


CRON_JOBS = [
    cron(monthly_links, hour={11}, minute={20}, name="payments.monthly_links"),
    cron(reconcile_pending, minute={7, 37}, name="payments.reconcile_pending"),
]
