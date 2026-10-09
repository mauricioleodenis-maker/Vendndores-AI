from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.crypto import phone_hash
from app.core.errors import AppError, ConflictError, ForbiddenError
from app.db.models.outreach import CampaignTarget, OutreachMessage
from app.db.models.privacy import SuppressionEntry
from app.outreach import campaigns
from app.outreach.campaigns import AudienceFilter, CampaignIn


async def _create(session, user, tpl, **aud):  # type: ignore[no-untyped-def]
    return await campaigns.create_campaign(
        session,
        user,
        CampaignIn(name="Dentistas Cali", template_id=tpl.id, audience=AudienceFilter(**aud)),
    )


async def test_create_validates_template(session, owner_user, templates) -> None:  # type: ignore[no-untyped-def]
    c = await _create(session, owner_user, templates["lead_contacto_inicial"], niche="dentista")
    assert c.status == "draft" and c.niche == "dentista" and c.send_window["start"] == "08:00"
    with pytest.raises(AppError):
        await _create(session, owner_user, templates["optout_confirmacion"])


async def test_daily_limit_hard_cap() -> None:
    with pytest.raises(ValueError):
        CampaignIn(name="abc", template_id="00000000-0000-0000-0000-000000000000", daily_limit=500)  # type: ignore[arg-type]


async def test_audience_filters_and_preview(session, owner_user, pitch, make_lead) -> None:  # type: ignore[no-untyped-def]
    a = await make_lead(name="Sonrisa", niche="dentista", city="Cali", score=80)
    await make_lead(niche="taller")
    await make_lead(city="Bogota")
    await make_lead(score=10)
    await make_lead(phone_type="landline")
    await make_lead(disposition="no_contactar")
    sup = await make_lead()
    session.add(
        SuppressionEntry(phone_hash=phone_hash(sup.phone_e164), scope="global", tenant_key="")
    )
    await session.commit()
    c = await _create(session, owner_user, pitch, niche="dentista", city="cali", min_score=50)
    pv = await campaigns.preview(session, c.id)
    assert pv["audience_count"] == 1 and pv["samples"][0]["business"] == a.name
    assert pv["template_approved"] is True
    c2 = await _create(session, owner_user, pitch, niche="dentista")
    pv2 = await campaigns.preview(session, c2.id)
    assert pv2["excluded_suppressed"] == 1 and len(pv2["samples"]) <= 5


async def test_start_requires_admin_confirm_and_approved(
    session, owner_user, make_user, templates, make_lead
) -> None:  # type: ignore[no-untyped-def]
    await make_lead()
    operator = await make_user("operator")
    c = await _create(session, owner_user, templates["lead_contacto_inicial"])
    with pytest.raises(ForbiddenError):
        await campaigns.start(session, c.id, operator, confirm=True)
    with pytest.raises(AppError):
        await campaigns.start(session, c.id, owner_user, confirm=False)
    with pytest.raises(ConflictError, match="aprobada"):
        await campaigns.start(session, c.id, owner_user, confirm=True)


async def test_start_freezes_targets_and_lifecycle(session, owner_user, pitch, make_lead) -> None:  # type: ignore[no-untyped-def]
    for _ in range(3):
        await make_lead()
    c = await _create(session, owner_user, pitch)
    await campaigns.start(session, c.id, owner_user, confirm=True)
    assert c.status == "running" and c.stats["targets"] == 3 and "started_at" in c.stats
    # un lead nuevo posterior no entra: la audiencia esta congelada
    await make_lead()
    targets = (await session.execute(select(CampaignTarget))).scalars().all()
    assert len(targets) == 3
    with pytest.raises(ConflictError):
        await campaigns.start(session, c.id, owner_user, confirm=True)
    await campaigns.pause(session, c.id, owner_user)
    assert c.status == "paused"
    await campaigns.resume(session, c.id, owner_user)
    assert c.status == "running"
    session.add(
        OutreachMessage(
            campaign_id=c.id, lead_id=targets[0].lead_id, status="queued", idempotency_key="z"
        )
    )
    await session.flush()
    await campaigns.cancel(session, c.id, owner_user)
    assert c.status == "cancelled"
    assert {t.status for t in (await session.execute(select(CampaignTarget))).scalars()} == {
        "cancelled"
    }
    assert (await session.execute(select(OutreachMessage))).scalar_one().status == "skipped"
    with pytest.raises(ConflictError):
        await campaigns.cancel(session, c.id, owner_user)


async def test_start_blocked_by_kill_switch_and_empty(
    session, owner_user, pitch, make_lead, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    c = await _create(session, owner_user, pitch)
    with pytest.raises(ConflictError, match="vac"):
        await campaigns.start(session, c.id, owner_user, confirm=True)
    await make_lead()
    monkeypatch.setenv("VAI_OUTREACH_ENABLED", "false")
    get_settings.cache_clear()
    with pytest.raises(ConflictError, match="desactivado"):
        await campaigns.start(session, c.id, owner_user, confirm=True)


async def test_quality_pause_resume_owner_only(
    session, owner_user, make_user, pitch, make_lead
) -> None:  # type: ignore[no-untyped-def]
    await make_lead()
    admin = await make_user("admin")
    c = await _create(session, owner_user, pitch)
    await campaigns.start(session, c.id, owner_user, confirm=True)
    await campaigns.pause(session, c.id, None, reason="calidad")
    with pytest.raises(ForbiddenError):
        await campaigns.resume(session, c.id, admin)
    await campaigns.resume(session, c.id, owner_user)
    assert c.status == "running"


async def test_preview_skips_leads_missing_evidence(
    session, owner_user, templates, make_lead
) -> None:  # type: ignore[no-untyped-def]
    from tests.outreach.conftest import approve

    await approve(session, templates["lead_prueba_secreta"])
    await make_lead()
    c = await _create(session, owner_user, templates["lead_prueba_secreta"])
    pv = await campaigns.preview(session, c.id)
    assert pv["audience_count"] == 0 and pv["excluded_missing_data"] == 1
