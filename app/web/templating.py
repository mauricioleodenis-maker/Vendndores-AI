"""Entorno Jinja2 compartido y ``render()`` para todos los modulos."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.web.filters import FILTERS
from app.web.nav import nav_for_role

WEB_DIR = Path(__file__).parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.filters.update(FILTERS)
templates.env.globals["app_name"] = "Vendedores AI"


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    """Renderiza ``name`` con usuario, CSRF, menu y ruta actual ya en el contexto.

    Los routers pasan solo sus datos; las plantillas extienden ``base.html``.
    """
    user = getattr(request.state, "user", None)
    ctx: dict[str, Any] = {
        "user": user,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "nav": nav_for_role(getattr(user, "role", None)),
        "current_path": request.url.path,
    }
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code, headers=headers)
