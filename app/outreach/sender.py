"""Envio de un paso de outreach por la cuenta de la agencia (Twilio Content API)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.sender import SendResult, send_whatsapp_template
from app.core.clock import utcnow
from app.db.models.outreach import OutreachMessage


class OutreachSender:
    async def send_step(
        self, session: AsyncSession, msg: OutreachMessage, *, to_e164: str, content_sid: str
    ) -> SendResult:
        """Envia ``msg`` y actualiza su estado. Respeta supresion y dry-run (en el sender)."""
        result = await send_whatsapp_template(
            session,
            tenant_id=None,
            to_e164=to_e164,
            content_sid=content_sid,
            variables={str(k): str(v) for k, v in (msg.content_variables or {}).items()},
        )
        if result.ok:
            msg.status = "sent"
            msg.provider_sid = result.sid
            msg.sent_at = utcnow()
            msg.error = None
        else:
            msg.status = "failed"
            msg.error = (result.error or result.status or "error")[:300]
            msg.delivery_error_code = (
                result.error if result.error and result.error.isdigit() else None
            )
        await session.flush()
        return result
