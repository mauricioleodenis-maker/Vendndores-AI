"""Rutas /admin/inicio, /admin/conversaciones y /admin/citas/resumen."""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation.handoff import bot_is_paused
from app.core.deps import current_user, get_session
from app.dashboard import service
from app.db.models.users import User
from app.web.templating import render

router = APIRouter(tags=["dashboard"])


def _can_see_billing(user: User) -> bool:
    return user.role in ("owner", "admin")


def _flash(request: Request) -> dict[str, str] | None:
    if msg := request.query_params.get("ok"):
        return {"kind": "ok", "message": msg[:200]}
    if msg := request.query_params.get("error"):
        return {"kind": "error", "message": msg[:200]}
    return None


@router.get("/admin/inicio", response_class=HTMLResponse)
async def inicio(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    kpis = await service.compute_kpis(session, include_billing=_can_see_billing(user))
    return render(request, "dashboard/inicio.html", {"kpis": kpis})


@router.get("/admin/inicio/kpis", response_class=HTMLResponse)
async def inicio_kpis(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    kpis = await service.compute_kpis(session, include_billing=_can_see_billing(user))
    return render(request, "dashboard/_kpis.html", {"kpis": kpis})


@router.get("/admin/conversaciones", response_class=HTMLResponse)
async def conversaciones(
    request: Request,
    estado: str | None = Query(default=None, max_length=20),
    q: str | None = Query(default=None, max_length=100),
    pagina: int = Query(default=1, ge=1, le=1000),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows, has_more = await service.list_conversations(session, status=estado, q=q, page=pagina)
    base = "/admin/conversaciones?estado=" + quote(estado or "") + "&q=" + quote(q or "")
    return render(
        request,
        "dashboard/conversaciones.html",
        {
            "rows": rows,
            "has_more": has_more,
            "page": pagina,
            "estado": estado,
            "q": q or "",
            "estados": service.CONVERSATION_FILTERS,
            "base_url": base,
        },
    )


@router.get("/admin/conversaciones/{conversation_id}", response_class=HTMLResponse)
async def conversacion(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    conv = await service.get_conversation(session, conversation_id)
    thread = await service.load_thread(session, conv, actor=user)
    return render(
        request,
        "dashboard/conversacion.html",
        {
            "conv": conv,
            "thread": thread,
            "paused": bot_is_paused(conv),
            "window_open": service.window_open(conv),
            "flash": _flash(request),
        },
    )


def _back(
    conversation_id: uuid.UUID, ok: str | None = None, error: str | None = None
) -> RedirectResponse:
    url = f"/admin/conversaciones/{conversation_id}"
    if ok:
        url += f"?ok={quote(ok)}"
    elif error:
        url += f"?error={quote(error)}"
    return RedirectResponse(url, status_code=303)


@router.post("/admin/conversaciones/{conversation_id}/tomar")
async def tomar(
    conversation_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    conv = await service.get_conversation(session, conversation_id)
    await service.take_control(session, conv, actor=user)
    return _back(conversation_id, ok="Tomaste el control: el bot no responderá")


@router.post("/admin/conversaciones/{conversation_id}/devolver")
async def devolver(
    conversation_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    conv = await service.get_conversation(session, conversation_id)
    await service.return_to_bot(session, conv, actor=user)
    return _back(conversation_id, ok="Conversación devuelta al bot")


@router.post("/admin/conversaciones/{conversation_id}/responder")
async def responder(
    conversation_id: uuid.UUID,
    mensaje: Annotated[str, Form(max_length=service.MAX_REPLY_LEN)],
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    conv = await service.get_conversation(session, conversation_id)
    await service.send_manual_reply(session, conv, mensaje, actor=user)
    return _back(conversation_id, ok="Mensaje enviado")


@router.get("/admin/citas/resumen", response_class=HTMLResponse)
async def citas_resumen(
    request: Request,
    dias: int = Query(default=7, ge=1, le=60),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows, per_tenant = await service.appointments_overview(session, days=dias)
    return render(
        request,
        "dashboard/citas_resumen.html",
        {"rows": rows, "per_tenant": per_tenant, "dias": dias},
    )
