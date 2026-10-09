"""Webhooks de Twilio para outreach: estado de entrega y respuestas (firma obligatoria)."""

from __future__ import annotations

from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.channels import service as channels_service
from app.channels.sender import from_whatsapp, resolve_auth_token, validate_twilio_signature
from app.core.clock import utcnow
from app.core.crypto import phone_hash
from app.core.deps import get_session
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models.leads import Lead
from app.db.models.outreach import CampaignTarget, OutreachMessage
from app.leads import pipeline
from app.privacy.service import apply_optout, is_optout_message

log = get_logger(__name__)

router = APIRouter(prefix="/webhooks/twilio", tags=["outreach-webhooks"], include_in_schema=False)

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'
OPTOUT_REPLY = (
    "Listo. Dejamos de enviarte mensajes de Vendedores AI. "
    "Si cambias de opinion, escribenos en cualquier momento."
)
MAX_PARAMS = 100
_RANK = {"queued": 0, "sent": 1, "delivered": 2, "read": 3, "replied": 4}
_TWILIO_TO_INTERNAL = {
    "queued": "queued",
    "accepted": "queued",
    "sending": "queued",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "undelivered": "failed",
}
OPTOUT_ERROR_CODES = frozenset({"63032", "21610"})
_ACTIVE_STAGE_FOR_REPLY = ("nuevo", "prueba_secreta")


def _twiml(body: str = EMPTY_TWIML) -> Response:
    return Response(content=body, media_type="application/xml")


MAX_BODY_BYTES = 64 * 1024


async def _params(request: Request) -> dict[str, str]:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise AppError("payload_grande", "Cuerpo demasiado grande", 413)
    form = await request.form()
    params: dict[str, str] = {}
    for key, value in form.multi_items():
        if isinstance(value, str) and len(params) < MAX_PARAMS:
            params[key] = value
    return params


async def _verify(request: Request, session: AsyncSession, params: dict[str, str]) -> None:
    token = await resolve_auth_token(session, None)
    url = channels_service.public_url(request.url.path, request.url.query)
    if not validate_twilio_signature(
        url, params, request.headers.get("X-Twilio-Signature", ""), token
    ):
        log.warning("outreach.bad_signature")
        raise AppError("firma_invalida", "Firma invalida", 403)


# --------------------------------------------------------------------------- estado
async def apply_status(session: AsyncSession, params: dict[str, str]) -> bool:
    """Actualiza ``outreach_messages`` por ``MessageSid`` (monotono). ``True`` si aplico."""
    sid = params.get("MessageSid") or params.get("SmsSid") or ""
    raw = (params.get("MessageStatus") or params.get("SmsStatus") or "").lower()
    new = _TWILIO_TO_INTERNAL.get(raw)
    if not sid or new is None:
        return False
    msg = (
        await session.execute(select(OutreachMessage).where(OutreachMessage.provider_sid == sid))
    ).scalar_one_or_none()
    if msg is None:
        return False
    if not await channels_service.claim_event(
        session,
        kind="status",
        sid=sid,
        status_value=f"outreach:{raw}",
        tenant_id=None,
        params=params,
    ):
        return False
    code = params.get("ErrorCode") or None
    if new == "failed":
        if msg.status in ("queued", "sent", "delivered", "read"):
            msg.status = "failed"
            msg.delivery_error_code = code
            msg.error = f"twilio:{code or raw}"[:300]
    elif _RANK.get(new, 0) > _RANK.get(msg.status, -1):
        msg.status = new
    if code in OPTOUT_ERROR_CODES:
        lead = await session.get(Lead, msg.lead_id)
        if lead is not None and lead.phone_e164:
            await _suppress_lead(session, lead, evidence=f"twilio_error_{code}")
    await session.flush()
    return True


async def _status_fallback(session: AsyncSession, params: dict[str, str]) -> None:
    await apply_status(session, params)


if _status_fallback not in channels_service.STATUS_FALLBACKS:
    # El StatusCallback global apunta a /webhooks/twilio/status: ahi tambien llega el outreach.
    channels_service.STATUS_FALLBACKS.append(_status_fallback)


@router.post("/outreach-status")
async def outreach_status(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    params = await _params(request)
    await _verify(request, session, params)
    await apply_status(session, params)
    return _twiml()


# --------------------------------------------------------------------------- respuestas
async def _suppress_lead(session: AsyncSession, lead: Lead, *, evidence: str) -> None:
    """Supresion global + ``no_contactar`` + cancela cola pendiente del lead."""
    assert lead.phone_e164 is not None
    await apply_optout(session, phone_e164=lead.phone_e164, evidence=evidence)
    if lead.disposition != "no_contactar":
        lead.disposition = "no_contactar"
        await pipeline.add_event(session, lead, "opt_out", {"evidence": evidence[:60]})
    await session.execute(
        update(OutreachMessage)
        .where(OutreachMessage.lead_id == lead.id, OutreachMessage.status == "queued")
        .values(status="skipped", error="opt_out")
    )
    await session.execute(
        update(CampaignTarget)
        .where(CampaignTarget.lead_id == lead.id, CampaignTarget.status.in_(("pending", "sent")))
        .values(status="cancelled")
    )
    await log_event(
        session,
        actor=None,
        actor_type="webhook",
        action="outreach.optout",
        entity_type="lead",
        entity_id=lead.id,
    )


async def handle_inbound(session: AsyncSession, params: dict[str, str]) -> str | None:
    """Procesa una respuesta. Devuelve texto para responder por TwiML (solo en opt-out)."""
    sender = from_whatsapp(params.get("From", ""))
    body = params.get("Body", "")
    sid = params.get("MessageSid", "")
    if not sender or not sid:
        return None
    if not await channels_service.claim_event(
        session, kind="inbound", sid=sid, status_value="outreach", tenant_id=None, params=params
    ):
        return None
    optout = is_optout_message(body)
    leads = list(
        (
            await session.execute(
                select(Lead).where(Lead.phone_hash == phone_hash(sender)).order_by(Lead.created_at)
            )
        ).scalars()
    )
    if optout:
        # Se suprime el numero aunque no corresponda a un lead (opt-out siempre prevalece).
        await apply_optout(session, phone_e164=sender, evidence=body[:60])
        for lead in leads:
            lead.phone_e164 = lead.phone_e164 or sender
            await _suppress_lead(session, lead, evidence=body[:60])
        return OPTOUT_REPLY
    lead = next((lead for lead in leads if lead.disposition != "perdido"), None)
    if lead is None:
        return None
    last = (
        await session.execute(
            select(OutreachMessage)
            .where(
                OutreachMessage.lead_id == lead.id,
                OutreachMessage.status.in_(("sent", "delivered", "read")),
            )
            .order_by(OutreachMessage.sent_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is None:
        return None  # no es respuesta a un outreach nuestro
    now = utcnow()
    last.status = "replied"
    last.replied_at = now
    await session.execute(
        update(CampaignTarget)
        .where(CampaignTarget.lead_id == lead.id, CampaignTarget.status == "sent")
        .values(status="done")
    )
    await pipeline.add_event(
        session, lead, "reply_received", {"campaign_id": str(last.campaign_id), "step": last.step}
    )
    if lead.stage in _ACTIVE_STAGE_FOR_REPLY and lead.disposition == "activo":
        await pipeline.transition_stage(
            session, lead, "contactado", actor=None, note="Respondio al outreach", system=True
        )
    log.info("outreach.reply", lead_id=str(lead.id))  # aviso al operador: handoff humano
    return None


@router.post("/outreach-inbound")
async def outreach_inbound(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    params = await _params(request)
    await _verify(request, session, params)
    reply = await handle_inbound(session, params)
    if reply:
        head = '<?xml version="1.0" encoding="UTF-8"?>'
        return _twiml(f"{head}<Response><Message>{escape(reply)}</Message></Response>")
    return _twiml()
