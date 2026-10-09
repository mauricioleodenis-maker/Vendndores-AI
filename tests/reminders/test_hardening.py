from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import worker
from app.core.config import Settings
from app.db.models.conversations import Conversation
from app.db.models.scheduling import ScheduledJob
from app.reminders import jobs as rjobs
from app.reminders import policies as pol
from app.reminders import service
from tests.reminders.test_reminders import (
    NOON,
    build,
    claim_one,
)


def test_render_text_single_pass_no_rewrite():
    out = pol.render_text("Hola {nombre}, a las {hora}", {"nombre": "{hora}", "hora": "9:00"})
    assert out == "Hola {hora}, a las 9:00"


def test_outside_send_window_boundaries():
    def at(h, m=0):
        return datetime(2026, 10, 9, 0, m, tzinfo=UTC) + timedelta(hours=h + 5)  # Bogota = UTC-5

    assert pol.outside_send_window(at(7, 59)) and not pol.outside_send_window(at(8))
    assert not pol.outside_send_window(at(19, 59)) and pol.outside_send_window(at(20))
    assert pol.in_quiet_hours is pol.outside_send_window


def test_relative_phrases_use_send_time():
    start = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)  # 09:00 Bogota
    assert pol.time_left_phrase(start, start - timedelta(hours=2)) == "2 horas"
    assert pol.time_left_phrase(start, start - timedelta(hours=1)) == "1 hora"
    assert pol.time_left_phrase(start, start - timedelta(minutes=45)) == "45 minutos"
    assert pol.when_phrase(start, start - timedelta(days=1)) == "de mañana"
    assert pol.when_phrase(start, start - timedelta(hours=3)) == "de hoy"


async def test_postponed_2h_text_says_real_time_left(session, tenant, contact, sent, monkeypatch):
    monkeypatch.setattr("app.reminders.scheduler.utcnow", lambda: NOON - timedelta(days=2))
    session.add(Conversation(tenant_id=tenant.id, contact_id=contact.id, last_inbound_at=NOON))
    appt = await build(session, tenant, contact, hours=1)  # cita en 1 h
    appt.starts_at = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)
    await session.commit()
    job = (await session.execute(select(ScheduledJob))).scalars().first()
    job.kind = "reminder_2h"
    job.dedupe_key = f"reminder_2h:{appt.id}:{appt.starts_at.isoformat()}"
    job.status = "running"
    await session.commit()
    now = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)  # 08:00 Bogota, 1 h antes
    assert await service.process_job(session, job.id, now=now) == "done"
    body = sent[0][1]["body"]
    assert "en 1 hora" in body and "en 2 horas" not in body


async def test_second_execution_is_ignored(session, tenant, contact, sent, monkeypatch):
    monkeypatch.setattr("app.reminders.scheduler.utcnow", lambda: NOON - timedelta(days=2))
    session.add(Conversation(tenant_id=tenant.id, contact_id=contact.id, last_inbound_at=NOON))
    appt = await build(session, tenant, contact, hours=30)
    job = await claim_one(session, appt, NOON)
    assert await service.process_job(session, job.id, now=NOON) == "done"
    await session.commit()
    assert await service.process_job(session, job.id, now=NOON) == "ignored"
    assert len(sent) == 1


async def test_enqueue_failure_releases_claims(session, tenant, contact, monkeypatch):
    monkeypatch.setattr("app.reminders.scheduler.utcnow", lambda: NOON - timedelta(days=2))
    appt = await build(session, tenant, contact, hours=30)
    (job,) = (await session.execute(select(ScheduledJob))).scalars().all()
    job.run_at = NOON - timedelta(minutes=1)
    await session.commit()
    monkeypatch.setattr(service, "utcnow", lambda: NOON)

    async def boom(*a, **k):
        raise ConnectionError("redis")

    monkeypatch.setattr(rjobs, "enqueue", boom)
    assert await rjobs.enqueue_due({}) == 0
    await session.refresh(job)
    assert job.status == "pending" and job.locked_at is None
    assert appt.id == job.appointment_id


def test_worker_requires_redis_in_prod(monkeypatch):
    monkeypatch.setattr(
        worker, "get_settings", lambda: Settings.model_construct(env="prod", redis_url=None)
    )
    with pytest.raises(RuntimeError):
        worker.resolve_redis_dsn()
    monkeypatch.setattr(
        worker, "get_settings", lambda: Settings.model_construct(env="dev", redis_url=None)
    )
    assert worker.resolve_redis_dsn().startswith("redis://")


# ------------------------------------------------------------------ UI
async def test_ui_requires_login(client):
    r = await client.get("/admin/recordatorios", headers={"accept": "text/html"})
    assert r.status_code in (303, 401)


async def test_ui_empty_state(authenticated_client):
    r = await authenticated_client.get("/admin/recordatorios")
    assert r.status_code == 200 and "Sin empresas" in r.text


async def test_ui_list_cancel_and_tenant_isolation(
    authenticated_client,
    session,
    tenant,
    contact,
    monkeypatch,
):
    monkeypatch.setattr("app.reminders.scheduler.utcnow", lambda: NOON - timedelta(days=2))
    await build(session, tenant, contact, hours=30)
    (job,) = (await session.execute(select(ScheduledJob))).scalars().all()
    c = authenticated_client
    page = await c.get(f"/admin/recordatorios?tenant_id={tenant.id}")
    assert page.status_code == 200 and "Ana" in page.text and "Programado" in page.text
    assert "+573001112233" not in page.text
    import uuid

    wrong = await c.post(
        f"/admin/recordatorios/{job.id}/cancelar", data={"tenant_id": str(uuid.uuid4())}
    )
    assert "error=" in wrong.headers["location"]
    await session.refresh(job)
    assert job.status == "pending"
    ok = await c.post(f"/admin/recordatorios/{job.id}/cancelar", data={"tenant_id": str(tenant.id)})
    assert "ok=" in ok.headers["location"]
    await session.refresh(job)
    assert job.status == "cancelled"
    job.status = "failed"
    await session.commit()
    r = await c.post(
        f"/admin/recordatorios/{job.id}/reintentar", data={"tenant_id": str(tenant.id)}
    )
    assert "ok=" in r.headers["location"]
    await session.refresh(job)
    assert job.status == "pending" and job.attempts == 0
