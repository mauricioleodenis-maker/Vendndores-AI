"""Rutas de leads: API JSON (/api/leads) y paginas Jinja+HTMX (/admin/leads).

Busqueda en Google Places, tabla con filtros, kanban por etapa, detalle con timeline e importacion
de CSV (vista previa + confirmacion). La logica vive en ``places``/``search``/``listing``.
"""

from __future__ import annotations

import io
import json
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.core.config import get_settings
from app.core.deps import current_user, get_session
from app.core.errors import AppError
from app.core.jobs import enqueue
from app.core.rate_limit import enforce
from app.db.models.leads import LEAD_STAGES, Lead, LeadSource
from app.db.models.users import User
from app.leads import csv_import as ci
from app.leads import listing
from app.leads.export import export_leads_csv
from app.leads.listing import BANDS, LeadFilters
from app.leads.places import NICHE_QUERIES, PlacesNotConfiguredError
from app.leads.scoring import DEFAULT, band_for
from app.leads.search import (
    JOB_NAME,
    SearchParams,
    create_search_source,
    get_source,
    parse_neighborhoods,
    plan_search,
    source_status,
)
from app.web.templating import render

router = APIRouter(tags=["leads"])

CSV_CONTENT_TYPES = frozenset(
    {
        "text/csv",
        "application/csv",
        "application/vnd.ms-excel",
        "text/plain",
        "application/octet-stream",
    }
)
SEARCH_LIMIT_PER_HOUR = 10
NICHE_CHOICES = tuple(NICHE_QUERIES) + ("otro",)
FIELD_LABELS = {
    "niche": "Nicho",
    "city": "Ciudad",
    "neighborhoods": "Barrios",
    "max_results": "Máximo de resultados",
    "min_reviews": "Mínimo de reseñas",
}
EVENT_LABELS = {
    "created": "Lead creado",
    "imported": "Importado",
    "scored": "Puntaje recalculado",
    "stage_changed": "Cambio de etapa",
    "note": "Nota",
    "outreach_sent": "Mensaje de campaña enviado",
    "reply_received": "Respuesta recibida",
    "opt_out": "Pidió no ser contactado",
    "secret_shop_sent": "Prueba secreta enviada",
    "secret_shop_replied": "Prueba secreta respondida",
    "demo_created": "Demo creada",
    "converted": "Convertido en cliente",
}


# --------------------------------------------------------------------------- esquemas
class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    niche: str
    city: str
    phone_e164: str | None
    website: str | None
    rating: float | None
    review_count: int | None
    score: int
    band: str
    stage: str
    disposition: str


class LeadListOut(BaseModel):
    items: list[LeadOut]
    page: int
    has_more: bool


class StageIn(BaseModel):
    stage: str


class SearchDryRunOut(BaseModel):
    dry_run: bool = True
    max_requests: int
    max_cost_usd: float
    queries: list[str]


class SearchQueuedOut(BaseModel):
    source_id: uuid.UUID
    status: str = "queued"
    max_requests: int
    max_cost_usd: float


def lead_out(lead: Lead) -> LeadOut:
    return LeadOut(
        id=lead.id,
        name=lead.name,
        niche=lead.niche,
        city=lead.city,
        phone_e164=lead.phone_e164,
        website=lead.website,
        rating=float(lead.rating) if lead.rating is not None else None,
        review_count=lead.review_count,
        score=lead.score,
        band=band_for(lead.score),
        stage=lead.stage,
        disposition=lead.disposition,
    )


# --------------------------------------------------------------------------- helpers
def filters_from(
    q: str = "",
    niche: str = "",
    city: str = "",
    stage: str = "",
    band: str = "",
    disposition: str = "",
    min_score: int = 0,
    page: int = 1,
) -> LeadFilters:
    return LeadFilters(
        q=q.strip()[:100],
        niche=niche.strip()[:30],
        city=city.strip()[:100],
        stage=stage if stage in LEAD_STAGES else "",
        band=band if band in BANDS else "",
        disposition=disposition,
        min_score=max(0, min(100, min_score)),
        page=max(1, page),
    )


def filters_dep(
    q: str = Query("", max_length=100),
    niche: str = Query("", max_length=30),
    city: str = Query("", max_length=100),
    stage: str = Query("", max_length=20),
    band: str = Query("", max_length=10),
    disposition: str = Query("", max_length=20),
    min_score: int = Query(0, ge=0, le=100),
    page: int = Query(1, ge=1, le=10_000),
) -> LeadFilters:
    return filters_from(q, niche, city, stage, band, disposition, min_score, page)


def validation_messages(exc: ValidationError) -> list[str]:
    out: list[str] = []
    for err in exc.errors():
        loc = str(err["loc"][0]) if err["loc"] else ""
        msg = str(err["msg"]).removeprefix("Value error, ")
        out.append(f"Revisa «{FIELD_LABELS.get(loc, loc)}»: {msg}")
    return out


async def launch_search(
    session: AsyncSession, params: SearchParams, user: User, request: Request
) -> uuid.UUID:
    """Crea la fuente, confirma la transaccion y encola el job."""
    if not get_settings().google_places_api_key.get_secret_value():
        raise PlacesNotConfiguredError()
    await enforce(f"leads:search:{user.id}", limit=SEARCH_LIMIT_PER_HOUR, window_s=3600)
    source = await create_search_source(session, params, user_id=user.id)
    await audit.log_event(
        session,
        actor=user,
        action="lead.search",
        entity_type="lead_source",
        entity_id=source.id,
        diff={"params": source.params},
        ip=request.client.host if request.client else None,
    )
    source_id = source.id
    await session.commit()  # el worker debe ver la fuente al correr el job
    await enqueue(JOB_NAME, str(source_id))
    return source_id


@dataclass(slots=True)
class ImportPreview:
    sha256: str
    headers: list[str]
    colmap: ci.ColumnMap
    report: ci.CsvImportReport
    reupload: LeadSource | None


def resolve_colmap(headers: list[str], overrides: dict[str, str]) -> ci.ColumnMap:
    """Autodetecta columnas y aplica las elegidas por la persona (solo encabezados existentes)."""
    valid = {k: v for k, v in overrides.items() if v and v in headers}
    try:
        colmap = ci.detect_columns(headers)
    except ci.CsvImportError:
        if "name" not in valid:
            raise
        colmap = ci.ColumnMap(name=valid["name"])
    for key, value in valid.items():
        if key in ci.ColumnMap.__slots__:
            setattr(colmap, key, value)
    return colmap


def validate_niche_city(niche: str, city: str) -> tuple[str, str]:
    niche = niche.strip().lower()
    city = " ".join(city.split())
    if niche not in NICHE_CHOICES:
        raise AppError("invalid_niche", "Elige un nicho válido", 422)
    if not 2 <= len(city) <= 100:
        raise AppError("invalid_city", "Escribe la ciudad (2 a 100 caracteres)", 422)
    return niche, city


async def read_upload(file: UploadFile) -> bytes:
    name = (file.filename or "").lower()
    if not name.endswith(".csv") or (file.content_type or "") not in CSV_CONTENT_TYPES:
        raise AppError("csv_tipo", "Sube un archivo .csv", 415)
    data = await file.read(ci.MAX_BYTES + 1)
    if not data:
        raise AppError("csv_vacio", "El archivo está vacío", 422)
    return data


async def build_preview(
    session: AsyncSession, data: bytes, niche: str, city: str, overrides: dict[str, str]
) -> ImportPreview:
    headers = ci.read_headers(data)
    colmap = resolve_colmap(headers, overrides)
    sha = ci.file_sha256(data)
    candidates = ci.parse_csv(io.BytesIO(data), colmap, niche=niche, city=city)
    report = await ci.import_csv(session, LeadSource(kind="csv_import"), candidates, dry_run=True)
    return ImportPreview(sha, headers, colmap, report, await ci.find_reupload(session, sha))


async def commit_import(
    session: AsyncSession,
    *,
    sha256: str,
    niche: str,
    city: str,
    overrides: dict[str, str],
    force: bool,
    user: User,
) -> LeadSource:
    data = listing.load_upload(sha256)
    if not force and await ci.find_reupload(session, sha256):
        raise AppError(
            "csv_reupload", "Este archivo ya se importó antes. Confirma para repetirlo.", 409
        )
    headers = ci.read_headers(data)
    colmap = resolve_colmap(headers, overrides)
    source = LeadSource(
        kind="csv_import",
        label=f"CSV {niche} {city}"[:200],
        params={"sha256": sha256, "niche": niche, "city": city},
        created_by=user.id,
    )
    session.add(source)
    await session.flush()
    candidates = ci.parse_csv(io.BytesIO(data), colmap, niche=niche, city=city)
    await ci.import_csv(session, source, candidates, dry_run=False)
    await audit.log_event(
        session,
        actor=user,
        action="lead.import_csv",
        entity_type="lead_source",
        entity_id=source.id,
        diff={"stats": source.stats},
    )
    listing.discard_upload(sha256)
    return source


def preview_json(p: ImportPreview) -> dict[str, Any]:
    return {
        "sha256": p.sha256,
        "headers": p.headers,
        "columns": p.colmap.as_dict(),
        "summary": p.report.summary(),
        "errors": p.report.errors,
        "preview": p.report.preview,
        "reupload": p.reupload is not None,
    }


def overrides_from_form(form: Any) -> dict[str, str]:
    return {
        field: str(form.get(f"col_{field}") or "")
        for field in ci.ColumnMap.__slots__
        if form.get(f"col_{field}")
    }


# --------------------------------------------------------------------------- API JSON
@router.post("/api/leads/search", response_model=None)
async def api_search(
    body: SearchParams,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """``dry_run`` devuelve el estimado de costo sin llamar a Google ni guardar nada."""
    estimate = plan_search(body)
    if body.dry_run:
        return JSONResponse(
            SearchDryRunOut(
                max_requests=estimate.max_requests,
                max_cost_usd=estimate.max_cost_usd,
                queries=estimate.queries,
            ).model_dump()
        )
    source_id = await launch_search(session, body, user, request)
    out = SearchQueuedOut(
        source_id=source_id,
        max_requests=estimate.max_requests,
        max_cost_usd=estimate.max_cost_usd,
    )
    return JSONResponse(json.loads(out.model_dump_json()), status_code=202)


@router.get("/api/leads/sources/{source_id}/status")
async def api_source_status(
    source_id: uuid.UUID,
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return source_status(await get_source(session, source_id))


@router.get("/api/leads", response_model=LeadListOut)
async def api_list(
    f: LeadFilters = Depends(filters_dep),
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> LeadListOut:
    rows, more = await listing.query_leads(session, f)
    return LeadListOut(items=[lead_out(r) for r in rows], page=f.page, has_more=more)


@router.get("/api/leads/{lead_id}", response_model=LeadOut)
async def api_detail(
    lead_id: uuid.UUID,
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> LeadOut:
    return lead_out(await listing.get_lead(session, lead_id))


@router.post("/api/leads/{lead_id}/stage", response_model=LeadOut)
async def api_stage(
    lead_id: uuid.UUID,
    body: StageIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> LeadOut:
    lead = await listing.get_lead(session, lead_id)
    await listing.move_stage(session, lead, body.stage, user)
    return lead_out(lead)


@router.post("/api/leads/import/preview")
async def api_import_preview(
    file: UploadFile = File(...),
    niche: str = Form(...),
    city: str = Form("Cali"),
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    niche, city = validate_niche_city(niche, city)
    data = await read_upload(file)
    preview = await build_preview(session, data, niche, city, {})
    listing.stash_upload(data, preview.sha256)
    return preview_json(preview)


@router.post("/api/leads/import/commit", status_code=201)
async def api_import_commit(
    request: Request,
    sha256: str = Form(...),
    niche: str = Form(...),
    city: str = Form("Cali"),
    force: bool = Form(False),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    niche, city = validate_niche_city(niche, city)
    form = await request.form()
    source = await commit_import(
        session,
        sha256=sha256,
        niche=niche,
        city=city,
        overrides=overrides_from_form(form),
        force=force,
        user=user,
    )
    return {"source_id": str(source.id), "summary": source.stats}


# --------------------------------------------------------------------------- paginas
def lead_ctx(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "stages": LEAD_STAGES,
        "bands": BANDS,
        "band_for": band_for,
        "hot": DEFAULT.hot,
        "warm": DEFAULT.warm,
        "niche_choices": NICHE_CHOICES,
    }
    ctx.update(extra or {})
    return ctx


@router.get("/admin/leads", response_class=HTMLResponse)
async def page_list(
    request: Request,
    f: LeadFilters = Depends(filters_dep),
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows, more = await listing.query_leads(session, f)
    qs = f.query_string()
    return render(
        request,
        "leads/list.html",
        lead_ctx(
            {
                "leads": rows,
                "f": f,
                "has_more": more,
                "base_url": "/admin/leads" + (f"?{qs}" if qs else ""),
                "export_url": "/admin/leads/export.csv" + (f"?{qs}" if qs else ""),
                "options": await listing.distinct_values(session),
                "view": "tabla",
            }
        ),
    )


@router.get("/admin/leads/kanban", response_class=HTMLResponse)
async def page_kanban(
    request: Request,
    f: LeadFilters = Depends(filters_dep),
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    return render(
        request,
        "leads/kanban.html",
        lead_ctx(
            {
                "columns": await listing.kanban_columns(session, f),
                "f": f,
                "limit": listing.KANBAN_COLUMN_LIMIT,
                "options": await listing.distinct_values(session),
                "view": "kanban",
            }
        ),
    )


@router.get("/admin/leads/export.csv")
async def export_csv(
    request: Request,
    f: LeadFilters = Depends(filters_dep),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows = await listing.all_matching(session, f)
    await audit.log_event(
        session,
        actor=user,
        action="lead.export",
        entity_type="lead",
        diff={"count": len(rows)},
        ip=request.client.host if request.client else None,
    )
    return Response(
        export_leads_csv(rows).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="leads.csv"'},
    )


@router.get("/admin/leads/buscar", response_class=HTMLResponse)
async def page_search(request: Request, _user: User = Depends(current_user)) -> Response:
    configured = bool(get_settings().google_places_api_key.get_secret_value())
    return render(
        request,
        "leads/search.html",
        lead_ctx(
            {
                "view": "buscar",
                "configured": configured,
                "monthly_budget": get_settings().google_places_budget_usd_month,
                "form": {"niche": "dentista", "city": "Cali", "max_results": 60, "min_reviews": 0},
            }
        ),
    )


@router.post("/admin/leads/buscar", response_class=HTMLResponse)
async def page_search_submit(
    request: Request,
    niche: str = Form(""),
    city: str = Form("Cali"),
    neighborhoods: str = Form("", max_length=2000),
    max_results: str = Form("60"),
    min_reviews: str = Form("0"),
    modo: str = Form("estimar"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        params = SearchParams(
            niche=niche,
            city=city,
            neighborhoods=parse_neighborhoods(neighborhoods),
            max_results=int(max_results or 60),
            min_reviews=int(min_reviews or 0),
        )
    except (ValidationError, ValueError) as exc:
        errors = (
            validation_messages(exc)
            if isinstance(exc, ValidationError)
            else ["Revisa los números ingresados"]
        )
        # 200 a proposito: HTMX no intercambia el contenido de respuestas 4xx.
        return render(request, "leads/_search_result.html", {"errors": errors})

    estimate = plan_search(params)
    if modo != "buscar":
        return render(
            request,
            "leads/_search_result.html",
            {"estimate": estimate, "params": params, "dry_run": True},
        )
    try:
        source_id = await launch_search(session, params, user, request)
    except AppError as exc:
        return render(request, "leads/_search_result.html", {"errors": [exc.message]})
    source = await get_source(session, source_id)
    return render(
        request,
        "leads/_status.html",
        {"status": source_status(source), "estimate": estimate},
    )


@router.get("/admin/leads/sources/{source_id}/status", response_class=HTMLResponse)
async def page_source_status(
    request: Request,
    source_id: uuid.UUID,
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    source = await get_source(session, source_id)
    return render(request, "leads/_status.html", {"status": source_status(source)})


@router.get("/admin/leads/importar", response_class=HTMLResponse)
async def page_import(request: Request, _user: User = Depends(current_user)) -> Response:
    return render(
        request,
        "leads/import.html",
        lead_ctx({"view": "importar", "form": {"niche": "dentista", "city": "Cali"}}),
    )


@router.post("/admin/leads/importar", response_class=HTMLResponse)
async def page_import_preview(
    request: Request,
    file: UploadFile = File(...),
    niche: str = Form(""),
    city: str = Form("Cali"),
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Paso 1: sube el archivo y muestra la vista previa (no escribe leads)."""
    niche, city = validate_niche_city(niche, city)
    data = await read_upload(file)
    preview = await build_preview(session, data, niche, city, {})
    listing.stash_upload(data, preview.sha256)
    return render(
        request,
        "leads/import_preview.html",
        lead_ctx(
            {
                "view": "importar",
                "p": preview,
                "niche": niche,
                "city": city,
                "mapping": preview.colmap.as_dict(),
            }
        ),
    )


@router.post("/admin/leads/importar/vista-previa", response_class=HTMLResponse)
async def page_import_repreview(
    request: Request,
    sha256: str = Form(...),
    niche: str = Form(...),
    city: str = Form("Cali"),
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Recalcula la vista previa con el mapeo de columnas elegido."""
    niche, city = validate_niche_city(niche, city)
    form = await request.form()
    overrides = overrides_from_form(form)
    data = listing.load_upload(sha256)
    preview = await build_preview(session, data, niche, city, overrides)
    return render(
        request,
        "leads/import_preview.html",
        lead_ctx(
            {
                "view": "importar",
                "p": preview,
                "niche": niche,
                "city": city,
                "mapping": preview.colmap.as_dict(),
            }
        ),
    )


@router.post("/admin/leads/importar/confirmar", response_class=HTMLResponse)
async def page_import_commit(
    request: Request,
    sha256: str = Form(...),
    niche: str = Form(...),
    city: str = Form("Cali"),
    force: bool = Form(False),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    niche, city = validate_niche_city(niche, city)
    form = await request.form()
    source = await commit_import(
        session,
        sha256=sha256,
        niche=niche,
        city=city,
        overrides=overrides_from_form(form),
        force=force,
        user=user,
    )
    return render(
        request,
        "leads/import_done.html",
        lead_ctx({"view": "importar", "summary": source.stats, "source": source}),
    )


@router.get("/admin/leads/{lead_id}", response_class=HTMLResponse)
async def page_detail(
    request: Request,
    lead_id: uuid.UUID,
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    lead = await listing.get_lead(session, lead_id)
    return render(
        request,
        "leads/detail.html",
        lead_ctx(
            {
                "lead": lead,
                "events": await listing.timeline(session, lead.id),
                "event_labels": EVENT_LABELS,
                "view": "detalle",
            }
        ),
    )


@router.post("/admin/leads/{lead_id}/etapa")
async def page_move_stage(
    request: Request,
    lead_id: uuid.UUID,
    stage: str = Form(...),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    lead = await listing.get_lead(session, lead_id)
    await listing.move_stage(session, lead, stage, user)
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return RedirectResponse(f"/admin/leads/{lead.id}", status_code=303)


@router.post("/admin/leads/{lead_id}/nota")
async def page_add_note(
    lead_id: uuid.UUID,
    text: Annotated[str, Form(max_length=listing.MAX_NOTE_CHARS)],
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    lead = await listing.get_lead(session, lead_id)
    await listing.add_note(session, lead, text, user)
    return RedirectResponse(f"/admin/leads/{lead.id}", status_code=303)


from app.leads.sales_router import router as sales_router  # noqa: E402

routers = [sales_router]
