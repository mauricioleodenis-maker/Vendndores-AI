"""Servicio de campanas: crear, previsualizar, iniciar (congela audiencia), pausar y cancelar."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.crypto import phone_hash
from app.core.errors import AppError, ConflictError, ForbiddenError, NotFoundError
from app.db.models.leads import Lead
from app.db.models.outreach import Campaign, CampaignTarget, MessageTemplate, OutreachMessage
from app.db.models.privacy import SuppressionEntry
from app.db.models.users import User
from app.leads import secret_shop
from app.outreach.compliance import MAX_DAILY, RAMP_START
from app.outreach.templates import (
    TemplateRenderError,
    build_variables,
    is_sendable,
    render_body,
)

PREVIEW_SAMPLES = 5
MAX_AUDIENCE = 5000
SEND_WINDOW = {"days": "L-S", "start": "08:00", "end": "19:00", "tz": "America/Bogota"}
OPEN_STATUSES = ("draft", "ready", "running", "paused")


class AudienceFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    niche: Annotated[str | None, Field(max_length=30)] = None
    city: Annotated[str | None, Field(max_length=100)] = None
    min_score: Annotated[int, Field(ge=0, le=100)] = 0
    stage: Annotated[str | None, Field(max_length=20)] = None
    tag: Annotated[str | None, Field(max_length=50)] = None


class CampaignIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Annotated[str, Field(min_length=3, max_length=200)]
    template_id: uuid.UUID
    audience: AudienceFilter = Field(default_factory=AudienceFilter)
    daily_limit: Annotated[int, Field(ge=1, le=MAX_DAILY)] = RAMP_START
    followup_template_id: uuid.UUID | None = None


def require_admin(actor: User) -> None:
    if actor.role not in ("owner", "admin"):
        raise ForbiddenError("Solo owner o admin pueden realizar esta accion")


async def get_campaign(session: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError("Campaña no encontrada")
    return campaign


async def get_template(session: AsyncSession, template_id: uuid.UUID) -> MessageTemplate:
    tpl = await session.get(MessageTemplate, template_id)
    if tpl is None or tpl.scope != "outreach":
        raise NotFoundError("Plantilla no encontrada")
    return tpl


async def create_campaign(session: AsyncSession, actor: User, data: CampaignIn) -> Campaign:
    tpl = await get_template(session, data.template_id)
    if tpl.kind != "pitch":
        raise AppError("plantilla_invalida", "La plantilla inicial debe ser de tipo pitch", 422)
    audience = data.audience.model_dump(exclude_none=True)
    if data.followup_template_id:
        follow = await get_template(session, data.followup_template_id)
        if follow.kind != "seguimiento":
            raise AppError("plantilla_invalida", "El seguimiento debe ser de tipo seguimiento", 422)
        audience["followup_template_id"] = str(follow.id)
    campaign = Campaign(
        name=data.name,
        niche=data.audience.niche,
        template_id=tpl.id,
        daily_limit=min(data.daily_limit, MAX_DAILY),
        send_window=dict(SEND_WINDOW),
        audience_filter=audience,
        stage_target=None,
        status="draft",
        stats={},
        created_by=actor.id,
    )
    session.add(campaign)
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="outreach.campaign_create",
        entity_type="campaign",
        entity_id=campaign.id,
    )
    return campaign


# --------------------------------------------------------------------------- audiencia
def _audience_stmt(audience: dict[str, Any]) -> Any:
    stmt = select(Lead).where(
        Lead.disposition == "activo",
        Lead.phone_type == "mobile",
        Lead.phone_e164.is_not(None),
        Lead.score >= int(audience.get("min_score", 0)),
    )
    if audience.get("niche"):
        stmt = stmt.where(Lead.niche == audience["niche"])
    if audience.get("city"):
        stmt = stmt.where(func.lower(Lead.city) == str(audience["city"]).lower())
    if audience.get("stage"):
        stmt = stmt.where(Lead.stage == audience["stage"])
    return stmt.order_by(Lead.score.desc(), Lead.id).limit(MAX_AUDIENCE)


async def select_audience(
    session: AsyncSession, audience: dict[str, Any]
) -> tuple[list[Lead], int]:
    """Leads elegibles (activos, moviles, no suprimidos) y cuantos se excluyeron por supresion."""
    leads = list((await session.execute(_audience_stmt(audience))).scalars())
    tag = audience.get("tag")
    if tag:
        leads = [lead for lead in leads if tag in (lead.tags or [])]
    suppressed = await _suppressed_hashes(
        session, [phone_hash(lead.phone_e164) for lead in leads if lead.phone_e164]
    )
    eligible: list[Lead] = []
    excluded = 0
    for lead in leads:
        if lead.phone_e164 and phone_hash(lead.phone_e164) in suppressed:
            excluded += 1
            continue
        eligible.append(lead)
    return eligible, excluded


async def _suppressed_hashes(session: AsyncSession, hashes: list[str]) -> set[str]:
    """Una consulta por lote (en vez de una por lead) contra la lista de supresion."""
    found: set[str] = set()
    for i in range(0, len(hashes), 500):
        chunk = hashes[i : i + 500]
        rows = await session.execute(
            select(SuppressionEntry.phone_hash).where(SuppressionEntry.phone_hash.in_(chunk))
        )
        found.update(rows.scalars())
    return found


async def render_for_lead(
    session: AsyncSession, template: MessageTemplate, lead: Lead
) -> tuple[str, dict[str, str]]:
    evidence = await secret_shop.pitch_evidence(session, lead.id)
    variables = build_variables(template, lead, evidence)
    return render_body(template.body, variables), variables


async def preview(session: AsyncSession, campaign_id: uuid.UUID) -> dict[str, Any]:
    campaign = await get_campaign(session, campaign_id)
    tpl = await get_template(session, campaign.template_id) if campaign.template_id else None
    if tpl is None:
        raise ConflictError("La campaña no tiene plantilla")
    leads, excluded = await select_audience(session, campaign.audience_filter or {})
    samples: list[dict[str, str]] = []
    unrenderable = 0
    for lead in leads:
        try:
            body, _ = await render_for_lead(session, tpl, lead)
        except TemplateRenderError:
            unrenderable += 1
            continue
        if len(samples) < PREVIEW_SAMPLES:
            samples.append({"lead_id": str(lead.id), "business": lead.name, "body": body})
    return {
        "audience_count": len(leads) - unrenderable,
        "excluded_suppressed": excluded,
        "excluded_missing_data": unrenderable,
        "template_approved": is_sendable(tpl),
        "samples": samples,
    }


# --------------------------------------------------------------------------- estados
async def start(
    session: AsyncSession, campaign_id: uuid.UUID, actor: User, *, confirm: bool
) -> Campaign:
    require_admin(actor)
    if not confirm:
        raise AppError("confirmacion_requerida", "Confirma explicitamente el inicio", 422)
    if not get_settings().outreach_enabled:
        raise ConflictError("El envio de outreach esta desactivado (VAI_OUTREACH_ENABLED=false)")
    # Bloqueo de fila: dos "iniciar" simultaneos no duplican objetivos.
    campaign = (
        await session.execute(select(Campaign).where(Campaign.id == campaign_id).with_for_update())
    ).scalar_one_or_none()
    if campaign is None:
        raise NotFoundError("Campaña no encontrada")
    if campaign.status not in ("draft", "ready"):
        raise ConflictError("La campaña ya fue iniciada o finalizo")
    tpl = await get_template(session, campaign.template_id) if campaign.template_id else None
    if tpl is None or not is_sendable(tpl):
        raise ConflictError("La plantilla no esta aprobada por Meta")
    leads, _ = await select_audience(session, campaign.audience_filter or {})
    count = 0
    for lead in leads:
        try:
            await render_for_lead(session, tpl, lead)
        except TemplateRenderError:
            continue
        session.add(CampaignTarget(campaign_id=campaign.id, lead_id=lead.id, status="pending"))
        count += 1
    if count == 0:
        raise ConflictError("La audiencia esta vacia")
    now = utcnow()
    campaign.status = "running"
    campaign.paused_reason = None
    campaign.stats = {**(campaign.stats or {}), "started_at": now.isoformat(), "targets": count}
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="outreach.campaign_start",
        entity_type="campaign",
        entity_id=campaign.id,
        diff={"targets": count},
    )
    return campaign


async def pause(
    session: AsyncSession, campaign_id: uuid.UUID, actor: User | None, *, reason: str = "manual"
) -> Campaign:
    if actor is not None:
        require_admin(actor)
    campaign = await get_campaign(session, campaign_id)
    if campaign.status != "running":
        raise ConflictError("Solo se puede pausar una campaña en curso")
    campaign.status = "paused"
    campaign.paused_reason = reason[:300]
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="outreach.campaign_pause",
        entity_type="campaign",
        entity_id=campaign.id,
        diff={"reason": reason},
        actor_type=None if actor else "system",
    )
    return campaign


async def resume(session: AsyncSession, campaign_id: uuid.UUID, actor: User) -> Campaign:
    require_admin(actor)
    campaign = await get_campaign(session, campaign_id)
    if campaign.status != "paused":
        raise ConflictError("Solo se puede reanudar una campaña pausada")
    if campaign.paused_reason == "calidad" and actor.role != "owner":
        raise ForbiddenError("Solo el owner puede reanudar una pausa por calidad")
    if not get_settings().outreach_enabled:
        raise ConflictError("El envio de outreach esta desactivado (VAI_OUTREACH_ENABLED=false)")
    campaign.status = "running"
    campaign.paused_reason = None
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="outreach.campaign_resume",
        entity_type="campaign",
        entity_id=campaign.id,
    )
    return campaign


async def cancel(session: AsyncSession, campaign_id: uuid.UUID, actor: User) -> Campaign:
    require_admin(actor)
    campaign = await get_campaign(session, campaign_id)
    if campaign.status not in OPEN_STATUSES:
        raise ConflictError("La campaña ya finalizo")
    campaign.status = "cancelled"
    await session.execute(
        update(CampaignTarget)
        .where(
            CampaignTarget.campaign_id == campaign.id,
            CampaignTarget.status.in_(("pending", "sent")),
        )
        .values(status="cancelled")
    )
    await session.execute(
        update(OutreachMessage)
        .where(OutreachMessage.campaign_id == campaign.id, OutreachMessage.status == "queued")
        .values(status="skipped", error="campana_cancelada")
    )
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="outreach.campaign_cancel",
        entity_type="campaign",
        entity_id=campaign.id,
    )
    return campaign


# --------------------------------------------------------------------------- lecturas
async def list_campaigns(session: AsyncSession, *, limit: int = 100) -> list[Campaign]:
    stmt = select(Campaign).order_by(Campaign.created_at.desc()).limit(min(limit, 200))
    return list((await session.execute(stmt)).scalars())


async def target_counts(session: AsyncSession, campaign_id: uuid.UUID) -> dict[str, int]:
    rows = (
        await session.execute(
            select(CampaignTarget.status, func.count())
            .where(CampaignTarget.campaign_id == campaign_id)
            .group_by(CampaignTarget.status)
        )
    ).all()
    return {status: int(n) for status, n in rows}


async def message_counts(session: AsyncSession, campaign_id: uuid.UUID) -> dict[str, int]:
    rows = (
        await session.execute(
            select(OutreachMessage.status, func.count())
            .where(OutreachMessage.campaign_id == campaign_id)
            .group_by(OutreachMessage.status)
        )
    ).all()
    return {status: int(n) for status, n in rows}


async def list_messages(
    session: AsyncSession, campaign_id: uuid.UUID, *, limit: int = 100
) -> list[OutreachMessage]:
    stmt = (
        select(OutreachMessage)
        .where(OutreachMessage.campaign_id == campaign_id)
        .order_by(OutreachMessage.created_at.desc())
        .limit(min(limit, 500))
    )
    return list((await session.execute(stmt)).scalars())
