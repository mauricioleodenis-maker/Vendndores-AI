import httpx
import pytest
from fastapi import APIRouter, FastAPI

from app.core.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    register_error_handlers,
)
from app.core.middleware import (
    CSP,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    WebhookBodyLimitMiddleware,
)


def make_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.add_middleware(WebhookBodyLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, hsts=True)
    app.add_middleware(RequestContextMiddleware)
    r = APIRouter()

    @r.get("/api/boom")
    async def boom():
        raise AppError("custom", "Mensaje en español", 418)

    @r.get("/api/unhandled")
    async def unhandled():
        raise RuntimeError("secret detail")

    @r.get("/api/int/{n}")
    async def intv(n: int):
        return {"n": n}

    @r.get("/admin/page")
    async def page():
        raise NotFoundError()

    @r.get("/admin/auth")
    async def auth():
        raise AppError("not_authenticated", "x", 401)

    @r.get("/admin/forbidden")
    async def forbidden():
        raise ForbiddenError()

    @r.post("/webhooks/x")
    async def hook():
        return {"ok": True}

    app.include_router(r)
    return app


@pytest.fixture
async def c():
    app = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        yield client


async def test_json_problem_for_api(c):
    r = await c.get("/api/boom")
    assert r.status_code == 418
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "custom" and r.json()["title"] == "Mensaje en español"


async def test_unhandled_does_not_leak(c):
    r = await c.get("/api/unhandled")
    assert r.status_code == 500 and "secret detail" not in r.text


async def test_validation_error_422(c):
    r = await c.get("/api/int/abc")
    assert r.status_code == 422 and r.json()["code"] == "validation_error"


async def test_html_partial_for_htmx_and_escaping(c):
    r = await c.get("/admin/page", headers={"HX-Request": "true"})
    assert r.status_code == 404 and 'class="alert alert-error"' in r.text


async def test_unauthenticated_redirects_html_and_htmx(c):
    r = await c.get("/admin/auth", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    r = await c.get("/admin/auth", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"
    r = await c.get("/admin/auth", headers={"accept": "application/json"})
    assert r.status_code == 401


async def test_404_route(c):
    assert (await c.get("/nada")).status_code == 404


def test_error_subclasses():
    assert ConflictError().status == 409 and ForbiddenError().status == 403


async def test_security_headers(c):
    r = await c.get("/admin/forbidden")
    h = r.headers
    assert h["content-security-policy"] == CSP and "script-src 'self'" in CSP
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "same-origin" and "strict-transport-security" in h
    assert h["cache-control"] == "no-store" and h["x-request-id"]


async def test_request_id_sanitized(c):
    r = await c.get("/nada", headers={"X-Request-ID": "bad id\n<script>"})
    assert r.headers["x-request-id"] != "bad id\n<script>"
    r = await c.get("/nada", headers={"X-Request-ID": "abc123"})
    assert r.headers["x-request-id"] == "abc123"


async def test_webhook_body_limit(c):
    ok = await c.post("/webhooks/x", content=b"a" * 100)
    assert ok.status_code == 200
    big = await c.post("/webhooks/x", content=b"a" * 70_000)
    assert big.status_code == 413
