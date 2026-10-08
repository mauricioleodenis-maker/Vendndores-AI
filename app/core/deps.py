"""Dependencias FastAPI compartidas: sesion DB, usuario actual, roles, CSRF y LLM."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import get_llm  # noqa: F401  (re-export: tests lo sobreescriben)
from app.core.config import get_settings
from app.core.errors import AppError, ForbiddenError
from app.core.security import verify_csrf_token
from app.db.models.users import User, UserSession
from app.db.session import get_sessionmaker

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"


def session_cookie_name() -> str:
    return "__Host-vai_session" if get_settings().cookie_secure else "vai_session"


async def get_session() -> AsyncIterator[AsyncSession]:
    """Sesion por solicitud: commit si todo sale bien, rollback si hay excepcion."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _authenticate(request: Request, session: AsyncSession) -> tuple[User, UserSession]:
    cached = getattr(request.state, "auth", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    from app.auth import service as auth_service

    token = request.cookies.get(session_cookie_name())
    resolved = await auth_service.resolve_session(session, token) if token else None
    if resolved is None:
        raise AppError("not_authenticated", "Inicia sesion para continuar", 401)
    user, user_session = resolved
    request.state.auth = (user, user_session)
    request.state.user = user
    request.state.csrf_token = user_session.csrf_token
    return user, user_session


async def current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    user, _ = await _authenticate(request, session)
    return user


async def current_auth_session(
    request: Request, session: AsyncSession = Depends(get_session)
) -> UserSession:
    _, user_session = await _authenticate(request, session)
    return user_session


def require_role(*roles: str) -> Callable[..., Coroutine[Any, Any, User]]:
    """El owner siempre pasa; los demas deben estar en ``roles``."""
    allowed = frozenset(roles) | {"owner"}

    async def _dep(user: User = Depends(current_user)) -> User:
        if user.role not in allowed:
            raise ForbiddenError()
        return user

    return _dep


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin is None
    host = urlsplit(origin).netloc
    allowed = {request.headers.get("host", ""), urlsplit(get_settings().public_base_url).netloc}
    return host in allowed


async def _submitted_token(request: Request) -> str | None:
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    ctype = request.headers.get("content-type", "")
    if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        value = form.get(CSRF_FIELD)
        return value if isinstance(value, str) else None
    return None


async def verify_csrf(request: Request, session: AsyncSession = Depends(get_session)) -> None:
    """Exige token CSRF (header ``X-CSRF-Token`` o campo ``csrf_token``) en metodos que mutan."""
    if request.method not in MUTATING_METHODS:
        return
    _, user_session = await _authenticate(request, session)
    if not _origin_ok(request):
        raise AppError("csrf_origin", "Origen no permitido", 403)
    provided = await _submitted_token(request)
    if not verify_csrf_token(user_session.csrf_token, provided):
        raise AppError("csrf_invalid", "Token CSRF invalido o ausente", 403)


async def csrf_guard(request: Request, session: AsyncSession = Depends(get_session)) -> None:
    """Dependency global para routers ``/admin`` y ``/api`` (webhooks quedan exentos)."""
    path = request.url.path
    if request.method in MUTATING_METHODS and (
        path.startswith("/admin") or path.startswith("/api")
    ):
        await verify_csrf(request, session)
