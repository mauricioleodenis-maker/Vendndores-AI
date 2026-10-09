"""Fixtures de outreach: kill switch encendido, token de Twilio y fabricas de datos."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import phone_hash
from app.db.models.leads import Lead
from app.db.models.outreach import Campaign, CampaignTarget, MessageTemplate
from app.outreach.templates import seed_templates

PLATFORM_TOKEN = "platform-token-xyz"
CONTENT_SID = "HX" + "a" * 32
# Martes 13-oct-2026 10:00 en Bogota (el lunes 12 es festivo).
TUESDAY_10AM = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
SUNDAY_10AM = datetime(2026, 10, 11, 15, 0, tzinfo=UTC)
HOLIDAY_10AM = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _outreach_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAI_OUTREACH_ENABLED", "true")
    monkeypatch.setenv("VAI_TWILIO_AUTH_TOKEN", PLATFORM_TOKEN)
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def templates(session: AsyncSession) -> dict[str, MessageTemplate]:
    await seed_templates(session)
    await session.commit()
    from sqlalchemy import select

    rows = (await session.execute(select(MessageTemplate))).scalars().all()
    return {t.name: t for t in rows}


async def approve(session: AsyncSession, tpl: MessageTemplate, sid: str = CONTENT_SID) -> None:
    tpl.twilio_content_sid = sid
    tpl.approval_status = "approved"
    tpl.approved = True
    await session.commit()


@pytest_asyncio.fixture
async def pitch(session: AsyncSession, templates: dict[str, MessageTemplate]) -> MessageTemplate:
    tpl = templates["lead_contacto_inicial"]
    await approve(session, tpl)
    return tpl


@pytest_asyncio.fixture
async def followup(session: AsyncSession, templates: dict[str, MessageTemplate]) -> MessageTemplate:
    tpl = templates["lead_seguimiento"]
    await approve(session, tpl, "HX" + "b" * 32)
    return tpl


@pytest_asyncio.fixture
async def make_lead(session: AsyncSession) -> Callable[..., Any]:
    counter = itertools.count(1)

    async def _make(**kw: Any) -> Lead:
        n = next(counter)
        phone = kw.pop("phone_e164", f"+57300123{n:04d}")
        lead = Lead(
            name=kw.pop("name", f"Clinica {n}"),
            name_key=f"clinica{n}",
            niche=kw.pop("niche", "dentista"),
            city=kw.pop("city", "Cali"),
            phone_e164=phone,
            phone_hash=phone_hash(phone) if phone else None,
            phone_type=kw.pop("phone_type", "mobile"),
            score=kw.pop("score", 60),
            **kw,
        )
        session.add(lead)
        await session.commit()
        return lead

    return _make


@pytest_asyncio.fixture
async def make_campaign(session: AsyncSession) -> Callable[..., Any]:
    async def _make(
        tpl: MessageTemplate, leads: list[Lead], *, status: str = "running", **kw: Any
    ) -> Campaign:
        audience = kw.pop("audience_filter", {})
        c = Campaign(
            name=kw.pop("name", "Campana de prueba"),
            template_id=tpl.id,
            daily_limit=kw.pop("daily_limit", 20),
            audience_filter=audience,
            status=status,
            stats=kw.pop("stats", {"started_at": TUESDAY_10AM.isoformat()}),
            **kw,
        )
        session.add(c)
        await session.flush()
        for lead in leads:
            session.add(CampaignTarget(campaign_id=c.id, lead_id=lead.id, status="pending"))
        await session.commit()
        return c

    return _make
