"""Traspaso a humano: crea ``handoffs``, pausa el bot y lo reanuda."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.core.logging import get_logger
from app.db.models.conversations import HANDOFF_REASONS, Conversation, Handoff
from app.privacy.service import redact_pii

log = get_logger(__name__)

PAUSE_HOURS = 12

#: Motivos que el LLM puede pedir (incluye alias del plan 13).
LLM_REASONS = (
    "user_request",
    "low_confidence",
    "medical_urgent",
    "complaint",
    "out_of_scope_repeated",
    "tool_failure",
)
_ALIASES = {
    "asked_for_human": "user_request",
    "urgent_medical": "medical_urgent",
    "out_of_scope": "low_confidence",
    "refund_or_billing": "complaint",
    "unclear": "low_confidence",
    "price_unknown": "low_confidence",
}


def normalize_reason(reason: str) -> str:
    reason = _ALIASES.get(reason, reason)
    return reason if reason in HANDOFF_REASONS else "low_confidence"


async def open_handoff(
    session: AsyncSession, conversation: Conversation, reason: str, summary: str = ""
) -> Handoff:
    """Abre (o reutiliza) el handoff de la conversacion y silencia al bot ``PAUSE_HOURS``."""
    reason = normalize_reason(reason)
    existing = (
        await session.execute(
            select(Handoff).where(
                Handoff.tenant_id == conversation.tenant_id,
                Handoff.conversation_id == conversation.id,
                Handoff.status.in_(("open", "claimed")),
            )
        )
    ).scalar_one_or_none()
    conversation.status = "handoff"
    conversation.handoff_reason = reason
    conversation.bot_paused_until = utcnow() + timedelta(hours=PAUSE_HOURS)
    if existing is not None:
        return existing
    handoff = Handoff(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        reason=reason,
        summary=redact_pii(summary)[:500],
        opened_at=utcnow(),
    )
    session.add(handoff)
    await session.flush()
    log.info("handoff.opened", tenant_id=str(conversation.tenant_id), reason=reason)
    return handoff


def bot_is_paused(conversation: Conversation) -> bool:
    """True si el bot no debe responder (humano a cargo y la pausa sigue vigente)."""
    if conversation.status != "handoff":
        return bool(conversation.bot_paused_until and conversation.bot_paused_until > utcnow())
    until = conversation.bot_paused_until
    return until is None or until > utcnow()


def resume_bot(conversation: Conversation) -> None:
    conversation.status = "open"
    conversation.bot_paused_until = None
    conversation.handoff_reason = None
