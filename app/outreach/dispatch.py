"""Despacho de campanas: lotes de objetivos, ComplianceGate y envio idempotente."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.leads import Lead
from app.db.models.outreach import Campaign, CampaignTarget, MessageTemplate, OutreachMessage
from app.leads import pipeline
from app.outreach import campaigns
from app.outreach.compliance import MIN_GAP, ComplianceGate, GateDecision, in_send_window
from app.outreach.sender import OutreachSender
from app.outreach.templates import TemplateRenderError, is_sendable

log = get_logger(__name__)

BATCH_SENDS = 10  # por campana y corrida (cron cada minuto + 1 msg/s => muy por debajo de MPS)
SCAN_LIMIT = 200
STOP_REASONS = frozenset({"kill_switch", "campana_no_activa", "plantilla_no_aprobada", "calidad"})
HOLD_REASONS = frozenset({"fuera_de_ventana", "limite_diario", "tope_global_diario"})
SENT_STATUSES = ("sent", "delivered", "read", "replied")

Sleep = Callable[[float], Awaitable[None]]


def idempotency_key(campaign_id: uuid.UUID, lead_id: uuid.UUID, step: int) -> str:
    return f"{campaign_id}:{lead_id}:{step}"


def _bump(campaign: Campaign, key: str) -> None:
    stats = dict(campaign.stats or {})
    bucket = dict(stats.get("skipped", {}))
    bucket[key] = int(bucket.get(key, 0)) + 1
    stats["skipped"] = bucket
    campaign.stats = stats


async def _load_template(session: AsyncSession, template_id: object) -> MessageTemplate | None:
    if not template_id:
        return None
    tid = template_id if isinstance(template_id, uuid.UUID) else uuid.UUID(str(template_id))
    tpl = await session.get(MessageTemplate, tid)
    return tpl if tpl is not None and is_sendable(tpl) else None


async def _pending_targets(
    session: AsyncSession, campaign: Campaign, now: datetime
) -> list[CampaignTarget]:
    stmt = (
        select(CampaignTarget)
        .where(
            CampaignTarget.campaign_id == campaign.id,
            or_(
                CampaignTarget.status == "pending",
                (CampaignTarget.status == "sent") & (CampaignTarget.updated_at <= now - MIN_GAP),
            ),
        )
        .order_by(CampaignTarget.created_at, CampaignTarget.id)
        .limit(SCAN_LIMIT)
    )
    return list((await session.execute(stmt)).scalars())


async def _deliver_step(
    session: AsyncSession,
    campaign: Campaign,
    target: CampaignTarget,
    lead: Lead,
    tpl: MessageTemplate,
    step: int,
    sender: OutreachSender,
    now: datetime,
) -> str:
    """Crea (o reutiliza) el mensaje por ``idempotency_key`` y lo envia. Devuelve el resultado."""
    key = idempotency_key(campaign.id, lead.id, step)
    msg = (
        await session.execute(select(OutreachMessage).where(OutreachMessage.idempotency_key == key))
    ).scalar_one_or_none()
    if msg is not None and msg.status != "queued":
        return "duplicate"
    if msg is None:
        body, variables = await campaigns.render_for_lead(session, tpl, lead)
        msg = OutreachMessage(
            campaign_id=campaign.id,
            lead_id=lead.id,
            channel="whatsapp",
            template_name=tpl.name,
            body=body,
            step=step,
            idempotency_key=key,
            content_variables=variables,
            status="queued",
        )
        session.add(msg)
        await session.flush()
        await session.commit()  # el registro queda antes de llamar al proveedor
    assert lead.phone_e164 is not None and tpl.twilio_content_sid is not None
    result = await sender.send_step(
        session, msg, to_e164=lead.phone_e164, content_sid=tpl.twilio_content_sid
    )
    if result.ok:
        lead.last_contacted_at = now
        await pipeline.add_event(
            session, lead, "outreach_sent", {"campaign_id": str(campaign.id), "step": step}
        )
        await log_event(
            session,
            actor=None,
            actor_type="system",
            action="outreach.send",
            entity_type="outreach_message",
            entity_id=msg.id,
            diff={"campaign_id": str(campaign.id), "step": step, "dry_run": result.dry_run},
        )
        return "sent"
    if result.status == "suppressed":
        msg.status = "skipped"
        return "suppressed"
    return "failed"


async def _handle_decision(
    campaign: Campaign, target: CampaignTarget, decision: GateDecision
) -> str:
    """Devuelve ``stop`` (cortar campana), ``hold`` (cortar por limite), ``next`` o ``skip``."""
    if decision.action == "defer":
        return "hold" if decision.reason in HOLD_REASONS else "next"
    if decision.reason in STOP_REASONS:
        return "stop"
    target.status = "skipped"
    _bump(campaign, decision.reason)
    return "skip"


async def dispatch_campaign(
    session: AsyncSession,
    campaign: Campaign,
    *,
    now: datetime | None = None,
    sender: OutreachSender | None = None,
    pace: float = 1.0,
    sleep: Sleep = asyncio.sleep,
    batch: int = BATCH_SENDS,
) -> dict[str, int]:
    now = now or utcnow()
    sender = sender or OutreachSender()
    gate = ComplianceGate(session)
    out = {"sent": 0, "failed": 0, "skipped": 0}
    if campaign.status != "running" or not in_send_window(now):
        return out
    main_tpl = await _load_template(session, campaign.template_id)
    follow_tpl = await _load_template(
        session, (campaign.audience_filter or {}).get("followup_template_id")
    )
    targets = await _pending_targets(session, campaign, now)
    lead_ids = {t.lead_id for t in targets}
    leads_by_id: dict[uuid.UUID, Lead] = {}
    if lead_ids:
        leads_by_id = {
            lead.id: lead
            for lead in (await session.execute(select(Lead).where(Lead.id.in_(lead_ids)))).scalars()
        }
    for target in targets:
        if out["sent"] + out["failed"] >= batch:
            break
        step = 1 if target.status == "pending" else 2
        tpl = main_tpl if step == 1 else follow_tpl
        if step == 2 and tpl is None:
            target.status = "done"
            continue
        lead = leads_by_id.get(target.lead_id)
        if lead is None:
            target.status = "skipped"
            continue
        decision = await gate.check(lead, campaign, now, template=tpl, step=step)
        if not decision.allowed:
            if decision.reason == "calidad":
                await campaigns.pause(session, campaign.id, None, reason="calidad")
                log.warning("outreach.paused_quality", campaign_id=str(campaign.id))
            verdict = await _handle_decision(campaign, target, decision)
            out["skipped"] += verdict == "skip"
            if verdict in ("stop", "hold"):
                break
            continue
        assert tpl is not None
        try:
            outcome = await _deliver_step(session, campaign, target, lead, tpl, step, sender, now)
        except TemplateRenderError:
            target.status = "skipped"
            _bump(campaign, "datos_incompletos")
            out["skipped"] += 1
            continue
        if outcome == "sent":
            target.status = "sent" if step == 1 and follow_tpl is not None else "done"
            out["sent"] += 1
        elif outcome == "duplicate":
            target.status = "sent" if step == 1 and follow_tpl is not None else "done"
        elif outcome == "suppressed":
            target.status = "skipped"
            out["skipped"] += 1
        else:
            target.status = "failed"
            out["failed"] += 1
        await session.commit()
        if pace > 0:
            await sleep(pace)
    await _maybe_complete(session, campaign)
    stats = dict(campaign.stats or {})
    stats["last_dispatch_at"] = now.isoformat()
    campaign.stats = stats
    await session.commit()
    return out


async def _maybe_complete(session: AsyncSession, campaign: Campaign) -> None:
    if campaign.status != "running":
        return
    left = (
        await session.execute(
            select(CampaignTarget.id)
            .where(
                CampaignTarget.campaign_id == campaign.id,
                CampaignTarget.status.in_(("pending", "sent")),
            )
            .limit(1)
        )
    ).first()
    if left is None:
        campaign.status = "completed"


async def dispatch_all(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    sender: OutreachSender | None = None,
    pace: float = 1.0,
    sleep: Sleep = asyncio.sleep,
) -> dict[str, Any]:
    """Recorre las campanas en curso. Con el kill switch apagado no hace nada."""
    if not get_settings().outreach_enabled:
        return {"enabled": False, "campaigns": 0, "sent": 0}
    ids = list(
        (await session.execute(select(Campaign.id).where(Campaign.status == "running"))).scalars()
    )
    total = 0
    for cid in ids:
        campaign = await session.get(Campaign, cid)
        if campaign is None:
            continue
        try:
            res = await dispatch_campaign(
                session, campaign, now=now, sender=sender, pace=pace, sleep=sleep
            )
        except Exception:
            # Una campana rota no debe frenar a las demas (y no se traga en silencio).
            await session.rollback()
            log.error("outreach.campaign_dispatch_failed", campaign_id=str(cid), exc_info=True)
            continue
        total += res["sent"]
    return {"enabled": True, "campaigns": len(ids), "sent": total}
