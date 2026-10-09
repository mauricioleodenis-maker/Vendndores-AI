"""Envio por Twilio WhatsApp (REST con httpx). Dueno: B6 (firmas fijas MAESTRO §5).

- ``VAI_TWILIO_DRY_RUN=true`` => no hay red: se registra en el log y se devuelve un SID falso.
- Credenciales: por tenant (``tenant_secrets`` twilio_sid + twilio_auth_token) o de la plataforma.
- Respeta la lista de supresion y la ventana de 24 h (texto libre solo dentro de ella).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.crypto import EncryptedBlob, get_crypto, make_aad, phone_hash
from app.core.logging import get_logger
from app.db.models.contacts import Contact
from app.db.models.conversations import Conversation
from app.db.models.tenants import ChannelAccount, TenantSecret
from app.privacy.service import is_suppressed

log = get_logger(__name__)

TWILIO_API = "https://api.twilio.com/2010-04-01"
WINDOW = timedelta(hours=24)
REQUEST_TIMEOUT_S = 10.0
MAX_ATTEMPTS = 3
# Solo 429 se reintenta: un 5xx no garantiza que Twilio no haya aceptado el mensaje y
# ``POST /Messages`` no es idempotente (evita WhatsApp duplicados al paciente).
RETRY_STATUSES = frozenset({429})
_SAFE_RETRY_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)

# Codigos de error de Twilio -> motivo interno legible (panel admin).
FAILURE_REASONS: dict[str, str] = {
    "63016": "Fuera de la ventana de 24 h: requiere plantilla aprobada",
    "63003": "Canal de WhatsApp no encontrado o mal configurado",
    "63007": "Remitente de WhatsApp no encontrado o mal configurado",
    "63018": "Límite de envíos alcanzado (reintentar más tarde)",
    "20429": "Límite de solicitudes alcanzado (reintentar más tarde)",
    "63038": "Límite diario de mensajes del remitente alcanzado",
    "63024": "El destinatario no tiene WhatsApp",
    "21211": "Número de destino inválido",
    "21610": "El destinatario se dio de baja (STOP)",
    "21614": "El número no es móvil",
    "21608": "Número no verificado (cuenta de prueba)",
    "63049": "Mensaje de marketing bloqueado por política del destinatario",
}


@dataclass(frozen=True, slots=True)
class SendResult:
    ok: bool
    sid: str | None = None
    status: str = ""
    error: str | None = None
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class TwilioCreds:
    account_sid: str
    auth_token: str
    from_number: str  # E.164 sin prefijo ``whatsapp:``
    messaging_service_sid: str = ""


def failure_reason(code: str | None) -> str:
    if not code:
        return ""
    return FAILURE_REASONS.get(code, f"Error del proveedor ({code})")


# --------------------------------------------------------------------------- firma
def validate_twilio_signature(
    url: str, params: dict[str, str], signature: str, auth_token: str
) -> bool:
    """Firma Twilio: base64(HMAC-SHA1(token, url + params ordenados concatenados))."""
    if not auth_token or not signature:
        return False
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()  # noqa: S324
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def sign_twilio(url: str, params: dict[str, str], auth_token: str) -> str:
    """Calcula la firma (usada por tests y utilidades locales)."""
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()  # noqa: S324
    return base64.b64encode(digest).decode()


# --------------------------------------------------------------------------- helpers
def to_whatsapp(e164: str) -> str:
    e164 = e164.strip()
    return e164 if e164.startswith("whatsapp:") else f"whatsapp:{e164}"


def from_whatsapp(value: str) -> str:
    return value.strip().removeprefix("whatsapp:").replace(" ", "")


def window_is_open(last_inbound_at: datetime | None, now: datetime | None = None) -> bool:
    """Ventana de servicio: 24 h desde el ultimo mensaje entrante del cliente."""
    if last_inbound_at is None:
        return False
    return (now or utcnow()) - last_inbound_at < WINDOW


async def read_tenant_secret(session: AsyncSession, tenant_id: uuid.UUID, kind: str) -> str | None:
    row = (
        await session.execute(
            select(TenantSecret).where(
                TenantSecret.tenant_id == tenant_id, TenantSecret.kind == kind
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    blob = EncryptedBlob(row.ciphertext, row.nonce, row.key_version)
    try:
        return get_crypto().decrypt_str(blob, aad=make_aad("tenant_secrets", tenant_id, kind))
    except Exception:  # noqa: BLE001 - secreto corrupto o clave rotada: se trata como ausente
        log.error("twilio.secret_unreadable", tenant_id=str(tenant_id), kind=kind)
        return None


async def resolve_auth_token(session: AsyncSession, tenant_id: uuid.UUID | None) -> str:
    """Token para validar firmas: el del tenant si lo tiene; si no, el de la plataforma."""
    if tenant_id is not None:
        token = await read_tenant_secret(session, tenant_id, "twilio_auth_token")
        if token:
            return token
    return get_settings().twilio_auth_token.get_secret_value()


async def resolve_credentials(
    session: AsyncSession, tenant_id: uuid.UUID | None, *, from_override: str | None = None
) -> TwilioCreds:
    s = get_settings()
    sid = s.twilio_account_sid
    token = s.twilio_auth_token.get_secret_value()
    from_number = s.twilio_whatsapp_from
    ms_sid = s.twilio_messaging_service_sid
    if tenant_id is not None:
        t_sid = await read_tenant_secret(session, tenant_id, "twilio_sid")
        t_token = await read_tenant_secret(session, tenant_id, "twilio_auth_token")
        if t_sid and t_token:
            sid, token = t_sid, t_token
        account = (
            await session.execute(
                select(ChannelAccount).where(
                    ChannelAccount.tenant_id == tenant_id,
                    ChannelAccount.channel == "whatsapp",
                    ChannelAccount.status == "active",
                )
            )
        ).scalar_one_or_none()
        if account is not None:
            from_number = account.phone_e164
            ms_sid = account.twilio_messaging_service_sid or ""
        else:
            from_number, ms_sid = "", ""  # nunca enviar un tenant desde el numero de la agencia
    if from_override:
        from_number = from_whatsapp(from_override)
    return TwilioCreds(sid, token, from_number, ms_sid)


async def _window_open_for(session: AsyncSession, tenant_id: uuid.UUID, to_e164: str) -> bool:
    last = (
        await session.execute(
            select(func.max(Conversation.last_inbound_at))
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(
                Conversation.tenant_id == tenant_id,
                Contact.phone_hash == phone_hash(to_e164),
            )
        )
    ).scalar_one_or_none()
    return window_is_open(last)


def _status_callback() -> str:
    return get_settings().public_base_url.rstrip("/") + "/webhooks/twilio/status"


async def _post_message(creds: TwilioCreds, data: dict[str, str]) -> SendResult:
    url = f"{TWILIO_API}/Accounts/{creds.account_sid}/Messages.json"
    last_error = "sin_respuesta"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = await client.post(url, data=data, auth=(creds.account_sid, creds.auth_token))
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
                log.warning("twilio.send_network_error", error=last_error, attempt=attempt)
                if not isinstance(exc, _SAFE_RETRY_ERRORS):
                    # La peticion pudo llegar a Twilio (p. ej. ReadTimeout): reintentar duplicaria.
                    return SendResult(ok=False, status="failed", error=last_error)
            else:
                if resp.status_code in (200, 201):
                    body = resp.json()
                    return SendResult(
                        ok=True, sid=body.get("sid"), status=str(body.get("status", "queued"))
                    )
                code, last_error = _parse_error(resp)
                if resp.status_code not in RETRY_STATUSES:
                    log.warning("twilio.send_rejected", http=resp.status_code, code=code)
                    return SendResult(ok=False, status="failed", error=last_error)
                log.warning("twilio.send_retry", http=resp.status_code, attempt=attempt)
            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(0.2 * 2**attempt)
    return SendResult(ok=False, status="failed", error=last_error)


def _parse_error(resp: httpx.Response) -> tuple[str, str]:
    try:
        body = resp.json()
        code = str(body.get("code", "")) if isinstance(body, dict) else ""
    except (json.JSONDecodeError, ValueError):
        code = ""
    return code, code or f"http_{resp.status_code}"


async def _deliver(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    to_e164: str,
    fields: dict[str, str],
    kind: str,
    from_override: str | None = None,
) -> SendResult:
    s = get_settings()
    if s.twilio_dry_run:
        sid = f"DRYRUN{uuid.uuid4().hex[:28]}"
        log.info("twilio.dry_run", kind=kind, tenant_id=str(tenant_id), sid=sid)
        return SendResult(ok=True, sid=sid, status="queued", dry_run=True)
    creds = await resolve_credentials(session, tenant_id, from_override=from_override)
    if not (
        creds.account_sid
        and creds.auth_token
        and (creds.from_number or creds.messaging_service_sid)
    ):
        log.error("twilio.not_configured", tenant_id=str(tenant_id))
        return SendResult(ok=False, status="failed", error="twilio_no_configurado")
    data = {"To": to_whatsapp(to_e164), "StatusCallback": _status_callback(), **fields}
    if creds.messaging_service_sid and not from_override:
        data["MessagingServiceSid"] = creds.messaging_service_sid
    else:
        data["From"] = to_whatsapp(creds.from_number)
    return await _post_message(creds, data)


# --------------------------------------------------------------------------- API publica
async def send_whatsapp_text(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    to_e164: str,
    body: str,
    bypass_suppression: bool = False,
) -> SendResult:
    """Texto libre; solo dentro de la ventana de 24 h. ``bypass_suppression`` es exclusivo de la
    confirmacion de baja (STOP)."""
    if not bypass_suppression and await is_suppressed(session, to_e164, tenant_id=tenant_id):
        return SendResult(ok=False, status="suppressed", error="suprimido")
    if not await _window_open_for(session, tenant_id, to_e164):
        return SendResult(ok=False, status="failed", error="63016")
    return await _deliver(
        session, tenant_id=tenant_id, to_e164=to_e164, fields={"Body": body}, kind="text"
    )


async def send_whatsapp_template(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    to_e164: str,
    content_sid: str,
    variables: dict[str, str],
    from_override: str | None = None,
) -> SendResult:
    """``tenant_id=None`` => cuenta de la agencia (outreach). Respeta supresion y dry-run."""
    if await is_suppressed(session, to_e164):
        return SendResult(ok=False, status="suppressed", error="suprimido")
    return await _deliver(
        session,
        tenant_id=tenant_id,
        to_e164=to_e164,
        fields={
            "ContentSid": content_sid,
            "ContentVariables": json.dumps(variables, ensure_ascii=False),
        },
        kind="template",
        from_override=from_override,
    )
