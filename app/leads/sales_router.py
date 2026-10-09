"""Rutas de ventas: etapa, prueba secreta, demo en un clic, conversion y chat publico de demo."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session, require_role
from app.core.errors import AppError
from app.db.models.leads import Lead, SecretShopTest
from app.db.models.users import User
from app.leads import demo, pipeline, secret_shop
from app.web.templating import render

router = APIRouter(tags=["leads-ventas"])
operator = require_role("admin", "operator")
admin_only = require_role("admin")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SecretShopIn(_In):
    scenario: Annotated[str, Field(max_length=20)] = "precio"
    channel: Annotated[str, Field(max_length=20)] = "whatsapp"
    sent_at: datetime | None = None
    message_text: Annotated[str, Field(max_length=1000)] = ""


class ReplyIn(_In):
    first_reply_at: datetime
    excerpt: Annotated[str, Field(max_length=1000)] = ""
    closed_booking: bool = False


class ConvertIn(_In):
    plan_code: Annotated[str, Field(min_length=1, max_length=40)]
    offer_code: Annotated[str | None, Field(max_length=40)] = None
    include_iva: bool = False


def _test_out(t: SecretShopTest) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "lead_id": str(t.lead_id),
        "scenario": t.scenario,
        "channel": t.channel,
        "sent_at": t.sent_at.isoformat(),
        "message_text": t.message_text,
        "first_reply_at": t.first_reply_at.isoformat() if t.first_reply_at else None,
        "response_seconds": t.response_seconds,
        "after_hours": t.after_hours,
        "outcome": t.outcome,
    }


def _demo_out(r: demo.DemoResult) -> dict[str, Any]:
    return {
        "tenant_id": str(r.tenant_id),
        "bot_config_id": str(r.bot_config_id) if r.bot_config_id else None,
        "demo_link": r.demo_link,
        "whatsapp_link": r.whatsapp_link,
        "expires_at": r.expires_at.isoformat(),
        "slug": r.slug,
        "reused": r.reused,
    }


def _convert_out(r: demo.ConvertResult) -> dict[str, Any]:
    return {
        "tenant_id": str(r.tenant_id),
        "subscription_id": str(r.subscription_id),
        "plan_code": r.plan_code,
        "setup_net_cop": r.setup_net_cop,
        "billing_record_created": r.billing_record_created,
    }


# ------------------------------------------------------------------ API JSON
@router.get("/api/leads/{lead_id}/secret-shop/script")
async def api_script(
    lead_id: uuid.UUID,
    scenario: str = Query("precio", max_length=20),
    _user: User = Depends(operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    lead = await pipeline.get_lead(session, lead_id)
    return {"scenario": scenario, "script": secret_shop.suggested_script(scenario, lead.niche)}


@router.post("/api/leads/{lead_id}/secret-shop", status_code=201)
async def api_create_secret_shop(
    lead_id: uuid.UUID,
    body: SecretShopIn,
    user: User = Depends(operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    test = await secret_shop.create_test(session, lead_id, actor=user, **body.model_dump())
    return _test_out(test)


@router.post("/api/secret-shop/{test_id}/reply")
async def api_secret_shop_reply(
    test_id: uuid.UUID,
    body: ReplyIn,
    user: User = Depends(operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    test = await secret_shop.record_reply(
        session,
        test_id,
        first_reply_at=body.first_reply_at,
        excerpt=body.excerpt,
        closed_booking=body.closed_booking,
        actor=user,
    )
    return _test_out(test)


@router.get("/api/leads/{lead_id}/pitch-evidence")
async def api_pitch_evidence(
    lead_id: uuid.UUID,
    _user: User = Depends(operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await secret_shop.pitch_evidence(session, lead_id)


@router.post("/api/leads/{lead_id}/demo-bot")
async def api_demo_bot(
    lead_id: uuid.UUID,
    rebuild: bool = Query(False),
    niche: str | None = Query(None, max_length=30),
    user: User = Depends(operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    result = await demo.lead_to_demo_bot(session, lead_id, actor=user, rebuild=rebuild, niche=niche)
    return _demo_out(result)


@router.post("/api/leads/{lead_id}/convert")
async def api_convert(
    lead_id: uuid.UUID,
    body: ConvertIn,
    user: User = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return _convert_out(await demo.convert_lead(session, lead_id, actor=user, **body.model_dump()))


# ------------------------------------------------------------------ UI parciales (HTMX)
async def _actions(
    request: Request,
    session: AsyncSession,
    lead: Lead,
    *,
    message: str = "",
    error: str = "",
    demo_result: demo.DemoResult | None = None,
    convert_result: demo.ConvertResult | None = None,
) -> HTMLResponse:
    test = await secret_shop.latest_test(session, lead.id)
    ctx = {
        "lead": lead,
        "test": test,
        "evidence": await secret_shop.pitch_evidence(session, lead.id) if test else None,
        "message": message,
        "error": error,
        "demo": demo_result,
        "converted": convert_result,
        "needs_niche": lead.niche not in demo.DEMO_NICHES,
        "niches": demo.DEMO_NICHES,
        "scenarios": secret_shop.SCENARIOS,
        "script": secret_shop.suggested_script("precio", lead.niche),
        "can_convert": lead.demo_tenant_id is not None and lead.converted_tenant_id is None,
    }
    return render(request, "leads_sales/_acciones.html", ctx)


async def _guarded(
    request: Request, session: AsyncSession, lead_id: uuid.UUID, fn: Any, ok: str
) -> HTMLResponse:
    """Ejecuta ``fn`` y devuelve el parcial; los errores de negocio se muestran, no se propagan."""
    lead = await pipeline.get_lead(session, lead_id)
    try:
        async with session.begin_nested():
            result = await fn()
    except AppError as exc:
        return await _actions(request, session, lead, error=exc.message)
    kwargs: dict[str, Any] = {"message": ok}
    if isinstance(result, demo.DemoResult):
        kwargs["demo_result"] = result
    if isinstance(result, demo.ConvertResult):
        kwargs["convert_result"] = result
    return await _actions(request, session, lead, **kwargs)


@router.get("/admin/leads/{lead_id}/ventas", response_class=HTMLResponse)
async def ui_actions(
    request: Request,
    lead_id: uuid.UUID,
    _user: User = Depends(operator),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    return await _actions(request, session, await pipeline.get_lead(session, lead_id))


@router.post("/admin/leads/{lead_id}/ventas/prueba", response_class=HTMLResponse)
async def ui_secret_shop(
    request: Request,
    lead_id: uuid.UUID,
    scenario: str = Form("precio", max_length=20),
    channel: str = Form("whatsapp", max_length=20),
    message_text: str = Form("", max_length=1000),
    user: User = Depends(operator),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    async def run() -> SecretShopTest:
        return await secret_shop.create_test(
            session,
            lead_id,
            actor=user,
            scenario=scenario,
            channel=channel,
            message_text=message_text,
        )

    return await _guarded(request, session, lead_id, run, "Prueba secreta registrada.")


@router.post("/admin/leads/{lead_id}/ventas/respuesta", response_class=HTMLResponse)
async def ui_reply(
    request: Request,
    lead_id: uuid.UUID,
    first_reply_at: datetime = Form(...),
    excerpt: str = Form("", max_length=1000),
    closed_booking: bool = Form(False),
    user: User = Depends(operator),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    async def run() -> SecretShopTest:
        test = await secret_shop.latest_test(session, lead_id)
        if test is None:
            raise AppError("no_test", "Aún no hay prueba secreta", 404)
        return await secret_shop.record_reply(
            session,
            test.id,
            first_reply_at=first_reply_at,
            excerpt=excerpt,
            closed_booking=closed_booking,
            actor=user,
        )

    return await _guarded(request, session, lead_id, run, "Respuesta registrada.")


@router.post("/admin/leads/{lead_id}/ventas/demo", response_class=HTMLResponse)
async def ui_demo(
    request: Request,
    lead_id: uuid.UUID,
    niche: str = Form("", max_length=30),
    rebuild: bool = Form(False),
    user: User = Depends(operator),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    async def run() -> demo.DemoResult:
        return await demo.lead_to_demo_bot(
            session, lead_id, actor=user, rebuild=rebuild, niche=niche or None
        )

    return await _guarded(request, session, lead_id, run, "Demo lista. Comparte el enlace.")


@router.post("/admin/leads/{lead_id}/ventas/convertir", response_class=HTMLResponse)
async def ui_convert(
    request: Request,
    lead_id: uuid.UUID,
    plan_code: str = Form(..., max_length=40),
    offer_code: str = Form("", max_length=40),
    include_iva: bool = Form(False),
    user: User = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    async def run() -> demo.ConvertResult:
        return await demo.convert_lead(
            session,
            lead_id,
            plan_code=plan_code,
            offer_code=offer_code or None,
            include_iva=include_iva,
            actor=user,
        )

    return await _guarded(request, session, lead_id, run, "Lead convertido en cliente.")


# ------------------------------------------------------------------ chat publico de demo
def _chat(
    request: Request,
    token: str,
    name: str,
    history: list[dict[str, str]],
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    return render(
        request,
        "leads_sales/demo_chat.html"
        if request.headers.get("HX-Request") != "true"
        else "leads_sales/_demo_chat.html",
        {
            "token": token,
            "business_name": name,
            "history": history,
            "history_json": json.dumps(history, ensure_ascii=False),
            "error": error,
            "notice": demo.DEMO_NOTICE,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
    )


@router.get("/demo/{token}", response_class=HTMLResponse)
async def demo_page(
    request: Request, token: str, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    tenant = await demo.load_demo_tenant(session, token)
    return _chat(request, token, tenant.name, [])


@router.post("/demo/{token}/mensaje", response_class=HTMLResponse)
async def demo_message(
    request: Request,
    token: str,
    text: str = Form("", max_length=1000),
    history: str = Form("[]", max_length=20000),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    tenant = await demo.load_demo_tenant(session, token)
    try:
        parsed = json.loads(history)
    except ValueError:
        parsed = []
    turns = demo.clean_history(parsed)
    error = ""
    try:
        reply = await demo.demo_chat_reply(session, token, turns, text)
        turns = [*turns, {"role": "user", "content": text.strip()[: demo.MAX_TEXT]}]
        turns.append({"role": "assistant", "content": reply})
        turns = turns[-demo.MAX_HISTORY :]
    except AppError as exc:
        error = (
            "Se alcanzó el límite de mensajes de esta demostración."
            if exc.status == 429
            else exc.message
        )
    return _chat(request, token, tenant.name, turns, error)
