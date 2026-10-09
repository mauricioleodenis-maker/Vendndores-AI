"""Webhooks de Twilio (WhatsApp, status y voz). Exentos de CSRF; autenticados por firma."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels import service
from app.channels.sender import validate_twilio_signature
from app.core.config import get_settings
from app.core.deps import get_session
from app.core.errors import AppError
from app.core.jobs import enqueue
from app.core.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/webhooks/twilio", tags=["twilio"], include_in_schema=False)

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'
VOICE_TWIML = (
    '<?xml version="1.0" encoding="UTF-8"?><Response>'
    '<Say language="es-MX">Hola. Por ahora atendemos solo por WhatsApp. '
    "Escríbenos y con gusto te ayudamos. Hasta pronto.</Say><Hangup/></Response>"
)
MAX_PARAMS = 100


def twiml(body: str = EMPTY_TWIML, status: int = 200) -> Response:
    return Response(content=body, media_type="application/xml", status_code=status)


async def _form_params(request: Request) -> dict[str, str]:
    form = await request.form()
    params: dict[str, str] = {}
    for key, value in form.multi_items():
        if not isinstance(value, str):
            continue
        if len(params) >= MAX_PARAMS and key not in params:
            log.warning("twilio.params_truncated")
            continue
        params[key] = value
    return params


def _forbidden() -> AppError:
    return AppError("firma_invalida", "Firma inválida", 403)


def _signature_ok(request: Request, params: dict[str, str], token: str) -> bool:
    url = service.public_url(request.url.path, request.url.query)
    return validate_twilio_signature(
        url, params, request.headers.get("X-Twilio-Signature", ""), token
    )


@router.post("/whatsapp")
async def whatsapp_inbound(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    params = await _form_params(request)
    channel = await service.resolve_channel(session, params.get("To", ""), "whatsapp")
    if channel is None:
        log.warning("twilio.unknown_destination")
        raise _forbidden()
    token = await service.auth_token_for(session, channel.tenant.id)
    if not _signature_ok(request, params, token):
        log.warning("twilio.bad_signature", tenant_id=str(channel.tenant.id))
        raise _forbidden()
    result = await service.persist_inbound(session, channel, params)
    if result.missing_sid:
        raise AppError("sid_requerido", "Falta MessageSid", 400)
    if result.duplicate or result.message_id is None:
        return twiml()
    await session.commit()  # el job debe ver el mensaje ya persistido
    try:
        await enqueue(
            "channels.process_inbound",
            str(result.tenant_id),
            str(result.conversation_id),
            str(result.message_id),
        )
    except Exception:
        log.error("twilio.enqueue_failed", tenant_id=str(result.tenant_id))
        sid = params.get("MessageSid") or params.get("SmsSid") or ""
        await service.release_inbound(session, channel.tenant.id, sid)
        raise AppError("cola_no_disponible", "Cola no disponible, reintenta", 503) from None
    return twiml()


@router.post("/status")
async def message_status(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    params = await _form_params(request)
    channel = await service.resolve_channel(session, params.get("From", ""), "whatsapp")
    if channel is not None:
        token = await service.auth_token_for(session, channel.tenant.id)
        if not _signature_ok(request, params, token):
            raise _forbidden()
        res = await service.apply_status(session, channel.tenant.id, params)
        if res.retry:
            return twiml(status=503)
        return twiml()
    # Mensajes de la cuenta de la agencia (outreach): token de la plataforma.
    platform = get_settings().twilio_auth_token.get_secret_value()
    if not _signature_ok(request, params, platform):
        raise _forbidden()
    for handler in list(service.STATUS_FALLBACKS):
        await handler(session, params)
    return twiml()


@router.post("/voice")
async def voice_inbound(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """Stub de voz (fase posterior): firma valida y mensaje de cortesia."""
    params = await _form_params(request)
    channel = await service.resolve_channel(session, params.get("To", ""), None)
    if channel is None:
        raise _forbidden()
    token = await service.auth_token_for(session, channel.tenant.id)
    if not _signature_ok(request, params, token):
        raise _forbidden()
    return twiml(VOICE_TWIML)


@router.post("/voice/status")
async def voice_status(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    params = await _form_params(request)
    channel = await service.resolve_channel(session, params.get("To", ""), None)
    if channel is None:
        raise _forbidden()
    token = await service.auth_token_for(session, channel.tenant.id)
    if not _signature_ok(request, params, token):
        raise _forbidden()
    log.info("twilio.voice_status", call_status=params.get("CallStatus", ""))
    return twiml()


__all__ = ["router"]
