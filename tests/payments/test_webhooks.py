from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, date, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.db.models.audit import AuditLog
from app.db.models.payments import PaymentIntent
from app.db.models.plans import BillingRecord, Plan, Subscription
from app.payments.settings import WompiSettings, get_wompi_settings
from app.payments.webhooks import webhooks_router

pytestmark = pytest.mark.asyncio
SECRET = "evsecret"
URL = "/webhooks/wompi/events"


@pytest.fixture(autouse=True)
def _wompi(app):
    app.include_router(webhooks_router)
    app.dependency_overrides[get_wompi_settings] = lambda: WompiSettings(
        events_secret=SecretStr(SECRET)
    )


@pytest.fixture
async def intent(session, make_tenant):
    t = await make_tenant()
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
        iva_cop=0,
        status="pendiente",
        due_date=date(2026, 2, 28),
    )
    session.add(rec)
    await session.flush()
    pi = PaymentIntent(billing_record_id=rec.id, reference="REF1", amount_cop=5000)
    session.add(pi)
    await session.commit()
    session.expunge(pi)
    return pi


def make_event(status="APPROVED", amount=500000, ts=None, secret=SECRET, ref="REF1", tx="t1"):
    ts = int(time.time()) if ts is None else ts
    props = ["transaction.id", "transaction.status", "transaction.amount_in_cents"]
    checksum = hashlib.sha256(f"{tx}{status}{amount}{ts}{secret}".encode()).hexdigest()
    return {
        "event": "transaction.updated",
        "data": {
            "transaction": {
                "id": tx,
                "status": status,
                "amount_in_cents": amount,
                "reference": ref,
                "payment_method_type": "NEQUI",
            }
        },
        "timestamp": ts,
        "signature": {"properties": props, "checksum": checksum},
    }


async def post(client, ev):
    return await client.post(
        URL, content=json.dumps(ev), headers={"content-type": "application/json"}
    )


async def reload(session, pi):
    session.expire_all()
    rec = await session.get(BillingRecord, pi.billing_record_id)
    return await session.get(PaymentIntent, pi.id), rec


async def test_approved_marks_paid_and_audits(client, session, intent):
    r = await post(client, make_event())
    assert r.status_code == 200 and r.json()["status"] == "processed"
    pi, rec = await reload(session, intent)
    assert pi.status == "approved" and pi.provider_tx_id == "t1"
    assert rec.status == "pagado" and rec.method == "nequi"
    actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert "payments.wompi_event" in actions and "billing.mark_paid" in actions


async def test_bad_checksum_rejected(client, session, intent):
    ev = make_event()
    ev["signature"]["checksum"] = "0" * 64
    assert (await post(client, ev)).status_code == 401
    assert (await post(client, make_event(secret="otro"))).status_code == 401
    pi, rec = await reload(session, intent)
    assert pi.status == "pending" and rec.status == "pendiente"


async def test_tampered_amount_rejected(client, intent):
    ev = make_event()
    ev["data"]["transaction"]["amount_in_cents"] = 1
    assert (await post(client, ev)).status_code == 401


async def test_duplicate_is_idempotent(client, session, intent):
    ev = make_event()
    await post(client, ev)
    r = await post(client, ev)
    assert r.json()["status"] in {"duplicate", "ignored"}
    n = (
        (await session.execute(select(AuditLog).where(AuditLog.action == "billing.mark_paid")))
        .scalars()
        .all()
    )
    assert len(n) == 1


async def test_stale_and_future_timestamps(client, intent):
    assert (await post(client, make_event(ts=int(time.time()) - 90000))).status_code == 400
    assert (await post(client, make_event(ts=int(time.time()) + 3600))).status_code == 400


async def test_declined_does_not_pay_and_no_downgrade(client, session, intent):
    await post(client, make_event("DECLINED", ts=int(time.time()) - 5))
    pi, rec = await reload(session, intent)
    assert pi.status == "declined" and rec.status == "pendiente"
    await post(client, make_event("APPROVED"))
    await post(client, make_event("DECLINED", ts=int(time.time()) + 1))
    pi, rec = await reload(session, intent)
    assert pi.status == "approved" and rec.status == "pagado"


async def test_amount_mismatch_not_paid(client, session, intent):
    r = await post(client, make_event(amount=100))
    assert r.json()["status"] == "amount_mismatch"
    pi, rec = await reload(session, intent)
    assert pi.status == "error" and rec.status == "pendiente"


async def test_unknown_reference_and_other_event(client, intent):
    assert (await post(client, make_event(ref="NOPE"))).json()["status"] == "unknown_reference"
    ev = make_event()
    ev["event"] = "nequi_token.updated"
    assert (await post(client, ev)).json()["status"] == "ignored"


async def test_not_configured_and_malformed(client, app, intent):
    assert (await client.post(URL, content="nope")).status_code == 400
    assert (await client.post(URL, json=[1])).status_code == 401
    app.dependency_overrides[get_wompi_settings] = lambda: WompiSettings()
    assert (await post(client, make_event())).status_code == 503
