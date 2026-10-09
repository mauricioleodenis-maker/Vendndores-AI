"""Paginas publicas de privacidad y endpoints DSAR (solo owner/admin)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session, require_role
from app.core.errors import NotFoundError
from app.db.models.tenants import Tenant
from app.db.models.users import User
from app.privacy import dsar
from app.privacy.texts import LEGAL_DRAFT_NOTICE, POLICY_SECTIONS, POLICY_VERSION
from app.web.templating import render

router = APIRouter(tags=["privacidad"])


def _policy_ctx(tenant: Tenant | None) -> dict[str, Any]:
    return {
        "sections": POLICY_SECTIONS,
        "version": POLICY_VERSION,
        "draft_notice": LEGAL_DRAFT_NOTICE,
        "negocio": tenant.name if tenant else None,
    }


@router.get("/privacidad", response_class=HTMLResponse)
async def public_policy(request: Request) -> HTMLResponse:
    return render(request, "privacy/policy.html", _policy_ctx(None))


@router.get("/privacidad/{tenant_slug}", response_class=HTMLResponse)
async def tenant_policy(
    request: Request, tenant_slug: str, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    if len(tenant_slug) > 80:
        raise NotFoundError("Página no encontrada")
    tenant = (
        await session.execute(
            select(Tenant).where(Tenant.slug == tenant_slug, Tenant.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if tenant is None:
        raise NotFoundError("Página no encontrada")
    return render(request, "privacy/policy.html", _policy_ctx(tenant))


@router.get("/api/privacidad/{tenant_id}/contactos/{contact_id}/exportar")
async def export_contact(
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    user: User = Depends(require_role("owner", "admin")),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    data = await dsar.export_contact_data(session, tenant_id, contact_id, actor=user)
    await session.commit()
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@router.post("/api/privacidad/{tenant_id}/contactos/{contact_id}/borrar")
async def erase_contact(
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    user: User = Depends(require_role("owner", "admin")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    await dsar.erase_contact(session, tenant_id, contact_id, actor=user)
    await session.commit()
    return {"status": "erased"}
