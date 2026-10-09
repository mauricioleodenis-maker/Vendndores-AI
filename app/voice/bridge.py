"""Puente voz -> motor conversacional: un turno hablado = un turno del engine (canal ``voice``)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels import service as channel_service
from app.conversation.engine import ConversationEngine
from app.core.clock import utcnow
from app.core.logging import get_logger
from app.db.models.contacts import Contact
from app.db.models.conversations import Conversation
from app.db.models.tenants import ChannelAccount, TenantProfile
from app.db.models.voice import CallSession
from app.plans.entitlements import get_entitlements
from app.voice.speech import normalize_for_tts

log = get_logger(__name__)

MAX_TURNS = 12
MAX_SENTENCES = 2
MAX_REPLY_CHARS = 280

NO_VOICE_TEXT = (
    "Por ahora este número no atiende llamadas con asistente. "
    "Escríbenos por WhatsApp. Hasta pronto."
)
TRANSFER_TEXT = "Te comunico con una persona del equipo. Un momento, por favor."
NO_AGENT_TEXT = "Una persona del equipo te contactará pronto. Gracias por llamar."
MAX_TURNS_TEXT = "Para ayudarte mejor, te comunico con una persona del equipo."
MAX_TURNS_NO_AGENT = (
    "Hemos hablado un buen rato. Una persona del equipo te contactará. Hasta pronto."
)
FALLBACK_TEXT = "Disculpa, ¿me lo puedes repetir?"
_SENT_SPLIT = re.compile(r"(?<=[.!?¿?])\s+")


@dataclass(frozen=True, slots=True)
class VoiceReply:
    text: str
    end_call: bool = False
    transfer_to: str | None = None


def shorten(
    text: str, *, max_sentences: int = MAX_SENTENCES, max_chars: int = MAX_REPLY_CHARS
) -> str:
    """Recorta a pocas frases para voz (sin viñetas ni markdown)."""
    clean = re.sub(r"[*_`#>]+", "", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    parts = [p for p in _SENT_SPLIT.split(clean) if p]
    out = " ".join(parts[:max_sentences]) if parts else clean
    if len(out) > max_chars:
        cut = out[:max_chars].rsplit(" ", 1)[0].rstrip(",;:")
        out = cut + "."
    return out


async def _get_call(session: AsyncSession, tenant_id: uuid.UUID, call_sid: str) -> CallSession:
    stmt = select(CallSession).where(
        CallSession.call_sid == call_sid, CallSession.tenant_id == tenant_id
    )
    call = (await session.execute(stmt)).scalar_one_or_none()
    if call is not None:
        return call
    call = CallSession(
        tenant_id=tenant_id, call_sid=call_sid, status="active", turns=0, consent_at=utcnow()
    )
    try:
        async with session.begin_nested():
            session.add(call)
            await session.flush()
    except IntegrityError:
        call = (await session.execute(stmt)).scalar_one()
    return call


async def _conversation(
    session: AsyncSession, tenant_id: uuid.UUID, call: CallSession, caller_e164: str
) -> Conversation:
    if call.conversation_id is not None:
        conv = await session.get(Conversation, call.conversation_id)
        if conv is not None and conv.tenant_id == tenant_id:
            return conv
    contact: Contact = await channel_service.get_or_create_contact(
        session, tenant_id, caller_e164, None
    )
    account_id = (
        await session.execute(
            select(ChannelAccount.id)
            .where(ChannelAccount.tenant_id == tenant_id, ChannelAccount.channel == "voice")
            .limit(1)
        )
    ).scalar_one_or_none()
    conv = Conversation(
        tenant_id=tenant_id,
        contact_id=contact.id,
        channel_account_id=account_id,
        channel="voice",
        status="open",
    )
    session.add(conv)
    await session.flush()
    call.conversation_id = conv.id
    return conv


async def _handoff_phone(session: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    profile = await session.get(TenantProfile, tenant_id)
    phone = (profile.handoff_phone or "").strip() if profile else ""
    return phone or None


async def voice_turn(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    call_sid: str,
    caller_e164: str,
    utterance: str,
    engine: ConversationEngine | None = None,
) -> VoiceReply:
    """Procesa un turno hablado. No hace commit (lo hace el router)."""
    ent = await get_entitlements(session, tenant_id)
    if not ent.has_feature("voice"):
        return VoiceReply(NO_VOICE_TEXT, end_call=True)

    call = await _get_call(session, tenant_id, call_sid)
    if call.status != "active":
        return VoiceReply("Gracias por llamar. Hasta pronto.", end_call=True)

    if call.consent_at is None:
        call.consent_at = utcnow()

    if call.turns >= MAX_TURNS:
        return await _escalate(session, tenant_id, call, MAX_TURNS_TEXT, MAX_TURNS_NO_AGENT)

    conv = await _conversation(session, tenant_id, call, caller_e164)
    call.turns += 1
    replies = await (engine or ConversationEngine()).handle_inbound(
        session, tenant_id=tenant_id, conversation_id=conv.id, text=utterance
    )
    await session.refresh(conv)
    if conv.status == "handoff":
        text = shorten(" ".join(replies)) if replies else ""
        return await _escalate(session, tenant_id, call, text or TRANSFER_TEXT, NO_AGENT_TEXT)
    if not replies:
        return VoiceReply(FALLBACK_TEXT)
    return VoiceReply(normalize_for_tts(shorten(" ".join(replies))))


async def _escalate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    call: CallSession,
    say_transfer: str,
    say_no_agent: str,
) -> VoiceReply:
    phone = await _handoff_phone(session, tenant_id)
    if phone:
        call.status = "transferred"
        return VoiceReply(normalize_for_tts(say_transfer), transfer_to=phone)
    call.status = "completed"
    return VoiceReply(normalize_for_tts(say_no_agent), end_call=True)


__all__ = ["VoiceReply", "voice_turn", "shorten", "MAX_TURNS"]
