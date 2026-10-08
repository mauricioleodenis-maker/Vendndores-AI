"""Errores de aplicacion y handlers (JSON RFC 7807 o parcial HTML)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

log = get_logger(__name__)


class AppError(Exception):
    """Error controlado con codigo estable, mensaje en espanol y status HTTP."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class NotFoundError(AppError):
    def __init__(self, message: str = "No encontrado") -> None:
        super().__init__("not_found", message, 404)


class ForbiddenError(AppError):
    def __init__(self, message: str = "No tienes permiso para esta accion") -> None:
        super().__init__("forbidden", message, 403)


class ConflictError(AppError):
    def __init__(self, message: str = "Conflicto con el estado actual") -> None:
        super().__init__("conflict", message, 409)


def _wants_html(request: Request) -> bool:
    path = request.url.path
    if path.startswith("/api/") or path.startswith("/webhooks/"):
        return False
    if request.headers.get("hx-request"):
        return True
    return "text/html" in request.headers.get("accept", "")


def _render(request: Request, status: int, code: str, message: str) -> Response:
    if code == "not_authenticated" and _wants_html(request):
        from urllib.parse import quote

        target = "/login?next=" + quote(request.url.path)
        if request.headers.get("hx-request"):
            return HTMLResponse("", status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse(target, status_code=303)
    if _wants_html(request):
        from html import escape

        body = (
            f'<div class="alert alert-error" role="alert" data-code="{escape(code)}">'
            f"{escape(message)}</div>"
        )
        return HTMLResponse(body, status_code=status)
    return JSONResponse(
        {
            "type": f"about:blank#{code}",
            "title": message,
            "status": status,
            "code": code,
            "detail": message,
        },
        status_code=status,
        media_type="application/problem+json",
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> Response:
        return _render(request, exc.status, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        resp = _render(request, exc.status_code, f"http_{exc.status_code}", str(exc.detail))
        for k, v in (exc.headers or {}).items():
            resp.headers[k] = v
        return resp

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
        fields = [".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()]
        return _render(request, 422, "validation_error", "Datos invalidos: " + ", ".join(fields))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        log.error("unhandled_error", error_type=type(exc).__name__, path=request.url.path)
        return _render(request, 500, "internal_error", "Error interno. Intenta de nuevo.")
