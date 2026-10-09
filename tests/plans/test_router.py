from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.usefixtures("seeded")


async def test_public_plans_no_auth(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/plans")
    assert r.status_code == 200
    data = r.json()
    assert [p["code"] for p in data] == ["basico", "pro", "premium"]
    assert data[1]["monthly_fee_cop"] == 390_000
    assert "id" not in data[0]


async def test_admin_endpoints_require_auth(client: httpx.AsyncClient, tenant: Any) -> None:
    assert (await client.get("/api/offers")).status_code == 401
    assert (await client.get(f"/api/tenants/{tenant.id}/entitlements")).status_code == 401
    assert (
        await client.get("/admin/planes", headers={"accept": "application/json"})
    ).status_code == 401


async def test_operator_forbidden(
    client: httpx.AsyncClient, make_user: Callable[..., Any], login: Callable[..., Any]
) -> None:
    await login(client, await make_user("operator"))
    assert (await client.get("/api/offers")).status_code == 403
    assert (await client.get("/admin/planes")).status_code == 403


async def test_offers_and_quote(authenticated_client: httpx.AsyncClient) -> None:
    c = authenticated_client
    offers = (await c.get("/api/offers")).json()
    assert offers[0]["code"] == "fundador" and offers[0]["remaining"] == 5
    r = await c.post("/api/plans/quote", json={"plan_code": "pro", "offer_code": "fundador"})
    assert r.status_code == 200
    body = r.json()
    assert body["setup_net_cop"] == 0 and body["total_first_invoice_cop"] == 390_000
    r = await c.post("/api/plans/quote", json={"plan_code": "nope"})
    assert r.status_code == 404
    r = await c.post("/api/plans/quote", json={"plan_code": "pro", "custom_monthly_fee_cop": -5})
    assert r.status_code == 422


async def test_quote_requires_csrf(authenticated_client: httpx.AsyncClient) -> None:
    r = await authenticated_client.post(
        "/api/plans/quote", json={"plan_code": "pro"}, headers={"X-CSRF-Token": ""}
    )
    assert r.status_code == 403


async def test_subscription_lifecycle(authenticated_client: httpx.AsyncClient, tenant: Any) -> None:
    c = authenticated_client
    url = f"/api/tenants/{tenant.id}/subscription"
    assert (await c.get(url)).status_code == 404
    r = await c.post(url, json={"plan_code": "basico", "offer_code": "fundador"})
    assert r.status_code == 201
    sub = r.json()
    assert sub["plan_code"] == "basico" and sub["offer_code"] == "fundador"
    assert sub["quote"]["total_first_invoice_cop"] == 250_000
    assert (await c.post(url, json={"plan_code": "pro"})).status_code == 409

    r = await c.patch(url, json={"plan_code": "pro"})
    assert r.status_code == 200 and r.json()["plan_code"] == "pro"
    r = await c.patch(url, json={})
    assert r.status_code == 200 and r.json()["plan_code"] == "pro"
    r = await c.patch(url, json={"status": "past_due"})
    assert r.json()["status"] == "past_due"

    ent = (await c.get(f"/api/tenants/{tenant.id}/entitlements")).json()
    assert ent["plan_code"] == "pro" and ent["limits"]["max_conversations_month"] == 1500
    assert ent["conversations_pct"] == 0 and ent["near_conversation_limit"] is False

    r = await c.patch(url, json={"status": "cancelled"})
    assert r.json()["guarantee_status"] == "reclamada"
    assert (await c.get(url)).status_code == 404
    assert (await c.patch(url, json={})).status_code == 404


async def test_subscription_unknown_tenant_and_bad_payload(
    authenticated_client: httpx.AsyncClient,
) -> None:
    c = authenticated_client
    ghost = "00000000-0000-4000-8000-000000000001"
    assert (
        await c.post(f"/api/tenants/{ghost}/subscription", json={"plan_code": "pro"})
    ).status_code == 404
    assert (await c.post(f"/api/tenants/{ghost}/subscription", json={})).status_code == 422
    assert (
        await c.patch(f"/api/tenants/{ghost}/subscription", json={"status": "trial"})
    ).status_code == 422


async def test_planes_page_renders(authenticated_client: httpx.AsyncClient) -> None:
    r = await authenticated_client.get("/admin/planes", headers={"accept": "text/html"})
    assert r.status_code == 200
    html = r.text
    assert "Planes" in html and "Oferta Fundador" in html and "Quedan 5 de 5" in html
    assert "$390.000" in html and "Cotizador" in html
    assert "<script" not in html.replace('<script src="/static', "")  # CSP: sin scripts inline


async def test_cotizar_partial(authenticated_client: httpx.AsyncClient) -> None:
    c = authenticated_client
    r = await c.get(
        "/admin/planes/cotizar", params={"plan": "pro", "offer": "fundador", "iva": "1"}
    )
    assert r.status_code == 200
    assert "Total primera factura" in r.text and "$464.100" in r.text  # 390.000 * 1,19
    r = await c.get("/admin/planes/cotizar", params={"plan": "pro", "offer": "nope"})
    assert r.status_code == 200 and "alert-warn" in r.text and "Oferta no encontrada" in r.text
    r = await c.get("/admin/planes/cotizar", params={"plan": "zzz"})
    assert "Plan no encontrado" in r.text


async def test_planes_page_empty_state(
    authenticated_client: httpx.AsyncClient, session: Any
) -> None:
    from sqlalchemy import update

    from app.db.models.plans import Plan

    await session.execute(update(Plan).values(is_public=False))
    await session.commit()
    r = await authenticated_client.get("/admin/planes")
    assert r.status_code == 200 and "Sin planes" in r.text
