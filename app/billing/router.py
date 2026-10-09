"""UI Facturacion (/admin/facturacion) y resumen JSON (/api/billing/summary)."""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.billing import service
from app.core.deps import get_session, require_role
from app.db.models.plans import BILLING_STATUSES, PAYMENT_METHODS
from app.db.models.users import User
from app.web.templating import render

router = APIRouter(tags=["facturacion"])
admin = require_role("admin")


def _redirect(ok: str | None = None, error: str | None = None) -> RedirectResponse:
    url = "/admin/facturacion"
    if ok:
        url += f"?ok={quote(ok)}"
    elif error:
        url += f"?error={quote(error)}"
    return RedirectResponse(url, status_code=303)


def _status(value: str | None) -> str | None:
    return value if value in BILLING_STATUSES else None


@router.get("/admin/facturacion", response_class=HTMLResponse)
async def billing_page(
    request: Request,
    estado: str | None = Query(default=None, max_length=20),
    user: User = Depends(admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    estado = _status(estado)
    rows = await service.list_records(session, status=estado)
    flash = None
    if msg := request.query_params.get("ok"):
        flash = {"kind": "ok", "message": msg[:200]}
    elif msg := request.query_params.get("error"):
        flash = {"kind": "error", "message": msg[:200]}
    return render(
        request,
        "billing/facturacion.html",
        {
            "rows": rows,
            "summary": await service.summary(session),
            "estado": estado,
            "estados": BILLING_STATUSES,
            "methods": PAYMENT_METHODS,
            "flash": flash,
        },
    )


@router.post("/admin/facturacion/{record_id}/pagar")
async def pay(
    record_id: uuid.UUID,
    metodo: Annotated[str, Form(max_length=20)] = "",
    referencia: Annotated[str, Form(max_length=120)] = "",
    factura: Annotated[str, Form(max_length=60)] = "",
    user: User = Depends(admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.mark_paid(
        session,
        record_id,
        actor=user,
        method=metodo or None,
        reference=referencia,
        external_invoice_no=factura or None,
    )
    return _redirect(ok="Cobro marcado como pagado")


@router.post("/admin/facturacion/{record_id}/anular")
async def void(
    record_id: uuid.UUID,
    user: User = Depends(admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.void_record(session, record_id, actor=user)
    return _redirect(ok="Cobro anulado")


@router.post("/admin/facturacion/generar")
async def generate(
    user: User = Depends(admin), session: AsyncSession = Depends(get_session)
) -> Response:
    created = await service.generate_monthly_records(session)
    await service.mark_overdue(session)
    await log_event(
        session,
        actor=user,
        action="billing.generate",
        entity_type="billing",
        diff={"created": created},
    )
    return _redirect(ok=f"Mensualidades generadas: {created}")


@router.get("/admin/facturacion/exportar.csv")
async def export(
    request: Request,
    estado: str | None = Query(default=None, max_length=20),
    user: User = Depends(admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows = await service.list_records(session, status=_status(estado), limit=1000)
    await log_event(
        session,
        actor=user,
        action="billing.export",
        entity_type="billing",
        diff={"rows": len(rows), "estado": _status(estado) or "todos"},
        ip=request.client.host if request.client else None,
    )
    return Response(
        service.export_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="facturacion.csv"'},
    )


@router.get("/api/billing/summary")
async def api_summary(
    user: User = Depends(admin), session: AsyncSession = Depends(get_session)
) -> dict[str, int]:
    s = await service.summary(session)
    return {
        "mrr_cop": s.mrr_cop,
        "setups_month_cop": s.setups_month_cop,
        "overdue_cop": s.overdue_cop,
        "overdue_count": s.overdue_count,
        "pending_cop": s.pending_cop,
        "active_subscriptions": s.active_subscriptions,
    }
