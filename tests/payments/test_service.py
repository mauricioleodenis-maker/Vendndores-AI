from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select

from app.core.errors import ConflictError
from app.db.models.payments import PaymentIntent
from app.db.models.plans import BillingRecord, Plan, Subscription
from app.payments import WompiClient
from app.payments import service as svc

pytestmark = pytest.mark.asyncio
BASE = "https://sandbox.wompi.co/v1"


def client() -> WompiClient:
    return WompiClient(
        "pub", SecretStr("prv"), SecretStr("ev"), SecretStr("integ"), BASE, retry_backoff_s=0
    )


@pytest.fixture
async def record(session, make_tenant):
    t = await make_tenant(phone_contact="+573001112233", owner_name="Ana")
    plan = Plan(code="p", name="P", setup_fee_cop=1, monthly_fee_cop=1)
    session.add(plan)
    await session.flush()
    sub = Subscription(
        tenant_id=t.id,
        plan_id=plan.id,
        status="active",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(sub)
    await session.flush()
    rec = BillingRecord(
        tenant_id=t.id,
        subscription_id=sub.id,
        kind="mensualidad",
        period="2026-02",
        amount_cop=5000,
        status="pendiente",
        due_date=date(2026, 2, 28),
    )
    session.add(rec)
    await session.commit()
    return rec


def mock_link(router, link_id="lnk1"):
    return router.post(f"{BASE}/payment_links").mock(
        return_value=httpx.Response(200, json={"data": {"id": link_id}})
    )


async def test_create_payment_is_idempotent_and_unique_reference(session, record):
    with respx.mock(assert_all_called=False) as r:
        route = mock_link(r)
        a = await svc.create_payment_for_billing_record(
            session, client(), record.id, redirect_url="https://x/ok"
        )
        b = await svc.create_payment_for_billing_record(
            session, client(), record.id, redirect_url="https://x/ok"
        )
    assert a.id == b.id and route.call_count == 1
    assert a.status == "pending" and a.amount_cop == 5000 and a.link_url.endswith("lnk1")
    assert a.reference.startswith("vai-") and len(a.reference) <= 100
    sig = client().integrity_signature(a.reference, 500000)
    assert a.raw_last_event["integrity_signature"] == sig


async def test_new_attempt_after_declined_gets_new_reference(session, record):
    with respx.mock(assert_all_called=False) as r:
        mock_link(r)
        a = await svc.create_payment_for_billing_record(
            session, client(), record.id, redirect_url="https://x"
        )
        a.status = "declined"
        await session.flush()
        b = await svc.create_payment_for_billing_record(
            session, client(), record.id, redirect_url="https://x"
        )
    assert a.reference != b.reference


async def test_paid_record_rejected(session, record):
    record.status = "pagado"
    with pytest.raises(ConflictError):
        await svc.create_payment_for_billing_record(
            session, client(), record.id, redirect_url="https://x"
        )


async def _pending_intent(session, record, age_min=45, tx="tx1"):
    pi = PaymentIntent(
        billing_record_id=record.id,
        reference="REFX",
        amount_cop=5000,
        provider_tx_id=tx,
    )
    session.add(pi)
    await session.flush()
    pi.created_at = datetime.now(UTC) - timedelta(minutes=age_min)
    await session.commit()
    return pi


def tx_json(status="APPROVED", amount=500000, ref="REFX"):
    return {
        "data": {
            "id": "tx1",
            "reference": ref,
            "status": status,
            "amount_in_cents": amount,
            "payment_method_type": "NEQUI",
        }
    }


async def test_reconcile_marks_paid(session, record):
    pi = await _pending_intent(session, record)
    with respx.mock() as r:
        r.get(f"{BASE}/transactions/tx1").mock(return_value=httpx.Response(200, json=tx_json()))
        out = await svc.reconcile_pending(session, client())
    await session.refresh(pi)
    await session.refresh(record)
    assert out["approved"] == 1 and pi.status == "approved"
    assert record.status == "pagado" and record.method == "nequi"


async def test_reconcile_skips_recent_and_pending_and_mismatch(session, record):
    pi = await _pending_intent(session, record, age_min=5)
    with respx.mock(assert_all_called=False) as r:
        route = r.get(f"{BASE}/transactions/tx1").mock(
            return_value=httpx.Response(200, json=tx_json(amount=1))
        )
        out = await svc.reconcile_pending(session, client())
        assert out["checked"] == 0 and route.call_count == 0
        pi.created_at = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()
        out = await svc.reconcile_pending(session, client())
    await session.refresh(pi)
    await session.refresh(record)
    assert pi.status == "error" and record.status == "pendiente"


async def test_reconcile_gateway_error_counted(session, record):
    await _pending_intent(session, record)
    with respx.mock() as r:
        r.get(f"{BASE}/transactions/tx1").mock(return_value=httpx.Response(404))
        out = await svc.reconcile_pending(session, client())
    assert out["errors"] == 1


async def test_monthly_links_creates_and_sends_dry_run(session, record):
    with respx.mock(assert_all_called=False) as r:
        mock_link(r)
        out = await svc.create_links_for_new_monthly(
            session, client(), redirect_url="https://x", content_sid="HX123"
        )
        again = await svc.create_links_for_new_monthly(
            session, client(), redirect_url="https://x", content_sid="HX123"
        )
    assert out == {"created": 1, "sent": 1, "failed": 0}
    assert again["created"] == 0
    assert len((await session.execute(select(PaymentIntent))).scalars().all()) == 1


async def test_monthly_links_without_template_or_contact_does_not_send(
    session, record, make_tenant
):
    with respx.mock(assert_all_called=False) as r:
        mock_link(r)
        out = await svc.create_links_for_new_monthly(
            session, client(), redirect_url="https://x", content_sid=""
        )
    assert out["created"] == 1 and out["sent"] == 0


async def test_gateway_failure_does_not_abort_batch(session, record):
    with respx.mock() as r:
        r.post(f"{BASE}/payment_links").mock(return_value=httpx.Response(422))
        out = await svc.create_links_for_new_monthly(session, client(), redirect_url="https://x")
    assert out == {"created": 0, "sent": 0, "failed": 1}


async def test_jobs_registered_and_disabled_noop(monkeypatch):
    from app.core.jobs import JOB_REGISTRY
    from app.payments import jobs

    monkeypatch.delenv("VAI_WOMPI_PUBLIC_KEY", raising=False)
    assert "payments.monthly_links" in JOB_REGISTRY and "payments.reconcile_pending" in JOB_REGISTRY
    assert (await jobs.monthly_links({}))["created"] == 0
    assert (await jobs.reconcile_pending({}))["checked"] == 0
    assert len(jobs.CRON_JOBS) == 2
