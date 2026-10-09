"""UI de Recordatorios (/admin/recordatorios): ver, cancelar y reintentar envios programados."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.core.deps import current_user, get_session
from app.core.errors import NotFoundError
from app.db.models.contacts import Contact
from app.db.models.scheduling import ScheduledJob
from app.db.models.tenants import Tenant
from app.db.models.users import User
from app.reminders import policies as pol
from app.reminders.service import _contact_name
from app.web.templating import render

router = APIRouter(tags=["recordatorios"])

PAGE_SIZE = 25
KIND_LABELS = {
    pol.REMINDER_24H: "Recordatorio 24 h antes",
    pol.REMINDER_2H: "Recordatorio 2 h antes",
    pol.FOLLOWUP_NOSHOW: "Seguimiento por inasistencia",
    pol.FOLLOWUP_UNBOOKED: "Seguimiento sin cita",
}
STATUS_LABELS = {
    "pending": "Programado",
    "running": "Enviando",
    "done": "Enviado",
    "failed": "Fallido",
    "cancelled": "No enviado",
}
STATUS_TONE = {"pending": "info", "running": "warn", "done": "ok", "failed": "danger"}
FILTERS = (("", "Todos"), ("pending", "Programados"), ("done", "Enviados"),
           ("failed", "Fallidos"), ("cancelled", "No enviados"))  # fmt: skip
REASONS = {
    "skipped_tenant_inactivo": "La empresa no está activa.",
    "skipped_sin_contacto": "El contacto ya no existe o fue eliminado.",
    "skipped_opt_out": "El cliente no quiere recibir mensajes.",
    "skipped_sin_cita": "La cita ya no existe.",
    "skipped_cita_no_activa": "La cita ya no está activa.",
    "skipped_cita_pasada": "La cita ya pasó.",
    "skipped_ya_enviado": "Este aviso ya se había enviado.",
    "skipped_cita_reprogramada": "La cita se reprogramó; se creó un aviso nuevo.",
    "skipped_ya_agendo": "El cliente ya tiene una cita agendada.",
    "skipped_plan_sin_seguimientos": "Tu plan no incluye seguimientos.",
    "skipped_horario_silencio": "No se envía de noche y la cita llega antes de las 8:00 a. m.",
    "skipped_telefono_ilegible": "No se pudo leer el teléfono del cliente.",
    "skipped_ventana_cerrada": "Pasaron más de 24 h desde su último mensaje.",
    "skipped_no_template": "Falta una plantilla de WhatsApp aprobada.",
    "skipped_suprimido": "El número está en la lista de bloqueo.",
    "cita_cancelada_o_reprogramada": "La cita se canceló o se reprogramó.",
}


def _redirect(url: str, *, ok: str | None = None, error: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if ok:
        url += f"{sep}ok={quote(ok)}"
    elif error:
        url += f"{sep}error={quote(error)}"
    return RedirectResponse(url, status_code=303)


def _reason(code: str | None) -> str:
    if not code:
        return ""
    return REASONS.get(code, "No se pudo enviar el mensaje. Revisa el número y la plantilla.")


async def _pick_tenant(
    session: AsyncSession, tenant_id: uuid.UUID | None
) -> tuple[Tenant | None, list[Tenant]]:
    tenants = list(
        (
            await session.execute(
                select(Tenant).where(Tenant.deleted_at.is_(None)).order_by(Tenant.name)
            )
        ).scalars()
    )
    if tenant_id is not None:
        chosen = next((t for t in tenants if t.id == tenant_id), None)
        if chosen is None:
            raise NotFoundError("Empresa no encontrada")
        return chosen, tenants
    return (tenants[0] if tenants else None), tenants


@router.get("/admin/recordatorios", response_class=HTMLResponse)
async def recordatorios_page(
    request: Request,
    tenant_id: uuid.UUID | None = Query(None),
    estado: str = Query("", max_length=12),
    page: int = Query(1, ge=1, le=10_000),
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    tenant, tenants = await _pick_tenant(session, tenant_id)
    flash: dict[str, str] | None = None
    if msg := request.query_params.get("ok"):
        flash = {"kind": "ok", "message": msg[:200]}
    elif msg := request.query_params.get("error"):
        flash = {"kind": "error", "message": msg[:200]}
    ctx: dict[str, Any] = {"tenants": tenants, "tenant": tenant, "flash": flash}
    if tenant is None:
        return render(request, "reminders/index.html", ctx)

    estado = estado if estado in STATUS_LABELS else ""
    base = ScheduledJob.tenant_id == tenant.id, ScheduledJob.kind.in_(tuple(KIND_LABELS))
    query = select(ScheduledJob).where(*base)
    if estado:
        query = query.where(ScheduledJob.status == estado)
    jobs = list(
        (
            await session.execute(
                query.order_by(ScheduledJob.run_at.desc())
                .offset((page - 1) * PAGE_SIZE)
                .limit(PAGE_SIZE + 1)
            )
        ).scalars()
    )
    has_more = len(jobs) > PAGE_SIZE
    jobs = jobs[:PAGE_SIZE]
    counts = {
        status: n
        for status, n in (
            await session.execute(
                select(ScheduledJob.status, func.count()).where(*base).group_by(ScheduledJob.status)
            )
        ).all()
    }
    ids = {j.contact_id for j in jobs if j.contact_id}
    contacts: dict[uuid.UUID, Contact] = {}
    if ids:
        rows = await session.execute(
            select(Contact).where(Contact.tenant_id == tenant.id, Contact.id.in_(ids))
        )
        contacts = {c.id: c for c in rows.scalars()}
    now = utcnow()
    rows_ctx = []
    for j in jobs:
        c = contacts.get(j.contact_id) if j.contact_id else None
        rows_ctx.append(
            {
                "id": j.id,
                "kind": KIND_LABELS.get(j.kind, j.kind),
                "run_at": j.run_at,
                "status": j.status,
                "status_label": STATUS_LABELS.get(j.status, j.status),
                "tone": STATUS_TONE.get(j.status, "neutral"),
                "contact": _contact_name(tenant.id, c) if c else "Sin contacto",
                "reason": _reason(j.last_error),
                "attempts": j.attempts,
                "can_cancel": j.status == "pending",
                "can_retry": j.status == "failed",
            }
        )
    ctx.update(
        rows=rows_ctx,
        estado=estado,
        page=page,
        has_more=has_more,
        filters=FILTERS,
        counts=counts,
        now=now,
        pager_url=f"/admin/recordatorios?tenant_id={tenant.id}&estado={quote(estado)}",
    )
    return render(request, "reminders/index.html", ctx)


async def _mutate(
    session: AsyncSession, job_id: uuid.UUID, tenant_id: uuid.UUID, *, retry: bool
) -> bool:
    now: datetime = utcnow()
    if retry:
        stmt = (
            update(ScheduledJob)
            .where(ScheduledJob.id == job_id, ScheduledJob.status == "failed")
            .values(status="pending", attempts=0, last_error=None, next_attempt_at=None, run_at=now)
        )
    else:
        stmt = (
            update(ScheduledJob)
            .where(ScheduledJob.id == job_id, ScheduledJob.status == "pending")
            .values(status="cancelled", last_error="cancelado_manual")
        )
    res = await session.execute(
        stmt.where(ScheduledJob.tenant_id == tenant_id, ScheduledJob.kind.in_(tuple(KIND_LABELS)))
    )
    return bool(res.rowcount)  # type: ignore[attr-defined]


@router.post("/admin/recordatorios/{job_id}/cancelar")
async def cancelar_recordatorio(
    job_id: uuid.UUID,
    tenant_id: Annotated[uuid.UUID, Form()],
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    back = f"/admin/recordatorios?tenant_id={tenant_id}"
    if await _mutate(session, job_id, tenant_id, retry=False):
        return _redirect(back, ok="Recordatorio cancelado. No se enviará.")
    return _redirect(back, error="Ese recordatorio ya no se puede cancelar.")


@router.post("/admin/recordatorios/{job_id}/reintentar")
async def reintentar_recordatorio(
    job_id: uuid.UUID,
    tenant_id: Annotated[uuid.UUID, Form()],
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    back = f"/admin/recordatorios?tenant_id={tenant_id}&estado=failed"
    if await _mutate(session, job_id, tenant_id, retry=True):
        return _redirect(back, ok="Listo, se volverá a intentar en los próximos minutos.")
    return _redirect(back, error="Ese recordatorio no se puede reintentar.")
