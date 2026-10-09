"""UI de pagos Wompi: /admin/pagos (admin) y pagina publica /pagos/retorno."""

from __future__ import annotations

import re
import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.core.config import get_settings
from app.core.deps import get_session, require_role, verify_csrf
from app.core.errors import AppError
from app.db.models.payments import PAYMENT_INTENT_STATUSES, PaymentIntent
from app.db.models.plans import BillingRecord
from app.db.models.tenants import Tenant
from app.db.models.users import User
from app.payments import service
from app.payments.client import WompiClient, WompiError
from app.payments.settings import WompiSettings, get_wompi_settings
from app.web.templating import render

router = APIRouter(tags=["pagos"])
admin = require_role("admin")

PAGE_SIZE = 25
_TX_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

INTENT_LABELS: dict[str, tuple[str, str]] = {
    "pending": ("Pendiente", "warn"),
    "approved": ("Aprobado", "ok"),
    "declined": ("Rechazado", "danger"),
    "voided": ("Anulado", "neutral"),
    "error": ("Con error", "danger"),
}

# Mensaje publico por estado: nunca expone montos, referencias ni datos del cliente.
RETURN_MESSAGES: dict[str, tuple[str, str]] = {
    "approved": ("Pago recibido", "Gracias, tu pago fue aprobado."),
    "declined": (
        "Pago rechazado",
        "Tu pago no fue aprobado. Puedes intentarlo de nuevo con el mismo link.",
    ),
    "voided": ("Pago anulado", "Este pago fue anulado."),
    "error": (
        "No pudimos confirmar el pago",
        "Hubo un problema. Escríbenos y lo revisamos contigo.",
    ),
    "pending": (
        "Estamos confirmando tu pago",
        "Aún no recibimos la confirmación del banco. Puede tardar unos minutos.",
    ),
}


def get_wompi_client(
    settings: WompiSettings = Depends(get_wompi_settings),
) -> WompiClient | None:
    return WompiClient.from_settings(settings) if settings.enabled else None


def _redirect(ok: str | None = None, error: str | None = None) -> RedirectResponse:
    url = "/admin/pagos"
    if ok:
        url += f"?ok={quote(ok)}"
    elif error:
        url += f"?error={quote(error)}"
    return RedirectResponse(url, status_code=303)


@router.get("/admin/pagos", response_class=HTMLResponse)
async def payments_page(
    request: Request,
    estado: str | None = Query(default=None, max_length=20),
    page: int = Query(default=1, ge=1, le=100000),
    user: User = Depends(admin),
    session: AsyncSession = Depends(get_session),
    settings: WompiSettings = Depends(get_wompi_settings),
) -> Response:
    estado = estado if estado in PAYMENT_INTENT_STATUSES else None
    base = (
        select(PaymentIntent, BillingRecord, Tenant.name)
        .join(BillingRecord, BillingRecord.id == PaymentIntent.billing_record_id)
        .join(Tenant, Tenant.id == BillingRecord.tenant_id)
    )
    count_q = select(func.count()).select_from(PaymentIntent)
    if estado:
        base = base.where(PaymentIntent.status == estado)
        count_q = count_q.where(PaymentIntent.status == estado)
    res = await session.execute(
        base.order_by(PaymentIntent.created_at.desc())
        .limit(PAGE_SIZE + 1)
        .offset((page - 1) * PAGE_SIZE)
    )
    rows = [
        {
            "intent": i,
            "record": r,
            "tenant_name": n,
            "label": INTENT_LABELS.get(i.status, (i.status, "neutral"))[0],
            "cls": INTENT_LABELS.get(i.status, (i.status, "neutral"))[1],
        }
        for i, r, n in res.all()
    ]
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    total = (await session.execute(count_q)).scalar_one()
    has_pending_link = (
        select(PaymentIntent.id)
        .where(
            PaymentIntent.billing_record_id == BillingRecord.id,
            PaymentIntent.status == "pending",
            PaymentIntent.link_url.is_not(None),
        )
        .exists()
    )
    payable = (
        await session.execute(
            select(BillingRecord, Tenant.name)
            .join(Tenant, Tenant.id == BillingRecord.tenant_id)
            .where(
                BillingRecord.status.in_(service.PAYABLE_STATUSES),
                BillingRecord.amount_cop > 0,
                ~has_pending_link,
            )
            .order_by(BillingRecord.due_date.asc())
            .limit(50)
        )
    ).all()
    flash = None
    if msg := request.query_params.get("ok"):
        flash = {"kind": "ok", "message": msg[:200]}
    elif msg := request.query_params.get("error"):
        flash = {"kind": "error", "message": msg[:200]}
    return render(
        request,
        "payments/pagos.html",
        {
            "rows": rows,
            "payable": [{"record": r, "tenant_name": n} for r, n in payable],
            "page": page,
            "has_more": has_more,
            "total": total,
            "estado": estado,
            "estados": [(e, INTENT_LABELS[e][0]) for e in PAYMENT_INTENT_STATUSES],
            "enabled": settings.enabled,
            "flash": flash,
        },
    )


@router.post("/admin/pagos/generar/{record_id}", dependencies=[Depends(verify_csrf)])
async def generate_link(
    record_id: uuid.UUID,
    email: Annotated[str, Form(max_length=254)] = "",
    user: User = Depends(admin),
    session: AsyncSession = Depends(get_session),
    client: WompiClient | None = Depends(get_wompi_client),
) -> Response:
    if client is None:
        return _redirect(error="Wompi no está configurado")
    redirect_url = get_settings().public_base_url.rstrip("/") + "/pagos/retorno"
    try:
        intent = await service.create_payment_for_billing_record(
            session,
            client,
            record_id,
            redirect_url=redirect_url,
            customer_email=email.strip() or None,
        )
    except WompiError:
        return _redirect(error="No se pudo crear el link de pago. Intenta de nuevo.")
    except AppError as exc:
        return _redirect(error=exc.message)
    await log_event(
        session,
        actor=user,
        action="payments.link_created",
        entity_type="payment_intent",
        entity_id=intent.id,
        diff={"billing_record_id": str(record_id)},
    )
    return _redirect(ok="Link de pago generado")


@router.get("/pagos/retorno", response_class=HTMLResponse)
async def payment_return(
    request: Request,
    id: str | None = Query(default=None, max_length=100),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Pagina publica a la que Wompi redirige. Solo muestra un estado generico."""
    status = "pending"
    if id and _TX_ID.match(id):
        found = (
            await session.execute(
                select(PaymentIntent.status).where(PaymentIntent.provider_tx_id == id)
            )
        ).scalar_one_or_none()
        if found in RETURN_MESSAGES:
            status = found
    title, message = RETURN_MESSAGES[status]
    return render(
        request,
        "payments/retorno.html",
        {"status": status, "title": title, "message": message},
        headers={"Cache-Control": "no-store"},
    )


try:  # el webhook vive en su propio modulo; el router sigue funcionando si faltara
    from app.payments.webhooks import webhooks_router

    router.include_router(webhooks_router)
except ModuleNotFoundError as _exc:  # pragma: no cover
    if _exc.name != "app.payments.webhooks":
        raise
