"""Rutas de empresas: lista, ficha (pestanas), wizard "Nueva empresa" (HTMX) y API JSON."""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session, require_role
from app.core.errors import AppError
from app.db.models.tenants import NICHES, TENANT_STATUSES, Tenant
from app.db.models.users import User
from app.tenants import service
from app.tenants.schemas import (
    DAY_KEYS,
    DAY_LABELS,
    SECRET_LABELS,
    WIZARD_NICHES,
    ChannelIn,
    FaqIn,
    SecretIn,
    ServiceIn,
    TenantIn,
    TenantOut,
    clean_phone,
    clean_url,
    error_map,
    hours_from_form,
    hours_to_text,
)
from app.web.templating import render

router = APIRouter(tags=["tenants"])
api_router = APIRouter(prefix="/api/negocios", tags=["tenants-api"])
routers = [api_router]

Admin = Depends(require_role("admin"))
Owner = Depends(require_role())
TABS = ("datos", "servicios", "faqs", "horarios", "canales")
STEP_TEMPLATES = {
    1: "tenants/wizard_paso1.html",
    2: "tenants/wizard_paso2.html",
    3: "tenants/wizard_paso3.html",
}
MAX_FORM_SERVICES = 60


# --------------------------------------------------------------------------- helpers
def _back(
    tenant_id: uuid.UUID, tab: str, *, ok: str | None = None, error: str | None = None
) -> Response:
    url = f"/admin/negocios/{tenant_id}?tab={tab}"
    if ok:
        url += f"&ok={quote(ok)}"
    if error:
        url += f"&error={quote(error[:200])}"
    return RedirectResponse(url, status_code=303)


def _flash(request: Request) -> dict[str, str] | None:
    if msg := request.query_params.get("ok"):
        return {"kind": "ok", "message": msg[:200]}
    if err := request.query_params.get("error"):
        return {"kind": "error", "message": err[:200]}
    return None


def _first_error(exc: ValidationError) -> str:
    field, msg = next(iter(error_map(exc).items()))
    return f"{field}: {msg}"


def _parse_uuid(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw) if raw else None
    except ValueError:
        return None


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


# --------------------------------------------------------------------------- lista
@router.get("/admin/negocios", response_model=None)
async def list_page(
    request: Request,
    q: str = "",
    niche: str = "",
    status: str = "",
    page: int = 1,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    niche = niche if niche in NICHES else ""
    status = status if status in TENANT_STATUSES else ""
    rows, has_more = await service.list_tenants(
        session, q=q[:100], niche=niche, status=status, page=page
    )
    qs = f"/admin/negocios?q={quote(q[:100])}&niche={niche}&status={status}"
    ctx = {
        "tenants": rows,
        "has_more": has_more,
        "page": max(page, 1),
        "q": q[:100],
        "niche": niche,
        "status": status,
        "niches": NICHES,
        "statuses": TENANT_STATUSES,
        "base_url": qs,
        "flash": _flash(request),
    }
    name = "tenants/_tabla.html" if _is_htmx(request) else "tenants/list.html"
    return render(request, name, ctx)


# --------------------------------------------------------------------------- wizard
async def _niche_defaults(niche: str) -> list[dict[str, Any]]:
    """Servicios sugeridos por la plantilla de nicho (best-effort: B2 puede no estar listo)."""
    try:
        from app.niches.loader import get_niche_template

        data = get_niche_template(niche).model_dump()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for item in data.get("typical_services") or []:
        if isinstance(item, dict) and item.get("name"):
            dur = item.get("duration_min") or item.get("default_duration_min") or 30
            out.append({"name": str(item["name"])[:200], "price_cop": None, "duration_min": dur})
    return out[:20]


def _wizard_page(request: Request, step: int, ctx: dict[str, Any]) -> Response:
    ctx = {"step": step, "step_template": STEP_TEMPLATES[step], "niches": WIZARD_NICHES, **ctx}
    ctx.setdefault("errors", {})
    return render(
        request, STEP_TEMPLATES[step] if _is_htmx(request) else "tenants/wizard.html", ctx
    )


async def _step2_ctx(
    session: AsyncSession, tenant: Tenant, *, errors: dict[str, str] | None = None
) -> dict[str, Any]:
    services = [
        {"name": s.name, "price_cop": s.price_cop, "duration_min": s.duration_min}
        for s in await service.list_services(session, tenant.id)
    ] or await _niche_defaults(tenant.niche)
    profile = await service.get_profile(session, tenant.id)
    return {
        "tenant": tenant,
        "services": services,
        "days": [(d, DAY_LABELS[d], hours_to_text(profile.hours, d)) for d in DAY_KEYS],
        "errors": errors or {},
        "notes": "",
    }


@router.get("/admin/negocios/nuevo", response_model=None)
async def wizard_start(request: Request, user: User = Admin) -> Response:
    return _wizard_page(request, 1, {"tenant": None, "values": {}})


@router.get("/admin/negocios/nuevo/servicio-fila", response_model=None)
async def wizard_service_row(request: Request, user: User = Admin) -> Response:
    return render(request, "tenants/_servicio_fila.html", {"s": {"duration_min": 30}})


@router.post("/admin/negocios/validar-campo", response_model=None)
async def validate_field(
    field: str = Form(""), value: str = Form(""), user: User = Admin
) -> HTMLResponse:
    """Validacion en vivo de un campo del paso 1; responde el mensaje (o vacio)."""
    try:
        if field == "name" and len(value.strip()) < 2:
            raise ValueError("Escribe el nombre del negocio")
        if field == "phone_contact":
            clean_phone(value)
        elif field == "website_url":
            clean_url(value)
        elif field == "instagram_url":
            clean_url(value, instagram=True)
    except ValueError as exc:
        from html import escape

        return HTMLResponse(f'<small class="error">{escape(str(exc))}</small>')
    return HTMLResponse("")


@router.post("/admin/negocios/nuevo/paso1", response_model=None)
async def wizard_step1(
    request: Request,
    name: str = Form(""),
    niche: str = Form(""),
    city: str = Form("Cali"),
    address: str = Form(""),
    phone_contact: str = Form(""),
    owner_name: str = Form(""),
    website_url: str = Form(""),
    instagram_url: str = Form(""),
    tenant_id: str = Form(""),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    existing_id = _parse_uuid(tenant_id)
    values = {
        "name": name,
        "niche": niche,
        "city": city,
        "address": address,
        "phone_contact": phone_contact,
        "owner_name": owner_name,
        "website_url": website_url,
        "instagram_url": instagram_url,
    }
    try:
        data = TenantIn.model_validate(values)
        if data.niche not in WIZARD_NICHES:
            raise ValueError("Elige uno de los nichos disponibles")
    except ValidationError as exc:
        return _wizard_page(
            request,
            1,
            {"tenant": None, "values": values, "errors": error_map(exc), "tenant_id": existing_id},
        )
    except ValueError as exc:
        return _wizard_page(
            request,
            1,
            {
                "tenant": None,
                "values": values,
                "errors": {"niche": str(exc)},
                "tenant_id": existing_id,
            },
        )
    if existing_id:
        tenant = await service.get_tenant(session, existing_id)
        if tenant.status != "draft":
            raise AppError("conflict", "Esta empresa ya no es un borrador", 409)
        await service.update_tenant(session, tenant, data, actor=user)
    else:
        tenant = await service.create_tenant(session, data, actor=user)
    return _wizard_page(request, 2, await _step2_ctx(session, tenant))


@router.get("/admin/negocios/{tenant_id}/wizard", response_model=None)
async def wizard_resume(
    request: Request,
    tenant_id: uuid.UUID,
    paso: int = 2,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    tenant = await service.get_tenant(session, tenant_id)
    if tenant.status == "building":
        return _wizard_page(request, 3, {"tenant": tenant, "generating": True})
    if paso == 1:
        values = {f: getattr(tenant, f) or "" for f in TenantIn.model_fields}
        return _wizard_page(
            request, 1, {"tenant": tenant, "values": values, "tenant_id": tenant.id}
        )
    ctx = await _step2_ctx(session, tenant)
    if paso >= 3:
        return _wizard_page(request, 3, await _step3_ctx(session, tenant))
    return _wizard_page(request, 2, ctx)


async def _step3_ctx(session: AsyncSession, tenant: Tenant, notes: str = "") -> dict[str, Any]:
    profile = await service.get_profile(session, tenant.id)
    return {
        "tenant": tenant,
        "services": await service.list_services(session, tenant.id),
        "days": [(DAY_LABELS[d], hours_to_text(profile.hours, d) or "Cerrado") for d in DAY_KEYS],
        "notes": notes,
        "generating": tenant.status == "building",
    }


@router.post("/admin/negocios/{tenant_id}/paso2", response_model=None)
async def wizard_step2(
    request: Request,
    tenant_id: uuid.UUID,
    service_name: list[str] = Form(default=[]),
    service_price: list[str] = Form(default=[]),
    service_duration: list[str] = Form(default=[]),
    notes: str = Form(""),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    tenant = await service.get_tenant(session, tenant_id)
    form = await request.form()
    day_texts = {d: str(form.get(f"hours_{d}", ""))[:100] for d in DAY_KEYS}
    errors: dict[str, str] = {}
    items: list[ServiceIn] = []
    rows: list[dict[str, Any]] = []
    for i, name in enumerate(service_name[:MAX_FORM_SERVICES]):
        price_raw = (service_price[i] if i < len(service_price) else "").strip()
        dur_raw = (service_duration[i] if i < len(service_duration) else "").strip() or "30"
        rows.append({"name": name, "price_cop": price_raw, "duration_min": dur_raw})
        if not name.strip():
            continue
        digits = price_raw.replace(".", "").replace(",", "").replace("$", "").strip()
        try:
            items.append(
                ServiceIn(
                    name=name,
                    price_cop=int(digits) if digits else None,
                    duration_min=int(dur_raw),
                )
            )
        except (ValidationError, ValueError):
            errors[f"service_{i}"] = (
                f"Revisa el servicio «{name[:40]}» (precio y duración deben ser números)"
            )
    hours: dict[str, Any] = {}
    try:
        hours = hours_from_form(day_texts)
    except ValueError as exc:
        errors["hours"] = str(exc)
    if errors:
        ctx = await _step2_ctx(session, tenant, errors=errors)
        ctx.update(
            services=rows,
            notes=notes,
            days=[(d, DAY_LABELS[d], day_texts[d]) for d in DAY_KEYS],
        )
        return _wizard_page(request, 2, ctx)
    await service.replace_services(session, tenant.id, items, actor=user)
    await service.update_profile(session, tenant, hours=hours, actor=user)
    return _wizard_page(request, 3, await _step3_ctx(session, tenant, notes[:2000]))


@router.post("/admin/negocios/{tenant_id}/generar", response_model=None)
async def wizard_generate(
    request: Request,
    tenant_id: uuid.UUID,
    consent: str = Form(""),
    notes: str = Form(""),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    tenant = await service.get_tenant(session, tenant_id)
    if consent != "on":
        ctx = await _step3_ctx(session, tenant, notes)
        ctx["errors"] = {"consent": "Debes autorizar el uso de datos del negocio para continuar"}
        return _wizard_page(request, 3, ctx)
    await service.start_generation(session, tenant, actor=user, notes=notes, consent=True)
    return render(request, "tenants/_generando.html", {"tenant": tenant, "state": "building"})


@router.get("/admin/negocios/{tenant_id}/estado", response_model=None)
async def generation_status(
    request: Request,
    tenant_id: uuid.UUID,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    tenant = await service.get_tenant(session, tenant_id)
    ctx = {"tenant": tenant, "state": tenant.status}
    if tenant.status in {"review", "active", "paused"}:
        target = f"/admin/negocios/{tenant.id}/bot"
        resp = render(request, "tenants/_generando.html", {**ctx, "bot_url": target})
        if _is_htmx(request):
            resp.headers["HX-Redirect"] = target
        return resp
    return render(request, "tenants/_generando.html", ctx)


# --------------------------------------------------------------------------- ficha
@router.get("/admin/negocios/{tenant_id}", response_model=None)
async def detail_page(
    request: Request,
    tenant_id: uuid.UUID,
    tab: str = "datos",
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    tab = tab if tab in TABS else "datos"
    tenant = await service.get_tenant(session, tenant_id)
    ctx: dict[str, Any] = {
        "tenant": tenant,
        "tab": tab,
        "tabs": TABS,
        "flash": _flash(request),
        "niches": NICHES,
        "values": {f: getattr(tenant, f) or "" for f in TenantIn.model_fields},
        "errors": {},
    }
    if tab == "servicios":
        ctx["services"] = await service.list_services(session, tenant_id)
    elif tab == "faqs":
        ctx["faqs"] = await service.list_faqs(session, tenant_id)
    elif tab == "horarios":
        profile = await service.get_profile(session, tenant_id)
        ctx["profile"] = profile
        ctx["days"] = [(d, DAY_LABELS[d], hours_to_text(profile.hours, d)) for d in DAY_KEYS]
    elif tab == "canales":
        ctx["channels"] = await service.list_channels(session, tenant_id)
        ctx["secrets"] = await service.list_secret_meta(session, tenant_id)
        ctx["secret_labels"] = SECRET_LABELS
    return render(request, "tenants/detail.html", ctx)


@router.post("/admin/negocios/{tenant_id}/datos", response_model=None)
async def update_data(
    request: Request,
    tenant_id: uuid.UUID,
    name: str = Form(""),
    niche: str = Form(""),
    city: str = Form(""),
    address: str = Form(""),
    phone_contact: str = Form(""),
    owner_name: str = Form(""),
    website_url: str = Form(""),
    instagram_url: str = Form(""),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    tenant = await service.get_tenant(session, tenant_id)
    values = {
        "name": name, "niche": niche, "city": city, "address": address,
        "phone_contact": phone_contact, "owner_name": owner_name,
        "website_url": website_url, "instagram_url": instagram_url,
    }  # fmt: skip
    try:
        data = TenantIn.model_validate(values)
    except ValidationError as exc:
        ctx = {
            "tenant": tenant, "tab": "datos", "tabs": TABS, "niches": NICHES,
            "values": values, "errors": error_map(exc), "flash": None,
        }  # fmt: skip
        return render(request, "tenants/detail.html", ctx, status_code=422)
    await service.update_tenant(session, tenant, data, actor=user)
    return _back(tenant_id, "datos", ok="Datos guardados")


@router.post("/admin/negocios/{tenant_id}/estado", response_model=None)
async def change_status(
    tenant_id: uuid.UUID,
    nuevo: str = Form(...),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    tenant = await service.get_tenant(session, tenant_id)
    try:
        await service.set_status(session, tenant, nuevo, actor=user)
    except AppError as exc:
        return _back(tenant_id, "datos", error=exc.message)
    return _back(tenant_id, "datos", ok="Estado actualizado")


@router.post("/admin/negocios/{tenant_id}/eliminar", response_model=None)
async def remove_tenant(
    tenant_id: uuid.UUID,
    user: User = Owner,
    session: AsyncSession = Depends(get_session),
) -> Response:
    tenant = await service.get_tenant(session, tenant_id)
    await service.delete_tenant(session, tenant, actor=user)
    return RedirectResponse("/admin/negocios?ok=Empresa%20eliminada", status_code=303)


# --------------------------------------------------------------------------- servicios y FAQs
def _service_form(
    name: str, description: str, price_cop: str, price_note: str, duration_min: str,
    requires_deposit: str, is_active: str,
) -> ServiceIn:  # fmt: skip
    digits = price_cop.replace(".", "").replace(",", "").replace("$", "").strip()
    return ServiceIn(
        name=name,
        description=description,
        price_cop=int(digits) if digits else None,
        price_note=price_note,
        duration_min=int(duration_min or 30),
        requires_deposit=requires_deposit == "on",
        is_active=is_active == "on",
    )


@router.post("/admin/negocios/{tenant_id}/servicios", response_model=None)
async def create_service(
    tenant_id: uuid.UUID,
    name: str = Form(""),
    description: str = Form(""),
    price_cop: str = Form(""),
    price_note: str = Form(""),
    duration_min: str = Form("30"),
    requires_deposit: str = Form(""),
    is_active: str = Form("on"),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.get_tenant(session, tenant_id)
    try:
        data = _service_form(
            name, description, price_cop, price_note, duration_min, requires_deposit, is_active
        )
        await service.add_service(session, tenant_id, data, actor=user)
    except (ValidationError, ValueError) as exc:
        msg = (
            _first_error(exc)
            if isinstance(exc, ValidationError)
            else "Precio y duración deben ser números"
        )
        return _back(tenant_id, "servicios", error=msg)
    except AppError as exc:
        return _back(tenant_id, "servicios", error=exc.message)
    return _back(tenant_id, "servicios", ok="Servicio agregado")


@router.post("/admin/negocios/{tenant_id}/servicios/{service_id}", response_model=None)
async def edit_service(
    tenant_id: uuid.UUID,
    service_id: uuid.UUID,
    name: str = Form(""),
    description: str = Form(""),
    price_cop: str = Form(""),
    price_note: str = Form(""),
    duration_min: str = Form("30"),
    requires_deposit: str = Form(""),
    is_active: str = Form(""),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        data = _service_form(
            name, description, price_cop, price_note, duration_min, requires_deposit, is_active
        )
    except (ValidationError, ValueError) as exc:
        msg = (
            _first_error(exc)
            if isinstance(exc, ValidationError)
            else "Precio y duración deben ser números"
        )
        return _back(tenant_id, "servicios", error=msg)
    await service.update_service(session, tenant_id, service_id, data, actor=user)
    return _back(tenant_id, "servicios", ok="Servicio actualizado")


@router.post("/admin/negocios/{tenant_id}/servicios/{service_id}/eliminar", response_model=None)
async def remove_service(
    tenant_id: uuid.UUID,
    service_id: uuid.UUID,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.delete_service(session, tenant_id, service_id, actor=user)
    return _back(tenant_id, "servicios", ok="Servicio eliminado")


@router.post("/admin/negocios/{tenant_id}/faqs", response_model=None)
async def create_faq(
    tenant_id: uuid.UUID,
    question: str = Form(""),
    answer: str = Form(""),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.get_tenant(session, tenant_id)
    try:
        await service.add_faq(
            session, tenant_id, FaqIn(question=question, answer=answer), actor=user
        )
    except ValidationError as exc:
        return _back(tenant_id, "faqs", error=_first_error(exc))
    return _back(tenant_id, "faqs", ok="Pregunta agregada")


@router.post("/admin/negocios/{tenant_id}/faqs/{faq_id}", response_model=None)
async def edit_faq(
    tenant_id: uuid.UUID,
    faq_id: uuid.UUID,
    question: str = Form(""),
    answer: str = Form(""),
    is_active: str = Form(""),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        data = FaqIn(question=question, answer=answer, is_active=is_active == "on")
    except ValidationError as exc:
        return _back(tenant_id, "faqs", error=_first_error(exc))
    await service.update_faq(session, tenant_id, faq_id, data, actor=user)
    return _back(tenant_id, "faqs", ok="Pregunta actualizada")


@router.post("/admin/negocios/{tenant_id}/faqs/{faq_id}/eliminar", response_model=None)
async def remove_faq(
    tenant_id: uuid.UUID,
    faq_id: uuid.UUID,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.delete_faq(session, tenant_id, faq_id, actor=user)
    return _back(tenant_id, "faqs", ok="Pregunta eliminada")


# --------------------------------------------------------------------------- horarios / perfil
@router.post("/admin/negocios/{tenant_id}/horarios", response_model=None)
async def save_hours(
    request: Request,
    tenant_id: uuid.UUID,
    tone: str = Form(""),
    handoff_phone: str = Form(""),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    tenant = await service.get_tenant(session, tenant_id)
    form = await request.form()
    try:
        hours = hours_from_form({d: str(form.get(f"hours_{d}", ""))[:100] for d in DAY_KEYS})
        phone = clean_phone(handoff_phone)
    except ValueError as exc:
        return _back(tenant_id, "horarios", error=str(exc))
    await service.update_profile(
        session, tenant, hours=hours, tone=tone, handoff_phone=phone, set_handoff=True, actor=user
    )
    return _back(tenant_id, "horarios", ok="Horarios guardados")


# --------------------------------------------------------------------------- canales y secretos
@router.post("/admin/negocios/{tenant_id}/canales", response_model=None)
async def create_channel(
    tenant_id: uuid.UUID,
    phone_e164: str = Form(""),
    twilio_messaging_service_sid: str = Form(""),
    whatsapp_sender_status: str = Form("sandbox"),
    voice_enabled: str = Form(""),
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.get_tenant(session, tenant_id)
    try:
        data = ChannelIn(
            phone_e164=phone_e164,
            twilio_messaging_service_sid=twilio_messaging_service_sid,
            whatsapp_sender_status=whatsapp_sender_status,
            voice_enabled=voice_enabled == "on",
        )
        await service.add_channel(session, tenant_id, data, actor=user)
    except ValidationError as exc:
        return _back(tenant_id, "canales", error=_first_error(exc))
    except AppError as exc:
        return _back(tenant_id, "canales", error=exc.message)
    return _back(tenant_id, "canales", ok="Canal agregado")


@router.post("/admin/negocios/{tenant_id}/canales/{channel_id}/eliminar", response_model=None)
async def remove_channel(
    tenant_id: uuid.UUID,
    channel_id: uuid.UUID,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.delete_channel(session, tenant_id, channel_id, actor=user)
    return _back(tenant_id, "canales", ok="Canal eliminado")


@router.post("/admin/negocios/{tenant_id}/secretos", response_model=None)
async def save_secret(
    tenant_id: uuid.UUID,
    kind: str = Form(""),
    value: str = Form(""),
    user: User = Owner,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.get_tenant(session, tenant_id)
    try:
        await service.set_secret(
            session, tenant_id, SecretIn(kind=kind, value=value.strip()), actor=user
        )
    except ValidationError as exc:
        return _back(tenant_id, "canales", error=_first_error(exc))
    return _back(tenant_id, "canales", ok="Secreto guardado")


@router.post("/admin/negocios/{tenant_id}/secretos/{kind}/eliminar", response_model=None)
async def remove_secret(
    tenant_id: uuid.UUID,
    kind: str,
    user: User = Owner,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.delete_secret(session, tenant_id, kind, actor=user)
    return _back(tenant_id, "canales", ok="Secreto eliminado")


# --------------------------------------------------------------------------- API JSON
class TenantList(BaseModel):
    items: list[TenantOut]
    has_more: bool


@api_router.get("", response_model=TenantList)
async def api_list(
    q: str = "",
    niche: str = "",
    status: str = "",
    page: int = 1,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> TenantList:
    rows, more = await service.list_tenants(
        session, q=q[:100], niche=niche, status=status, page=page
    )
    return TenantList(items=[TenantOut.model_validate(r) for r in rows], has_more=more)


@api_router.get("/{tenant_id}", response_model=TenantOut)
async def api_get(
    tenant_id: uuid.UUID,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> TenantOut:
    return TenantOut.model_validate(await service.get_tenant(session, tenant_id))


@api_router.post("", response_model=TenantOut, status_code=201)
async def api_create(
    data: TenantIn,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> TenantOut:
    return TenantOut.model_validate(await service.create_tenant(session, data, actor=user))
