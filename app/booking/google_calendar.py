"""Google Calendar por tenant (B9): tokens, freeBusy y eventos idempotentes via httpx.

Los tokens nunca se loguean ni salen en respuestas. El refresh token vive cifrado en
``tenant_secrets`` (``google_oauth_refresh``); el access token solo en memoria del proceso.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import BOGOTA, ensure_utc, to_bogota, utcnow
from app.core.config import get_settings
from app.core.crypto import EncryptedBlob, get_crypto, make_aad, pack_blob, unpack_blob
from app.core.logging import get_logger
from app.db.models.booking import Appointment, CalendarConnection
from app.db.models.catalog import Service
from app.db.models.contacts import Contact
from app.db.models.tenants import TenantSecret
from app.db.repo import TenantScopedRepo

log = get_logger(__name__)

SCOPE_EVENTS = "https://www.googleapis.com/auth/calendar.events"
SCOPE_FREEBUSY = "https://www.googleapis.com/auth/calendar.freebusy"
SCOPES = (SCOPE_EVENTS, SCOPE_FREEBUSY)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_BASE = "https://www.googleapis.com/calendar/v3"
REFRESH_SECRET_KIND = "google_oauth_refresh"  # noqa: S105

REFRESH_MARGIN = timedelta(seconds=60)
FREEBUSY_CACHE_TTL = timedelta(seconds=45)
BACKOFF_SECONDS = (0.5, 1.0, 2.0)
HTTP_TIMEOUT = 10.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class CalendarError(Exception):
    """Fallo al hablar con Google Calendar (el llamador cae al calendario local)."""


class CalendarAuthError(CalendarError):
    """Sin conexion activa o credenciales revocadas (requiere reconectar)."""


Sleeper = Callable[[float], Awaitable[None]]


# --------------------------------------------------------------------------- secretos
def _aad(tenant_id: uuid.UUID, kind: str) -> bytes:
    return make_aad("tenant_secrets", tenant_id, kind)  # mismo AAD que tenants.service


async def save_refresh_token(session: AsyncSession, tenant_id: uuid.UUID, token: str) -> None:
    blob = get_crypto().encrypt_str(token, aad=_aad(tenant_id, REFRESH_SECRET_KIND))
    repo = TenantScopedRepo(session, TenantSecret, tenant_id)
    rows = await repo.list(TenantSecret.kind == REFRESH_SECRET_KIND)
    if rows:
        row = rows[0]
        row.ciphertext, row.nonce, row.key_version = blob.ciphertext, blob.nonce, blob.key_version
        row.last4, row.rotated_at = token[-4:], utcnow()
    else:
        await repo.add(
            TenantSecret(
                kind=REFRESH_SECRET_KIND,
                ciphertext=blob.ciphertext,
                nonce=blob.nonce,
                key_version=blob.key_version,
                last4=token[-4:],
            )
        )
    await session.flush()


async def load_refresh_token(session: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    repo = TenantScopedRepo(session, TenantSecret, tenant_id)
    rows = await repo.list(TenantSecret.kind == REFRESH_SECRET_KIND)
    if not rows:
        return None
    row = rows[0]
    return get_crypto().decrypt_str(
        EncryptedBlob(row.ciphertext, row.nonce, row.key_version),
        aad=_aad(tenant_id, REFRESH_SECRET_KIND),
    )


async def delete_refresh_token(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    repo = TenantScopedRepo(session, TenantSecret, tenant_id)
    for row in await repo.list(TenantSecret.kind == REFRESH_SECRET_KIND):
        await repo.delete(row)


# --------------------------------------------------------------------------- conexion
async def get_connection(
    session: AsyncSession, tenant_id: uuid.UUID, *, only_active: bool = False
) -> CalendarConnection | None:
    stmt = select(CalendarConnection).where(
        CalendarConnection.tenant_id == tenant_id,
        CalendarConnection.provider == "google",
        CalendarConnection.status != "revoked",
    )
    if only_active:
        stmt = stmt.where(CalendarConnection.status == "active")
    return (await session.execute(stmt.limit(1))).scalar_one_or_none()


def encrypt_email(tenant_id: uuid.UUID, email: str) -> bytes:
    aad = make_aad("calendar_connections", tenant_id, "google_account_email_enc")
    return pack_blob(get_crypto().encrypt_str(email, aad=aad))


def decrypt_email(tenant_id: uuid.UUID, data: bytes | None) -> str | None:
    if not data:
        return None
    aad = make_aad("calendar_connections", tenant_id, "google_account_email_enc")
    try:
        return get_crypto().decrypt_str(unpack_blob(data), aad=aad)
    except Exception:  # clave rotada/dato corrupto: no romper la UI
        return None


async def active_provider(
    session: AsyncSession, tenant_id: uuid.UUID
) -> GoogleCalendarProvider | None:
    """Fabrica para B8: el proveedor si el tenant tiene Google activo, si no ``None``."""
    if await get_connection(session, tenant_id, only_active=True) is None:
        return None
    return GoogleCalendarProvider()


# --------------------------------------------------------------------------- helpers
def event_id_for(tenant_id: uuid.UUID, appointment_id: uuid.UUID) -> str:
    """Id determinista (base32hex minusculas, alfabeto ``[a-v0-9]``, 32 chars)."""
    digest = hashlib.sha256(f"{tenant_id}:{appointment_id}".encode()).digest()[:20]
    return base64.b32hexencode(digest).decode().lower().rstrip("=")


def _rfc3339(dt: datetime) -> str:
    return ensure_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(dt: datetime) -> dict[str, str]:
    return {"dateTime": to_bogota(dt).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "America/Bogota"}


def _parse_google_dt(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


@dataclass(slots=True)
class _CachedToken:
    token: str
    expires_at: datetime


_TOKENS: dict[uuid.UUID, _CachedToken] = {}
_LOCKS: dict[uuid.UUID, asyncio.Lock] = {}
_BUSY_CACHE: dict[tuple[uuid.UUID, str, str], tuple[datetime, list[tuple[datetime, datetime]]]] = {}


def reset_caches() -> None:
    """Para tests."""
    _TOKENS.clear()
    _LOCKS.clear()
    _BUSY_CACHE.clear()


def reset_caches_for(tenant_id: uuid.UUID) -> None:
    _TOKENS.pop(tenant_id, None)
    _drop_busy_cache(tenant_id)


def _drop_busy_cache(tenant_id: uuid.UUID) -> None:
    for key in [k for k in _BUSY_CACHE if k[0] == tenant_id]:
        del _BUSY_CACHE[key]


def _credentials() -> tuple[str, str]:
    s = get_settings()
    return s.google_oauth_client_id, s.google_oauth_client_secret.get_secret_value()


# --------------------------------------------------------------------------- proveedor
class GoogleCalendarProvider:
    """Implementa ``CalendarProvider`` contra la API v3 de Google Calendar."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._transport = transport
        self._sleep = sleep

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=HTTP_TIMEOUT, transport=self._transport)

    # -- tokens
    async def _mark(
        self, session: AsyncSession, conn: CalendarConnection, status: str, err: str
    ) -> None:
        conn.status, conn.last_error = status, err[:300]
        await session.flush()
        _TOKENS.pop(conn.tenant_id, None)

    async def access_token(self, session: AsyncSession, tenant_id: uuid.UUID) -> str:
        cached = _TOKENS.get(tenant_id)
        if cached and cached.expires_at - utcnow() > REFRESH_MARGIN:
            return cached.token
        lock = _LOCKS.setdefault(tenant_id, asyncio.Lock())
        async with lock:
            cached = _TOKENS.get(tenant_id)
            if cached and cached.expires_at - utcnow() > REFRESH_MARGIN:
                return cached.token
            conn = await get_connection(session, tenant_id, only_active=True)
            if conn is None:
                raise CalendarAuthError("Google Calendar no esta conectado")
            refresh = await load_refresh_token(session, tenant_id)
            if not refresh:
                await self._mark(session, conn, "needs_reauth", "sin refresh token")
                raise CalendarAuthError("Falta el token de Google; reconecta el calendario")
            client_id, client_secret = _credentials()
            try:
                async with self._client() as client:
                    resp = await client.post(
                        TOKEN_URL,
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": refresh,
                            "client_id": client_id,
                            "client_secret": client_secret,
                        },
                    )
            except httpx.HTTPError as exc:
                raise CalendarError("No se pudo contactar a Google") from exc
            if resp.status_code in (400, 401):
                code = _error_code(resp)
                if code in ("invalid_grant", "invalid_client", "unauthorized_client"):
                    await self._mark(session, conn, "needs_reauth", code)
                    log.warning("gcal_needs_reauth", tenant_id=str(tenant_id), code=code)
                    raise CalendarAuthError("Google revoco el acceso; reconecta el calendario")
            if resp.status_code != 200:
                raise CalendarError(f"Google rechazo el refresh ({resp.status_code})")
            data = resp.json()
            token = str(data["access_token"])
            expires = utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)))
            _TOKENS[tenant_id] = _CachedToken(token, expires)
            return token

    # -- HTTP con reintentos
    async def _call(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        """Devuelve la respuesta si el status esta en ``ok`` o es 404/409/410."""
        passthrough = {*ok, 404, 409, 410}
        refreshed = False
        last: str = ""
        for attempt in range(len(BACKOFF_SECONDS) + 1):
            token = await self.access_token(session, tenant_id)
            try:
                async with self._client() as client:
                    resp = await client.request(
                        method,
                        f"{API_BASE}{path}",
                        params=params,
                        json=json_body,
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except httpx.HTTPError as exc:
                last = type(exc).__name__
                resp = None
            if resp is not None:
                if resp.status_code == 401 and not refreshed:
                    refreshed = True
                    _TOKENS.pop(tenant_id, None)
                    continue
                if resp.status_code in passthrough:
                    return resp
                if resp.status_code not in RETRY_STATUSES:
                    raise CalendarError(f"Google Calendar respondio {resp.status_code}")
                last = str(resp.status_code)
            if attempt < len(BACKOFF_SECONDS):
                await self._sleep(BACKOFF_SECONDS[attempt])
        raise CalendarError(f"Google Calendar no disponible ({last})")

    # -- CalendarProvider
    async def _calendar_id(self, session: AsyncSession, tenant_id: uuid.UUID) -> str:
        conn = await get_connection(session, tenant_id, only_active=True)
        if conn is None:
            raise CalendarAuthError("Google Calendar no esta conectado")
        return conn.google_calendar_id or "primary"

    async def busy_intervals(
        self, session: AsyncSession, tenant_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        cache_key = (tenant_id, _rfc3339(start), _rfc3339(end))
        hit = _BUSY_CACHE.get(cache_key)
        if hit and utcnow() - hit[0] < FREEBUSY_CACHE_TTL:
            return list(hit[1])
        cal = await self._calendar_id(session, tenant_id)
        resp = await self._call(
            session,
            tenant_id,
            "POST",
            "/freeBusy",
            json_body={
                "timeMin": _rfc3339(start),
                "timeMax": _rfc3339(end),
                "timeZone": "America/Bogota",
                "items": [{"id": cal}],
            },
        )
        if resp.status_code != 200:
            raise CalendarError("freeBusy no disponible")
        entry = (resp.json().get("calendars") or {}).get(cal) or {}
        if entry.get("errors"):
            raise CalendarError("Calendario sin disponibilidad confiable")
        busy = sorted(
            (_parse_google_dt(b["start"]), _parse_google_dt(b["end"]))
            for b in entry.get("busy", [])
        )
        _BUSY_CACHE[cache_key] = (utcnow(), busy)
        return list(busy)

    async def _event_body(
        self, session: AsyncSession, tenant_id: uuid.UUID, appt: Appointment
    ) -> dict[str, Any]:
        service_name = "Cita"
        if appt.service_id:
            svc = await session.get(Service, appt.service_id)
            if svc is not None and svc.tenant_id == tenant_id:
                service_name = svc.name
        who = "Cliente"
        if appt.contact_id:
            contact = await session.get(Contact, appt.contact_id)
            if contact is not None and contact.tenant_id == tenant_id and contact.display_name_enc:
                try:
                    who = get_crypto().decrypt_str(
                        unpack_blob(contact.display_name_enc),
                        aad=make_aad("contacts", tenant_id, "display_name_enc"),
                    )
                except Exception:  # nombre ilegible: usar alias generico
                    who = "Cliente"
        return {
            "summary": f"{service_name} - {who}"[:200],
            "description": "Canal: WhatsApp. Agendado por Vendedores AI. Sin datos clinicos.",
            "start": _local(appt.starts_at),
            "end": _local(appt.ends_at),
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 60}]},
            "extendedProperties": {
                "private": {"appt_id": str(appt.id), "tenant_id": str(tenant_id)}
            },
        }

    def _apply(self, appt: Appointment, event: dict[str, Any]) -> None:
        appt.google_event_id = str(event.get("id", appt.google_event_id))
        appt.google_event_etag = event.get("etag")
        appt.sync_status = "synced"

    async def create_event(
        self, session: AsyncSession, tenant_id: uuid.UUID, appointment: Appointment
    ) -> str | None:
        cal = await self._calendar_id(session, tenant_id)
        event_id = event_id_for(tenant_id, appointment.id)
        body = await self._event_body(session, tenant_id, appointment)
        body["id"] = event_id
        path = f"/calendars/{_q(cal)}/events"
        resp = await self._call(
            session, tenant_id, "POST", path, params={"sendUpdates": "none"}, json_body=body
        )
        if resp.status_code == 409:  # ya existe: reintento idempotente
            resp = await self._call(session, tenant_id, "GET", f"{path}/{event_id}")
            if resp.status_code != 200:
                raise CalendarError("El evento existe pero no se pudo leer")
        elif resp.status_code != 200:
            raise CalendarError(f"No se pudo crear el evento ({resp.status_code})")
        _drop_busy_cache(tenant_id)
        self._apply(appointment, resp.json())
        return appointment.google_event_id

    async def update_event(
        self, session: AsyncSession, tenant_id: uuid.UUID, appointment: Appointment
    ) -> None:
        if not appointment.google_event_id:
            return
        cal = await self._calendar_id(session, tenant_id)
        body = await self._event_body(session, tenant_id, appointment)
        resp = await self._call(
            session,
            tenant_id,
            "PATCH",
            f"/calendars/{_q(cal)}/events/{appointment.google_event_id}",
            params={"sendUpdates": "none"},
            json_body=body,
        )
        if resp.status_code in (404, 410):  # borrado a mano en Google: recrear
            appointment.google_event_id = None
            await self.create_event(session, tenant_id, appointment)
            return
        if resp.status_code != 200:
            raise CalendarError(f"No se pudo actualizar el evento ({resp.status_code})")
        _drop_busy_cache(tenant_id)
        self._apply(appointment, resp.json())

    async def delete_event(
        self, session: AsyncSession, tenant_id: uuid.UUID, appointment: Appointment
    ) -> None:
        if not appointment.google_event_id:
            return
        cal = await self._calendar_id(session, tenant_id)
        resp = await self._call(
            session,
            tenant_id,
            "DELETE",
            f"/calendars/{_q(cal)}/events/{appointment.google_event_id}",
            params={"sendUpdates": "none"},
            ok=(200, 204),
        )
        if resp.status_code not in (200, 204, 404, 410):
            raise CalendarError(f"No se pudo borrar el evento ({resp.status_code})")
        _drop_busy_cache(tenant_id)
        appointment.google_event_id = None
        appointment.google_event_etag = None

    # -- desconexion
    async def revoke(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        """Revoca en Google (mejor esfuerzo); el llamador borra secreto y marca la conexion."""
        refresh = await load_refresh_token(session, tenant_id)
        if not refresh:
            return
        try:
            async with self._client() as client:
                await client.post(REVOKE_URL, data={"token": refresh})
        except httpx.HTTPError:
            log.warning("gcal_revoke_failed", tenant_id=str(tenant_id))
        _TOKENS.pop(tenant_id, None)


def _q(calendar_id: str) -> str:
    return quote(calendar_id, safe="")


def _error_code(resp: httpx.Response) -> str:
    try:
        body = json.loads(resp.text)
    except ValueError:
        return ""
    err = body.get("error") if isinstance(body, dict) else ""
    return err if isinstance(err, str) else ""


__all__ = [
    "BOGOTA",
    "SCOPES",
    "CalendarAuthError",
    "CalendarError",
    "GoogleCalendarProvider",
    "active_provider",
    "event_id_for",
]
