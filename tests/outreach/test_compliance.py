from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.config import get_settings
from app.core.crypto import phone_hash
from app.db.models.outreach import OutreachMessage
from app.db.models.privacy import SuppressionEntry
from app.outreach import compliance as c
from app.outreach.compliance import ComplianceGate

from .conftest import HOLIDAY_10AM, SUNDAY_10AM, TUESDAY_10AM


async def _check(session, lead, campaign, tpl, now=TUESDAY_10AM, step=1):  # type: ignore[no-untyped-def]
    return await ComplianceGate(session).check(lead, campaign, now, template=tpl, step=step)


async def test_allow_happy_path(session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead])
    assert (await _check(session, lead, camp, pitch)).allowed


async def test_kill_switch(session, pitch, make_lead, make_campaign, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead])
    monkeypatch.setenv("VAI_OUTREACH_ENABLED", "false")
    get_settings.cache_clear()
    assert (await _check(session, lead, camp, pitch)).reason == "kill_switch"


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"disposition": "no_contactar"}, "lead_no_activo"),
        ({"disposition": "perdido"}, "lead_no_activo"),
        ({"phone_type": "landline"}, "no_movil"),
        ({"phone_type": "unknown"}, "no_movil"),
    ],
)
async def test_lead_level_denials(session, pitch, make_lead, make_campaign, kw, reason) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead(**kw)
    camp = await make_campaign(pitch, [lead])
    d = await _check(session, lead, camp, pitch)
    assert d.action == "deny" and d.reason == reason


async def test_no_phone_denied(session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead(phone_e164=None)
    camp = await make_campaign(pitch, [lead])
    assert (await _check(session, lead, camp, pitch)).reason == "no_movil"


async def test_suppressed(session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    session.add(
        SuppressionEntry(phone_hash=phone_hash(lead.phone_e164), scope="global", tenant_key="")
    )
    await session.commit()
    camp = await make_campaign(pitch, [lead])
    assert (await _check(session, lead, camp, pitch)).reason == "suprimido"


async def test_template_not_approved_or_missing(
    session, templates, make_lead, make_campaign
) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    tpl = templates["lead_contacto_inicial"]
    camp = await make_campaign(tpl, [lead])
    assert (await _check(session, lead, camp, tpl)).reason == "plantilla_no_aprobada"
    assert (await _check(session, lead, camp, None)).reason == "plantilla_no_aprobada"


async def test_campaign_must_be_running(session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead], status="paused")
    assert (await _check(session, lead, camp, pitch)).reason == "campana_no_activa"


@pytest.mark.parametrize("when", [SUNDAY_10AM, HOLIDAY_10AM])
async def test_sunday_and_holiday_deferred(session, pitch, make_lead, make_campaign, when) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead])
    d = await _check(session, lead, camp, pitch, now=when)
    assert d.action == "defer" and d.reason == "fuera_de_ventana"
    assert d.until == c.next_window_start(when)


def test_window_boundaries() -> None:
    from datetime import UTC, datetime

    from app.core.clock import BOGOTA

    def bog(h: int, m: int = 0, day: int = 13) -> datetime:
        return datetime(2026, 10, day, h, m, tzinfo=BOGOTA).astimezone(UTC)

    assert not c.in_send_window(bog(7, 59))
    assert c.in_send_window(bog(8))
    assert c.in_send_window(bog(18, 59))
    assert not c.in_send_window(bog(19))
    assert c.in_send_window(bog(10, day=17))  # sabado
    # viernes tras cierre -> sabado 8:00; sabado tras cierre -> lunes 19
    assert c.next_window_start(bog(20, day=16)) == bog(8, day=17)
    assert c.next_window_start(bog(20, day=17)) == bog(8, day=19)
    assert c.next_window_start(bog(5, day=13)) == bog(8, day=13)


async def test_per_lead_gap_and_max_steps(
    session, pitch, followup, make_lead, make_campaign
) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead], daily_limit=50)
    sent = TUESDAY_10AM - timedelta(hours=10)
    session.add(
        OutreachMessage(
            campaign_id=camp.id,
            lead_id=lead.id,
            step=1,
            status="sent",
            sent_at=sent,
            idempotency_key="k1",
        )
    )
    await session.commit()
    assert (await _check(session, lead, camp, pitch, step=1)).reason == "max_pasos"
    d = await _check(session, lead, camp, followup, step=2)
    assert d.action == "defer" and d.reason == "espera_72h" and d.until == sent + c.MIN_GAP
    later = sent + timedelta(hours=73)
    # 73h despues sigue en dia habil? 13-oct 05:00 + 73h = 16-oct 06:00 Bogota -> fuera de ventana
    later = later.replace(hour=15, minute=0)
    assert (await _check(session, lead, camp, followup, now=later, step=2)).allowed
    session.add(
        OutreachMessage(
            campaign_id=camp.id,
            lead_id=lead.id,
            step=2,
            status="delivered",
            sent_at=later,
            idempotency_key="k2",
        )
    )
    await session.commit()
    assert (
        await _check(session, lead, camp, followup, now=later + timedelta(days=5), step=2)
    ).reason == "max_pasos"
    assert (await _check(session, lead, camp, followup, step=3)).reason == "max_pasos"


async def test_replied_lead_denied(session, pitch, followup, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead])
    session.add(
        OutreachMessage(
            campaign_id=camp.id,
            lead_id=lead.id,
            step=1,
            status="replied",
            sent_at=TUESDAY_10AM - timedelta(days=5),
            idempotency_key="r1",
        )
    )
    await session.commit()
    assert (await _check(session, lead, camp, followup, step=2)).reason == "ya_respondio"


def test_ramp_and_caps(make_campaign=None) -> None:  # type: ignore[no-untyped-def]
    from app.db.models.outreach import Campaign

    camp = Campaign(name="x", daily_limit=200, stats={"started_at": TUESDAY_10AM.isoformat()})
    assert c.effective_daily_limit(camp, TUESDAY_10AM) == 20
    assert c.effective_daily_limit(camp, TUESDAY_10AM + timedelta(days=3)) == 50
    assert c.effective_daily_limit(camp, TUESDAY_10AM + timedelta(days=30)) == c.MAX_DAILY
    camp.daily_limit = 25
    assert c.effective_daily_limit(camp, TUESDAY_10AM + timedelta(days=30)) == 25
    camp.stats = {}
    assert c.effective_daily_limit(camp, TUESDAY_10AM) == 20


async def test_daily_limit_defers(session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    others = [await make_lead() for _ in range(2)]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead], daily_limit=2)
    for i, o in enumerate(others):
        session.add(
            OutreachMessage(
                campaign_id=camp.id,
                lead_id=o.id,
                step=1,
                status="sent",
                sent_at=TUESDAY_10AM - timedelta(hours=1),
                idempotency_key=f"d{i}",
            )
        )
    await session.commit()
    d = await _check(session, lead, camp, pitch)
    assert d.action == "defer" and d.reason == "limite_diario"


async def test_global_daily_cap(session, pitch, make_lead, make_campaign, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(c, "MAX_DAILY", 1)
    other = await make_lead()
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead], daily_limit=50)
    session.add(
        OutreachMessage(
            campaign_id=None,
            lead_id=other.id,
            step=1,
            status="sent",
            sent_at=TUESDAY_10AM - timedelta(hours=1),
            idempotency_key="g1",
        )
    )
    await session.commit()
    assert (await _check(session, lead, camp, pitch)).reason == "tope_global_diario"


async def test_quality_pause_threshold(session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead], daily_limit=80)
    senders = [await make_lead() for _ in range(20)]
    for i, s in enumerate(senders):
        session.add(
            OutreachMessage(
                campaign_id=camp.id,
                lead_id=s.id,
                step=1,
                status="failed" if i == 0 else "delivered",
                sent_at=TUESDAY_10AM - timedelta(days=2),
                idempotency_key=f"q{i}",
            )
        )
    await session.commit()
    # 1/20 = 5% -> no supera el umbral
    assert not await c.quality_degraded(session, camp)
    senders[1]  # noqa: B018
    session.add(
        OutreachMessage(
            campaign_id=camp.id,
            lead_id=senders[1].id,
            step=2,
            status="delivered",
            delivery_error_code="63049",
            sent_at=TUESDAY_10AM - timedelta(days=2),
            idempotency_key="q-block",
        )
    )
    await session.commit()
    assert await c.quality_degraded(session, camp)
    assert (await _check(session, lead, camp, pitch)).reason == "calidad"


async def test_fail_closed_on_error(session, pitch, make_lead, make_campaign, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead])

    async def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("db caida")

    monkeypatch.setattr(c, "is_suppressed", boom)
    d = await _check(session, lead, camp, pitch)
    assert d.action == "deny" and d.reason == "error_interno"
