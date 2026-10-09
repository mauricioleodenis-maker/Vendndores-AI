from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.channels import sender as sender_mod
from app.channels.service import encrypt_phone
from app.core import jobs as core_jobs
from app.core.clock import utcnow
from app.core.crypto import get_crypto, make_aad, pack_blob, phone_hash
from app.db.models.booking import Appointment
from app.db.models.bots import BotConfig
from app.db.models.contacts import Consent, Contact
from app.db.models.conversations import Conversation
from app.db.models.outreach import MessageTemplate
from app.db.models.plans import Plan, Subscription
from app.db.models.scheduling import ScheduledJob
from app.reminders import jobs as rjobs
from app.reminders import policies as pol
from app.reminders import scheduler, service

PHONE = "+573001112233"


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_text(session, *, tenant_id, to_e164, body, bypass_suppression=False):
        calls.append(("text", {"to": to_e164, "body": body}))
        return sender_mod.SendResult(ok=True, sid="SMx", status="queued")

    async def fake_tpl(session, *, tenant_id, to_e164, content_sid, variables, from_override=None):
        calls.append(("tpl", {"to": to_e164, "sid": content_sid, "vars": variables}))
        return sender_mod.SendResult(ok=True, sid="SMy", status="queued")

    monkeypatch.setattr(service, "send_whatsapp_text", fake_text)
    monkeypatch.setattr(service, "send_whatsapp_template", fake_tpl)
    return calls


@pytest.fixture
async def contact(session, tenant):
    aad = make_aad("contacts", tenant.id, "display_name_enc")
    c = Contact(
        tenant_id=tenant.id,
        phone_hash=phone_hash(PHONE),
        phone_enc=encrypt_phone(tenant.id, PHONE),
        display_name_enc=pack_blob(get_crypto().encrypt_str("Ana Pérez", aad=aad)),
    )
    session.add(c)
    await session.commit()
    return c


async def make_appt(session, tenant, contact, *, hours=48, status="confirmed", key="k1"):
    start = (utcnow() + timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    a = Appointment(
        tenant_id=tenant.id,
        contact_id=contact.id,
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        status=status,
        idempotency_key=key,
    )
    session.add(a)
    await session.commit()
    return a


async def set_plan(session, tenant, code):
    plan = Plan(code=code, name=code, setup_fee_cop=0, monthly_fee_cop=0,
                limits={"followups_enabled": code != "basico"})  # fmt: skip
    session.add(plan)
    await session.flush()
    session.add(Subscription(tenant_id=tenant.id, plan_id=plan.id, status="active"))
    await session.commit()


async def jobs_of(session, appt):
    rows = await session.execute(
        select(ScheduledJob)
        .where(ScheduledJob.appointment_id == appt.id)
        .order_by(ScheduledJob.run_at)
    )
    return list(rows.scalars())


# ------------------------------------------------------------------ policies
def test_quiet_hours_and_next_allowed():
    night = datetime(2026, 10, 9, 5, 0, tzinfo=UTC)  # 00:00 Bogota
    assert pol.in_quiet_hours(night)
    assert pol.next_allowed(night) == datetime(2026, 10, 9, 13, 0, tzinfo=UTC)  # 08:00 Bogota
    late = datetime(2026, 10, 9, 1, 30, tzinfo=UTC)  # 20:30 Bogota del 8
    assert pol.next_allowed(late) == datetime(2026, 10, 9, 13, 0, tzinfo=UTC)
    noon = datetime(2026, 10, 9, 17, 0, tzinfo=UTC)
    assert not pol.in_quiet_hours(noon) and pol.next_allowed(noon) == noon
    # naive se trata como UTC
    assert pol.in_quiet_hours(datetime(2026, 10, 9, 5, 0))


def test_formatting_and_render():
    dt = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)  # miercoles 15:00 Bogota
    assert pol.format_date_es(dt) == "miércoles 14 de octubre"
    assert pol.format_time_es(dt) == "3:00 p. m."
    assert pol.format_time_es(datetime(2026, 10, 14, 17, 5, tzinfo=UTC)) == "12:05 p. m."
    assert pol.format_time_es(datetime(2026, 10, 14, 14, 0, tzinfo=UTC)) == "9:00 a. m."
    assert pol.render_text(
        "Hola {nombre} {{negocio}} {x.__class__}", {"nombre": "A", "negocio": "B"}
    ) == ("Hola A B {x.__class__}")
    assert pol.tz_of("No/Existe").key == "America/Bogota"


# ------------------------------------------------------------------ scheduler
async def test_schedule_basico_only_24h_and_dedupe(session, tenant, contact):
    appt = await make_appt(session, tenant, contact)
    await scheduler.schedule_for_appointment(session, appt)
    await scheduler.schedule_for_appointment(session, appt)
    await session.commit()
    jobs = await jobs_of(session, appt)
    assert [j.kind for j in jobs] == ["reminder_24h"]
    assert jobs[0].dedupe_key.startswith(f"reminder_24h:{appt.id}:")


async def test_schedule_pro_has_2h_and_skips_past(session, tenant, contact):
    await set_plan(session, tenant, "pro")
    far = await make_appt(session, tenant, contact, hours=72, key="a")
    soon = await make_appt(session, tenant, contact, hours=5, key="b")
    for a in (far, soon):
        await scheduler.schedule_for_appointment(session, a)
    await session.commit()
    assert [j.kind for j in await jobs_of(session, far)] == ["reminder_24h", "reminder_2h"]
    assert [j.kind for j in await jobs_of(session, soon)] == ["reminder_2h"]


async def test_schedule_ignores_inactive_optout_and_no_contact(session, tenant, contact):
    cancelled = await make_appt(session, tenant, contact, status="cancelled", key="c")
    await scheduler.schedule_for_appointment(session, cancelled)
    contact.opted_out = True
    ok = await make_appt(session, tenant, contact, key="d")
    await scheduler.schedule_for_appointment(session, ok)
    orphan = Appointment(tenant_id=tenant.id, starts_at=utcnow() + timedelta(days=3),
                         ends_at=utcnow() + timedelta(days=3, hours=1), status="confirmed")  # fmt: skip
    await scheduler.schedule_for_appointment(session, orphan)
    await session.commit()
    assert (await session.execute(select(ScheduledJob))).first() is None


async def test_cancel_and_revive_same_slot(session, tenant, contact):
    appt = await make_appt(session, tenant, contact)
    await scheduler.schedule_for_appointment(session, appt)
    await scheduler.cancel_for_appointment(session, appt.id)
    await session.commit()
    (job,) = await jobs_of(session, appt)
    assert job.status == "cancelled"
    await scheduler.schedule_for_appointment(session, appt)
    await session.commit()
    await session.refresh(job)
    assert job.status == "pending" and job.attempts == 0


async def test_reschedule_creates_new_keys(session, tenant, contact):
    appt = await make_appt(session, tenant, contact)
    await scheduler.schedule_for_appointment(session, appt)
    await scheduler.cancel_for_appointment(session, appt.id)
    appt.starts_at = appt.starts_at + timedelta(days=1)
    await scheduler.schedule_for_appointment(session, appt)
    await session.commit()
    statuses = sorted(j.status for j in await jobs_of(session, appt))
    assert statuses == ["cancelled", "pending"]


async def test_noshow_followup_requires_plan(session, tenant, contact):
    appt = await make_appt(session, tenant, contact, status="no_show")
    await scheduler.schedule_noshow_followup(session, appt)
    assert (await session.execute(select(ScheduledJob))).first() is None
    await set_plan(session, tenant, "pro")
    await scheduler.schedule_noshow_followup(session, appt)
    await scheduler.schedule_noshow_followup(session, appt)
    await session.commit()
    (job,) = await jobs_of(session, appt)
    assert job.kind == "followup_noshow"
    appt.status = "confirmed"
    other = await make_appt(session, tenant, contact, hours=60, key="z")
    await scheduler.schedule_noshow_followup(session, other)  # no es no_show
    assert len(await jobs_of(session, other)) == 0


async def test_unbooked_followup_max_two(session, tenant, contact):
    conv = Conversation(tenant_id=tenant.id, contact_id=contact.id)
    session.add(conv)
    await session.commit()
    kw = {"tenant_id": tenant.id, "contact_id": contact.id, "conversation_id": conv.id}
    assert await scheduler.schedule_unbooked_followup(session, **kw) is None  # plan basico
    await set_plan(session, tenant, "pro")
    first = await scheduler.schedule_unbooked_followup(session, **kw)
    second = await scheduler.schedule_unbooked_followup(session, **kw)
    assert first and second and first.run_at < second.run_at
    assert await scheduler.schedule_unbooked_followup(session, **kw) is None
    kw["contact_id"] = __import__("uuid").uuid4()
    assert await scheduler.schedule_unbooked_followup(session, **kw) is None


# ------------------------------------------------------------------ service
NOON = datetime(2026, 10, 9, 17, 0, tzinfo=UTC)  # 12:00 Bogota


async def claim_one(session, appt, now):
    (job,) = await jobs_of(session, appt)
    job.run_at = now - timedelta(minutes=1)
    await session.commit()
    ids = await service.claim_due(session, now=now)
    await session.commit()
    assert ids == [job.id]
    return job


async def build(session, tenant, contact, now=NOON, hours=24):
    appt = await make_appt(session, tenant, contact, key="s")
    appt.starts_at = now + timedelta(hours=hours)
    appt.ends_at = appt.starts_at + timedelta(minutes=30)
    await session.commit()
    await scheduler.schedule_for_appointment(session, appt)
    await session.commit()
    return appt


async def test_send_text_in_window_marks_sent(session, tenant, contact, sent, monkeypatch):
    monkeypatch.setattr("app.reminders.scheduler.utcnow", lambda: NOON - timedelta(days=2))
    appt = await build(session, tenant, contact, hours=30)
    session.add(
        Conversation(
            tenant_id=tenant.id, contact_id=contact.id, last_inbound_at=NOON - timedelta(hours=1)
        )
    )
    await session.commit()
    job = await claim_one(session, appt, NOON)
    assert await service.process_job(session, job.id, now=NOON) == "done"
    await session.commit()
    kind, data = sent[0]
    assert (
        kind == "text"
        and data["to"] == PHONE
        and "Hola Ana" in data["body"]
        and "Clinica Demo" in data["body"]
    )
    await session.refresh(appt)
    assert appt.reminder_24h_sent_at is not None and appt.reminder_sent_at is not None
    assert job.status == "done"
    assert await service.process_job(session, job.id, now=NOON) == "ignored"


async def test_bot_template_overrides_text(session, tenant, contact, sent, monkeypatch):
    monkeypatch.setattr("app.reminders.scheduler.utcnow", lambda: NOON - timedelta(days=2))
    session.add(BotConfig(tenant_id=tenant.id, version=1, status="published",
                          templates={"recordatorio_24h": "Ey {nombre}: {{hora}} en {negocio}"}))  # fmt: skip
    session.add(
        Conversation(
            tenant_id=tenant.id, contact_id=contact.id, last_inbound_at=NOON - timedelta(hours=1)
        )
    )
    appt = await build(session, tenant, contact, hours=30)
    job = await claim_one(session, appt, NOON)
    await service.process_job(session, job.id, now=NOON)
    assert sent[0][1]["body"].startswith("Ey Ana: ") and "Clinica Demo" in sent[0][1]["body"]


async def test_template_outside_window_and_missing(session, tenant, contact, sent, monkeypatch):
    monkeypatch.setattr("app.reminders.scheduler.utcnow", lambda: NOON - timedelta(days=2))
    appt = await build(session, tenant, contact, hours=30)
    job = await claim_one(session, appt, NOON)
    assert await service.process_job(session, job.id, now=NOON) == "skipped_no_template"
    assert sent == []
    # con plantilla aprobada (la del tenant gana a la global)
    session.add_all([
        MessageTemplate(name="cita_recordatorio_24h", scope="tenant", body="x", approval_status="approved",
                        approved=True, twilio_content_sid="HXglobal"),
        MessageTemplate(name="cita_recordatorio_24h", scope="tenant", tenant_id=tenant.id, version=2, body="x",
                        approval_status="approved", approved=True, twilio_content_sid="HXtenant"),
    ])  # fmt: skip
    job.status = "running"
    await session.commit()
    assert await service.process_job(session, job.id, now=NOON) == "done"
    kind, data = sent[0]
    assert kind == "tpl" and data["sid"] == "HXtenant"
    assert data["vars"]["1"] == "Ana" and data["vars"]["4"] == "Clinica Demo"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda a, c, s: setattr(a, "status", "cancelled"), "skipped_cita_no_activa"),
        (lambda a, c, s: setattr(a, "reminder_24h_sent_at", NOON), "skipped_ya_enviado"),
        (lambda a, c, s: setattr(c, "opted_out", True), "skipped_opt_out"),
        (lambda a, c, s: setattr(c, "erased_at", NOON), "skipped_sin_contacto"),
        (lambda a, c, s: setattr(a, "starts_at", NOON - timedelta(hours=1)), "skipped_cita_pasada"),
        (
            lambda a, c, s: setattr(a, "starts_at", NOON + timedelta(days=3)),
            "skipped_cita_reprogramada",
        ),
    ],
)
async def test_recheck_skips(session, tenant, contact, sent, monkeypatch, mutate, reason):
    monkeypatch.setattr("app.reminders.scheduler.utcnow", lambda: NOON - timedelta(days=2))
    appt = await build(session, tenant, contact, hours=30)
    job = await claim_one(session, appt, NOON)
    mutate(appt, contact, None)
    await session.commit()
    assert await service.process_job(session, job.id, now=NOON) == reason
    assert sent == [] and job.status == "cancelled"


async def test_revoked_consent_and_inactive_tenant(session, tenant, contact, sent, monkeypatch):
    monkeypatch.setattr("app.reminders.scheduler.utcnow", lambda: NOON - timedelta(days=2))
    appt = await build(session, tenant, contact, hours=30)
    job = await claim_one(session, appt, NOON)
    session.add(
        Consent(
            tenant_id=tenant.id, contact_id=contact.id, purpose="recordatorios", revoked_at=NOON
        )
    )
    await session.commit()
    assert await service.process_job(session, job.id, now=NOON) == "skipped_opt_out"
    job.status = "running"
    tenant.status = "suspended" if False else "draft"
    await session.commit()
    assert await service.process_job(session, job.id, now=NOON) == "skipped_tenant_inactivo"


async def test_quiet_hours_postpone_and_skip(session, tenant, contact, sent, monkeypatch):
    night = datetime(2026, 10, 9, 7, 0, tzinfo=UTC)  # 02:00 Bogota
    monkeypatch.setattr("app.reminders.scheduler.utcnow", lambda: night - timedelta(days=2))
    appt = await build(session, tenant, contact, now=night, hours=30)
    job = await claim_one(session, appt, night)
    assert await service.process_job(session, job.id, now=night) == "postponed"
    assert job.status == "pending" and job.run_at.replace(tzinfo=UTC) == datetime(
        2026, 10, 9, 13, 0, tzinfo=UTC
    )
    # el siguiente horario permitido queda despues de la cita -> se omite
    appt.starts_at = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    job.dedupe_key = f"reminder_24h:{appt.id}:{appt.starts_at.isoformat()}"
    job.status = "running"
    await session.commit()
    assert await service.process_job(session, job.id, now=night) == "skipped_horario_silencio"
    assert sent == []


async def test_failure_retries_then_fails(session, tenant, contact, monkeypatch):
    monkeypatch.setattr("app.reminders.scheduler.utcnow", lambda: NOON - timedelta(days=2))

    async def boom(session, **kw):
        return sender_mod.SendResult(ok=False, status="failed", error="63018")

    monkeypatch.setattr(service, "send_whatsapp_text", boom)
    session.add(
        Conversation(
            tenant_id=tenant.id, contact_id=contact.id, last_inbound_at=NOON - timedelta(hours=1)
        )
    )
    appt = await build(session, tenant, contact, hours=30)
    job = await claim_one(session, appt, NOON)
    assert await service.process_job(session, job.id, now=NOON) == "retry"
    assert job.status == "pending" and job.next_attempt_at is not None and job.attempts == 1
    assert await service.claim_due(session, now=NOON) == []  # backoff
    for _ in range(2):
        job.status = "running"
        await service.process_job(session, job.id, now=NOON)
    assert job.status == "failed" and job.last_error == "63018"


async def test_suppressed_is_cancelled(session, tenant, contact, monkeypatch):
    monkeypatch.setattr("app.reminders.scheduler.utcnow", lambda: NOON - timedelta(days=2))

    async def supp(session, **kw):
        return sender_mod.SendResult(ok=False, status="suppressed", error="suprimido")

    monkeypatch.setattr(service, "send_whatsapp_text", supp)
    session.add(
        Conversation(
            tenant_id=tenant.id, contact_id=contact.id, last_inbound_at=NOON - timedelta(hours=1)
        )
    )
    appt = await build(session, tenant, contact, hours=30)
    job = await claim_one(session, appt, NOON)
    assert await service.process_job(session, job.id, now=NOON) == "skipped_suprimido"


async def test_noshow_and_unbooked_execution(session, tenant, contact, sent):
    await set_plan(session, tenant, "pro")
    appt = await make_appt(session, tenant, contact, status="no_show", key="n")
    await scheduler.schedule_noshow_followup(session, appt)
    conv = Conversation(
        tenant_id=tenant.id, contact_id=contact.id, last_inbound_at=NOON - timedelta(hours=1)
    )
    session.add(conv)
    await session.commit()
    (job,) = await jobs_of(session, appt)
    job.status, job.run_at = "running", NOON - timedelta(hours=1)
    await session.commit()
    assert await service.process_job(session, job.id, now=NOON) == "done"
    assert "no alcanzamos a verte" in sent[0][1]["body"]

    other = await scheduler.schedule_unbooked_followup(
        session, tenant_id=tenant.id, contact_id=contact.id, conversation_id=conv.id
    )
    other.status = "running"
    await session.commit()
    assert await service.process_job(session, other.id, now=NOON) == "done"
    # ya tiene cita activa -> omite
    await make_appt(session, tenant, contact, key="fut")
    again = await scheduler.schedule_unbooked_followup(
        session, tenant_id=tenant.id, contact_id=contact.id, conversation_id=conv.id
    )
    again.status = "running"
    await session.commit()
    assert await service.process_job(session, again.id, now=utcnow()) == "skipped_ya_agendo"


async def test_unbooked_window_closed_and_plan_downgrade(session, tenant, contact, sent):
    await set_plan(session, tenant, "pro")
    conv = Conversation(
        tenant_id=tenant.id, contact_id=contact.id, last_inbound_at=NOON - timedelta(days=5)
    )
    session.add(conv)
    await session.commit()
    job = await scheduler.schedule_unbooked_followup(
        session, tenant_id=tenant.id, contact_id=contact.id, conversation_id=conv.id
    )
    job.status = "running"
    await session.commit()
    assert await service.process_job(session, job.id, now=NOON) == "skipped_ventana_cerrada"
    nos = await make_appt(session, tenant, contact, status="no_show", key="q")
    await scheduler.schedule_noshow_followup(session, nos)
    (j2,) = await jobs_of(session, nos)
    j2.status = "running"
    sub = (await session.execute(select(Subscription))).scalar_one()
    plan = await session.get(Plan, sub.plan_id)
    plan.limits = {"followups_enabled": False}
    await session.commit()
    assert await service.process_job(session, j2.id, now=NOON) == "skipped_plan_sin_seguimientos"


async def test_claim_recovers_stale_locks(session, tenant, contact):
    appt = await make_appt(session, tenant, contact)
    await scheduler.schedule_for_appointment(session, appt)
    (job,) = await jobs_of(session, appt)
    job.status, job.locked_at, job.run_at = (
        "running",
        NOON - timedelta(hours=1),
        NOON - timedelta(hours=2),
    )
    await session.commit()
    assert await service.claim_due(session, now=NOON) == [job.id]
    assert await service.claim_due(session, now=NOON) == []


async def test_run_batch_counts(session, tenant, contact):
    assert await service.run_batch(session, [__import__("uuid").uuid4()]) == {"ignored": 1}


# ------------------------------------------------------------------ jobs
async def test_enqueue_due_job_and_send(session, tenant, contact, sent, monkeypatch):
    appt = await make_appt(session, tenant, contact, hours=30)
    start = utcnow() + timedelta(hours=1)
    appt.starts_at, appt.ends_at = start, start + timedelta(minutes=30)
    session.add(Conversation(tenant_id=tenant.id, contact_id=contact.id, last_inbound_at=utcnow()))
    await session.commit()
    job = ScheduledJob(tenant_id=tenant.id, kind="reminder_24h", run_at=utcnow() - timedelta(minutes=1),
                       contact_id=contact.id, appointment_id=appt.id,
                       dedupe_key=f"reminder_24h:{appt.id}:{start.isoformat()}")  # fmt: skip
    session.add(job)
    await session.commit()
    # fuera de horario de silencio siempre: fija la hora del servicio
    monkeypatch.setattr(service, "utcnow", lambda: NOON)
    assert await rjobs.enqueue_due({}) == 1
    assert [n for n, *_ in core_jobs.ENQUEUED] == ["reminders.send"]
    appt.starts_at = NOON + timedelta(hours=24)
    appt.ends_at = appt.starts_at + timedelta(minutes=30)
    job.dedupe_key = f"reminder_24h:{appt.id}:{appt.starts_at.isoformat()}"
    await session.commit()
    assert await core_jobs.run_enqueued() == 1
    await session.refresh(job)
    assert job.status in {"done", "pending", "cancelled"}
    assert await rjobs.send({}, str(job.id)) == "ignored"


async def test_send_job_rolls_back_on_crash(session, monkeypatch):
    async def bad(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(service, "process_job", bad)
    with pytest.raises(RuntimeError):
        await rjobs.send({}, str(__import__("uuid").uuid4()))


def test_cron_registered():
    assert [c.name for c in rjobs.CRON_JOBS] == ["reminders.enqueue_due"]
