"""Login/logout y ajustes de cuenta. CSRF de login por doble envio (cookie + campo)."""

from __future__ import annotations

import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.core.config import get_settings
from app.core.deps import (
    current_auth_session,
    current_user,
    get_session,
    require_role,
    session_cookie_name,
    verify_csrf,
)
from app.core.errors import AppError
from app.core.security import new_csrf_token, verify_csrf_token
from app.db.models.users import UserSession
from app.web.templating import render

router = APIRouter(tags=["auth"])
LOGIN_CSRF_COOKIE = "vai_login_csrf"


def safe_next(target: str | None) -> str:
    """Solo rutas internas (evita open redirect)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/admin/inicio"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        session_cookie_name(),
        token,
        max_age=settings.session_ttl_min * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _login_page(
    request: Request,
    *,
    error: str | None = None,
    email: str = "",
    next_url: str = "",
    status: int = 200,
) -> Response:
    token = new_csrf_token()
    resp = render(
        request,
        "auth/login.html",
        {"error": error, "email": email, "next": next_url, "login_csrf": token},
        status_code=status,
    )
    resp.set_cookie(
        LOGIN_CSRF_COOKIE,
        token,
        max_age=900,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="strict",
        path="/login",
    )
    return resp


@router.get("/login", response_model=None)
async def login_page(request: Request, next: str = "") -> Response:
    return _login_page(request, next_url=safe_next(next) if next else "")


@router.post("/login", response_model=None)
async def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    login_csrf: str = Form(""),
    next: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> Response:
    cookie = request.cookies.get(LOGIN_CSRF_COOKIE)
    if not verify_csrf_token(cookie, login_csrf):
        return _login_page(
            request, error="La página expiró. Intenta de nuevo.", email=email, status=403
        )
    try:
        _user, token, _us = await service.authenticate(
            session,
            email=email,
            password=password,
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except AppError as exc:
        return _login_page(
            request, error=exc.message, email=email, next_url=safe_next(next), status=exc.status
        )
    resp = RedirectResponse(safe_next(next), status_code=303)
    _set_session_cookie(resp, token)
    resp.delete_cookie(LOGIN_CSRF_COOKIE, path="/login")
    return resp


@router.post("/logout", response_model=None)
async def logout(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user=Depends(current_user),  # noqa: B008
    _csrf: None = Depends(verify_csrf),
) -> Response:
    from app.audit.service import log_event

    await service.revoke_session(session, request.cookies.get(session_cookie_name()))
    await log_event(
        session, actor=user, action="auth.logout", entity_type="user", entity_id=str(user.id)
    )
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(session_cookie_name(), path="/")
    return resp


@router.get("/admin/ajustes", response_model=None)
async def settings_page(
    request: Request,
    user=Depends(current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),
) -> Response:
    users = await service.list_users(session) if user.role == "owner" else []
    return render(request, "auth/ajustes.html", {"users": users, "flash": _flash(request)})


_FLASH_OK = {
    "password_ok": "Contraseña actualizada. Las demás sesiones se cerraron.",
    "user_created": "Usuario creado. Compártele su contraseña inicial por un canal seguro.",
    "user_updated": "Usuario actualizado.",
}
_FLASH_ERROR = {
    "invalid_credentials": "La contraseña actual no es correcta.",
    "weak_password": "La contraseña es débil: usa mínimo 12 caracteres con letras y números.",
    "invalid_role": "Rol no válido.",
    "invalid_email": "Correo no válido.",
    "invalid_name": "El nombre es demasiado largo (máximo 200 caracteres).",
    "email_taken": "Ya existe un usuario con ese correo.",
    "self_deactivate": "No puedes desactivar tu propia cuenta.",
    "last_owner": "Debe quedar al menos un dueño activo.",
    "not_found": "Usuario no encontrado.",
}


def _flash(request: Request) -> dict[str, str] | None:
    """Mensaje fijo a partir de un codigo (nunca se muestra texto arbitrario de la URL)."""
    ok = _FLASH_OK.get(request.query_params.get("ok", ""))
    if ok:
        return {"kind": "ok", "message": ok}
    err = request.query_params.get("error")
    if err:
        return {
            "kind": "error",
            "message": _FLASH_ERROR.get(err, "No se pudo completar la acción."),
        }
    return None


def _redir(kind: str, code: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/ajustes?{urlencode({kind: code})}", status_code=303)


@router.post("/admin/ajustes/password", response_model=None)
async def change_password(
    user=Depends(current_user),  # noqa: B008
    user_session: UserSession = Depends(current_auth_session),
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
    current_password: str = Form(...),
    new_password: str = Form(...),
) -> Response:
    try:
        await service.change_password(
            session,
            user,
            current_password=current_password,
            new_password=new_password,
            keep_session_id=user_session.id,
        )
    except AppError as exc:
        return _redir("error", exc.code)
    return _redir("ok", "password_ok")


@router.post("/admin/ajustes/usuarios", response_model=None)
async def create_user(
    owner=Depends(require_role()),  # noqa: B008
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    role: str = Form("operator"),
) -> Response:
    try:
        await service.create_user(
            session, email=email, password=password, full_name=full_name, role=role, actor=owner
        )
    except AppError as exc:
        return _redir("error", exc.code)
    return _redir("ok", "user_created")


@router.post("/admin/ajustes/usuarios/{user_id}/{action}", response_model=None)
async def toggle_user(
    user_id: uuid.UUID,
    action: str,
    owner=Depends(require_role()),  # noqa: B008
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> Response:
    if action not in {"activar", "desactivar"}:
        raise AppError("not_found", "No encontrado", 404)
    try:
        await service.set_user_active(session, owner, user_id, active=action == "activar")
    except AppError as exc:
        return _redir("error", exc.code)
    return _redir("ok", "user_updated")
