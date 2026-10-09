from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rate_limit
from app.core.deps import csrf_guard
from app.core.errors import AppError
from app.db.models.bots import BotConfig
from app.db.models.leads import Lead
from app.leads import demo, sales_router
from app.plans import catalog
from tests.leads.test_sales_pipeline import make_lead


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    """Registra las rutas de ventas (el integrador las une a ``leads.router.routers``)."""
    if not any(getattr(r, "path", "").startswith("/demo/") for r in app.routes):
        app.include_router(sales_router.router, dependencies=[Depends(csrf_guard)])
    return app


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_build(session: AsyncSession, tenant_id: Any, inputs: Any, **kw: Any):
        bot = BotConfig(tenant_id=tenant_id, version=1, status="draft")
        session.add(bot)
        await session.flush()
        return bot

    async def fake_sandbox(session: Any, tenant_id: Any, history: Any, text: str, **kw: Any):
        return f"eco: {text}"

    monkeypatch.setattr(demo, "build_bot", fake_build)
    monkeypatch.setattr(demo, "sandbox_reply", fake_sandbox)
    rate_limit.set_rate_limiter(rate_limit.MemoryRateLimiter())


@pytest_asyncio.fixture
async def lead(session: AsyncSession) -> Lead:
    await catalog.seed(session)
    lead = await make_lead(session, phone_e164="+573001112233", phone_type="mobile")
    await session.commit()
    return lead


async def test_requires_auth(client: httpx.AsyncClient, lead: Lead) -> None:
    r = await client.get(f"/api/leads/{lead.id}/pitch-evidence")
    assert r.status_code == 401


async def test_secret_shop_flow_api(authenticated_client: httpx.AsyncClient, lead: Lead) -> None:
    c = authenticated_client
    r = await c.get(f"/api/leads/{lead.id}/secret-shop/script", params={"scenario": "cita"})
    assert "limpieza dental" in r.json()["script"]
    sent = datetime.now(UTC) - timedelta(minutes=30)
    r = await c.post(
        f"/api/leads/{lead.id}/secret-shop",
        json={"scenario": "precio", "sent_at": sent.isoformat()},
    )
    assert r.status_code == 201, r.text
    test_id = r.json()["id"]
    assert (await c.post(f"/api/leads/{lead.id}/secret-shop", json={})).status_code == 409
    reply_at = sent + timedelta(minutes=3)
    r = await c.post(
        f"/api/secret-shop/{test_id}/reply",
        json={"first_reply_at": reply_at.isoformat(), "excerpt": "Hola"},
    )
    assert r.status_code == 200 and r.json()["outcome"] == "respuesta_ok"
    ev = (await c.get(f"/api/leads/{lead.id}/pitch-evidence")).json()
    assert ev["response_minutes"] == 3


async def test_demo_and_convert_api(authenticated_client: httpx.AsyncClient, lead: Lead) -> None:
    c = authenticated_client
    r = await c.post(f"/api/leads/{lead.id}/demo-bot")
    assert r.status_code == 200, r.text
    link = r.json()["demo_link"]
    page = await c.get(link.split("://test", 1)[1])
    assert page.status_code == 200 and "Esto es una demostración" in page.text
    assert page.headers["x-robots-tag"].startswith("noindex")
    r = await c.post(f"/api/leads/{lead.id}/convert", json={"plan_code": "pro"})
    assert r.status_code == 200 and r.json()["billing_record_created"]
    assert (
        await c.post(f"/api/leads/{lead.id}/convert", json={"plan_code": "pro"})
    ).status_code == 409


async def test_convert_forbidden_for_operator(
    client: httpx.AsyncClient, make_user: Any, login: Any, lead: Lead
) -> None:
    op = await make_user("operator")
    await login(client, op)
    assert (
        await client.post(f"/api/leads/{lead.id}/convert", json={"plan_code": "pro"})
    ).status_code == 403
    assert (await client.post(f"/api/leads/{lead.id}/demo-bot")).status_code == 200


async def test_public_demo_chat(
    client: httpx.AsyncClient, authenticated_client: httpx.AsyncClient, lead: Lead
) -> None:
    r = await authenticated_client.post(f"/api/leads/{lead.id}/demo-bot")
    token = r.json()["demo_link"].rsplit("/", 1)[1]
    anon = httpx.AsyncClient(transport=client._transport, base_url="http://test")
    async with anon:
        page = await anon.get(f"/demo/{token}")
        assert page.status_code == 200
        r = await anon.post(
            f"/demo/{token}/mensaje",
            data={"text": "hola <b>", "history": "no-json"},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200 and "eco: hola" in r.text and "<b>" not in r.text
        assert "<html" not in r.text  # parcial HTMX
        bad = await anon.get("/demo/token-falso")
        assert bad.status_code == 404
        empty = await anon.post(f"/demo/{token}/mensaje", data={"text": " ", "history": "[]"})
        assert empty.status_code == 200 and "Escribe un mensaje" in empty.text


async def test_public_demo_cap_message(
    client: httpx.AsyncClient,
    authenticated_client: httpx.AsyncClient,
    lead: Lead,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "demo_max_messages", 1)
    r = await authenticated_client.post(f"/api/leads/{lead.id}/demo-bot")
    token = r.json()["demo_link"].rsplit("/", 1)[1]
    anon = httpx.AsyncClient(transport=client._transport, base_url="http://test")
    async with anon:
        await anon.post(f"/demo/{token}/mensaje", data={"text": "uno"})
        r = await anon.post(f"/demo/{token}/mensaje", data={"text": "dos"})
        assert "límite de mensajes" in r.text


async def test_ui_partials(authenticated_client: httpx.AsyncClient, lead: Lead) -> None:
    c = authenticated_client
    h = {"HX-Request": "true"}
    r = await c.get(f"/admin/leads/{lead.id}/ventas")
    assert r.status_code == 200 and "Registrar prueba secreta" in r.text
    r = await c.post(
        f"/admin/leads/{lead.id}/ventas/prueba", data={"scenario": "precio"}, headers=h
    )
    assert "Prueba secreta registrada" in r.text and "Registrar respuesta" in r.text
    r = await c.post(
        f"/admin/leads/{lead.id}/ventas/prueba", data={"scenario": "precio"}, headers=h
    )
    assert "alert-error" in r.text  # duplicada: error mostrado, no 500
    when = (datetime.now(UTC) + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    r = await c.post(
        f"/admin/leads/{lead.id}/ventas/respuesta",
        data={"first_reply_at": when, "excerpt": "ok"},
        headers=h,
    )
    assert "Respuesta registrada" in r.text
    r = await c.post(f"/admin/leads/{lead.id}/ventas/demo", data={}, headers=h)
    assert "Demo lista" in r.text and "/demo/" in r.text and "Convertir en cliente" in r.text
    r = await c.post(
        f"/admin/leads/{lead.id}/ventas/convertir", data={"plan_code": "pro"}, headers=h
    )
    assert "Cliente creado" in r.text
    r = await c.post(
        f"/admin/leads/{lead.id}/ventas/convertir", data={"plan_code": "pro"}, headers=h
    )
    assert "alert-error" in r.text


async def test_ui_reply_without_test_and_niche_prompt(
    authenticated_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    c = authenticated_client
    otro = await make_lead(session, name="Sin nicho", niche="otro")
    await session.commit()
    h = {"HX-Request": "true"}
    r = await c.post(
        f"/admin/leads/{otro.id}/ventas/respuesta",
        data={"first_reply_at": "2026-01-01T10:00:00+00:00"},
        headers=h,
    )
    assert "Aún no hay prueba secreta" in r.text
    page = await c.get(f"/admin/leads/{otro.id}/ventas")
    assert 'name="niche"' in page.text
    r = await c.post(f"/admin/leads/{otro.id}/ventas/demo", data={}, headers=h)
    assert "Elige el tipo de negocio" in r.text


async def test_public_demo_llm_failure_is_controlled(
    client: httpx.AsyncClient,
    authenticated_client: httpx.AsyncClient,
    lead: Lead,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = (
        (await authenticated_client.post(f"/api/leads/{lead.id}/demo-bot"))
        .json()["demo_link"]
        .rsplit("/", 1)[1]
    )

    async def boom(*a: Any, **kw: Any) -> str:
        raise RuntimeError("secreto-interno")

    monkeypatch.setattr(demo, "sandbox_reply", boom)
    anon = httpx.AsyncClient(transport=client._transport, base_url="http://test")
    async with anon:
        r = await anon.post(f"/demo/{token}/mensaje", data={"text": "hola", "history": "[]"})
    assert r.status_code == 200
    assert "no está disponible" in r.text and "secreto-interno" not in r.text


async def test_public_demo_headers_and_ip_limit(
    client: httpx.AsyncClient,
    authenticated_client: httpx.AsyncClient,
    lead: Lead,
    session: AsyncSession,
) -> None:
    token = (
        (await authenticated_client.post(f"/api/leads/{lead.id}/demo-bot"))
        .json()["demo_link"]
        .rsplit("/", 1)[1]
    )
    anon = httpx.AsyncClient(transport=client._transport, base_url="http://test")
    async with anon:
        page = await anon.get(f"/demo/{token}")
        assert page.headers["referrer-policy"] == "no-referrer"
        assert page.headers["cache-control"] == "no-store"
        tenant = await demo.load_demo_tenant(session, token)
        with pytest.raises(AppError) as exc:
            for _ in range(demo.IP_MSGS_PER_MIN + 1):
                await demo.demo_chat_reply(
                    session, token, [], "x", tenant=tenant, client_ip="9.9.9.9"
                )
        assert exc.value.status == 429


async def test_ui_reply_naive_time_is_bogota(
    authenticated_client: httpx.AsyncClient, lead: Lead, session: AsyncSession
) -> None:
    from app.leads import secret_shop

    c = authenticated_client
    sent = datetime.now(UTC) - timedelta(hours=2)
    await c.post(f"/api/leads/{lead.id}/secret-shop", json={"sent_at": sent.isoformat()})
    # hora local de Bogota (UTC-5) hace 1h, enviada como datetime-local (sin zona)
    local = (datetime.now(UTC) - timedelta(hours=1) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M")
    r = await c.post(f"/admin/leads/{lead.id}/ventas/respuesta", data={"first_reply_at": local})
    assert r.status_code == 200 and "Respuesta registrada" in r.text
    assert "HX-Trigger" in r.headers or "hx-trigger" in r.headers
    test = await secret_shop.latest_test(session, lead.id)
    await session.refresh(test)
    assert 3000 < test.response_seconds < 4300  # ~1 h, no -4 h ni +6 h
