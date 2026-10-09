from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.channels.sender import SendResult
from app.core.config import get_settings
from app.db.models.leads import LeadEvent
from app.db.models.outreach import CampaignTarget, OutreachMessage
from app.outreach import dispatch
from app.outreach.sender import OutreachSender

from .conftest import SUNDAY_10AM, TUESDAY_10AM


class FakeSender(OutreachSender):
    def __init__(self, ok: bool = True, status: str = "queued", error: str | None = None) -> None:
        self.calls: list[str] = []
        self.ok, self.status, self.error = ok, status, error

    async def send_step(self, session, msg, *, to_e164, content_sid):  # type: ignore[no-untyped-def]
        self.calls.append(to_e164)
        res = SendResult(
            ok=self.ok, sid=f"SM{len(self.calls):030d}", status=self.status, error=self.error
        )
        if self.ok:
            msg.status, msg.provider_sid = "sent", res.sid
            msg.sent_at = TUESDAY_10AM
        else:
            msg.status, msg.error = "failed", self.error
        return res


async def _run(session, camp, sender, now=TUESDAY_10AM):  # type: ignore[no-untyped-def]
    return await dispatch.dispatch_campaign(session, camp, now=now, sender=sender, pace=0)


async def test_sends_and_records(session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    leads = [await make_lead() for _ in range(3)]
    camp = await make_campaign(pitch, leads)
    sender = FakeSender()
    res = await _run(session, camp, sender)
    assert res["sent"] == 3 and len(sender.calls) == 3
    msgs = (await session.execute(select(OutreachMessage))).scalars().all()
    assert {m.idempotency_key for m in msgs} == {f"{camp.id}:{lead.id}:1" for lead in leads}
    assert all(m.status == "sent" and m.body and "{{" not in m.body for m in msgs)
    events = (
        (await session.execute(select(LeadEvent).where(LeadEvent.kind == "outreach_sent")))
        .scalars()
        .all()
    )
    assert len(events) == 3
    assert camp.status == "completed"


async def test_retry_is_idempotent(session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead])
    sender = FakeSender()
    await _run(session, camp, sender)
    # simula reintento: el objetivo vuelve a pending (p. ej. job duplicado)
    target = (await session.execute(select(CampaignTarget))).scalar_one()
    target.status = "pending"
    camp.status = "running"
    await session.commit()
    res = await _run(session, camp, sender)
    assert res["sent"] == 0 and len(sender.calls) == 1
    assert len((await session.execute(select(OutreachMessage))).scalars().all()) == 1


async def test_queued_message_is_reused_after_crash(
    session, pitch, make_lead, make_campaign
) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead])
    session.add(
        OutreachMessage(
            campaign_id=camp.id,
            lead_id=lead.id,
            step=1,
            status="queued",
            idempotency_key=dispatch.idempotency_key(camp.id, lead.id, 1),
            body="x",
            content_variables={"1": "hola", "2": "Vendedores AI", "3": "tu clinica"},
        )
    )
    await session.commit()
    sender = FakeSender()
    await _run(session, camp, sender)
    assert len(sender.calls) == 1
    assert len((await session.execute(select(OutreachMessage))).scalars().all()) == 1


async def test_outside_window_sends_nothing(session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    camp = await make_campaign(pitch, [await make_lead()])
    sender = FakeSender()
    res = await _run(session, camp, sender, now=SUNDAY_10AM)
    assert res["sent"] == 0 and not sender.calls and camp.status == "running"


async def test_skips_ineligible_leads(session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    ok = await make_lead()
    bad = await make_lead(disposition="no_contactar")
    camp = await make_campaign(pitch, [bad, ok])
    sender = FakeSender()
    res = await _run(session, camp, sender)
    assert res["sent"] == 1 and res["skipped"] == 1
    assert camp.stats["skipped"] == {"lead_no_activo": 1}


async def test_daily_limit_stops_batch(session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    leads = [await make_lead() for _ in range(4)]
    camp = await make_campaign(pitch, leads, daily_limit=2)
    sender = FakeSender()
    res = await _run(session, camp, sender)
    assert res["sent"] == 2 and camp.status == "running"
    # al dia siguiente (miercoles) continua
    res2 = await _run(session, camp, sender, now=TUESDAY_10AM + timedelta(days=1))
    assert res2["sent"] == 2 and camp.status == "completed"


async def test_failure_marks_target_failed(session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    camp = await make_campaign(pitch, [await make_lead()])
    res = await _run(session, camp, FakeSender(ok=False, status="failed", error="21211"))
    assert res["failed"] == 1
    msg = (await session.execute(select(OutreachMessage))).scalar_one()
    assert msg.status == "failed" and msg.error == "21211"


async def test_quality_pauses_campaign(session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead], daily_limit=80)
    for i in range(20):
        other = await make_lead()
        session.add(
            OutreachMessage(
                campaign_id=camp.id,
                lead_id=other.id,
                step=1,
                status="failed" if i < 3 else "delivered",
                sent_at=TUESDAY_10AM - timedelta(days=1),
                idempotency_key=f"x{i}",
            )
        )
    await session.commit()
    sender = FakeSender()
    await _run(session, camp, sender)
    assert camp.status == "paused" and camp.paused_reason == "calidad" and not sender.calls


async def test_followup_after_72h(session, pitch, followup, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(
        pitch, [lead], audience_filter={"followup_template_id": str(followup.id)}
    )
    sender = FakeSender()
    await _run(session, camp, sender)
    assert len(sender.calls) == 1 and camp.status == "running"
    target = (await session.execute(select(CampaignTarget))).scalar_one()
    assert target.status == "sent"
    # antes de 72h no hay seguimiento
    await _run(session, camp, sender, now=TUESDAY_10AM + timedelta(days=1))
    assert len(sender.calls) == 1
    # a los 4 dias (sabado 17: dia habil) sale el seguimiento
    target.updated_at = TUESDAY_10AM - timedelta(days=1)
    msg = (await session.execute(select(OutreachMessage))).scalar_one()
    msg.sent_at = TUESDAY_10AM - timedelta(days=1)
    await session.commit()
    await _run(session, camp, sender, now=TUESDAY_10AM + timedelta(days=3))
    assert len(sender.calls) == 2
    steps = sorted(m.step for m in (await session.execute(select(OutreachMessage))).scalars())
    assert steps == [1, 2] and camp.status == "completed"


async def test_dispatch_all_respects_kill_switch(
    session, pitch, make_lead, make_campaign, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    camp = await make_campaign(pitch, [await make_lead()])
    monkeypatch.setenv("VAI_OUTREACH_ENABLED", "false")
    get_settings.cache_clear()
    sender = FakeSender()
    out = await dispatch.dispatch_all(session, now=TUESDAY_10AM, sender=sender, pace=0)
    assert out["enabled"] is False and not sender.calls and camp.status == "running"


async def test_dry_run_does_not_call_twilio(session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    """Con el sender real y VAI_TWILIO_DRY_RUN=true no hay red (el conftest la bloquea)."""
    camp = await make_campaign(pitch, [await make_lead()])
    out = await dispatch.dispatch_all(session, now=TUESDAY_10AM, pace=0)
    assert out["sent"] == 1
    msg = (await session.execute(select(OutreachMessage))).scalar_one()
    assert msg.provider_sid and msg.provider_sid.startswith("DRYRUN")
    assert camp.status == "completed"


async def test_job_wrapper(engine, session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    from app.outreach.jobs import dispatch_campaign_job

    await make_campaign(
        pitch, [await make_lead()], stats={"started_at": "2020-01-01T00:00:00+00:00"}
    )
    out = await dispatch_campaign_job({"pace": 0})
    assert out["enabled"] is True and out["campaigns"] == 1


async def test_dispatch_all_isolates_failing_campaign(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.outreach import dispatch as d

    calls: list[object] = []

    async def boom(sess, campaign, **kw):  # type: ignore[no-untyped-def]
        calls.append(campaign.id)
        if len(calls) == 1:
            raise RuntimeError("x")
        return {"sent": 1, "failed": 0, "skipped": 0}

    from tests.outreach.conftest import CONTENT_SID  # noqa: F401

    monkeypatch.setattr(d, "dispatch_campaign", boom)
    from app.db.models.outreach import Campaign

    for n in ("uno", "dos"):
        session.add(Campaign(name=n, status="running", daily_limit=5, stats={}, audience_filter={}))
    await session.commit()
    res = await d.dispatch_all(session, pace=0)
    assert len(calls) == 2 and res["sent"] == 1
