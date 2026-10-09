from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app.db.models.audit import AuditLog
from app.db.models.payments import PaymentIntent
from app.db.models.plans import BillingRecord, Plan, Subscription
from app.payments.client import WompiError
from app.payments.router import get_wompi_client, router
from app.payments.schemas import PaymentLink
from app.payments.settings import WompiSettings, get_wompi_settings

pytestmark = pytest.mark.asyncio


class FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple] = []

    async def create_payment_link(self, *args):
        self.calls.append(args)
        if self.fail:
            raise WompiError("boom")
        return PaymentLink(id="lnk1", url="https://checkout.wompi.co/l/lnk1")

    def integrity_signature(self, reference, amount_in_cents, currency="COP"):
        return "sig"


@pytest.fixture(autouse=True)
def _setup(app):
    app.include_router(router)
    app.dependency_overrides[get_wompi_settings] = lambda: WompiSettings(
        public_key="pub", private_key="priv"
    )


@pytest.fixture
def fake(app):
    f = FakeClient()
    app.dependency_overrides[get_wompi_client] = lambda: f
    return f


@pytest.fixture
async def record(session, make_tenant):
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
    await session.commit()
    return rec


async def test_list_requires_auth(client):
    assert (await client.get("/admin/pagos")).status_code == 401


async def test_list_shows_payable_and_intents(authenticated_client, record, fake):
    r = await authenticated_client.get("/admin/pagos")
    assert r.status_code == 200
    assert "Generar link de pago" in r.text
    assert "2026-02" in r.text


async def test_generate_link_and_listing(authenticated_client, record, fake, session):
    r = await authenticated_client.post(f"/admin/pagos/generar/{record.id}", data={"email": ""})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    intent = (await session.execute(select(PaymentIntent))).scalar_one()
    assert intent.link_url == "https://checkout.wompi.co/l/lnk1"
    assert (
        (await session.execute(select(AuditLog).where(AuditLog.action == "payments.link_created")))
        .scalars()
        .first()
    )
    page = (await authenticated_client.get("/admin/pagos")).text
    assert "https://checkout.wompi.co/l/lnk1" in page
    assert "Pendiente" in page and "data-copy" in page


async def test_generate_requires_csrf(authenticated_client, record, fake):
    r = await authenticated_client.post(
        f"/admin/pagos/generar/{record.id}", headers={"X-CSRF-Token": ""}
    )
    assert r.status_code == 403
    assert fake.calls == []


async def test_generate_forbidden_for_operator(client, login, make_user, record, fake):
    await login(client, await make_user("operator"))
    r = await client.post(f"/admin/pagos/generar/{record.id}")
    assert r.status_code == 403


async def test_generate_wompi_error_flash(authenticated_client, record, app):
    app.dependency_overrides[get_wompi_client] = lambda: FakeClient(fail=True)
    r = await authenticated_client.post(f"/admin/pagos/generar/{record.id}")
    assert r.status_code == 303 and "error=" in r.headers["location"]


async def test_generate_not_configured(authenticated_client, record, app):
    app.dependency_overrides[get_wompi_client] = lambda: None
    r = await authenticated_client.post(f"/admin/pagos/generar/{record.id}")
    assert "error=" in r.headers["location"]


async def test_return_page_public_and_safe(client, session, record):
    session.add(
        PaymentIntent(
            billing_record_id=record.id,
            reference="R1",
            amount_cop=5000,
            status="approved",
            provider_tx_id="tx-1",
        )
    )
    await session.commit()
    r = await client.get("/pagos/retorno?id=tx-1")
    assert r.status_code == 200 and "Pago recibido" in r.text
    assert "5.000" not in r.text and "R1" not in r.text
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("q", ["", "?id=nope", "?id=<script>alert(1)</script>"])
async def test_return_page_unknown_is_pending(client, q):
    r = await client.get(f"/pagos/retorno{q}")
    assert r.status_code == 200
    assert "confirmando" in r.text and "<script>alert" not in r.text
