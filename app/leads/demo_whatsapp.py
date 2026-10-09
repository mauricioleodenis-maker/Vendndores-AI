"""Demo por WhatsApp: el negocio escribe ``DEMO-<slug>`` al numero de demo de la agencia.

``demo_links`` genera ``wa.me/<VAI_DEMO_WHATSAPP_NUMBER>?text=DEMO-<slug>``. Aqui se enlaza ese
telefono con el tenant demo y cada mensaje siguiente se responde con su bot (mismo tope de
mensajes que la demo web). La respuesta va en la propia TwiML del webhook.
"""

from __future__ import annotations

import re
from datetime import timedelta
from xml.sax.saxutils import escape

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.sender import from_whatsapp
from app.core.clock import ensure_utc, utcnow
from app.core.config import get_settings
from app.core.crypto import phone_hash
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models.leads import DemoWhatsappSession
from app.db.models.tenants import Tenant
from app.leads import demo

log = get_logger(__name__)

_CODE_RE = re.compile(r"^\s*DEMO-([a-z0-9-]{1,80})\s*$", re.I)
UNKNOWN_TEXT = (
    "Hola. Para probar la demostración abre el enlace que te compartimos (empieza con DEMO-)."
)


def is_demo_number(number: str) -> bool:
    configured = get_settings().demo_whatsapp_number.strip()
    if not configured:
        return False
    return from_whatsapp(number) == "+" + configured.lstrip("+")


def message_twiml(text: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response><Message>'
        f"{escape(text)}</Message></Response>"
    )


async def _tenant_by_slug(session: AsyncSession, slug: str) -> Tenant | None:
    return (
        await session.execute(
            select(Tenant).where(
                Tenant.slug == slug.lower(), Tenant.is_demo.is_(True), Tenant.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()


async def handle_inbound(session: AsyncSession, sender: str, body: str) -> str:
    """Texto a responder para un mensaje entrante al numero de demo."""
    phone = from_whatsapp(sender)
    if not phone:
        return UNKNOWN_TEXT
    key = phone_hash(phone)
    now = utcnow()
    row = (
        await session.execute(
            select(DemoWhatsappSession).where(DemoWhatsappSession.phone_hash == key)
        )
    ).scalar_one_or_none()

    match = _CODE_RE.match(body or "")
    if match:
        tenant = await _tenant_by_slug(session, match.group(1))
        if tenant is None:
            return "Esa demostración no existe o ya venció. Pide un enlace nuevo."
        expires = now + timedelta(days=get_settings().demo_token_ttl_days)
        if row is None:
            row = DemoWhatsappSession(phone_hash=key, tenant_id=tenant.id, expires_at=expires)
            session.add(row)
        else:
            row.tenant_id, row.history, row.expires_at = tenant.id, [], expires
        await session.flush()
        return (
            f"¡Hola! Estás hablando con el asistente virtual de demostración de {tenant.name}. "
            "Escríbele como lo haría un cliente: pregunta un precio, un horario o pide una cita."
        )

    if row is None or ensure_utc(row.expires_at) < now:
        return UNKNOWN_TEXT
    tenant = await session.get(Tenant, row.tenant_id)
    if tenant is None or tenant.deleted_at is not None:
        return "Esta demostración ya no está disponible."
    history = demo.clean_history(row.history or [])
    try:
        reply = await demo.demo_chat_reply(session, "", history, body, tenant=tenant)
    except AppError as exc:
        if exc.status == 429:
            return "Se alcanzó el límite de mensajes de esta demostración."
        return exc.message
    turns = [*history, {"role": "user", "content": body.strip()[: demo.MAX_TEXT]}]
    turns.append({"role": "assistant", "content": reply})
    row.history = turns[-demo.MAX_HISTORY :]
    await session.flush()
    return reply
