"""Rutas de outreach: API JSON (/api) y UI "Campañas" (/admin/campanas)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.core.config import get_settings
from app.core.deps import get_session, require_role
from app.core.errors import AppError, NotFoundError
from app.db.models.outreach import Campaign, MessageTemplate, OutreachMessage
from app.db.models.users import User
from app.outreach import campaigns
from app.outreach import templates as tpl_service
from app.outreach.campaigns import AudienceFilter, CampaignIn
from app.outreach.webhooks import router as webhooks_router
from app.web.templating import render

router = APIRouter(tags=["outreach"])
operator = require_role("admin", "operator")
admin_only = require_role("admin")

ACTIONS = ("pause", "resume", "cancel")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TemplateIn(_In):
    name: Annotated[str, Field(pattern=r"^[a-z0-9_]{3,100}$")]
    kind: Annotated[str, Field(pattern=r"^(pitch|seguimiento|optout_confirm|general)$")]
    category: Annotated[str, Field(pattern=r"^(utility|marketing)$")] = "marketing"
    body: Annotated[str, Field(min_length=10, max_length=1024)]
    variables: Annotated[list[str], Field(max_length=10)]
    niche: Annotated[str | None, Field(max_length=30)] = None


class ApprovalIn(_In):
    content_sid: Annotated[str, Field(pattern=r"^HX[0-9a-fA-F]{32}$")]
    approval_status: Annotated[str, Field(pattern=r"^(submitted|approved|rejected)$")] = "approved"


class StartIn(_In):
    confirm: bool = False


def _template_out(t: MessageTemplate) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "name": t.name,
        "version": t.version,
        "kind": t.kind,
        "category": t.category,
        "body": t.body,
        "variables": t.variables,
        "approval_status": t.approval_status,
        "approved": tpl_service.is_sendable(t),
        "has_content_sid": bool(t.twilio_content_sid),
    }


def _campaign_out(c: Campaign) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "name": c.name,
        "status": c.status,
        "paused_reason": c.paused_reason,
        "template_id": str(c.template_id) if c.template_id else None,
        "daily_limit": c.daily_limit,
        "audience_filter": c.audience_filter,
        "stats": c.stats,
    }


def _message_out(m: OutreachMessage) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "lead_id": str(m.lead_id),
        "step": m.step,
        "status": m.status,
        "template_name": m.template_name,
        "sent_at": m.sent_at.isoformat() if m.sent_at else None,
        "replied_at": m.replied_at.isoformat() if m.replied_at else None,
        "error_code": m.delivery_error_code,
    }


async def _templates(session: AsyncSession) -> list[MessageTemplate]:
    await tpl_service.seed_templates(session)
    stmt = (
        select(MessageTemplate)
        .where(MessageTemplate.scope == "outreach")
        .order_by(MessageTemplate.name, MessageTemplate.version)
    )
    return list((await session.execute(stmt)).scalars())


# --------------------------------------------------------------------------- API plantillas
@router.get("/api/outreach/templates")
async def api_list_templates(
    session: AsyncSession = Depends(get_session), _: User = Depends(operator)
) -> list[dict[str, Any]]:
    return [_template_out(t) for t in await _templates(session)]


@router.post("/api/outreach/templates", status_code=201)
async def api_create_template(
    data: TemplateIn, session: AsyncSession = Depends(get_session), user: User = Depends(admin_only)
) -> dict[str, Any]:
    try:
        tpl_service.validate_template(data.body, data.variables)
    except tpl_service.TemplateRenderError as exc:
        raise AppError("plantilla_invalida", str(exc), 422) from exc
    latest = (
        await session.execute(
            select(MessageTemplate.version)
            .where(MessageTemplate.name == data.name)
            .order_by(MessageTemplate.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    tpl = MessageTemplate(
        scope="outreach",
        name=data.name,
        version=(latest or 0) + 1,
        kind=data.kind,
        category=data.category,
        niche=data.niche,
        body=data.body,
        variables=data.variables,
        approval_status="draft",
        approved=False,
    )
    session.add(tpl)
    await session.flush()
    await log_event(
        session,
        actor=user,
        action="outreach.template_create",
        entity_type="message_template",
        entity_id=tpl.id,
    )
    return _template_out(tpl)


@router.post("/api/outreach/templates/{template_id}/approval")
async def api_set_approval(
    template_id: uuid.UUID,
    data: ApprovalIn,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(admin_only),
) -> dict[str, Any]:
    """Registra el resultado de la aprobacion de Meta (ContentSid HX...)."""
    tpl = await campaigns.get_template(session, template_id)
    tpl.twilio_content_sid = data.content_sid
    tpl.approval_status = data.approval_status
    tpl.approved = data.approval_status == "approved"
    await session.flush()
    await log_event(
        session,
        actor=user,
        action="outreach.template_approval",
        entity_type="message_template",
        entity_id=tpl.id,
        diff={"approval_status": tpl.approval_status},
    )
    return _template_out(tpl)


# --------------------------------------------------------------------------- API campanas
@router.post("/api/campaigns", status_code=201)
async def api_create_campaign(
    data: CampaignIn, session: AsyncSession = Depends(get_session), user: User = Depends(operator)
) -> dict[str, Any]:
    return _campaign_out(await campaigns.create_campaign(session, user, data))


@router.get("/api/campaigns/{campaign_id}")
async def api_get_campaign(
    campaign_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(operator),
) -> dict[str, Any]:
    c = await campaigns.get_campaign(session, campaign_id)
    return {
        **_campaign_out(c),
        "targets": await campaigns.target_counts(session, c.id),
        "messages": await campaigns.message_counts(session, c.id),
    }


@router.get("/api/campaigns/{campaign_id}/messages")
async def api_messages(
    campaign_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(operator),
) -> list[dict[str, Any]]:
    await campaigns.get_campaign(session, campaign_id)
    return [_message_out(m) for m in await campaigns.list_messages(session, campaign_id)]


@router.post("/api/campaigns/{campaign_id}/preview")
async def api_preview(
    campaign_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(operator),
) -> dict[str, Any]:
    return await campaigns.preview(session, campaign_id)


@router.post("/api/campaigns/{campaign_id}/start")
async def api_start(
    campaign_id: uuid.UUID,
    data: StartIn,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(admin_only),
) -> dict[str, Any]:
    return _campaign_out(await campaigns.start(session, campaign_id, user, confirm=data.confirm))


async def _transition(
    action: str, session: AsyncSession, campaign_id: uuid.UUID, user: User
) -> Campaign:
    if action == "pause":
        return await campaigns.pause(session, campaign_id, user)
    if action == "resume":
        return await campaigns.resume(session, campaign_id, user)
    return await campaigns.cancel(session, campaign_id, user)


@router.post("/api/campaigns/{campaign_id}/{action}")
async def api_transition(
    campaign_id: uuid.UUID,
    action: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(admin_only),
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise NotFoundError()
    return _campaign_out(await _transition(action, session, campaign_id, user))


# --------------------------------------------------------------------------- UI
def _redirect(url: str, *, ok: str = "", error: str = "") -> RedirectResponse:
    if ok or error:
        key, val = ("ok", ok) if ok else ("error", error)
        url += f"{'&' if '?' in url else '?'}{key}={quote(val[:200])}"
    return RedirectResponse(url, status_code=303)


def _flash(request: Request) -> dict[str, str] | None:
    """Mensaje de resultado por query (se escapa al renderizar)."""
    if msg := request.query_params.get("error"):
        return {"kind": "error", "message": msg[:200]}
    if msg := request.query_params.get("ok"):
        return {"kind": "ok", "message": msg[:200]}
    return None


@router.get("/admin/campanas", response_class=HTMLResponse)
async def ui_list(
    request: Request, session: AsyncSession = Depends(get_session), _: User = Depends(operator)
) -> HTMLResponse:
    return render(
        request,
        "outreach/list.html",
        {
            "campaigns": await campaigns.list_campaigns(session),
            "enabled": get_settings().outreach_enabled,
            "dry_run": get_settings().twilio_dry_run,
            "flash": _flash(request),
        },
    )


@router.get("/admin/campanas/plantillas", response_class=HTMLResponse)
async def ui_templates(
    request: Request, session: AsyncSession = Depends(get_session), _: User = Depends(operator)
) -> HTMLResponse:
    return render(
        request,
        "outreach/templates.html",
        {"templates": await _templates(session), "flash": _flash(request)},
    )


@router.get("/admin/campanas/nueva", response_class=HTMLResponse)
async def ui_new(
    request: Request, session: AsyncSession = Depends(get_session), _: User = Depends(operator)
) -> HTMLResponse:
    tpls = await _templates(session)
    return render(
        request,
        "outreach/form.html",
        {
            "pitch": [t for t in tpls if t.kind == "pitch"],
            "followups": [t for t in tpls if t.kind == "seguimiento"],
            "flash": _flash(request),
        },
    )


@router.post("/admin/campanas/nueva")
async def ui_create(
    name: Annotated[str, Form()],
    template_id: Annotated[uuid.UUID, Form()],
    followup_template_id: Annotated[str, Form()] = "",
    niche: Annotated[str, Form()] = "",
    city: Annotated[str, Form()] = "",
    min_score: Annotated[int, Form(ge=0, le=100)] = 0,
    stage: Annotated[str, Form()] = "",
    daily_limit: Annotated[int, Form(ge=1, le=80)] = 20,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(operator),
) -> RedirectResponse:
    try:
        data = CampaignIn(
            name=name,
            template_id=template_id,
            followup_template_id=followup_template_id or None,  # type: ignore[arg-type]
            daily_limit=daily_limit,
            audience=AudienceFilter(
                niche=niche or None, city=city or None, min_score=min_score, stage=stage or None
            ),
        )
        campaign = await campaigns.create_campaign(session, user, data)
    except ValidationError:
        return _redirect(
            "/admin/campanas/nueva",
            error="Revisa los datos: el nombre debe tener al menos 3 caracteres y los campos "
            "no pueden exceder su largo.",
        )
    except AppError as exc:
        return _redirect("/admin/campanas/nueva", error=exc.message)
    return _redirect(f"/admin/campanas/{campaign.id}", ok="Borrador creado. Revisa la vista previa.")


@router.get("/admin/campanas/{campaign_id}", response_class=HTMLResponse)
async def ui_detail(
    request: Request,
    campaign_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(operator),
) -> HTMLResponse:
    c = await campaigns.get_campaign(session, campaign_id)
    preview = await campaigns.preview(session, campaign_id) if c.status == "draft" else None
    return render(
        request,
        "outreach/detail.html",
        {
            "c": c,
            "preview": preview,
            "targets": await campaigns.target_counts(session, c.id),
            "messages": await campaigns.list_messages(session, c.id, limit=50),
            "counts": await campaigns.message_counts(session, c.id),
            "can_manage": user.role in ("owner", "admin"),
            "enabled": get_settings().outreach_enabled,
            "dry_run": get_settings().twilio_dry_run,
            "flash": _flash(request),
        },
    )


@router.post("/admin/campanas/{campaign_id}/iniciar")
async def ui_start(
    campaign_id: uuid.UUID,
    confirm: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
    user: User = Depends(admin_only),
) -> RedirectResponse:
    back = f"/admin/campanas/{campaign_id}"
    try:
        await campaigns.start(session, campaign_id, user, confirm=confirm == "si")
    except NotFoundError:
        raise
    except AppError as exc:
        await session.rollback()
        return _redirect(back, error=exc.message)
    return _redirect(back, ok="Campaña iniciada. Los envíos respetan horario y límite diario.")


@router.post("/admin/campanas/{campaign_id}/{action}")
async def ui_transition(
    campaign_id: uuid.UUID,
    action: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(admin_only),
) -> RedirectResponse:
    if action not in ACTIONS:
        raise NotFoundError()
    back = f"/admin/campanas/{campaign_id}"
    try:
        await _transition(action, session, campaign_id, user)
    except NotFoundError:
        raise
    except AppError as exc:
        await session.rollback()
        return _redirect(back, error=exc.message)
    done = {"pause": "Campaña pausada.", "resume": "Campaña reanudada.", "cancel": "Campaña cancelada."}
    return _redirect(back, ok=done[action])


routers = [router, webhooks_router]
