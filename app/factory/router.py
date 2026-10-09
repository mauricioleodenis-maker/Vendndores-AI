"""UI de revision del bot: lado a lado (fuentes vs propuesta), edicion, publicar, rollback, chat."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import LLMClient, get_llm
from app.core.deps import get_session, require_role
from app.core.errors import AppError, NotFoundError
from app.db.models.bots import BotConfig
from app.db.models.tenants import Tenant
from app.db.models.users import User
from app.factory import review, service
from app.factory.grounding import fold
from app.factory.schemas import DAY_KEYS, FactoryInput
from app.web.templating import render

router = APIRouter(tags=["factory"])
Admin = Depends(require_role("admin"))

DAY_LABELS = {
    "mon": "Lunes", "tue": "Martes", "wed": "Miércoles", "thu": "Jueves",
    "fri": "Viernes", "sat": "Sábado", "sun": "Domingo",
}  # fmt: skip
SOURCE_LABELS = {"owner": "Dueño", "template": "Plantilla", "web": "Web"}
MAX_HISTORY = 20
MAX_MSG = 1000
_PRICE_RE = re.compile(r"[\s$.,']")


# --------------------------------------------------------------------------- helpers
async def _tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or tenant.deleted_at is not None:
        raise NotFoundError("Empresa no encontrada")
    return tenant


def _back(
    tenant_id: uuid.UUID,
    *,
    ok: str | None = None,
    error: str | None = None,
    version: int | None = None,
) -> Response:
    url = f"/admin/negocios/{tenant_id}/bot?"
    if version is not None:
        url += f"v={version}&"
    if ok:
        url += f"ok={quote(ok)}"
    if error:
        url += f"error={quote(error)}"
    return RedirectResponse(url.rstrip("?&"), status_code=303)


def _parse_price(raw: str) -> int | None:
    clean = _PRICE_RE.sub("", raw or "")
    if not clean:
        return None
    if not clean.isdigit() or len(clean) > 9:
        raise AppError("invalid_price", "El precio debe ser un número entero en COP", 422)
    return int(clean)


def _source_key(ref: str) -> str:
    return "web" if ref.startswith("web") else ref if ref in SOURCE_LABELS else "owner"


async def _page_context(
    session: AsyncSession, tenant: Tenant, version: int | None
) -> dict[str, Any]:
    versions = await review.list_versions(session, tenant.id)
    selected: BotConfig | None = None
    if version is not None:
        selected = next((v for v in versions if v.version == version), None)
    if selected is None:
        selected = next((v for v in versions if v.status == "draft"), None) or next(
            (v for v in versions if v.status == "published"), None
        )
    ctx: dict[str, Any] = {"tenant": tenant, "versions": versions, "bot": selected}
    if selected is None:
        return ctx
    cfg = review.parsed_config(selected)
    refs = {fold(s.name): _source_key(s.source_ref) for s in cfg.services}
    is_draft = selected.status == "draft"
    services = await review.list_services(session, tenant.id) if is_draft else []
    faqs = await review.list_faqs(session, tenant.id) if is_draft else []
    from app.factory.service import load_kb

    docs = await load_kb(session, tenant.id)
    ctx.update(
        cfg=cfg,
        is_draft=is_draft,
        services=[(s, SOURCE_LABELS[refs.get(fold(s.name), "owner")]) for s in services],
        faqs=faqs,
        snapshot_services=cfg.services,
        snapshot_faqs=cfg.faqs,
        blockers=await review.publish_blockers(session, selected, require_sandbox=True)
        if is_draft
        else [],
        info=review.review_info(selected),
        owner=selected.source_inputs or {},
        docs=docs,
        days=[(d, DAY_LABELS[d], review.hours_text(cfg, d)) for d in DAY_KEYS],
        meta=selected.generation_meta or {},
        has_injection_flag=any(f.type == "prompt_injection_suspected" for f in cfg.flags),
    )
    return ctx


async def _run(
    tenant_id: uuid.UUID, action: Any, ok: str, *, version: int | None = None
) -> Response:
    try:
        await action()
    except AppError as exc:
        return _back(tenant_id, error=exc.message, version=version)
    return _back(tenant_id, ok=ok, version=version)


# --------------------------------------------------------------------------- pagina
@router.get("/admin/negocios/{tenant_id}/bot", response_class=HTMLResponse)
async def bot_page(
    request: Request,
    tenant_id: uuid.UUID,
    v: int | None = None,
    ok: str = "",
    error: str = "",
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    tenant = await _tenant(session, tenant_id)
    ctx = await _page_context(session, tenant, v)
    ctx["flash"] = (
        {"kind": "error", "message": error[:300]}
        if error
        else {"kind": "ok", "message": ok[:300]}
        if ok
        else None
    )
    return render(request, "factory/bot.html", ctx)


# --------------------------------------------------------------------------- ediciones
@router.post("/admin/negocios/{tenant_id}/bot/servicios/{service_id}")
async def edit_service(
    tenant_id: uuid.UUID,
    service_id: uuid.UUID,
    name: str = Form(...),
    description: str = Form(""),
    price: str = Form(""),
    price_note: str = Form(""),
    duration_min: int = Form(30),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _tenant(session, tenant_id)

    async def act() -> None:
        await review.update_service(
            session, tenant_id, service_id, name=name, description=description,
            price_cop=_parse_price(price), price_note=price_note,
            duration_min=duration_min, actor=user,
        )  # fmt: skip

    return await _run(tenant_id, act, "Servicio confirmado")


@router.post("/admin/negocios/{tenant_id}/bot/faqs/{faq_id}")
async def edit_faq(
    tenant_id: uuid.UUID,
    faq_id: uuid.UUID,
    question: str = Form(...),
    answer: str = Form(...),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _tenant(session, tenant_id)

    async def act() -> None:
        await review.update_faq(
            session, tenant_id, faq_id, question=question, answer=answer, actor=user
        )

    return await _run(tenant_id, act, "Pregunta confirmada")


@router.post("/admin/negocios/{tenant_id}/bot/descartar/{kind}/{item_id}")
async def discard(
    tenant_id: uuid.UUID,
    kind: str,
    item_id: uuid.UUID,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _tenant(session, tenant_id)
    if kind not in ("service", "faq"):
        raise NotFoundError("Elemento no encontrado")

    async def act() -> None:
        await review.discard_item(session, tenant_id, kind, item_id, actor=user)

    return await _run(tenant_id, act, "Elemento descartado")


@router.post("/admin/negocios/{tenant_id}/bot/{bot_id}/horarios")
async def edit_hours(
    request: Request,
    tenant_id: uuid.UUID,
    bot_id: uuid.UUID,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    bot = await review.get_tenant_bot(session, tenant_id, bot_id)
    form = await request.form()
    values = {d: str(form.get(d, "")) for d in DAY_KEYS}

    async def act() -> None:
        await review.update_hours(session, bot, values, actor=user)

    return await _run(tenant_id, act, "Horarios confirmados", version=bot.version)


@router.post("/admin/negocios/{tenant_id}/bot/{bot_id}/tono")
async def edit_tone(
    tenant_id: uuid.UUID,
    bot_id: uuid.UUID,
    address_form: str = Form(...),
    emoji_level: str = Form(...),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    bot = await review.get_tenant_bot(session, tenant_id, bot_id)

    async def act() -> None:
        await review.update_tone(
            session, bot, register=address_form, emoji_level=emoji_level, actor=user
        )

    return await _run(tenant_id, act, "Tono actualizado", version=bot.version)


@router.post("/admin/negocios/{tenant_id}/bot/{bot_id}/preguntas/{index}")
async def resolve_question(
    tenant_id: uuid.UUID,
    bot_id: uuid.UUID,
    index: int,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    bot = await review.get_tenant_bot(session, tenant_id, bot_id)

    async def act() -> None:
        await review.resolve_open_question(session, bot, index, actor=user)

    return await _run(tenant_id, act, "Pregunta marcada como resuelta", version=bot.version)


@router.post("/admin/negocios/{tenant_id}/bot/{bot_id}/alertas")
async def ack_flags(
    tenant_id: uuid.UUID,
    bot_id: uuid.UUID,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    bot = await review.get_tenant_bot(session, tenant_id, bot_id)

    async def act() -> None:
        await review.acknowledge_flags(session, bot, actor=user)

    return await _run(tenant_id, act, "Alerta revisada", version=bot.version)


# --------------------------------------------------------------------------- ciclo de vida
@router.post("/admin/negocios/{tenant_id}/bot/borrador")
async def new_draft(
    tenant_id: uuid.UUID,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _tenant(session, tenant_id)
    try:
        draft = await review.ensure_draft(session, tenant_id, actor=user)
    except AppError as exc:
        return _back(tenant_id, error=exc.message)
    return _back(tenant_id, ok="Borrador listo para editar", version=draft.version)


@router.post("/admin/negocios/{tenant_id}/bot/regenerar")
async def regenerate(
    tenant_id: uuid.UUID,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
    llm: LLMClient = Depends(get_llm),
) -> Response:
    from app.tenants import service as tenants_service

    tenant = await _tenant(session, tenant_id)
    inputs: FactoryInput = await tenants_service.build_factory_input(session, tenant)

    async def act() -> None:
        await service.build_bot(
            session, tenant_id, inputs, scrape=bool(tenant.website_url), llm=llm
        )

    return await _run(tenant_id, act, "Se generó un borrador nuevo")


@router.post("/admin/negocios/{tenant_id}/bot/{bot_id}/publicar")
async def publish(
    tenant_id: uuid.UUID,
    bot_id: uuid.UUID,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    bot = await review.get_tenant_bot(session, tenant_id, bot_id)

    async def act() -> None:
        await service.approve_draft(session, bot.id, actor=user, require_sandbox=True)
        await service.publish_bot(session, bot.id, actor=user, require_sandbox=True)

    return await _run(tenant_id, act, "Bot publicado", version=bot.version)


@router.post("/admin/negocios/{tenant_id}/bot/rollback/{version}")
async def rollback(
    tenant_id: uuid.UUID,
    version: int,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _tenant(session, tenant_id)

    async def act() -> None:
        await service.rollback_bot(session, tenant_id, version, actor=user)

    return await _run(tenant_id, act, f"Se volvió a la versión {version}", version=version)


# --------------------------------------------------------------------------- chat de prueba
def _clean_history(raw: str) -> list[dict[str, str]]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    out: list[dict[str, str]] = []
    if isinstance(data, list):
        for item in data[-MAX_HISTORY:]:
            if (
                isinstance(item, dict)
                and item.get("role") in ("user", "assistant")
                and isinstance(item.get("content"), str)
            ):
                out.append({"role": item["role"], "content": item["content"][:MAX_MSG]})
    return out


@router.post("/admin/negocios/{tenant_id}/bot/{bot_id}/probar", response_class=HTMLResponse)
async def sandbox_chat(
    request: Request,
    tenant_id: uuid.UUID,
    bot_id: uuid.UUID,
    text: str = Form(...),
    history: str = Form("[]"),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    from app.conversation.engine import sandbox_reply

    bot = await review.get_tenant_bot(session, tenant_id, bot_id)
    messages = _clean_history(history)
    message = text.strip()[:MAX_MSG]
    error = ""
    if message:
        try:
            reply = await sandbox_reply(session, tenant_id, messages, message, bot_config_id=bot.id)
            messages += [
                {"role": "user", "content": message},
                {"role": "assistant", "content": reply},
            ]
            await review.mark_sandbox_tested(session, bot)
        except NotImplementedError:
            error = "El chat de prueba aún no está disponible."
        except AppError as exc:
            error = exc.message
    return render(
        request,
        "factory/_chat.html",
        {
            "tenant": {"id": tenant_id},
            "bot": bot,
            "messages": messages,
            "history_json": json.dumps(messages, ensure_ascii=False),
            "error": error,
        },
    )
