"""Job ``channels.process_inbound``: opt-out -> aviso/consentimiento -> motor -> envio."""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels import service
from app.channels.sender import SendResult, send_whatsapp_text
from app.conversation import memory
from app.conversation.engine import ConversationEngine
from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.jobs import register_job
from app.core.logging import get_logger
from app.db.models.contacts import Consent, Contact
from app.db.models.conversations import Conversation, Message
from app.db.models.tenants import Tenant
from app.db.session import session_scope
from app.plans.entitlements import record_usage
from app.privacy import service as privacy

log = get_logger(__name__)

AGENCY_NAME = "Vendedores AI"
NOTICE_TEMPLATE = "privacy_notice"
CONSENT_PURPOSE = "atencion"
_YES = {"si", "s", "ok", "okay", "acepto", "de acuerdo", "dale", "claro", "listo", "vale"}

OPTOUT_REPLY = (
    "Listo, no recibirás recordatorios ni promociones. Tus citas confirmadas siguen vigentes. "
    "Si quieres borrar tus datos escribe *BORRAR MIS DATOS*."
)
CONSENT_REPLY = (
    "Gracias. Usaremos tus datos solo para gestionar tu atención. "
    "Para ejercer tus derechos escribe *DERECHOS* o visita {url}."
)
RIGHTS_REPLY = (
    "Puedes ejercer tus derechos (conocer, actualizar, corregir o borrar tus datos) en {url}. "
    "Si prefieres que un asesor te ayude, escríbenos y te contactamos."
)
ERASE_REPLY = (
    "Recibimos tu solicitud de borrado. Para proteger tu información debemos verificar tu "
    "identidad: completa la solicitud en {url} o responde con tu nombre completo y un asesor "
    "la gestionará."
)
ENGINE_FALLBACK_REPLY = "Un momento, te comunicamos con el equipo para ayudarte."
MEDIA_REPLY = (
    "Por ahora solo puedo leer mensajes de texto. ¿Me cuentas por escrito en qué te puedo ayudar?"
)


def privacy_notice(business_name: str) -> str:
    url = get_settings().public_base_url.rstrip("/") + "/privacidad"
    return (
        f"Hola, soy el asistente virtual de *{business_name}*. Para gestionar tus citas tratamos "
        "tu nombre y teléfono. Te pedimos *no* compartir datos de salud por este chat. "
        f"Responsable: {business_name}; el servicio lo opera {AGENCY_NAME}. "
        f"Política de privacidad: {url}. Responde *SI* para continuar, o *STOP* para no recibir "
        "recordatorios."
    )


def _is_yes(text: str) -> bool:
    folded = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", "", folded).strip() in _YES


def _new_out_message(conv: Conversation, body: str, *, template_name: str | None = None) -> Message:
    return Message(
        tenant_id=conv.tenant_id,
        conversation_id=conv.id,
        direction="out",
        role="assistant",
        body_enc=memory.encrypt_body(conv.tenant_id, body),
        body_redacted=privacy.redact_pii(body)[:2000],
        status="queued",
        template_name=template_name,
        purge_after=(utcnow() + timedelta(days=get_settings().msg_retention_days)).date(),
    )


async def _deliver(
    session: AsyncSession,
    conv: Conversation,
    phone: str,
    body: str,
    message: Message | None = None,
    *,
    template_name: str | None = None,
    bypass_suppression: bool = False,
    before: Message | None = None,
) -> SendResult:
    """Envia ``body`` y deja constancia (crea el saliente si el motor no lo hizo)."""
    if message is None:
        message = _new_out_message(conv, body, template_name=template_name)
        if before is not None and before.created_at is not None:
            message.created_at = before.created_at - timedelta(microseconds=1)
        session.add(message)
        await session.flush()
    res = await send_whatsapp_text(
        session,
        tenant_id=conv.tenant_id,
        to_e164=phone,
        body=body,
        bypass_suppression=bypass_suppression,
    )
    await service.set_outbound_sent(
        session,
        message.id,
        sid=res.sid,
        status="sent" if res.ok and not res.dry_run else "queued",
        error=None if res.ok else (res.error or "error"),
    )
    if not res.ok:
        log.warning("channels.send_failed", tenant_id=str(conv.tenant_id), error=res.error)
    return res


async def _has_consent(session: AsyncSession, tenant_id: uuid.UUID, contact_id: uuid.UUID) -> bool:
    row = await session.execute(
        select(Consent.id).where(
            Consent.tenant_id == tenant_id,
            Consent.contact_id == contact_id,
            Consent.purpose == CONSENT_PURPOSE,
            Consent.revoked_at.is_(None),
        )
    )
    return row.first() is not None


async def _notice_sent(session: AsyncSession, conv: Conversation) -> bool:
    row = await session.execute(
        select(Message.id)
        .where(
            Message.tenant_id == conv.tenant_id,
            Message.conversation_id == conv.id,
            Message.template_name == NOTICE_TEMPLATE,
        )
        .limit(1)
    )
    return row.first() is not None


async def _unsent_outbound(session: AsyncSession, conv: Conversation) -> list[Message]:
    rows = await session.execute(
        select(Message)
        .where(
            Message.tenant_id == conv.tenant_id,
            Message.conversation_id == conv.id,
            Message.direction == "out",
            Message.status == "queued",
            Message.provider_sid.is_(None),
        )
        .order_by(Message.created_at, Message.id)
    )
    return list(rows.scalars())


async def _handle_optout(
    session: AsyncSession, conv: Conversation, contact: Contact, phone: str, text: str
) -> None:
    contact.opted_out = True
    contact.opted_out_at = utcnow()
    contact.opt_out_source = "keyword"
    contact.opt_out_keyword = text.strip()[:60]
    await privacy.apply_optout(
        session, phone_e164=phone, tenant_id=conv.tenant_id, evidence=f"keyword:{text.strip()[:40]}"
    )
    # La confirmacion de baja es la unica salida permitida a un contacto suprimido.
    await _deliver(session, conv, phone, OPTOUT_REPLY, bypass_suppression=True)


async def process(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    engine: ConversationEngine | None = None,
) -> int:
    """Procesa un entrante. Devuelve cuantos mensajes se enviaron. Idempotente."""
    msg = (
        await session.execute(
            select(Message).where(
                Message.id == message_id,
                Message.tenant_id == tenant_id,
                Message.conversation_id == conversation_id,
                Message.direction == "in",
            )
        )
    ).scalar_one_or_none()
    conv = await session.get(Conversation, conversation_id)
    if msg is None or conv is None or conv.tenant_id != tenant_id or msg.processed_at is not None:
        return 0
    contact = await session.get(Contact, conv.contact_id) if conv.contact_id else None
    phone = service.decrypt_phone(tenant_id, contact) if contact else None
    if contact is None or phone is None or contact.erased_at is not None:
        msg.processed_at = utcnow()
        return 0
    text = memory.decrypt_body(tenant_id, msg)
    await record_usage(session, tenant_id, messages_in=1)

    if privacy.is_optout_message(text):
        msg.processed_at = utcnow()
        await _handle_optout(session, conv, contact, phone, text)
        await record_usage(session, tenant_id, messages_out=1)
        return 1

    if contact.opted_out:
        if privacy.is_optin_message(text):
            contact.opted_out = False
            contact.opt_out_source = None
            contact.opted_out_at = None
            contact.opt_out_keyword = None
            await privacy.apply_optin(
                session, phone_e164=phone, tenant_id=tenant_id, evidence="keyword"
            )
        else:
            msg.processed_at = utcnow()
            return 0

    url = get_settings().public_base_url.rstrip("/") + "/privacidad"
    consent = await _has_consent(session, tenant_id, contact.id)
    notice_sent = await _notice_sent(session, conv)
    if not consent and notice_sent and _is_yes(text):
        msg.processed_at = utcnow()
        await privacy.record_consent(session, tenant_id, contact.id, CONSENT_PURPOSE, "whatsapp:SI")
        await _deliver(session, conv, phone, CONSENT_REPLY.format(url=url))
        await record_usage(session, tenant_id, messages_out=1)
        return 1

    if privacy.is_erase_request(text) or privacy.is_rights_request(text):
        msg.processed_at = utcnow()
        reply = ERASE_REPLY if privacy.is_erase_request(text) else RIGHTS_REPLY
        await _deliver(session, conv, phone, reply.format(url=url))
        await record_usage(session, tenant_id, messages_out=1)
        return 1

    if not text.strip():
        msg.processed_at = utcnow()
        sent = 0
        if not consent and not notice_sent:
            tenant = await session.get(Tenant, tenant_id)
            notice = privacy_notice(tenant.name if tenant else "el negocio")
            await _deliver(session, conv, phone, notice, template_name=NOTICE_TEMPLATE)
            sent += 1
        await _deliver(session, conv, phone, MEDIA_REPLY)
        await record_usage(session, tenant_id, messages_out=sent + 1)
        return sent + 1

    try:
        async with session.begin_nested():
            replies = await (engine or ConversationEngine()).handle_inbound(
                session, tenant_id=tenant_id, conversation_id=conversation_id, text=text
            )
    except Exception as exc:  # noqa: BLE001 - un fallo del motor no debe reintentar en bucle
        log.error("channels.engine_failed", error=type(exc).__name__, tenant_id=str(tenant_id))
        msg.processed_at = utcnow()
        # No dejar al paciente sin respuesta: aviso de respaldo (el equipo lo ve en el panel).
        await _deliver(session, conv, phone, ENGINE_FALLBACK_REPLY)
        await record_usage(session, tenant_id, messages_out=1)
        return 1
    if not replies:
        return 0

    queued = (await _unsent_outbound(session, conv))[-len(replies) :]
    pairs: list[tuple[str, Message | None]] = []
    offset = len(replies) - len(queued)
    for i, reply in enumerate(replies):
        j = i - offset
        pairs.append((reply, queued[j] if j >= 0 else None))

    sent = 0
    if not consent and not notice_sent:
        tenant = await session.get(Tenant, tenant_id)
        notice = privacy_notice(tenant.name if tenant else "el negocio")
        first = next((row for _, row in pairs if row is not None), None)
        await _deliver(session, conv, phone, notice, template_name=NOTICE_TEMPLATE, before=first)
        sent += 1
    for reply, row in pairs:
        await _deliver(session, conv, phone, reply, row)
        sent += 1
    await record_usage(session, tenant_id, messages_out=sent)
    return sent


@register_job("channels.process_inbound")
async def process_inbound(
    ctx: dict[str, Any], tenant_id: str, conversation_id: str, message_id: str
) -> int:
    async with session_scope() as session:
        return await process(
            session, uuid.UUID(tenant_id), uuid.UUID(conversation_id), uuid.UUID(message_id)
        )
