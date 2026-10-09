"""Pagina /admin/voz: llamadas por negocio y ajustes de voz (solo owner/admin de la agencia)."""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.core.config import get_settings
from app.core.deps import get_session, require_role, verify_csrf
from app.db.models.tenants import ChannelAccount, Tenant, TenantProfile
from app.db.models.users import User
from app.db.models.voice import CALL_STATUSES, CallSession
from app.plans.entitlements import get_entitlements
from app.web.templating import render

admin_router = APIRouter(tags=["voz"])
admin = require_role("admin")

PAGE_SIZE = 25
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
STATUS_LABELS: dict[str, tuple[str, str]] = {
    "active": ("En curso", "info"),
    "transferred": ("Transferida", "warn"),
    "completed": ("Completada", "ok"),
    "rejected": ("Rechazada", "neutral"),
}


def _redirect(tenant_id: uuid.UUID | None = None, **flash: str) -> RedirectResponse:
    from urllib.parse import urlencode

    params = {k: v[:200] for k, v in flash.items()}
    if tenant_id:
        params["negocio"] = str(tenant_id)
    q = urlencode(params)
    return RedirectResponse("/admin/voz" + (f"?{q}" if q else ""), status_code=303)


@admin_router.get("/admin/voz", response_class=HTMLResponse)
async def voice_page(
    request: Request,
    negocio: uuid.UUID | None = Query(default=None),
    estado: str | None = Query(default=None, max_length=20),
    page: int = Query(default=1, ge=1, le=100000),
    user: User = Depends(admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    estado = estado if estado in CALL_STATUSES else None

    # resumen por negocio con canal de voz o llamadas
    counts = dict(
        (
            await session.execute(
                select(CallSession.tenant_id, func.count()).group_by(CallSession.tenant_id)
            )
        ).all()
    )
    voice_accounts = {
        a.tenant_id: a
        for a in (
            await session.execute(select(ChannelAccount).where(ChannelAccount.channel == "voice"))
        )
        .scalars()
        .all()
    }
    tenant_ids = set(counts) | set(voice_accounts)
    tenants = (
        (
            await session.execute(
                select(Tenant)
                .where(Tenant.id.in_(tenant_ids), Tenant.deleted_at.is_(None))
                .order_by(Tenant.name)
            )
        )
        .scalars()
        .all()
        if tenant_ids
        else []
    )
    summary: list[dict[str, Any]] = []
    for t in tenants:
        ent = await get_entitlements(session, t.id)
        profile = await session.get(TenantProfile, t.id)
        acc = voice_accounts.get(t.id)
        summary.append(
            {
                "tenant": t,
                "calls": counts.get(t.id, 0),
                "number": acc.phone_e164 if acc else None,
                "channel_on": bool(acc and acc.voice_enabled and acc.status == "active"),
                "plan_voice": ent.has_feature("voice"),
                "handoff_phone": (profile.handoff_phone if profile else None) or "",
            }
        )

    q = (
        select(CallSession, Tenant.name)
        .join(Tenant, Tenant.id == CallSession.tenant_id)
        .order_by(CallSession.created_at.desc())
    )
    cq = select(func.count()).select_from(CallSession)
    if negocio:
        q = q.where(CallSession.tenant_id == negocio)
        cq = cq.where(CallSession.tenant_id == negocio)
    if estado:
        q = q.where(CallSession.status == estado)
        cq = cq.where(CallSession.status == estado)
    res = await session.execute(q.limit(PAGE_SIZE + 1).offset((page - 1) * PAGE_SIZE))
    calls = [
        {
            "call": c,
            "tenant_name": n,
            "label": STATUS_LABELS.get(c.status, (c.status, "neutral"))[0],
            "cls": STATUS_LABELS.get(c.status, (c.status, "neutral"))[1],
            # el CallSid no se muestra completo (dato de proveedor)
            "sid_tail": c.call_sid[-6:],
        }
        for c, n in res.all()
    ]
    has_more = len(calls) > PAGE_SIZE
    total = (await session.execute(cq)).scalar_one()
    flash = None
    if msg := request.query_params.get("ok"):
        flash = {"kind": "ok", "message": msg[:200]}
    elif msg := request.query_params.get("error"):
        flash = {"kind": "error", "message": msg[:200]}
    return render(
        request,
        "voice/voz.html",
        {
            "summary": summary,
            "calls": calls[:PAGE_SIZE],
            "has_more": has_more,
            "page": page,
            "total": total,
            "negocio": negocio,
            "estado": estado,
            "estados": [(e, STATUS_LABELS[e][0]) for e in CALL_STATUSES],
            "say_voice": getattr(get_settings(), "voice_say_voice", "") or "Polly.Mia-Neural",
            "flash": flash,
        },
    )


@admin_router.post("/admin/voz/{tenant_id}/ajustes", dependencies=[Depends(verify_csrf)])
async def save_voice_settings(
    tenant_id: uuid.UUID,
    handoff_phone: Annotated[str, Form(max_length=30)] = "",
    channel_enabled: Annotated[str, Form(max_length=5)] = "",
    user: User = Depends(admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or tenant.deleted_at is not None:
        return _redirect(error="Negocio no encontrado")
    phone = re.sub(r"[\s().-]", "", handoff_phone)
    if phone and not _E164.match(phone):
        return _redirect(
            tenant_id, error="El teléfono de transferencia debe estar en formato +57..."
        )
    profile = await session.get(TenantProfile, tenant_id)
    if profile is None:
        profile = TenantProfile(tenant_id=tenant_id)
        session.add(profile)
    before = {"handoff_phone_set": bool(profile.handoff_phone)}
    profile.handoff_phone = phone or None
    diff: dict[str, Any] = {"before": before, "after": {"handoff_phone_set": bool(phone)}}
    acc = (
        await session.execute(
            select(ChannelAccount).where(
                ChannelAccount.tenant_id == tenant_id, ChannelAccount.channel == "voice"
            )
        )
    ).scalar_one_or_none()
    if acc is not None:
        acc.voice_enabled = channel_enabled == "on"
        diff["after"]["channel_enabled"] = acc.voice_enabled
    await log_event(
        session,
        actor=user,
        action="voice.settings_updated",
        entity_type="tenant",
        entity_id=tenant_id,
        tenant_id=tenant_id,
        diff=diff,
    )
    return _redirect(tenant_id, ok="Ajustes de voz guardados")


__all__ = ["admin_router"]
