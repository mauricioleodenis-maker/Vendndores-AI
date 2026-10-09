from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models.audit import AuditLog
from app.db.models.leads import Lead, LeadEvent
from app.leads import pipeline


async def make_lead(session: AsyncSession, **kw: Any) -> Lead:
    lead = Lead(name=kw.pop("name", "Clínica Sonrisa"), niche=kw.pop("niche", "dentista"), **kw)
    session.add(lead)
    await session.flush()
    return lead


def test_allowed_table() -> None:
    assert pipeline.can_transition("nuevo", "prueba_secreta")
    assert pipeline.can_transition("nuevo", "contactado")
    assert pipeline.can_transition("demo", "contactado")
    assert not pipeline.can_transition("nuevo", "demo")
    assert not pipeline.can_transition("nuevo", "cerrado")
    assert not pipeline.can_transition("zzz", "nuevo")


async def test_transition_creates_event_and_audit(session: AsyncSession, owner_user: Any) -> None:
    lead = await make_lead(session)
    await pipeline.transition_stage(session, lead, "contactado", actor=owner_user, note="x")
    assert lead.stage == "contactado"
    ev = (await session.execute(select(LeadEvent))).scalar_one()
    assert ev.kind == "stage_changed" and ev.data["to"] == "contactado"
    audit = (await session.execute(select(AuditLog))).scalars().all()
    assert any(a.action == "lead.stage_change" for a in audit)
    assert len(await pipeline.list_events(session, lead.id)) == 1


async def test_invalid_and_noop_transitions(session: AsyncSession, owner_user: Any) -> None:
    lead = await make_lead(session)
    with pytest.raises(pipeline.InvalidTransitionError):
        await pipeline.transition_stage(session, lead.id, "demo", actor=owner_user)
    same = await pipeline.transition_stage(session, lead, "nuevo", actor=owner_user)
    assert same.stage == "nuevo"
    with pytest.raises(AppError):
        await pipeline.transition_stage(session, lead, "inventada", actor=owner_user)
    with pytest.raises(NotFoundError):
        await pipeline.transition_stage(session, uuid.uuid4(), "contactado", actor=owner_user)


async def test_cerrado_requires_conversion(session: AsyncSession, owner_user: Any) -> None:
    lead = await make_lead(session, stage="demo")
    with pytest.raises(ConflictError):
        await pipeline.transition_stage(session, lead, "cerrado", actor=owner_user)


async def test_inactive_lead_cannot_move(session: AsyncSession, owner_user: Any) -> None:
    lead = await make_lead(session, disposition="perdido")
    with pytest.raises(ConflictError):
        await pipeline.transition_stage(session, lead, "contactado", actor=owner_user)


async def test_disposition_rules(session: AsyncSession, owner_user: Any, make_user: Any) -> None:
    operator = await make_user("operator")
    lead = await make_lead(session)
    await pipeline.set_disposition(session, lead, "no_contactar", actor=operator, reason="pidió")
    assert lead.disposition == "no_contactar"
    assert (await pipeline.set_disposition(session, lead, "no_contactar", actor=operator)) is lead
    with pytest.raises(ConflictError):
        await pipeline.set_disposition(session, lead.id, "activo", actor=operator)
    await pipeline.set_disposition(session, lead.id, "activo", actor=owner_user)
    assert lead.disposition == "activo"
    with pytest.raises(AppError):
        await pipeline.set_disposition(session, lead, "otro", actor=owner_user)
