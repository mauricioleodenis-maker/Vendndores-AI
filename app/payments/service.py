"""Cobros con Wompi: intents por cobro, reconciliacion y envio del link por WhatsApp."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import service as billing_service
from app.channels.sender import send_whatsapp_template
from app.core.clock import utcnow
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.db.models.payments import PaymentIntent
from app.db.models.plans import BillingRecord
from app.db.models.tenants import Tenant
from app.payments.client import WompiClient, WompiError

log = get_logger(__name__)

PENDING_AFTER = timedelta(minutes=30)
PAYABLE_STATUSES = ("pendiente", "vencido")
_METHOD_MAP = {"NEQUI": "nequi", "PSE": "pse", "DAVIPLATA": "daviplata"}


class PaymentJobSettings(BaseSettings):
    """Ajustes del cobro mensual (env ``VAI_WOMPI_*``)."""

    model_config = SettingsConfigDict(env_prefix="VAI_WOMPI_", extra="ignore")

    redirect_url: str = "http://localhost:8000/pago/gracias"
    link_template_sid: str = ""  # ContentSid Twilio de la plantilla "link de pago"


async def create_payment_for_billing_record(
    session: AsyncSession,
    client: WompiClient,
    billing_record_id: uuid.UUID,
    *,
    redirect_url: str,
    customer_email: str | None = None,
) -> PaymentIntent:
    """Crea (o reutiliza el pendiente de) un intent con link de pago para un cobro."""
    record = await session.get(BillingRecord, billing_record_id)
    if record is None:
        raise NotFoundError("Cobro no encontrado")
    if record.status not in PAYABLE_STATUSES:
        raise ConflictError("El cobro no admite pago en línea")
    if record.amount_cop <= 0:
        raise AppError("invalid_amount", "Monto no cobrable", 422)
    existing = (
        (
            await session.execute(
                select(PaymentIntent)
                .where(
                    PaymentIntent.billing_record_id == record.id,
                    PaymentIntent.status == "pending",
                    PaymentIntent.link_url.is_not(None),
                )
                .order_by(PaymentIntent.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    attempts = (
        await session.execute(
            select(func.count())
            .select_from(PaymentIntent)
            .where(PaymentIntent.billing_record_id == record.id)
        )
    ).scalar_one()
    reference = f"vai-{record.id.hex}-{attempts + 1}"
    total = record.amount_cop
    link = await client.create_payment_link(
        reference,
        total,
        f"Mensualidad {record.period or ''}".strip(),
        redirect_url,
        customer_email,
    )
    intent = PaymentIntent(
        billing_record_id=record.id,
        reference=reference,
        provider="wompi",
        amount_cop=total,
        status="pending",
        link_url=link.url,
        raw_last_event={
            "link_id": link.id,
            "integrity_signature": client.integrity_signature(reference, total * 100),
        },
    )
    session.add(intent)
    await session.flush()
    return intent


async def reconcile_pending(
    session: AsyncSession,
    client: WompiClient,
    *,
    now: datetime | None = None,
    older_than: timedelta = PENDING_AFTER,
) -> dict[str, int]:
    """Consulta a Wompi los intents pendientes > 30 min que ya tienen transaccion."""
    cutoff = (now or utcnow()) - older_than
    intents = (
        (
            await session.execute(
                select(PaymentIntent)
                .where(
                    PaymentIntent.status == "pending",
                    PaymentIntent.created_at < cutoff,
                    PaymentIntent.provider_tx_id.is_not(None),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    out = {"checked": 0, "approved": 0, "updated": 0, "errors": 0}
    for intent in intents:
        out["checked"] += 1
        try:
            tx = await client.get_transaction(intent.provider_tx_id or "")
        except WompiError:
            out["errors"] += 1
            continue
        if tx.reference != intent.reference or tx.status == "PENDING":
            continue
        status = tx.status.lower()
        if status == "approved" and tx.amount_in_cents != intent.amount_cop * 100:
            status = "error"
        intent.status = status
        intent.raw_last_event = {
            **(intent.raw_last_event or {}),
            "reconciled": {"id": tx.id, "status": tx.status},
        }
        out["updated"] += 1
        if status == "approved":
            await billing_service.mark_paid(
                session,
                intent.billing_record_id,
                actor=None,
                method=_METHOD_MAP.get((tx.payment_method_type or "").upper(), "otro"),
                reference=f"wompi:{tx.id}",
            )
            out["approved"] += 1
    await session.flush()
    return out


async def send_payment_link(
    session: AsyncSession, intent: PaymentIntent, *, content_sid: str
) -> str:
    """Envia el link al contacto del tenant (plantilla WhatsApp; Twilio dry-run lo simula)."""
    record = await session.get(BillingRecord, intent.billing_record_id)
    tenant = await session.get(Tenant, record.tenant_id) if record else None
    if tenant is None or not tenant.phone_contact:
        return "skipped_no_contact"
    if not content_sid or not intent.link_url:
        return "skipped_no_template"
    res = await send_whatsapp_template(
        session,
        tenant_id=None,
        to_e164=tenant.phone_contact,
        content_sid=content_sid,
        variables={
            "1": tenant.owner_name or tenant.name,
            "2": f"{intent.amount_cop:,}".replace(",", "."),
            "3": intent.link_url,
        },
    )
    return "sent_dry_run" if res.ok and res.dry_run else ("sent" if res.ok else "failed")


async def create_links_for_new_monthly(
    session: AsyncSession,
    client: WompiClient,
    *,
    redirect_url: str,
    content_sid: str = "",
    period: str | None = None,
) -> dict[str, int]:
    """Crea link y notifica por cada mensualidad pendiente que aun no tiene intent."""
    has_intent = select(PaymentIntent.id).where(PaymentIntent.billing_record_id == BillingRecord.id)
    q = select(BillingRecord.id).where(
        BillingRecord.kind == "mensualidad",
        BillingRecord.status == "pendiente",
        BillingRecord.amount_cop > 0,
        ~has_intent.exists(),
    )
    if period:
        q = q.where(BillingRecord.period == period)
    ids = (await session.execute(q)).scalars().all()
    out = {"created": 0, "sent": 0, "failed": 0}
    for rid in ids:
        try:
            intent = await create_payment_for_billing_record(
                session, client, rid, redirect_url=redirect_url
            )
        except (WompiError, AppError) as exc:
            out["failed"] += 1
            log.warning("wompi link failed record=%s error=%s", rid, type(exc).__name__)
            continue
        out["created"] += 1
        if (await send_payment_link(session, intent, content_sid=content_sid)).startswith("sent"):
            out["sent"] += 1
    return out
