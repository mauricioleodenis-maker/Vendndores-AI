"""Webhooks de voz de Twilio: ``incoming`` (saludo + aviso) y ``gather`` (turnos de voz).

Autenticados por firma ``X-Twilio-Signature`` (token del negocio o de la plataforma). El negocio
se enruta por el numero llamado (``To``) en ``channel_accounts`` con ``channel='voice'``. Solo
planes con ``voice_enabled``. Las respuestas son TwiML con texto escapado para XML.
"""

from __future__ import annotations

import re
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels import service
from app.channels.router import _form_params, _signature_ok
from app.core.deps import get_session
from app.core.errors import AppError
from app.core.logging import get_logger
from app.plans.entitlements import get_entitlements

log = get_logger(__name__)

router = APIRouter(prefix="/webhooks/twilio/voice", tags=["twilio-voice"], include_in_schema=False)

GATHER_PATH = "/webhooks/twilio/voice/gather"
GATHER_LANGUAGE = "es-CO"
SAY_LANGUAGE = "es-MX"
SAY_VOICE = "Polly.Mia-Neural"  # configurable: VAI_VOICE_SAY_VOICE
MAX_EMPTY_RETRIES = 2
MAX_UTTERANCE = 500
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")

NOTICE = (
    "Hola. Esta llamada puede ser atendida por un asistente virtual y su conversación se "
    "registra para brindarle un mejor servicio. Si continúa, acepta este tratamiento."
)
NO_VOICE = (
    "Hola. Por ahora este número no atiende llamadas con asistente. "
    "Escríbenos por WhatsApp. Hasta pronto."
)
REPROMPT = "No te escuché bien. ¿Me puedes repetir, por favor?"
GOODBYE = "No logré escucharte. Puedes escribirnos por WhatsApp. Hasta pronto."
ERROR_SAY = "Tuvimos un inconveniente técnico. Por favor intenta de nuevo más tarde. Hasta pronto."
TRANSFER_SAY = "Te comunico con una persona del equipo. Un momento, por favor."
PROMPT_ASK = "¿En qué te puedo ayudar?"


def _voice_name() -> str:
    from app.core.config import get_settings

    return str(getattr(get_settings(), "voice_say_voice", "") or SAY_VOICE)


def _say(text: str) -> str:
    return (
        f"<Say language={quoteattr(SAY_LANGUAGE)} voice={quoteattr(_voice_name())}>"
        f"{escape(text)}</Say>"
    )


def _gather(action: str, *prompts: str) -> str:
    inner = "".join(_say(p) for p in prompts)
    return (
        f'<Gather input="speech" language={quoteattr(GATHER_LANGUAGE)} '
        f'action={quoteattr(action)} method="POST" speechTimeout="auto" '
        f'actionOnEmptyResult="true">{inner}</Gather>'
    )


def _response(*parts: str) -> Response:
    body = '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(parts) + "</Response>"
    return Response(content=body, media_type="application/xml")


def _hangup(text: str) -> Response:
    return _response(_say(text), "<Hangup/>")


def _dial(number: str) -> str:
    return f"<Dial>{escape(number)}</Dial>"


def _safe_number(value: str | None) -> str | None:
    cleaned = re.sub(r"[\s\-().]", "", value or "")
    return cleaned if _E164.match(cleaned) else None


def _action_url(retry: int = 0) -> str:
    return service.public_url(GATHER_PATH, f"r={retry}" if retry else "")


async def _authorize(request: Request, session: AsyncSession) -> tuple[dict[str, str], Any, bool]:
    """Params, canal resuelto y si el plan permite voz; 403 si destino o firma no validos."""
    params = await _form_params(request)
    channel = await service.resolve_channel(session, params.get("To", ""), "voice")
    if channel is None:
        log.warning("twilio.voice_unknown_destination")
        raise AppError("firma_invalida", "Firma inválida", 403)
    token = await service.auth_token_for(session, channel.tenant.id)
    if not _signature_ok(request, params, token):
        log.warning("twilio.voice_bad_signature", tenant_id=str(channel.tenant.id))
        raise AppError("firma_invalida", "Firma inválida", 403)
    ent = await get_entitlements(session, channel.tenant.id)
    return params, channel, ent.has_feature("voice")


@router.post("/incoming")
async def voice_incoming(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    _params, channel, allowed = await _authorize(request, session)
    if not allowed:
        return _hangup(NO_VOICE)
    greeting = f"Gracias por llamar a {channel.tenant.name}."
    return _response(_gather(_action_url(), NOTICE, greeting, PROMPT_ASK), _hangup_tail())


def _hangup_tail() -> str:
    return "<Hangup/>"


@router.post("/gather")
async def voice_gather(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    params, channel, allowed = await _authorize(request, session)
    if not allowed:
        return _hangup(NO_VOICE)
    utterance = (params.get("SpeechResult") or "").strip()[:MAX_UTTERANCE]
    if not utterance:
        try:
            retry = int(request.query_params.get("r", "0"))
        except ValueError:
            retry = MAX_EMPTY_RETRIES
        if retry >= MAX_EMPTY_RETRIES:
            return _hangup(GOODBYE)
        return _response(_gather(_action_url(retry + 1), REPROMPT), _hangup_tail())

    tenant_id = channel.tenant.id
    try:
        reply = await _run_turn(session, channel, params, utterance)
        await session.commit()
    except Exception:
        await session.rollback()
        log.error("twilio.voice_turn_failed", tenant_id=str(tenant_id))
        return _hangup(ERROR_SAY)

    transfer = _safe_number(getattr(reply, "transfer_to", None))
    if transfer:
        return _response(_say(reply.text or TRANSFER_SAY), _dial(transfer))
    if getattr(reply, "end_call", False):
        return _hangup(reply.text)
    return _response(_gather(_action_url(), reply.text), _hangup_tail())


async def _run_turn(
    session: AsyncSession, channel: Any, params: dict[str, str], utterance: str
) -> Any:
    from app.voice.bridge import voice_turn

    return await voice_turn(
        session,
        tenant_id=channel.tenant.id,
        call_sid=params.get("CallSid", ""),
        caller_e164=params.get("From", ""),
        utterance=utterance,
    )


from app.voice.admin import admin_router  # noqa: E402

routers = [admin_router]  # descubrimiento: fuera del prefijo de webhooks

__all__ = ["router", "routers"]
