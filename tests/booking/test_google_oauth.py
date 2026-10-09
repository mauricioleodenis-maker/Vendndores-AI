"""Flujo OAuth de Google Calendar por tenant (sin red)."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking import google_calendar as gc
from app.booking import oauth
from app.core.config import get_settings
from app.core.deps import csrf_guard
from app.db.models.booking import CalendarConnection
from app.db.models.tenants import Tenant
from app.db.models.users import User

SCOPES = " ".join(gc.SCOPES)


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> None:
    monkeypatch.setenv("VAI_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("VAI_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    gc.reset_caches()
    if not any(getattr(r, "path", "") == oauth.CALLBACK_PATH for r in app.routes):
        app.include_router(oauth.router, dependencies=[Depends(csrf_guard)])


def _id_token(email: str) -> str:
    seg = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"h.{seg}.s"


def _token_response(**over: Any) -> dict[str, Any]:
    return {
        "access_token": "at",
        "refresh_token": "rt-1234",
        "scope": SCOPES + " openid email",
        "id_token": _id_token("dueno@clinica.co"),
        **over,
    }


async def _start(client: httpx.AsyncClient, tenant: Tenant | uuid.UUID) -> str:
    tid = tenant if isinstance(tenant, uuid.UUID) else tenant.id
    r = await client.post(f"/admin/negocios/{tid}/calendario/google/connect")
    assert r.status_code == 303
    return r.headers["location"]


async def test_connect_builds_auth_url(
    authenticated_client: httpx.AsyncClient, tenant: Tenant
) -> None:
    loc = await _start(authenticated_client, tenant)
    q = parse_qs(urlparse(loc).query)
    assert loc.startswith(gc.AUTH_URL)
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert set(q["scope"][0].split()) == {*gc.SCOPES, "openid", "email"}
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == [oauth.redirect_uri()]
    state = oauth._serializer("gcal-state").loads(q["state"][0])
    assert state["t"] == str(tenant.id)


async def test_connect_requires_csrf_and_login(
    client: httpx.AsyncClient, authenticated_client: httpx.AsyncClient, tenant: Tenant
) -> None:
    r = await authenticated_client.post(
        f"/admin/negocios/{tenant.id}/calendario/google/connect", headers={"X-CSRF-Token": ""}
    )
    assert r.status_code == 403


async def test_connect_requires_auth(client: httpx.AsyncClient, tenant: Tenant) -> None:
    r = await client.post(f"/admin/negocios/{tenant.id}/calendario/google/connect")
    assert r.status_code in (401, 403)


async def test_connect_not_configured(
    authenticated_client: httpx.AsyncClient, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VAI_GOOGLE_OAUTH_CLIENT_ID", "")
    get_settings.cache_clear()
    r = await authenticated_client.post(f"/admin/negocios/{tenant.id}/calendario/google/connect")
    assert r.status_code == 503


async def test_connect_unknown_tenant(authenticated_client: httpx.AsyncClient) -> None:
    r = await authenticated_client.post(f"/admin/negocios/{uuid.uuid4()}/calendario/google/connect")
    assert r.status_code == 404


async def test_full_flow_connect_then_disconnect(
    authenticated_client: httpx.AsyncClient, tenant: Tenant, session: AsyncSession
) -> None:
    c = authenticated_client
    tid = tenant.id
    state = parse_qs(urlparse(await _start(c, tenant)).query)["state"][0]
    with respx.mock() as router:
        tok = router.post(gc.TOKEN_URL).respond(200, json=_token_response())
        r = await c.get(oauth.CALLBACK_PATH, params={"state": state, "code": "abc"})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    form = parse_qs(tok.calls[0].request.content.decode())
    assert form["code_verifier"] and form["grant_type"] == ["authorization_code"]

    conn = (await session.execute(select(CalendarConnection))).scalar_one()
    assert conn.status == "active" and conn.google_calendar_id == "primary"
    assert gc.decrypt_email(tenant.id, conn.google_account_email_enc) == "dueno@clinica.co"
    assert await gc.load_refresh_token(session, tenant.id) == "rt-1234"
    assert b"dueno@clinica.co" not in conn.google_account_email_enc

    page = await c.get(f"/admin/negocios/{tenant.id}/calendario/google")
    assert page.status_code == 200
    assert "dueno@clinica.co" in page.text and "Conectado" in page.text
    assert "rt-1234" not in page.text and "at" != ""

    with respx.mock() as router:
        rev = router.post(gc.REVOKE_URL).respond(200)
        r = await c.post(f"/admin/negocios/{tenant.id}/calendario/google/disconnect")
    assert r.status_code == 303 and rev.called
    session.expire_all()
    conn = (await session.execute(select(CalendarConnection))).scalar_one()
    assert conn.status == "revoked" and conn.google_account_email_enc is None
    assert await gc.load_refresh_token(session, tid) is None
    page = await c.get(f"/admin/negocios/{tid}/calendario/google")
    assert "Conectar Google Calendar" in page.text

    # reconectar crea una fila nueva (la revocada no cuenta para el unico activo)
    state = parse_qs(urlparse(await _start(c, tid)).query)["state"][0]
    with respx.mock() as router:
        router.post(gc.TOKEN_URL).respond(200, json=_token_response())
        await c.get(oauth.CALLBACK_PATH, params={"state": state, "code": "abc"})
    rows = (await session.execute(select(CalendarConnection))).scalars().all()
    assert sorted(x.status for x in rows) == ["active", "revoked"]


async def test_callback_rejects_bad_state(
    authenticated_client: httpx.AsyncClient, tenant: Tenant
) -> None:
    r = await authenticated_client.get(oauth.CALLBACK_PATH, params={"state": "xx", "code": "c"})
    assert r.status_code == 400


async def test_callback_rejects_missing_cookie(
    authenticated_client: httpx.AsyncClient, tenant: Tenant
) -> None:
    c = authenticated_client
    state = parse_qs(urlparse(await _start(c, tenant)).query)["state"][0]
    c.cookies.delete(oauth.COOKIE_NAME)
    r = await c.get(oauth.CALLBACK_PATH, params={"state": state, "code": "c"})
    assert r.status_code == 400


async def test_callback_rejects_other_user(
    authenticated_client: httpx.AsyncClient,
    tenant: Tenant,
    make_user: Callable[..., Any],
    login: Callable[..., Any],
) -> None:
    c = authenticated_client
    state = parse_qs(urlparse(await _start(c, tenant)).query)["state"][0]
    other: User = await make_user("admin")
    await login(c, other)
    r = await c.get(oauth.CALLBACK_PATH, params={"state": state, "code": "c"})
    assert r.status_code == 400


async def test_callback_rejects_nonce_mismatch(
    authenticated_client: httpx.AsyncClient,
    tenant: Tenant,
    owner_user: User,
    login: Callable[..., Any],
) -> None:
    c = authenticated_client
    state = parse_qs(urlparse(await _start(c, tenant)).query)["state"][0]
    c.cookies.clear()
    await login(c, owner_user)
    c.cookies.set(
        oauth.COOKIE_NAME,
        oauth._serializer("gcal-cookie").dumps({"n": "otro", "v": "v"}),
        path=oauth.CALLBACK_PATH,
    )
    r = await c.get(oauth.CALLBACK_PATH, params={"state": state, "code": "c"})
    assert r.status_code == 400


@pytest.mark.parametrize("params", [{"error": "access_denied"}, {"error": "other"}, {}])
async def test_callback_user_denied(
    authenticated_client: httpx.AsyncClient, tenant: Tenant, params: dict[str, str]
) -> None:
    c = authenticated_client
    state = parse_qs(urlparse(await _start(c, tenant)).query)["state"][0]
    r = await c.get(oauth.CALLBACK_PATH, params={"state": state, **params})
    assert r.status_code == 303 and "error=" in r.headers["location"]


async def _callback(c: httpx.AsyncClient, tenant: Tenant, resp: httpx.Response | Exception) -> int:
    state = parse_qs(urlparse(await _start(c, tenant)).query)["state"][0]
    with respx.mock() as router:
        route = router.post(gc.TOKEN_URL)
        if isinstance(resp, Exception):
            route.mock(side_effect=resp)
        else:
            route.mock(return_value=resp)
        r = await c.get(oauth.CALLBACK_PATH, params={"state": state, "code": "c"})
    return r.status_code


async def test_callback_exchange_failures(
    authenticated_client: httpx.AsyncClient, tenant: Tenant
) -> None:
    assert await _callback(authenticated_client, tenant, httpx.Response(400)) == 400
    assert await _callback(authenticated_client, tenant, httpx.ConnectError("x")) == 502
    no_refresh = httpx.Response(200, json=_token_response(refresh_token=None))
    assert await _callback(authenticated_client, tenant, no_refresh) == 400


async def test_callback_requires_all_scopes(
    authenticated_client: httpx.AsyncClient, tenant: Tenant, session: AsyncSession
) -> None:
    resp = httpx.Response(200, json=_token_response(scope=gc.SCOPE_EVENTS))
    assert await _callback(authenticated_client, tenant, resp) == 303
    assert (await session.execute(select(CalendarConnection))).first() is None


async def test_reconnect_without_new_refresh_keeps_old(
    authenticated_client: httpx.AsyncClient, tenant: Tenant, session: AsyncSession
) -> None:
    tid = tenant.id
    await gc.save_refresh_token(session, tid, "old-1234")
    session.add(CalendarConnection(tenant_id=tenant.id, provider="google", status="needs_reauth"))
    await session.commit()
    resp = httpx.Response(200, json=_token_response(refresh_token=None, id_token=None))
    assert await _callback(authenticated_client, tenant, resp) == 303
    session.expire_all()
    conn = (await session.execute(select(CalendarConnection))).scalar_one()
    assert conn.status == "active"
    assert await gc.load_refresh_token(session, tid) == "old-1234"


async def test_page_status_variants(
    authenticated_client: httpx.AsyncClient, tenant: Tenant, session: AsyncSession
) -> None:
    session.add(CalendarConnection(tenant_id=tenant.id, provider="google", status="needs_reauth"))
    await session.commit()
    page = await authenticated_client.get(
        f"/admin/negocios/{tenant.id}/calendario/google", params={"error": "Algo fallo"}
    )
    assert "Requiere reconectar" in page.text and "Algo fallo" in page.text


def test_id_token_email_edge_cases() -> None:
    assert oauth._id_token_email(None) is None
    assert oauth._id_token_email("abc") is None
    assert oauth._id_token_email("a.!!!.c") is None
    assert oauth._id_token_email(_id_token("x@y.co")) == "x@y.co"
