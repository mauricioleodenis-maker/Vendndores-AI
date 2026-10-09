from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.core import jobs as core_jobs
from app.db.models.audit import AuditLog
from app.db.models.leads import Lead, LeadEvent, LeadSource
from tests.leads.conftest import DATA

CSV = (DATA / "maps_scraper_sample.csv").read_bytes()
HX = {"HX-Request": "true"}


@pytest.fixture
def make_lead(session) -> Callable[..., Any]:
    n = {"i": 0}

    async def _make(**kw: Any) -> Lead:
        n["i"] += 1
        defaults: dict[str, Any] = {
            "name": f"Negocio {n['i']}",
            "name_key": f"negocio {n['i']}",
            "niche": "dentista",
            "city": "Cali",
            "score": 50,
            "phone_e164": f"+57300000{n['i']:04d}",
        }
        defaults.update(kw)
        lead = Lead(**defaults)
        session.add(lead)
        await session.commit()
        return lead

    return _make


def csv_files(
    data: bytes = CSV, name: str = "leads.csv", ctype: str = "text/csv"
) -> dict[str, Any]:
    return {"file": (name, data, ctype)}


# ------------------------------------------------------------------ autenticacion
async def test_requires_login(client):
    r = await client.get("/admin/leads", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert (await client.get("/api/leads")).status_code == 401


async def test_csrf_enforced_on_mutations(authenticated_client, make_lead):
    lead = await make_lead()
    r = await authenticated_client.post(
        f"/admin/leads/{lead.id}/etapa", data={"stage": "demo"}, headers={"X-CSRF-Token": ""}
    )
    assert r.status_code == 403


# ------------------------------------------------------------------ tabla
async def test_list_page_filters_and_bands(authenticated_client, make_lead):
    await make_lead(name="Caliente SAS", score=85, stage="contactado")
    await make_lead(name="Tibio SAS", score=55)
    await make_lead(name="Frio SAS", score=10, niche="taller", city="Bogota")
    c = authenticated_client
    page = (await c.get("/admin/leads")).text
    assert "Caliente SAS" in page and "Tibio SAS" in page and "Frio SAS" in page
    assert page.index("Caliente SAS") < page.index("Tibio SAS") < page.index("Frio SAS")

    assert "Caliente SAS" not in (await c.get("/admin/leads?band=tibio")).text
    only_hot = (await c.get("/admin/leads?band=caliente")).text
    assert "Caliente SAS" in only_hot and "Tibio SAS" not in only_hot
    cold = (await c.get("/admin/leads?band=frio")).text
    assert "Frio SAS" in cold and "Tibio SAS" not in cold
    assert "Tibio SAS" not in (await c.get("/admin/leads?min_score=60")).text
    assert "Frio SAS" in (await c.get("/admin/leads?niche=taller")).text
    assert "Tibio SAS" not in (await c.get("/admin/leads?city=bogota")).text
    stage = (await c.get("/admin/leads?stage=contactado")).text
    assert "Caliente SAS" in stage and "Tibio SAS" not in stage
    assert "Tibio SAS" in (await c.get("/admin/leads?q=tibio")).text


async def test_search_term_escapes_like_wildcards(authenticated_client, make_lead):
    await make_lead(name="Dental 100% Sonrisa")
    await make_lead(name="Otro lugar")
    text = (await authenticated_client.get("/admin/leads?q=%25")).text
    assert "Dental 100% Sonrisa" in text and "Otro lugar" not in text


async def test_list_pagination_and_empty_state(authenticated_client, make_lead):
    assert "Sin leads" in (await authenticated_client.get("/admin/leads")).text
    for _ in range(27):
        await make_lead()
    p1 = (await authenticated_client.get("/admin/leads")).text
    assert "Siguiente" in p1
    p2 = (await authenticated_client.get("/admin/leads?page=2")).text
    assert "Anterior" in p2 and "Siguiente" not in p2


async def test_invalid_filters_are_rejected_or_ignored(authenticated_client):
    assert (await authenticated_client.get("/admin/leads?min_score=500")).status_code == 422
    assert (await authenticated_client.get("/admin/leads?stage=hack")).status_code == 200


# ------------------------------------------------------------------ kanban y etapas
async def test_kanban_groups_by_stage_and_hides_lost(authenticated_client, make_lead):
    await make_lead(name="En demo", stage="demo")
    await make_lead(name="Perdido SAS", disposition="perdido")
    text = (await authenticated_client.get("/admin/leads/kanban")).text
    assert "En demo" in text and "Perdido SAS" not in text
    assert "Prueba secreta" in text and "Cerrado" in text


async def test_move_stage_htmx_and_redirect(authenticated_client, make_lead, session):
    lead = await make_lead()
    c = authenticated_client
    r = await c.post(f"/admin/leads/{lead.id}/etapa", data={"stage": "contactado"}, headers=HX)
    assert r.status_code == 204 and r.headers["HX-Refresh"] == "true"
    r = await c.post(f"/admin/leads/{lead.id}/etapa", data={"stage": "demo"})
    assert r.status_code == 303 and r.headers["location"] == f"/admin/leads/{lead.id}"
    await session.refresh(lead)
    assert lead.stage == "demo"
    ev = (
        (await session.execute(select(LeadEvent).where(LeadEvent.kind == "stage_changed")))
        .scalars()
        .all()
    )
    assert [e.data["to"] for e in sorted(ev, key=lambda e: e.data["to"])] == ["contactado", "demo"]
    assert (
        await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "lead.stage_changed")
        )
        == 2
    )
    # mismo valor: sin evento nuevo
    await c.post(f"/admin/leads/{lead.id}/etapa", data={"stage": "demo"})
    assert await session.scalar(select(func.count()).select_from(LeadEvent)) == 2


async def test_move_stage_invalid(authenticated_client, make_lead):
    lead = await make_lead()
    r = await authenticated_client.post(f"/api/leads/{lead.id}/stage", json={"stage": "inventada"})
    assert r.status_code == 422


# ------------------------------------------------------------------ detalle
async def test_detail_page_notes_and_timeline(authenticated_client, make_lead, session):
    lead = await make_lead(
        name="Clinica <b>X</b>",
        score_breakdown={"rating": {"points": 15, "detail": "3.9"}},
        website="https://x.co",
        website_domain="x.co",
        maps_url="javascript:alert(1)",
    )
    c = authenticated_client
    r = await c.post(f"/admin/leads/{lead.id}/nota", data={"text": "Llamar el lunes <script>"})
    assert r.status_code == 303
    page = (await c.get(f"/admin/leads/{lead.id}")).text
    assert "Llamar el lunes &lt;script&gt;" in page
    assert "Clinica &lt;b&gt;X&lt;/b&gt;" in page and "<b>X</b>" not in page
    assert "javascript:alert" not in page
    assert "Nota" in page and "+15" in page
    empty = await c.post(f"/admin/leads/{lead.id}/nota", data={"text": "   "})
    assert empty.status_code == 422


async def test_detail_404(authenticated_client):
    r = await authenticated_client.get(f"/admin/leads/{uuid.uuid4()}")
    assert r.status_code == 404


# ------------------------------------------------------------------ buscador (UI)
async def test_search_page_warns_without_key(authenticated_client):
    text = (await authenticated_client.get("/admin/leads/buscar")).text
    assert "Falta configurar la clave" in text


async def test_search_estimate_makes_no_calls_and_creates_nothing(authenticated_client, session):
    r = await authenticated_client.post(
        "/admin/leads/buscar",
        data={
            "niche": "dentista",
            "city": "Cali",
            "neighborhoods": "Granada, Centro",
            "modo": "estimar",
        },
        headers=HX,
    )
    assert r.status_code == 200
    assert "Estimado de costo" in r.text and "dentista en Granada, Cali, Colombia" in r.text
    assert await session.scalar(select(func.count()).select_from(LeadSource)) == 0
    assert core_jobs.ENQUEUED == []


async def test_search_form_validation_errors_render_with_200(authenticated_client):
    r = await authenticated_client.post(
        "/admin/leads/buscar", data={"niche": "astronauta", "city": "Cali"}, headers=HX
    )
    assert r.status_code == 200 and "Revisa «Nicho»" in r.text
    r = await authenticated_client.post(
        "/admin/leads/buscar", data={"niche": "taller", "max_results": "abc"}, headers=HX
    )
    assert "Revisa los números" in r.text


async def test_search_launch_without_key_shows_error(authenticated_client, session):
    r = await authenticated_client.post(
        "/admin/leads/buscar", data={"niche": "taller", "modo": "buscar"}, headers=HX
    )
    assert "VAI_GOOGLE_PLACES_API_KEY" in r.text
    assert await session.scalar(select(func.count()).select_from(LeadSource)) == 0


async def test_search_launch_enqueues_job_and_status_polls(
    authenticated_client, session, places_key
):
    r = await authenticated_client.post(
        "/admin/leads/buscar",
        data={"niche": "dentista", "city": "Cali", "modo": "buscar"},
        headers=HX,
    )
    assert r.status_code == 200 and "En cola" in r.text and "hx-trigger" in r.text
    source = (await session.execute(select(LeadSource))).scalars().one()
    assert [("leads.search_places", (str(source.id),), {})] == core_jobs.ENQUEUED
    audit_n = await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "lead.search")
    )
    assert audit_n == 1

    poll = await authenticated_client.get(f"/admin/leads/sources/{source.id}/status", headers=HX)
    assert "En cola" in poll.text

    source.stats = {
        "status": "done",
        "found": 8,
        "created": 7,
        "merged": 1,
        "requests": 4,
        "cost_usd": 0.14,
    }
    await session.commit()
    done = await authenticated_client.get(f"/admin/leads/sources/{source.id}/status", headers=HX)
    assert "Terminada" in done.text and "hx-trigger" not in done.text and "Ver leads" in done.text
    source.stats = {"status": "failed", "error": "Google Places no esta disponible."}
    await session.commit()
    failed = await authenticated_client.get(f"/admin/leads/sources/{source.id}/status", headers=HX)
    assert "Fallida" in failed.text and "no esta disponible" in failed.text


# ------------------------------------------------------------------ API
async def test_api_search_dry_run_and_queued(authenticated_client, session, places_key):
    c = authenticated_client
    dry = await c.post(
        "/api/leads/search", json={"niche": "dentista", "neighborhoods": ["A"], "dry_run": True}
    )
    assert dry.status_code == 200
    body = dry.json()
    assert body["dry_run"] is True and body["max_requests"] == 9 and len(body["queries"]) == 3
    assert core_jobs.ENQUEUED == []

    queued = await c.post("/api/leads/search", json={"niche": "restaurante", "max_results": 20})
    assert queued.status_code == 202
    sid = queued.json()["source_id"]
    assert core_jobs.ENQUEUED[0][1] == (sid,)
    status = (await c.get(f"/api/leads/sources/{sid}/status")).json()
    assert status["status"] == "queued" and status["finished"] is False


async def test_api_search_validation_and_missing_key(authenticated_client):
    c = authenticated_client
    assert (await c.post("/api/leads/search", json={"niche": "nope"})).status_code == 422
    r = await c.post("/api/leads/search", json={"niche": "dentista"})
    assert r.status_code == 503 and r.json()["code"] == "places_not_configured"


async def test_api_search_is_rate_limited(authenticated_client, places_key):
    codes = [
        (await authenticated_client.post("/api/leads/search", json={"niche": "taller"})).status_code
        for _ in range(11)
    ]
    assert codes[:10] == [202] * 10 and codes[10] == 429


async def test_api_status_404(authenticated_client):
    r = await authenticated_client.get(f"/api/leads/sources/{uuid.uuid4()}/status")
    assert r.status_code == 404


async def test_api_list_detail_stage(authenticated_client, make_lead):
    lead = await make_lead(name="API Lead", score=72, rating=4.3, review_count=10)
    c = authenticated_client
    items = (await c.get("/api/leads?band=caliente")).json()
    assert items["items"][0]["name"] == "API Lead" and items["items"][0]["band"] == "caliente"
    assert items["has_more"] is False
    detail = (await c.get(f"/api/leads/{lead.id}")).json()
    assert detail["rating"] == 4.3
    moved = await c.post(f"/api/leads/{lead.id}/stage", json={"stage": "demo"})
    assert moved.status_code == 200 and moved.json()["stage"] == "demo"
    assert (await c.get(f"/api/leads/{uuid.uuid4()}")).status_code == 404


# ------------------------------------------------------------------ export
async def test_export_csv_neutralizes_formulas_and_audits(authenticated_client, make_lead, session):
    await make_lead(name='=HYPERLINK("http://evil")')
    r = await authenticated_client.get("/admin/leads/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "'=HYPERLINK" in r.text and ",=HYPERLINK" not in r.text
    assert (
        await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "lead.export")
        )
        == 1
    )


# ------------------------------------------------------------------ importar CSV
async def test_import_page(authenticated_client):
    assert "Ver vista previa" in (await authenticated_client.get("/admin/leads/importar")).text


async def test_import_html_flow_preview_then_commit(authenticated_client, session):
    c = authenticated_client
    r = await c.post(
        "/admin/leads/importar", files=csv_files(), data={"niche": "dentista", "city": "Cali"}
    )
    assert r.status_code == 200 and "Vista previa de importación" in r.text
    assert "Spa Médico Sol" in r.text
    # los formularios HTML traen el token CSRF real (no vacio)
    assert f'name="csrf_token" value="{c.csrf_token}"' in r.text
    assert await session.scalar(select(func.count()).select_from(Lead)) == 0  # solo vista previa

    from app.leads.csv_import import file_sha256

    sha = file_sha256(CSV)
    base = {"sha256": sha, "niche": "dentista", "city": "Cali"}
    again = await c.post("/admin/leads/importar/vista-previa", data={**base, "col_name": "Titulo"})
    assert again.status_code == 200 and "Columnas detectadas" in again.text

    done = await c.post("/admin/leads/importar/confirmar", data={**base, "col_name": "Titulo"})
    assert done.status_code == 200 and "Importación terminada" in done.text
    total = await session.scalar(select(func.count()).select_from(Lead))
    assert total and total > 0
    src = (await session.execute(select(LeadSource))).scalars().one()
    assert (
        src.kind == "csv_import" and src.params["sha256"] == sha and src.stats["created"] == total
    )

    # el archivo ya se importo: pide confirmacion explicita
    r2 = await c.post("/admin/leads/importar", files=csv_files(), data={"niche": "dentista"})
    assert "Importar de todas formas" in r2.text


async def test_import_commit_blocks_reupload_unless_forced(authenticated_client, session):
    c = authenticated_client
    pv = await c.post(
        "/api/leads/import/preview", files=csv_files(), data={"niche": "dentista", "city": "Cali"}
    )
    assert pv.status_code == 200
    j = pv.json()
    assert j["summary"]["created"] > 0 and j["reupload"] is False and "Titulo" in j["headers"]
    assert j["columns"]["name"] == "Titulo"
    form = {"sha256": j["sha256"], "niche": "dentista", "city": "Cali"}
    first = await c.post("/api/leads/import/commit", data=form)
    assert (
        first.status_code == 201 and first.json()["summary"]["created"] == j["summary"]["created"]
    )

    # el temporal se descarta tras confirmar
    gone = await c.post("/api/leads/import/commit", data=form)
    assert gone.status_code == 410

    await c.post("/api/leads/import/preview", files=csv_files(), data={"niche": "dentista"})
    blocked = await c.post("/api/leads/import/commit", data=form)
    assert blocked.status_code == 409 and blocked.json()["code"] == "csv_reupload"
    forced = await c.post("/api/leads/import/commit", data={**form, "force": "true"})
    assert forced.status_code == 201 and forced.json()["summary"]["created"] == 0


@pytest.mark.parametrize(
    ("name", "ctype", "data", "status"),
    [
        ("leads.exe", "text/csv", b"a,b", 415),
        ("leads.csv", "image/png", b"a,b", 415),
        ("leads.csv", "text/csv", b"", 422),
        ("leads.csv", "text/csv", b"telefono,direccion\n1,2\n", 422),
    ],
)
async def test_import_rejects_bad_uploads(authenticated_client, name, ctype, data, status):
    r = await authenticated_client.post(
        "/api/leads/import/preview",
        files=csv_files(data, name, ctype),
        data={"niche": "dentista", "city": "Cali"},
    )
    assert r.status_code == status


async def test_import_rejects_oversize_and_bad_params(authenticated_client):
    big = b"name\n" + b"x" * (5 * 1024 * 1024 + 10)
    r = await authenticated_client.post(
        "/api/leads/import/preview", files=csv_files(big), data={"niche": "dentista"}
    )
    assert r.status_code == 413
    bad = await authenticated_client.post(
        "/api/leads/import/preview", files=csv_files(), data={"niche": "magia", "city": "Cali"}
    )
    assert bad.status_code == 422
    city = await authenticated_client.post(
        "/api/leads/import/preview", files=csv_files(), data={"niche": "taller", "city": "x"}
    )
    assert city.status_code == 422


async def test_import_commit_rejects_bad_sha_and_tampering(authenticated_client, tmp_path: Path):
    from app.leads import listing

    c = authenticated_client
    form = {"niche": "dentista", "city": "Cali"}
    assert (
        await c.post("/api/leads/import/commit", data={**form, "sha256": "../../etc"})
    ).status_code == 422
    sha = "a" * 64
    listing.stash_upload(b"name\nx\n", sha)
    r = await c.post("/api/leads/import/commit", data={**form, "sha256": sha})
    assert r.status_code == 422 and r.json()["code"] == "invalid_upload"
    listing.discard_upload(sha)
    with pytest.raises(Exception):
        listing.stash_upload(b"x", "nothex")


async def test_column_override_maps_custom_headers(authenticated_client, session):
    data = b"Razon,Cel\nDental Uno,3001234567\n"
    c = authenticated_client
    # sin columna de nombre reconocible: error claro
    bad = await c.post(
        "/api/leads/import/preview", files=csv_files(data), data={"niche": "dentista"}
    )
    assert bad.status_code == 422 and bad.json()["code"] == "csv_sin_nombre"

    from app.leads import listing
    from app.leads.csv_import import file_sha256

    sha = file_sha256(data)
    listing.stash_upload(data, sha)
    r = await c.post(
        "/admin/leads/importar/vista-previa",
        data={
            "sha256": sha,
            "niche": "dentista",
            "city": "Cali",
            "col_name": "Razon",
            "col_phone": "Cel",
        },
    )
    assert r.status_code == 200 and "Dental Uno" in r.text and "+573001234567" in r.text
    done = await c.post(
        "/admin/leads/importar/confirmar",
        data={
            "sha256": sha,
            "niche": "dentista",
            "city": "Cali",
            "col_name": "Razon",
            "col_phone": "Cel",
        },
    )
    assert "Se crearon 1 leads nuevos" in done.text
    assert (await session.execute(select(Lead.name))).scalars().all() == ["Dental Uno"]


def test_upload_purge_removes_old_files():
    import os
    import time

    from app.leads import listing

    sha = "b" * 64
    listing.stash_upload(b"name\nx\n", sha)
    path = listing.upload_dir() / f"{sha}.csv"
    old = time.time() - listing.UPLOAD_TTL_S - 10
    os.utime(path, (old, old))
    listing.stash_upload(b"name\ny\n", "c" * 64)
    assert not path.exists()
    listing.discard_upload("c" * 64)
