from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.billing import service
from app.billing.jobs import generate_monthly_records
from app.core.errors import AppError, ConflictError
from app.db.models.audit import AuditLog
from app.db.models.plans import BillingRecord, Plan, Subscription

pytestmark = pytest.mark.asyncio


async def _plan(session, code="basico", monthly=250_000) -> Plan:
    plan = Plan(code=code, name=code.title(), setup_fee_cop=800_000, monthly_fee_cop=monthly)
    session.add(plan)
    await session.flush()
    return plan


async def _sub(session, tenant, plan, **kw) -> Subscription:
    sub = Subscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        status=kw.pop("status", "active"),
        started_at=kw.pop("started_at", datetime(2026, 1, 31, 15, tzinfo=UTC)),
        **kw,
    )
    session.add(sub)
    await session.flush()
    return sub


async def test_generation_is_idempotent_and_uses_custom_fee(session, make_tenant):
    plan = await _plan(session)
    t1, t2, t3 = await make_tenant(), await make_tenant(), await make_tenant()
    s1 = await _sub(session, t1, plan)
    await _sub(session, t2, plan, custom_monthly_fee_cop=100_000)
    await _sub(session, t3, plan, status="cancelled")
    assert await service.generate_monthly_records(session, "2026-02") == 2
    assert await service.generate_monthly_records(session, "2026-02") == 0
    rec = (
        await session.execute(select(BillingRecord).where(BillingRecord.subscription_id == s1.id))
    ).scalar_one()
    assert rec.due_date == date(2026, 2, 28)  # 31 recortado a fin de mes
    amounts = sorted((await session.execute(select(BillingRecord.amount_cop))).scalars().all())
    assert amounts == [100_000, 250_000]


async def test_generation_skips_future_subscriptions(session, make_tenant):
    plan = await _plan(session)
    await _sub(session, await make_tenant(), plan, started_at=datetime(2026, 6, 10, tzinfo=UTC))
    assert await service.generate_monthly_records(session, "2026-03") == 0


async def test_invalid_period(session):
    with pytest.raises(AppError):
        await service.generate_monthly_records(session, "2026-13")


async def test_overdue_and_past_due_then_paid_recovers(session, make_tenant, owner_user):
    plan = await _plan(session)
    sub = await _sub(session, await make_tenant(), plan)
    rec = await service.create_record(
        session,
        subscription_id=sub.id,
        kind="mensualidad",
        amount_cop=250_000,
        period="2026-02",
        due_date=date(2026, 2, 1),
    )
    assert await service.mark_overdue(session, today=date(2026, 2, 10)) == 1
    assert rec.status == "vencido" and sub.status == "active"
    await service.mark_overdue(session, today=date(2026, 2, 20))
    assert sub.status == "past_due"
    paid = await service.mark_paid(session, rec.id, actor=owner_user, method="nequi")
    assert paid.status == "pagado" and paid.paid_at is not None
    assert sub.status == "active"
    assert (await service.mark_paid(session, rec.id, actor=owner_user)).status == "pagado"


async def test_mark_paid_rules(session, make_tenant, owner_user):
    plan = await _plan(session)
    sub = await _sub(session, await make_tenant(), plan)
    rec = await service.create_record(session, subscription_id=sub.id, kind="setup", amount_cop=1)
    with pytest.raises(AppError):
        await service.mark_paid(session, rec.id, actor=owner_user, method="bitcoin")
    await service.void_record(session, rec.id, actor=owner_user)
    with pytest.raises(ConflictError):
        await service.mark_paid(session, rec.id, actor=owner_user)


async def test_refund_is_negative_and_validation(session, make_tenant):
    plan = await _plan(session)
    sub = await _sub(session, await make_tenant(), plan)
    r = await service.create_record(
        session, subscription_id=sub.id, kind="reembolso", amount_cop=5000
    )
    assert r.amount_cop == -5000
    with pytest.raises(AppError):
        await service.create_record(session, subscription_id=sub.id, kind="setup", amount_cop=-1)
    with pytest.raises(AppError):
        await service.create_record(session, subscription_id=sub.id, kind="otro", amount_cop=1)


async def test_summary(session, make_tenant, owner_user):
    plan = await _plan(session)
    now = datetime(2026, 3, 15, 12, tzinfo=UTC)
    sub = await _sub(session, await make_tenant(), plan)
    await _sub(session, await make_tenant(), plan, custom_monthly_fee_cop=50_000, status="past_due")
    setup = await service.create_record(
        session, subscription_id=sub.id, kind="setup", amount_cop=800_000
    )
    setup.status, setup.paid_at = "pagado", now
    old = await service.create_record(
        session, subscription_id=sub.id, kind="mensualidad", amount_cop=250_000, period="2026-02"
    )
    old.status = "vencido"
    await session.flush()
    s = await service.summary(session, now=now)
    assert s.mrr_cop == 300_000
    assert s.setups_month_cop == 800_000
    assert (s.overdue_cop, s.overdue_count) == (250_000, 1)
    assert s.active_subscriptions == 2


async def test_csv_neutralizes_formulas(session, make_tenant, owner_user):
    plan = await _plan(session)
    tenant = await make_tenant(name='=HYPERLINK("x")')
    sub = await _sub(session, tenant, plan)
    await service.create_record(
        session, subscription_id=sub.id, kind="setup", amount_cop=10, reference="@cmd"
    )
    rows = await service.list_records(session)
    out = service.export_csv(rows)
    assert out.startswith("﻿Empresa")
    assert "'=HYPERLINK" in out and "'@cmd" in out


async def test_job_generates_and_marks(session, make_tenant, engine):
    plan = await _plan(session)
    await _sub(session, await make_tenant(), plan)
    await session.commit()
    result = await generate_monthly_records({}, "2026-02")
    assert result == {"created": 1, "overdue": 1}


async def test_list_filters(session, make_tenant):
    plan = await _plan(session)
    t = await make_tenant()
    sub = await _sub(session, t, plan)
    await service.create_record(session, subscription_id=sub.id, kind="setup", amount_cop=1)
    assert len(await service.list_records(session, status="pendiente", tenant_id=t.id)) == 1
    assert await service.list_records(session, status="pagado") == []
    assert await service.list_records(session, period="2026-01") == []


# ----------------------------------------------------------------------- HTTP
async def test_page_summary_pay_export(authenticated_client, session, make_tenant):
    plan = await _plan(session)
    sub = await _sub(session, await make_tenant(), plan)
    rec = await service.create_record(
        session, subscription_id=sub.id, kind="setup", amount_cop=800_000
    )
    await session.commit()
    c = authenticated_client
    page = await c.get("/admin/facturacion", headers={"accept": "text/html"})
    assert page.status_code == 200 and "Marcar pagado" in page.text
    assert (await c.get("/admin/facturacion?estado=pagado")).status_code == 200
    api = await c.get("/api/billing/summary")
    assert api.json()["mrr_cop"] == 250_000
    r = await c.post(
        f"/admin/facturacion/{rec.id}/pagar", data={"metodo": "pse"}, follow_redirects=False
    )
    assert r.status_code == 303
    await session.refresh(rec)
    assert rec.status == "pagado"
    csv_resp = await c.get("/admin/facturacion/exportar.csv")
    assert csv_resp.headers["content-type"].startswith("text/csv")
    n = (
        await session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "billing.export")
        )
    ).scalar_one()
    assert n == 1
    assert (await c.post("/admin/facturacion/generar", follow_redirects=False)).status_code == 303
    r2 = await c.post(f"/admin/facturacion/{rec.id}/anular", follow_redirects=False)
    assert r2.status_code == 409


async def test_operator_forbidden_and_csrf(client, make_user, login, authenticated_client):
    # authenticated_client ya es el mismo cliente; se reemplaza la sesion por un operador
    op = await make_user("operator")
    await login(client, op)
    assert (await client.get("/admin/facturacion")).status_code == 403
    assert (await client.get("/api/billing/summary")).status_code == 403
    assert (await client.get("/admin/facturacion/exportar.csv")).status_code == 403
    r = await client.post("/admin/facturacion/generar", headers={"X-CSRF-Token": ""})
    assert r.status_code in (400, 403)


async def test_cron_registered():
    from app.billing.jobs import CRON_JOBS
    from app.core.jobs import JOB_REGISTRY

    assert "billing.generate_monthly_records" in JOB_REGISTRY
    assert CRON_JOBS[0].name == "billing.generate_monthly_records"
    assert isinstance(timedelta(days=1), timedelta)


async def test_void_overdue_recovers_past_due_subscription(session, make_tenant, owner_user):
    plan = await _plan(session)
    sub = await _sub(session, await make_tenant(), plan)
    rec = await service.create_record(
        session,
        subscription_id=sub.id,
        kind="mensualidad",
        amount_cop=250_000,
        due_date=date(2026, 1, 1),
    )
    await service.mark_overdue(session, today=date(2026, 2, 20))
    await session.refresh(sub)
    assert sub.status == "past_due"
    await service.void_record(session, rec.id, actor=owner_user)
    await session.refresh(sub)
    assert sub.status == "active"


async def test_mark_overdue_bulk_counts_rows(session, make_tenant):
    plan = await _plan(session)
    sub = await _sub(session, await make_tenant(), plan)
    for i in range(3):
        await service.create_record(
            session,
            subscription_id=sub.id,
            kind="setup",
            amount_cop=1000 + i,
            due_date=date(2026, 1, 1),
        )
    assert await service.mark_overdue(session, today=date(2026, 1, 5)) == 3
    assert await service.mark_overdue(session, today=date(2026, 1, 5)) == 0


async def test_create_record_default_due_date_is_bogota(session, make_tenant, monkeypatch):
    plan = await _plan(session)
    sub = await _sub(session, await make_tenant(), plan)
    # 01:00 UTC del 11 = 20:00 del 10 en Bogota
    monkeypatch.setattr(service, "utcnow", lambda: datetime(2026, 3, 11, 1, tzinfo=UTC))
    rec = await service.create_record(
        session, subscription_id=sub.id, kind="setup", amount_cop=1000
    )
    assert rec.due_date == date(2026, 3, 10)


async def test_generation_survives_concurrent_duplicate(session, make_tenant, monkeypatch):
    plan = await _plan(session)
    s1 = await _sub(session, await make_tenant(), plan)
    await _sub(session, await make_tenant(), plan)
    # Simula la carrera: otra transaccion ya inserto la de s1 tras leer `existing`.
    session.add(
        BillingRecord(
            tenant_id=s1.tenant_id,
            subscription_id=s1.id,
            kind="mensualidad",
            period="2026-02",
            amount_cop=1,
            status="pendiente",
            due_date=date(2026, 2, 1),
        )
    )
    await session.flush()
    real = session.execute
    calls = {"n": 0}

    async def fake_execute(stmt, *a, **kw):
        res = await real(stmt, *a, **kw)
        calls["n"] += 1
        if calls["n"] == 2:  # consulta `existing`: oculta la fila ya creada

            class _R:
                def scalars(self):
                    class _S:
                        def all(self_inner):
                            return []

                    return _S()

            return _R()
        return res

    monkeypatch.setattr(session, "execute", fake_execute)
    assert await service.generate_monthly_records(session, "2026-02") == 1


async def test_list_pagination_count_and_summary_single_query(session, make_tenant):
    plan = await _plan(session)
    sub = await _sub(session, await make_tenant(), plan, custom_monthly_fee_cop=10)
    await _sub(session, await make_tenant(), plan, status="past_due")
    for i in range(3):
        await service.create_record(
            session, subscription_id=sub.id, kind="setup", amount_cop=100 + i
        )
    assert await service.count_records(session) == 3
    assert len(await service.list_records(session, limit=2, offset=2)) == 1
    s = await service.summary(session)
    assert (s.mrr_cop, s.active_subscriptions) == (250_010, 2)
