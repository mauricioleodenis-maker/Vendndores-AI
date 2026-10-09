"""Derechos del titular: exportar (acceso) y suprimir/anonimizar (art. 8 Ley 1581)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log_event
from app.core.clock import utcnow
from app.core.crypto import get_crypto, make_aad, unpack_blob
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.db.models.booking import Appointment
from app.db.models.contacts import Consent, Contact
from app.db.models.conversations import Conversation, Message

log = get_logger(__name__)
ERASED_BODY = "[eliminado a solicitud del titular]"


def _decrypt(tenant_id: uuid.UUID, table: str, column: str, blob: bytes | None) -> str | None:
    if not blob:
        return None
    try:
        return get_crypto().decrypt_str(unpack_blob(blob), aad=make_aad(table, tenant_id, column))
    except Exception:  # noqa: BLE001 - clave rotada o dato corrupto
        log.warning("privacy.undecryptable", table=table, column=column)
        return None


async def _get_contact(
    session: AsyncSession, tenant_id: uuid.UUID, contact_id: uuid.UUID
) -> Contact:
    contact = (
        await session.execute(
            select(Contact).where(Contact.id == contact_id, Contact.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if contact is None:
        raise NotFoundError("Contacto no encontrado")
    return contact


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def export_contact_data(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    *,
    actor: Any = None,
) -> dict[str, Any]:
    """Paquete de acceso del titular (JSON serializable)."""
    contact = await _get_contact(session, tenant_id, contact_id)
    consents = (
        (await session.execute(select(Consent).where(Consent.contact_id == contact_id)))
        .scalars()
        .all()
    )
    convs = (
        (
            await session.execute(
                select(Conversation).where(
                    Conversation.contact_id == contact_id, Conversation.tenant_id == tenant_id
                )
            )
        )
        .scalars()
        .all()
    )
    conversations: list[dict[str, Any]] = []
    for conv in convs:
        msgs = (
            (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conv.id)
                    .order_by(Message.created_at)
                )
            )
            .scalars()
            .all()
        )
        conversations.append(
            {
                "id": str(conv.id),
                "channel": conv.channel,
                "mensajes": [
                    {
                        "fecha": _iso(m.created_at),
                        "direccion": m.direction,
                        "texto": _decrypt(tenant_id, "messages", "body_enc", m.body_enc)
                        or m.body_redacted,
                    }
                    for m in msgs
                ],
            }
        )
    appts = (
        (await session.execute(select(Appointment).where(Appointment.contact_id == contact_id)))
        .scalars()
        .all()
    )
    data = {
        "generado_en": _iso(utcnow()),
        "contacto": {
            "id": str(contact.id),
            "telefono": _decrypt(tenant_id, "contacts", "phone_enc", contact.phone_enc),
            "nombre": _decrypt(tenant_id, "contacts", "display_name_enc", contact.display_name_enc),
            "primer_contacto": _iso(contact.first_seen_at),
            "baja_voluntaria": contact.opted_out,
            "suprimido_en": _iso(contact.erased_at),
        },
        "consentimientos": [
            {
                "finalidad": c.purpose,
                "version_politica": c.policy_version,
                "otorgado_en": _iso(c.granted_at),
                "revocado_en": _iso(c.revoked_at),
                "evidencia": c.evidence,
            }
            for c in consents
        ],
        "conversaciones": conversations,
        "citas": [
            {
                "id": str(a.id),
                "inicio": _iso(a.starts_at),
                "estado": a.status,
                "notas": _decrypt(tenant_id, "appointments", "notes_enc", a.notes_enc),
            }
            for a in appts
        ],
    }
    await log_event(
        session,
        actor=actor,
        action="privacy.dsar_export",
        entity_type="contact",
        entity_id=contact_id,
        tenant_id=tenant_id,
    )
    return data


async def erase_contact(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    *,
    actor: Any = None,
) -> None:
    """Anonimiza al contacto y sus mensajes. Conserva filas (citas, metricas) sin PII. El hash del
    telefono se reemplaza para permitir un contacto nuevo; la lista de supresion no se toca."""
    contact = await _get_contact(session, tenant_id, contact_id)
    now = utcnow()
    contact.phone_enc = None
    contact.display_name_enc = None
    contact.phone_hash = f"erased:{contact.id.hex}"
    contact.opt_out_keyword = None
    contact.erased_at = now
    conv_ids = (
        (
            await session.execute(
                select(Conversation.id).where(
                    Conversation.contact_id == contact_id, Conversation.tenant_id == tenant_id
                )
            )
        )
        .scalars()
        .all()
    )
    if conv_ids:
        await session.execute(
            update(Message)
            .where(Message.conversation_id.in_(conv_ids))
            .values(body_enc=None, body_redacted=ERASED_BODY, purge_after=None)
        )
        await session.execute(
            update(Conversation).where(Conversation.id.in_(conv_ids)).values(summary="")
        )
    await session.execute(
        update(Appointment).where(Appointment.contact_id == contact_id).values(notes_enc=None)
    )
    await session.execute(
        update(Consent)
        .where(Consent.contact_id == contact_id, Consent.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await log_event(
        session,
        actor=actor,
        action="privacy.dsar_erase",
        entity_type="contact",
        entity_id=contact_id,
        tenant_id=tenant_id,
    )
    await session.flush()
