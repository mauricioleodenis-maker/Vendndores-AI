"""``create_app()``: middlewares, estaticos, plantillas y auto-registro de routers de modulos."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.core.config import Settings, get_settings
from app.core.deps import csrf_guard, current_user
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    WebhookBodyLimitMiddleware,
)
from app.db.session import dispose_engine, get_engine
from app.web.nav import NAV_ITEMS, PLACEHOLDER_PATHS
from app.web.templating import STATIC_DIR, render

log = get_logger(__name__)

READY_TIMEOUT_S = 3.0

# Paquetes de dominio (MAESTRO §3). Cada uno puede exponer ``app.<pkg>.router`` con ``router``
# (y opcionalmente ``routers: list[APIRouter]``). Los que no existen se omiten.
ROUTER_PACKAGES: tuple[str, ...] = (
    "auth",
    "audit",
    "tenants",
    "plans",
    "billing",
    "niches",
    "scraping",
    "ai",
    "factory",
    "conversation",
    "privacy",
    "channels",
    "booking",
    "reminders",
    "leads",
    "outreach",
    "dashboard",
    "saleskit",
    "ventas",
)


def discover_routers() -> list[APIRouter]:
    found: list[APIRouter] = []
    for pkg in ROUTER_PACKAGES:
        modname = f"app.{pkg}.router"
        try:
            module = importlib.import_module(modname)
        except ModuleNotFoundError as exc:
            if exc.name in {modname, f"app.{pkg}"}:
                continue  # el modulo todavia no tiene router
            raise
        routers: list[APIRouter] = list(getattr(module, "routers", []))
        main_router = getattr(module, "router", None)
        if main_router is not None:
            routers.insert(0, main_router)
        found.extend(routers)
        log.info("router_registered", module=modname, count=len(routers))
    return found


def _registered_get_paths(app: FastAPI) -> set[str]:
    return {
        r.path  # type: ignore[attr-defined]
        for r in app.routes
        if "GET" in (getattr(r, "methods", None) or set())
    }


def _register_placeholders(app: FastAPI) -> None:
    """Paginas del menu sin modulo todavia: marcador en lugar de 404."""
    labels = {item.href: item.label for item in NAV_ITEMS}
    labels["/admin/negocios/nuevo"] = "Nueva empresa"
    existing = _registered_get_paths(app)
    for path in PLACEHOLDER_PATHS:
        if path in existing:
            continue
        title = labels.get(path, "Seccion")

        def make(title: str) -> Any:
            async def page(request: Request, _user: Any = Depends(current_user)) -> Response:
                return render(request, "partials/placeholder.html", {"title": title})

            return page

        app.add_api_route(path, make(title), methods=["GET"], include_in_schema=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging("DEBUG" if settings.debug else "INFO", json_logs=settings.env != "dev")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        get_engine()
        if settings.is_prod and not settings.redis_url:
            log.warning("redis_missing_in_prod", impact="rate_limit_por_proceso_y_jobs_locales")
        log.info("app_started", env=settings.env, version=__version__)
        try:
            yield
        finally:
            await dispose_engine()

    app = FastAPI(
        title="Vendedores AI",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if settings.is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_prod else "/openapi.json",
    )
    # Orden: el ultimo agregado es el mas externo.
    if settings.allowed_hosts and settings.allowed_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(WebhookBodyLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.cookie_secure)
    app.add_middleware(RequestContextMiddleware)

    register_error_handlers(app)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    for router in discover_routers():
        app.include_router(router, dependencies=[Depends(csrf_guard)])

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> Response:
        checks: dict[str, str] = {}
        ok = True

        async def _db() -> None:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))

        async def _redis() -> None:
            import redis.asyncio as aioredis

            client = aioredis.from_url(settings.redis_url)
            try:
                await client.ping()
            finally:
                await client.aclose()

        probes = {"db": _db()}
        if settings.redis_url:
            probes["redis"] = _redis()
        for name, probe in probes.items():
            try:
                await asyncio.wait_for(probe, timeout=READY_TIMEOUT_S)
                checks[name] = "ok"
            except Exception as exc:
                log.error("readiness_failed", check=name, error_type=type(exc).__name__)
                checks[name] = "error"
                ok = False
        return JSONResponse(
            {"status": "ok" if ok else "error", "checks": checks}, status_code=200 if ok else 503
        )

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin", status_code=307)

    @app.get("/admin", include_in_schema=False, response_class=HTMLResponse)
    async def admin_root() -> RedirectResponse:
        return RedirectResponse("/admin/inicio", status_code=307)

    _register_placeholders(app)
    return app


def get_app() -> FastAPI:  # ``uvicorn app.main:get_app --factory``
    return create_app()


def __getattr__(name: str) -> Any:
    """``app.main:app`` se crea de forma perezosa (no al importar en tests)."""
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)


_app: FastAPI | None = None
