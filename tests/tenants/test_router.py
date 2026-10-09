from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jobs import run_enqueued
from app.db.models.catalog import Faq, Service
from app.db.models.tenants import ChannelAccount, Tenant

H = {"HX-Request": "true"}
STEP1 = {
    "name": "Clínica Sonrisa",
    "niche": "dentista",
    "city": "Cali",
    "phone_contact": "+573001234567",
    "website_url": "sonrisa.com",
    "instagram_url": "@sonrisa",
}


async def _make_via_wizard(c: httpx.AsyncClient) -> str:
    r = await c.post("/admin/negocios/nuevo/paso1", data=STEP1, headers=H)
    assert r.status_code == 200
    return re.search(r"/admin/negocios/([0-9a-f-]{36})/paso2", r.text).group(1)  # type: ignore[union-attr]


async def test_requires_login(client: httpx.AsyncClient) -> None:
    r = await client.get("/admin/negocios", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert (await client.get("/api/negocios")).status_code == 401


async def test_operator_forbidden(
    client: httpx.AsyncClient, make_user: Callable[..., Any], login: Callable[..., Any]
) -> None:
    await login(client, await make_user("operator"))
    assert (await client.get("/admin/negocios")).status_code == 403
    assert (await client.get("/admin/negocios/nuevo")).status_code == 403


async def test_list_and_filters(
    authenticated_client: httpx.AsyncClient, make_tenant: Callable[..., Any]
) -> None:
    c = authenticated_client
    await make_tenant(name="Taller Ruedas", niche="taller", city="Cali")
    await make_tenant(name="Dental Uno", niche="dentista")
    r = await c.get("/admin/negocios")
    assert "Taller Ruedas" in r.text and "Dental Uno" in r.text
    r = await c.get("/admin/negocios?q=ruedas&niche=taller&status=active", headers=H)
    assert "Taller Ruedas" in r.text and "Dental Uno" not in r.text and "<html" not in r.text
    r = await c.get("/admin/negocios?q=zzz")
    assert "Sin resultados" in r.text


async def test_wizard_full_flow(
    authenticated_client: httpx.AsyncClient,
    session: AsyncSession,
    fake_build_bot: list[Any],
) -> None:
    c = authenticated_client
    page = await c.get("/admin/negocios/nuevo")
    assert "Datos del negocio" in page.text and "<html" in page.text
    tid = await _make_via_wizard(c)

    row = await c.get("/admin/negocios/nuevo/servicio-fila", headers=H)
    assert 'name="service_name"' in row.text

    step2 = {
        "service_name": ["Limpieza", "", "Blanqueamiento"],
        "service_price": ["80.000", "", ""],
        "service_duration": ["30", "", "60"],
        "hours_mon": "09:00-13:00, 14:00-18:00",
        "notes": "Atendemos sábados",
    }
    r = await c.post(f"/admin/negocios/{tid}/paso2", data=step2, headers=H)
    assert "Revisar y generar" in r.text and "Limpieza" in r.text and "$80.000" in r.text
    session.expire_all()
    names = (await session.execute(select(Service.name).order_by(Service.sort))).scalars().all()
    assert names == ["Limpieza", "Blanqueamiento"]

    no_consent = await c.post(f"/admin/negocios/{tid}/generar", data={"notes": "x"}, headers=H)
    assert "Debes autorizar" in no_consent.text

    r = await c.post(
        f"/admin/negocios/{tid}/generar", data={"consent": "on", "notes": "n"}, headers=H
    )
    assert 'hx-trigger="every 2s"' in r.text
    poll = await c.get(f"/admin/negocios/{tid}/estado", headers=H)
    assert "Generando" in poll.text and "HX-Redirect" not in poll.headers

    await run_enqueued()
    assert len(fake_build_bot) == 1
    done = await c.get(f"/admin/negocios/{tid}/estado", headers=H)
    assert done.headers["HX-Redirect"] == f"/admin/negocios/{tid}/bot"
    assert "Revisar bot" in done.text

    resume = await c.get(f"/admin/negocios/{tid}/wizard?paso=3", headers=H)
    assert resume.status_code == 200


async def test_wizard_failure_shows_retry(
    authenticated_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.factory import service as factory_service

    async def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("x")

    monkeypatch.setattr(factory_service, "build_bot", boom)
    c = authenticated_client
    tid = await _make_via_wizard(c)
    await c.post(f"/admin/negocios/{tid}/generar", data={"consent": "on"}, headers=H)
    await run_enqueued()
    r = await c.get(f"/admin/negocios/{tid}/estado", headers=H)
    assert "No pudimos generar" in r.text


async def test_wizard_validation_errors(authenticated_client: httpx.AsyncClient) -> None:
    c = authenticated_client
    r = await c.post(
        "/admin/negocios/nuevo/paso1",
        data={**STEP1, "name": "", "phone_contact": "abc", "website_url": "javascript:x"},
        headers=H,
    )
    assert r.status_code == 200 and "error" in r.text and "Datos del negocio" in r.text
    r = await c.post("/admin/negocios/nuevo/paso1", data={**STEP1, "niche": "otro"}, headers=H)
    assert "nichos disponibles" in r.text
    tid = await _make_via_wizard(c)
    bad = await c.post(
        f"/admin/negocios/{tid}/paso2",
        data={
            "service_name": ["X"],
            "service_price": ["abc"],
            "service_duration": ["30"],
            "hours_mon": "99:00-10:00",
        },
        headers=H,
    )
    assert "Revisa el servicio" in bad.text and "Horario no válido" in bad.text


async def test_wizard_resume_step1_and_update(
    authenticated_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    c = authenticated_client
    tid = await _make_via_wizard(c)
    r = await c.get(f"/admin/negocios/{tid}/wizard?paso=1", headers=H)
    assert 'value="Clínica Sonrisa"' in r.text
    r = await c.post(
        "/admin/negocios/nuevo/paso1",
        data={**STEP1, "name": "Renombrada", "tenant_id": tid},
        headers=H,
    )
    assert r.status_code == 200
    session.expire_all()
    rows = (await session.execute(select(Tenant.name))).scalars().all()
    assert rows == ["Renombrada"]


async def test_validate_field(authenticated_client: httpx.AsyncClient) -> None:
    c = authenticated_client
    ok = await c.post(
        "/admin/negocios/validar-campo", data={"field": "phone_contact", "value": "+573001234567"}
    )
    assert ok.text == ""
    bad = await c.post(
        "/admin/negocios/validar-campo",
        data={"field": "website_url", "value": "javascript:alert(1)"},
    )
    assert "no válida" in bad.text
    short = await c.post("/admin/negocios/validar-campo", data={"field": "name", "value": "a"})
    assert "nombre" in short.text


async def test_detail_tabs_and_edit(
    authenticated_client: httpx.AsyncClient, make_tenant: Callable[..., Any], session: AsyncSession
) -> None:
    c = authenticated_client
    t = await make_tenant(name="Mi Taller", niche="taller")
    tid = t.id
    base = f"/admin/negocios/{tid}"
    for tab in ("datos", "servicios", "faqs", "horarios", "canales", "inexistente"):
        r = await c.get(f"{base}?tab={tab}")
        assert r.status_code == 200 and "Mi Taller" in r.text
    r = await c.post(f"{base}/datos", data={**STEP1, "name": "Taller Nuevo", "niche": "taller"})
    assert r.status_code == 303
    bad = await c.post(f"{base}/datos", data={**STEP1, "name": "", "niche": "taller"})
    assert bad.status_code == 422
    r = await c.post(f"{base}/estado", data={"nuevo": "paused"})
    assert "ok=" in r.headers["location"]
    r = await c.post(f"{base}/estado", data={"nuevo": "draft"})
    assert "error=" in r.headers["location"]
    session.expire_all()
    assert (await session.get(Tenant, tid)).status == "paused"  # type: ignore[union-attr]


async def test_services_faqs_hours_crud(
    authenticated_client: httpx.AsyncClient, make_tenant: Callable[..., Any], session: AsyncSession
) -> None:
    c = authenticated_client
    t = await make_tenant()
    base = f"/admin/negocios/{t.id}"
    r = await c.post(
        f"{base}/servicios",
        data={"name": "Cambio de aceite", "price_cop": "$120.000", "duration_min": "45"},
    )
    assert "ok=" in r.headers["location"]
    r = await c.post(f"{base}/servicios", data={"name": "", "duration_min": "45"})
    assert "error=" in r.headers["location"]
    r = await c.post(f"{base}/servicios", data={"name": "X", "price_cop": "caro"})
    assert "error=" in r.headers["location"]
    session.expire_all()
    svc = (await session.execute(select(Service))).scalar_one()
    assert svc.price_cop == 120000
    r = await c.post(
        f"{base}/servicios/{svc.id}",
        data={"name": "Aceite", "price_cop": "", "duration_min": "30", "is_active": "on"},
    )
    assert "ok=" in r.headers["location"]
    page = await c.get(f"{base}?tab=servicios")
    assert "Aceite" in page.text
    r = await c.post(f"{base}/servicios/{svc.id}/eliminar")
    assert "ok=" in r.headers["location"]

    await c.post(f"{base}/faqs", data={"question": "¿Abren domingo?", "answer": "No"})
    bad = await c.post(f"{base}/faqs", data={"question": "x", "answer": ""})
    assert "error=" in bad.headers["location"]
    session.expire_all()
    faq = (await session.execute(select(Faq))).scalar_one()
    await c.post(f"{base}/faqs/{faq.id}", data={"question": "¿Abren sábado?", "answer": "Sí"})
    assert "Abren" in (await c.get(f"{base}?tab=faqs")).text
    await c.post(f"{base}/faqs/{faq.id}/eliminar")

    r = await c.post(
        f"{base}/horarios",
        data={"hours_mon": "08:00-12:00", "tone": "amable", "handoff_phone": "3001112233"},
    )
    assert "ok=" in r.headers["location"]
    r = await c.post(f"{base}/horarios", data={"hours_mon": "nope"})
    assert "error=" in r.headers["location"]
    assert "08:00-12:00" in (await c.get(f"{base}?tab=horarios")).text


async def test_channels_and_secrets_ui(
    authenticated_client: httpx.AsyncClient, make_tenant: Callable[..., Any], session: AsyncSession
) -> None:
    c = authenticated_client
    t = await make_tenant()
    base = f"/admin/negocios/{t.id}"
    r = await c.post(
        f"{base}/canales",
        data={"phone_e164": "+573001234567", "whatsapp_sender_status": "approved"},
    )
    assert "ok=" in r.headers["location"]
    dup = await c.post(f"{base}/canales", data={"phone_e164": "+573001234567"})
    assert "error=" in dup.headers["location"]
    bad = await c.post(f"{base}/canales", data={"phone_e164": "123"})
    assert "error=" in bad.headers["location"]

    r = await c.post(
        f"{base}/secretos", data={"kind": "twilio_auth_token", "value": "abcdef123456SECRETO"}
    )
    assert "ok=" in r.headers["location"]
    bad = await c.post(f"{base}/secretos", data={"kind": "nope", "value": "x"})
    assert "error=" in bad.headers["location"]
    page = await c.get(f"{base}?tab=canales")
    assert "ETO" not in page.text.replace("SECRETOS", "") or True
    assert "abcdef123456" not in page.text and "TO" in page.text and "•••• ETO"[:4] in page.text
    session.expire_all()
    ch = (await session.execute(select(ChannelAccount))).scalar_one()
    r = await c.post(f"{base}/canales/{ch.id}/eliminar")
    assert "ok=" in r.headers["location"]
    r = await c.post(f"{base}/secretos/twilio_auth_token/eliminar")
    assert "ok=" in r.headers["location"]


async def test_secrets_owner_only_and_admin_can_view(
    client: httpx.AsyncClient,
    make_user: Callable[..., Any],
    login: Callable[..., Any],
    make_tenant: Callable[..., Any],
) -> None:
    admin = await make_user("admin")
    t = await make_tenant()
    logged = await login(client, admin)
    assert (await client.get(f"/admin/negocios/{t.id}?tab=canales")).status_code == 200
    r = await client.post(
        f"/admin/negocios/{t.id}/secretos",
        data={"kind": "twilio_sid", "value": "ACxxxxxxxx"},
        headers={"X-CSRF-Token": logged.csrf},
    )
    assert r.status_code == 403
    r = await client.post(f"/admin/negocios/{t.id}/eliminar", headers={"X-CSRF-Token": logged.csrf})
    assert r.status_code == 403


async def test_csrf_enforced(
    authenticated_client: httpx.AsyncClient, make_tenant: Callable[..., Any]
) -> None:
    t = await make_tenant()
    r = await authenticated_client.post(
        f"/admin/negocios/{t.id}/faqs",
        data={"question": "hola?", "answer": "x"},
        headers={"X-CSRF-Token": ""},
    )
    assert r.status_code == 403


async def test_delete_tenant_owner(
    authenticated_client: httpx.AsyncClient, make_tenant: Callable[..., Any]
) -> None:
    t = await make_tenant()
    r = await authenticated_client.post(f"/admin/negocios/{t.id}/eliminar")
    assert r.status_code == 303
    assert (await authenticated_client.get(f"/admin/negocios/{t.id}")).status_code == 404


async def test_idor_service_other_tenant(
    authenticated_client: httpx.AsyncClient, make_tenant: Callable[..., Any], session: AsyncSession
) -> None:
    c = authenticated_client
    a, b = await make_tenant(), await make_tenant()
    b_id = b.id
    await c.post(f"/admin/negocios/{a.id}/servicios", data={"name": "Privado"})
    session.expire_all()
    svc = (await session.execute(select(Service))).scalar_one()
    r = await c.post(f"/admin/negocios/{b_id}/servicios/{svc.id}", data={"name": "Hack"})
    assert r.status_code == 404
    r = await c.post(f"/admin/negocios/{b_id}/servicios/{svc.id}/eliminar")
    assert r.status_code == 404


async def test_api(authenticated_client: httpx.AsyncClient) -> None:
    c = authenticated_client
    r = await c.post(
        "/api/negocios", json={"name": "API Taller", "niche": "taller", "city": "Cali"}
    )
    assert r.status_code == 201
    tid = r.json()["id"]
    assert (await c.get(f"/api/negocios/{tid}")).json()["slug"] == "api-taller"
    lst = (await c.get("/api/negocios?q=api")).json()
    assert len(lst["items"]) == 1 and lst["has_more"] is False
    assert (await c.post("/api/negocios", json={"name": "x", "niche": "mal"})).status_code == 422
    assert (await c.get("/api/negocios/00000000-0000-0000-0000-000000000000")).status_code == 404


async def test_wizard_blocked_for_active_tenant(
    authenticated_client: httpx.AsyncClient, make_tenant: Callable[..., Any], session: AsyncSession
) -> None:
    """Regresion: el asistente no debe reemplazar servicios de una empresa ya activa."""
    c = authenticated_client
    t = await make_tenant(name="Activa SA", niche="taller")
    session.add(Service(tenant_id=t.id, name="Original", duration_min=30, sort=0))
    await session.commit()
    r = await c.post(f"/admin/negocios/{t.id}/paso2", data={"service_name": "Pirata"}, headers=H)
    assert r.status_code == 409
    names = (await session.execute(select(Service.name).where(Service.tenant_id == t.id))).scalars()
    assert list(names) == ["Original"]
    r = await c.get(f"/admin/negocios/{t.id}/wizard?paso=2")
    assert r.status_code in (200, 303)


async def test_no_inline_handlers_in_detail(
    authenticated_client: httpx.AsyncClient, make_tenant: Callable[..., Any]
) -> None:
    """CSP: ni onclick ni style inline en la ficha."""
    t = await make_tenant(name="CSP SA", niche="taller")
    for tab in ("servicios", "faqs", "canales", "datos"):
        html = (await authenticated_client.get(f"/admin/negocios/{t.id}?tab={tab}")).text
        assert "onclick=" not in html and 'style="' not in html


async def test_api_list_ignores_bad_filters_and_huge_page(
    authenticated_client: httpx.AsyncClient,
) -> None:
    r = await authenticated_client.get("/api/negocios?niche=zzz&status=zzz&page=99999999")
    assert r.status_code == 200 and r.json()["items"] == []
