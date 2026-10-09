"""Middlewares ASGI: id de solicitud, cabeceras de seguridad y limite de cuerpo en webhooks."""

from __future__ import annotations

import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; "
    "base-uri 'self'; object-src 'none'"
)
WEBHOOK_MAX_BODY = 64 * 1024
_NO_STORE_PREFIXES = ("/admin", "/api", "/login", "/logout")


class RequestContextMiddleware:
    """Asigna ``X-Request-ID`` y lo liga a los logs."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1")[:64]
        request_id = incoming if incoming.isalnum() else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        await self.app(scope, receive, _send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", CSP)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "same-origin")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                if self.hsts:
                    headers.setdefault(
                        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
                    )
                if path.startswith(_NO_STORE_PREFIXES):
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, _send)


class _BodyTooLargeError(Exception):
    """Cuerpo de webhook mayor al limite."""


class WebhookBodyLimitMiddleware:
    """Rechaza (413) webhooks de mas de 64 KB (por Content-Length o por bytes recibidos)."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = WEBHOOK_MAX_BODY) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).startswith("/webhooks/"):
            raw = dict(scope.get("headers") or []).get(b"content-length")
            if raw and raw.isdigit() and int(raw) > self.max_bytes:
                resp = JSONResponse({"code": "payload_too_large"}, status_code=413)
                await resp(scope, receive, send)
                return
            await self._guarded(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _guarded(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Cuenta bytes reales (cubre ``Transfer-Encoding: chunked`` sin Content-Length)."""
        total = 0
        started = False

        async def _receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    raise _BodyTooLargeError
            return message

        async def _send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, _receive, _send)
        except _BodyTooLargeError:
            if started:
                raise
            resp = JSONResponse({"code": "payload_too_large"}, status_code=413)
            await resp(scope, receive, send)
