"""Logica de canal: ruteo por numero, idempotencia, persistencia de entrantes y status."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.sender import from_whatsapp, read_tenant_secret
from app.conversation.memory import encrypt_body
from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.crypto import get_crypto, make_aad, pack_blob, phone_hash, unpack_blob
from app.core.logging import get_logger
from app.db.models.contacts import Contact
from app.db.models.conversations import Conversation, Message
from app.db.models.scheduling import WebhookEvent
from app.db.models.tenants import ChannelAccount, Tenant
from app.privacy.service import apply_optout, redact_pii

log = get_logger(__name__)

MAX_BODY_CHARS = 4000
# Orden monotono de estados: nunca retroceder por llegada desordenada.
STATUS_RANK: dict[str, int] = {
    "received": 0,
    "queued": 1,
    "sent": 2,
    "delivered": 3,
    "read": 4,
    "failed": 5,
    "undelivered": 5,
}
TWILIO_STATUS_MAP: dict[str, str] = {
    "accepted": "queued",
    "scheduled": "queued",
    "queued": "queued",
    "sending": "queued",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "undelivered": "undelivered",
}


# Otros modulos (p. ej. outreach) registran aqui su manejo de status de mensajes que NO son de un
# tenant (cuenta de la agencia). Firma: ``async def fn(session, params) -> None``.
StatusFallback = Callable[[AsyncSession, dict[str, str]], Awaitable[None]]
STATUS_FALLBACKS: list[StatusFallback] = []


@dataclass(frozen=True, slots=True)
class ResolvedChannel:
    account: ChannelAccount
    tenant: Tenant


# --------------------------------------------------------------------------- cifrado de telefono
def encrypt_phone(tenant_id: uuid.UUID, phone_e164: str) -> bytes:
    aad = make_aad("contacts", tenant_id, "phone_enc")
    return pack_blob(get_crypto().encrypt_str(phone_e164, aad=aad))


def decrypt_phone(tenant_id: uuid.UUID, contact: Contact) -> str | None:
    if not contact.phone_enc:
        return None
    try:
        aad = make_aad("contacts", tenant_id, "phone_enc")
        return get_crypto().decrypt_str(unpack_blob(contact.phone_enc), aad=aad)
    except Exception:  # noqa: BLE001 - clave rotada/corrupta
        log.error("channels.phone_unreadable", contact_id=str(contact.id))
        return None


# --------------------------------------------------------------------------- ruteo
async def resolve_channel(
    session: AsyncSession, number: str, channel: str | None = "whatsapp"
) -> ResolvedChannel | None:
    """Canal activo cuyo numero coincide con ``number`` (acepta prefijo ``whatsapp:``)."""
    e164 = from_whatsapp(number)
    if not e164:
        return None
    stmt = (
        select(ChannelAccount, Tenant)
        .join(Tenant, Tenant.id == ChannelAccount.tenant_id)
        .where(
            ChannelAccount.phone_e164 == e164,
            ChannelAccount.status == "active",
            Tenant.deleted_at.is_(None),
        )
    )
    if channel is not None:
        stmt = stmt.where(ChannelAccount.channel == channel)
    row = (await session.execute(stmt)).first()
    return ResolvedChannel(row[0], row[1]) if row else None


async def auth_token_for(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    token = await read_tenant_secret(session, tenant_id, "twilio_auth_token")
    return token or get_settings().twilio_auth_token.get_secret_value()


def public_url(path: str, query: str = "") -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}{path}" + (f"?{query}" if query else "")


# --------------------------------------------------------------------------- idempotencia
async def claim_event(
    session: AsyncSession,
    *,
    kind: str,
    sid: str,
    status_value: str = "",
    tenant_id: uuid.UUID | None,
    params: dict[str, str],
) -> bool:
    """Registra el evento; ``False`` si ya se habia recibido (reintento de Twilio)."""
    payload_hash = hashlib.sha256(
        json.dumps(sorted(params.items()), ensure_ascii=False).encode()
    ).hexdigest()
    try:
        async with session.begin_nested():
            session.add(
                WebhookEvent(
                    provider="twilio",
                    provider_sid=sid[:64],
                    kind=kind,
                    status_value=status_value[:30],
                    tenant_id=tenant_id,
                    payload_hash=payload_hash,
                )
            )
        return True
    except IntegrityError:
        return False


# --------------------------------------------------------------------------- entrantes
async def get_or_create_contact(
    session: AsyncSession, tenant_id: uuid.UUID, phone_e164: str, profile_name: str | None
) -> Contact:
    h = phone_hash(phone_e164)
    contact = (
        await session.execute(
            select(Contact).where(Contact.tenant_id == tenant_id, Contact.phone_hash == h)
        )
    ).scalar_one_or_none()
    if contact is None:
        name_enc = None
        if profile_name:
            aad = make_aad("contacts", tenant_id, "display_name_enc")
            name_enc = pack_blob(get_crypto().encrypt_str(profile_name[:120], aad=aad))
        contact = Contact(
            tenant_id=tenant_id,
            phone_hash=h,
            phone_enc=encrypt_phone(tenant_id, phone_e164),
            display_name_enc=name_enc,
            source="inbound",
        )
        session.add(contact)
        await session.flush()
    elif contact.phone_enc is None and contact.erased_at is None:
        contact.phone_enc = encrypt_phone(tenant_id, phone_e164)
    return contact


async def get_or_create_conversation(
    session: AsyncSession, channel: ResolvedChannel, contact: Contact
) -> Conversation:
    conv = (
        (
            await session.execute(
                select(Conversation)
                .where(
                    Conversation.tenant_id == channel.tenant.id,
                    Conversation.contact_id == contact.id,
                    Conversation.channel == "whatsapp",
                    Conversation.status != "closed",
                )
                .order_by(Conversation.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if conv is None:
        conv = Conversation(
            tenant_id=channel.tenant.id,
            contact_id=contact.id,
            channel_account_id=channel.account.id,
            channel="whatsapp",
            status="open",
        )
        session.add(conv)
        await session.flush()
    return conv


@dataclass(frozen=True, slots=True)
class InboundResult:
    duplicate: bool
    message_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None


async def persist_inbound(
    session: AsyncSession, channel: ResolvedChannel, params: dict[str, str]
) -> InboundResult:
    sid = params.get("MessageSid") or params.get("SmsSid") or ""
    tenant_id = channel.tenant.id
    if not sid:
        return InboundResult(duplicate=True)
    if not await claim_event(session, kind="inbound", sid=sid, tenant_id=tenant_id, params=params):
        return InboundResult(duplicate=True)
    phone = from_whatsapp(params.get("From", ""))
    contact = await get_or_create_contact(session, tenant_id, phone, params.get("ProfileName"))
    conv = await get_or_create_conversation(session, channel, contact)
    text = (params.get("Body") or "")[:MAX_BODY_CHARS]
    now = utcnow()
    try:
        num_media = int(params.get("NumMedia", "0") or 0)
    except ValueError:
        num_media = 0
    msg = Message(
        tenant_id=tenant_id,
        conversation_id=conv.id,
        direction="in",
        role="user",
        provider_sid=sid[:64],
        body_enc=encrypt_body(tenant_id, text),
        body_redacted=redact_pii(text)[:2000],
        media_count=max(0, min(num_media, 10)),
        status="received",
        purge_after=(now + timedelta(days=get_settings().msg_retention_days)).date(),
    )
    session.add(msg)
    conv.last_inbound_at = now
    conv.last_message_at = now
    await session.flush()
    return InboundResult(False, msg.id, conv.id, tenant_id)


# --------------------------------------------------------------------------- status
@dataclass(frozen=True, slots=True)
class StatusResult:
    applied: bool
    opted_out: bool = False


async def apply_status(
    session: AsyncSession, tenant_id: uuid.UUID, params: dict[str, str]
) -> StatusResult:
    """Actualiza el estado del mensaje saliente de forma monotona y guarda ``ErrorCode``."""
    sid = params.get("MessageSid") or params.get("SmsSid") or ""
    raw_status = (params.get("MessageStatus") or params.get("SmsStatus") or "").lower()
    new = TWILIO_STATUS_MAP.get(raw_status)
    if not sid or new is None:
        return StatusResult(False)
    if not await claim_event(
        session, kind="status", sid=sid, status_value=raw_status, tenant_id=tenant_id, params=params
    ):
        return StatusResult(False)
    msg = (
        await session.execute(
            select(Message).where(Message.tenant_id == tenant_id, Message.provider_sid == sid)
        )
    ).scalar_one_or_none()
    if msg is None:
        return StatusResult(False)
    error_code = (params.get("ErrorCode") or "")[:20] or None
    if STATUS_RANK.get(new, 0) > STATUS_RANK.get(msg.status, 0):
        msg.status = new
    if error_code and new in ("failed", "undelivered"):
        msg.error_code = error_code
    opted_out = False
    if error_code == "21610":
        opted_out = await _optout_from_status(session, tenant_id, msg, error_code)
    await session.flush()
    return StatusResult(True, opted_out)


async def _optout_from_status(
    session: AsyncSession, tenant_id: uuid.UUID, msg: Message, code: str
) -> bool:
    conv = await session.get(Conversation, msg.conversation_id)
    contact = await session.get(Contact, conv.contact_id) if conv and conv.contact_id else None
    if contact is None:
        return False
    phone = decrypt_phone(tenant_id, contact)
    contact.opted_out = True
    contact.opted_out_at = utcnow()
    contact.opt_out_source = "complaint"
    if phone:
        await apply_optout(
            session, phone_e164=phone, tenant_id=tenant_id, evidence=f"twilio:{code}"
        )
    return True


async def set_outbound_sent(
    session: AsyncSession, message_id: uuid.UUID, *, sid: str | None, status: str, error: str | None
) -> None:
    """Marca un saliente (creado por el motor en ``queued``) con el resultado del envio."""
    values: dict[str, object] = {"status": status if status in STATUS_RANK else "queued"}
    if sid:
        values["provider_sid"] = sid[:64]
    if error:
        values["status"] = "failed"
        values["error_code"] = error[:20]
    await session.execute(update(Message).where(Message.id == message_id).values(**values))
