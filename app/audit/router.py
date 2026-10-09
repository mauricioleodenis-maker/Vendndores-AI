"""Vista de auditoria (solo owner)."""

from __future__ import annotations

import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service
from app.core.deps import get_session, require_role
from app.db.models.users import User
from app.web.templating import render

router = APIRouter(prefix="/admin/auditoria", tags=["auditoria"])
PAGE_SIZE = 50


@router.get("", response_class=HTMLResponse)
async def audit_list(
    request: Request,
    action: str = Query("", max_length=100),
    tenant_id: str = Query("", max_length=40),
    page: int = Query(1, ge=1, le=10_000),
    _user: User = Depends(require_role()),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    tenant_uuid = None
    tenant_error = False
    try:
        tenant_uuid = uuid.UUID(tenant_id.strip()) if tenant_id.strip() else None
    except ValueError:
        tenant_error = True
    events = (
        []
        if tenant_error
        else await service.list_events(
            session,
            action=action.strip() or None,
            tenant_id=tenant_uuid,
            limit=PAGE_SIZE + 1,
            offset=(page - 1) * PAGE_SIZE,
        )
    )
    params = {k: v for k, v in (("action", action), ("tenant_id", tenant_id)) if v}
    base = "/admin/auditoria" + (f"?{urlencode(params)}" if params else "")
    return render(
        request,
        "audit/list.html",
        {
            "events": events[:PAGE_SIZE],
            "has_more": len(events) > PAGE_SIZE,
            "page": page,
            "action": action,
            "tenant_id": tenant_id,
            "tenant_error": tenant_error,
            "base_url": base,
        },
    )


@router.post("/verificar", response_class=HTMLResponse)
async def verify(
    request: Request,
    _user: User = Depends(require_role()),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    ok, bad_id, checked = await service.verify_chain_counted(session)
    return render(
        request,
        "partials/chain_status.html",
        {"ok": ok, "bad_id": bad_id, "checked": checked},
    )
