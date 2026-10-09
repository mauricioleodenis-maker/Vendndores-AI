"""Opt-out/opt-in, lista de supresion y consentimientos (Ley 1581). Contrato MAESTRO §5."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.core.crypto import phone_hash
from app.core.errors import AppError
from app.db.models.contacts import Consent, Contact
from app.db.models.privacy import SuppressionEntry
from app.privacy.keywords import (
    is_erase_request,
    is_optin_message,
    is_optout_message,
    is_rights_request,
    normalize_keyword,
)
from app.privacy.redaction import redact_pii
from app.privacy.texts import POLICY_VERSION, PURPOSES

__all__ = [
    "apply_optin",
    "apply_optout",
    "has_consent",
    "is_erase_request",
    "is_optin_message",
    "is_optout_message",
    "is_rights_request",
    "is_suppressed",
    "normalize_keyword",
    "record_consent",
    "redact_pii",
    "revoke_consent",
]

_MAX_EVIDENCE = 500


async def apply_optout(
    session: AsyncSession,
    *,
    phone_e164: str,
    tenant_id: uuid.UUID | None = None,
    evidence: str,
) -> None:
    """Suprime el telefono (ambito tenant si se da ``tenant_id``, si no global), marca los
    contactos y revoca consentimientos de recordatorios/marketing. Idempotente."""
    h = phone_hash(phone_e164)
    scope = "tenant" if tenant_id else "global"
    key = str(tenant_id) if tenant_id else ""
    exists = (
        await session.execute(
            select(SuppressionEntry.id).where(
                SuppressionEntry.phone_hash == h,
                SuppressionEntry.scope == scope,
                SuppressionEntry.tenant_key == key,
            )
        )
    ).first()
    now = utcnow()
    if exists is None:
        session.add(
            SuppressionEntry(
                phone_hash=h,
                reason="opt_out",
                scope=scope,
                tenant_id=tenant_id,
                tenant_key=key,
                evidence=evidence[:_MAX_EVIDENCE],
                suppressed_at=now,
            )
        )
    contact_filter = [Contact.phone_hash == h, Contact.opted_out.is_(False)]
    if tenant_id:
        contact_filter.append(Contact.tenant_id == tenant_id)
    contact_ids = (await session.execute(select(Contact.id).where(*contact_filter))).scalars().all()
    if contact_ids:
        await session.execute(
            update(Contact)
            .where(Contact.id.in_(contact_ids))
            .values(
                opted_out=True,
                opted_out_at=now,
                opt_out_source="keyword",
                opt_out_keyword=evidence[:60],
            )
        )
        await session.execute(
            update(Consent)
            .where(
                Consent.contact_id.in_(contact_ids),
                Consent.purpose.in_(("recordatorios", "marketing")),
                Consent.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
    await session.flush()


async def apply_optin(
    session: AsyncSession, *, phone_e164: str, tenant_id: uuid.UUID | None = None, evidence: str
) -> bool:
    """Reactiva un telefono suprimido por palabra clave. Solo retira entradas ``opt_out`` del
    mismo ambito (quejas y supresiones manuales nunca se levantan automaticamente)."""
    h = phone_hash(phone_e164)
    scope = "tenant" if tenant_id else "global"
    key = str(tenant_id) if tenant_id else ""
    result = await session.execute(
        delete(SuppressionEntry).where(
            SuppressionEntry.phone_hash == h,
            SuppressionEntry.scope == scope,
            SuppressionEntry.tenant_key == key,
            SuppressionEntry.reason == "opt_out",
        )
    )
    contact_filter = [Contact.phone_hash == h, Contact.opt_out_source == "keyword"]
    if tenant_id:
        contact_filter.append(Contact.tenant_id == tenant_id)
    await session.execute(
        update(Contact)
        .where(*contact_filter)
        .values(opted_out=False, opted_out_at=None, opt_out_source=None, opt_out_keyword=None)
    )
    await session.flush()
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def is_suppressed(
    session: AsyncSession, phone_e164: str, *, tenant_id: uuid.UUID | None = None
) -> bool:
    """``True`` si el telefono no debe recibir mensajes. Sin ``tenant_id`` es conservador: cualquier
    entrada cuenta. Con ``tenant_id`` solo cuentan las globales/agencia y las de ese tenant."""
    stmt = select(SuppressionEntry.id).where(SuppressionEntry.phone_hash == phone_hash(phone_e164))
    if tenant_id is not None:
        stmt = stmt.where(
            or_(
                SuppressionEntry.scope.in_(("global", "agency")),
                SuppressionEntry.tenant_key == str(tenant_id),
            )
        )
    return (await session.execute(stmt.limit(1))).first() is not None


async def record_consent(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    purpose: str,
    evidence: str,
) -> None:
    """Registra un consentimiento con el texto exacto como evidencia. No duplica uno vigente."""
    if purpose not in PURPOSES:
        raise AppError("invalid_purpose", "Finalidad de consentimiento no válida", 422)
    active = (
        await session.execute(
            select(Consent.id).where(
                Consent.tenant_id == tenant_id,
                Consent.contact_id == contact_id,
                Consent.purpose == purpose,
                Consent.revoked_at.is_(None),
            )
        )
    ).first()
    if active is not None:
        return
    session.add(
        Consent(
            tenant_id=tenant_id,
            contact_id=contact_id,
            purpose=purpose,
            policy_version=POLICY_VERSION,
            evidence=evidence[:_MAX_EVIDENCE],
        )
    )
    await session.flush()


async def revoke_consent(
    session: AsyncSession, tenant_id: uuid.UUID, contact_id: uuid.UUID, purpose: str
) -> int:
    result = await session.execute(
        update(Consent)
        .where(
            Consent.tenant_id == tenant_id,
            Consent.contact_id == contact_id,
            Consent.purpose == purpose,
            Consent.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    await session.flush()
    return int(result.rowcount)  # type: ignore[attr-defined]


async def has_consent(
    session: AsyncSession, tenant_id: uuid.UUID, contact_id: uuid.UUID, purpose: str
) -> bool:
    row = (
        await session.execute(
            select(Consent.id).where(
                Consent.tenant_id == tenant_id,
                Consent.contact_id == contact_id,
                Consent.purpose == purpose,
                Consent.revoked_at.is_(None),
            )
        )
    ).first()
    return row is not None
