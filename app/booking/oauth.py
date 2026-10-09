"""OAuth web de Google Calendar por tenant (B9): connect / callback / disconnect.

``state`` firmado (itsdangerous, 10 min) con tenant+usuario+nonce; el nonce y el verificador PKCE
viajan ademas en una cookie firmada HttpOnly para atar el flujo al navegador que lo inicio.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.booking import google_calendar as gcal
from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.deps import get_session, require_role
from app.core.errors import AppError, NotFoundError
from app.core.logging import get_logger
from app.db.models.booking import CalendarConnection
from app.db.models.tenants import Tenant
from app.db.models.users import User
from app.web.templating import render

log = get_logger(__name__)

router = APIRouter(tags=["calendario-google"])

STATE_MAX_AGE_S = 600
COOKIE_NAME = "vai_gcal_oauth"
CALLBACK_PATH = "/admin/calendario/google/callback"
Admin = Depends(require_role("admin"))


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key.get_secret_value(), salt=salt)


def redirect_uri() -> str:
    return get_settings().public_base_url.rstrip("/") + CALLBACK_PATH


def _page_url(tenant_id: uuid.UUID, *, ok: str | None = None, error: str | None = None) -> str:
    url = f"/admin/negocios/{tenant_id}/calendario/google"
    if ok:
        return f"{url}?ok={quote(ok)}"
    if error:
        return f"{url}?error={quote(error)}"
    return url


def make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    return verifier, challenge.rstrip("=")


def build_auth_url(state: str, challenge: str) -> str:
    client_id, _ = gcal._credentials()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join((*gcal.SCOPES, "openid", "email")),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{gcal.AUTH_URL}?{urlencode(params)}"


def _id_token_email(id_token: str | None) -> str | None:
    """Email del payload del id_token (llega directo de Google por TLS en el intercambio)."""
    if not id_token or id_token.count(".") != 2:
        return None
    try:
        payload = id_token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    email = data.get("email") if isinstance(data, dict) else None
    return email if isinstance(email, str) and "@" in email else None


async def _require_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = (
        await session.execute(
            select(Tenant).where(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if tenant is None:
        raise NotFoundError("Empresa no encontrada")
    return tenant


async def calendar_context(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any]:
    """Datos para el parcial ``booking/_google_calendar.html`` (sin tokens)."""
    conn = await gcal.get_connection(session, tenant_id)
    configured = bool(gcal._credentials()[0])
    return {
        "gcal": {
            "configured": configured,
            "status": conn.status if conn else None,
            "email": gcal.decrypt_email(tenant_id, conn.google_account_email_enc) if conn else None,
            "calendar_id": conn.google_calendar_id if conn else None,
            "last_error": conn.last_error if conn else None,
        }
    }


# --------------------------------------------------------------------------- rutas
@router.get("/admin/negocios/{tenant_id}/calendario/google", response_class=HTMLResponse)
async def google_page(
    request: Request,
    tenant_id: uuid.UUID,
    ok: str | None = None,
    error: str | None = None,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    tenant = await _require_tenant(session, tenant_id)
    flash = None
    if ok:
        flash = {"kind": "success", "message": ok[:200]}
    elif error:
        flash = {"kind": "error", "message": error[:200]}
    ctx = {"tenant": tenant, "flash": flash, **await calendar_context(session, tenant_id)}
    return render(request, "booking/google.html", ctx)


@router.post("/admin/negocios/{tenant_id}/calendario/google/connect")
async def connect(
    tenant_id: uuid.UUID,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _require_tenant(session, tenant_id)
    if not gcal._credentials()[0]:
        raise AppError("gcal_not_configured", "Google Calendar no esta configurado", 503)
    nonce = secrets.token_urlsafe(16)
    verifier, challenge = make_pkce()
    state = _serializer("gcal-state").dumps({"t": str(tenant_id), "u": str(user.id), "n": nonce})
    response = RedirectResponse(build_auth_url(state, challenge), status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        _serializer("gcal-cookie").dumps({"n": nonce, "v": verifier}),
        max_age=STATE_MAX_AGE_S,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="lax",
        path=CALLBACK_PATH,
    )
    return response


async def _exchange_code(code: str, verifier: str) -> dict[str, Any]:
    client_id, client_secret = gcal._credentials()
    try:
        async with httpx.AsyncClient(timeout=gcal.HTTP_TIMEOUT) as client:
            resp = await client.post(
                gcal.TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri(),
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code_verifier": verifier,
                },
            )
    except httpx.HTTPError as exc:
        raise AppError("gcal_unreachable", "No se pudo contactar a Google", 502) from exc
    if resp.status_code != 200:
        raise AppError("gcal_exchange_failed", "Google rechazo la autorizacion", 400)
    data = resp.json()
    return data if isinstance(data, dict) else {}


@router.get(CALLBACK_PATH)
async def callback(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        st = _serializer("gcal-state").loads(state, max_age=STATE_MAX_AGE_S)
        cookie = _serializer("gcal-cookie").loads(
            request.cookies.get(COOKIE_NAME, ""), max_age=STATE_MAX_AGE_S
        )
        tenant_id = uuid.UUID(st["t"])
    except (BadData, KeyError, ValueError, TypeError) as exc:
        raise AppError("gcal_bad_state", "La autorizacion expiro o no es valida", 400) from exc
    if not (
        secrets.compare_digest(str(st.get("n", "")), str(cookie.get("n", "")))
        and st.get("u") == str(user.id)
    ):
        raise AppError("gcal_bad_state", "La autorizacion no corresponde a tu sesion", 400)
    await _require_tenant(session, tenant_id)

    def done(response: Response) -> Response:
        response.delete_cookie(COOKIE_NAME, path=CALLBACK_PATH)
        return response

    if error or not code:
        msg = (
            "Cancelaste la conexion con Google"
            if error == "access_denied"
            else "Google no autorizo la conexion"
        )
        return done(RedirectResponse(_page_url(tenant_id, error=msg), status_code=303))

    data = await _exchange_code(code, str(cookie.get("v", "")))
    granted = set(str(data.get("scope", "")).split())
    if not set(gcal.SCOPES) <= granted:
        msg = "Faltan permisos de calendario; vuelve a conectar y acepta todos"
        return done(RedirectResponse(_page_url(tenant_id, error=msg), status_code=303))
    refresh = data.get("refresh_token")
    if not refresh and not await gcal.load_refresh_token(session, tenant_id):
        raise AppError("gcal_no_refresh", "Google no entrego token de renovacion; reintenta", 400)
    if refresh:
        await gcal.save_refresh_token(session, tenant_id, str(refresh))
    email = _id_token_email(data.get("id_token"))

    conn = await gcal.get_connection(session, tenant_id)
    if conn is None:
        conn = CalendarConnection(tenant_id=tenant_id, provider="google")
        session.add(conn)
    conn.status, conn.last_error = "active", None
    conn.google_calendar_id = conn.google_calendar_id or "primary"
    conn.scopes = sorted(granted)
    conn.last_sync_at = utcnow()
    if email:
        conn.google_account_email_enc = gcal.encrypt_email(tenant_id, email)
    await session.flush()
    gcal.reset_caches_for(tenant_id)
    await log_event(
        session,
        actor=user,
        action="calendar.google.connect",
        entity_type="calendar_connection",
        entity_id=conn.id,
        tenant_id=tenant_id,
    )
    return done(
        RedirectResponse(_page_url(tenant_id, ok="Google Calendar conectado"), status_code=303)
    )


@router.post("/admin/negocios/{tenant_id}/calendario/google/disconnect")
async def disconnect(
    tenant_id: uuid.UUID,
    user: User = Admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _require_tenant(session, tenant_id)
    conn = await gcal.get_connection(session, tenant_id)
    if conn is not None:
        await gcal.GoogleCalendarProvider().revoke(session, tenant_id)
        await gcal.delete_refresh_token(session, tenant_id)
        conn.status, conn.google_account_email_enc = "revoked", None
        await session.flush()
        gcal.reset_caches_for(tenant_id)
        await log_event(
            session,
            actor=user,
            action="calendar.google.disconnect",
            entity_type="calendar_connection",
            entity_id=conn.id,
            tenant_id=tenant_id,
        )
    return RedirectResponse(
        _page_url(tenant_id, ok="Google Calendar desconectado"), status_code=303
    )
