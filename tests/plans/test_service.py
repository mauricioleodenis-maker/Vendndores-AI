from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models.audit import AuditLog
from app.db.models.plans import BillingRecord, Plan
from app.plans import service
from app.plans.offers import OfferUnavailableError, get_offer

pytestmark = pytest.mark.usefixtures("seeded")


def test_add_months_clamps_day() -> None:
    assert service.add_months(datetime(2026, 1, 31, tzinfo=UTC), 1) == datetime(
        2026, 2, 28, tzinfo=UTC
    )
    assert service.add_months(datetime(2028, 1, 31, tzinfo=UTC), 1) == datetime(
        2028, 2, 29, tzinfo=UTC
    )
    assert service.add_months(datetime(2026, 12, 15, tzinfo=UTC), 1) == datetime(
        2027, 1, 15, tzinfo=UTC
    )


async def test_list_public_plans_seeds_when_empty(engine: Any, session: AsyncSession) -> None:
    await session.execute(Plan.__table__.delete())  # type: ignore[attr-defined]
    plans = await service.list_public_plans(session)
    assert [p.code for p in plans] == ["basico", "pro", "premium"]
    plans[0].is_public = False
    await session.flush()
    assert len(await service.list_public_plans(session)) == 2


async def test_get_plan_missing(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await service.get_plan(session, "diamante")


async def test_create_subscription_basic(
    session: AsyncSession, tenant: Any, owner_user: Any
) -> None:
    with freeze_time("2026-06-10 15:00:00"):
        out = await service.create_subscription(
            session, tenant.id, "basico", actor=owner_user, include_iva=True
        )
    sub = out.subscription
    assert sub.status == "active" and sub.guarantee_status == "vigente"
    assert sub.current_period_end == datetime(2026, 7, 10, 15, 0, tzinfo=UTC)
    assert sub.guarantee_until == (datetime(2026, 6, 10, tzinfo=UTC) + timedelta(days=30)).date()
    assert out.quote.total_first_invoice_cop == int((800_000 + 250_000) * 1.19)
    rec = (await session.execute(select(BillingRecord))).scalar_one()
    assert (rec.kind, rec.status, rec.amount_cop, rec.iva_cop) == (
        "setup",
        "pendiente",
        800_000,
        152_000,
    )
    audit = (
        await session.execute(select(AuditLog).where(AuditLog.action == "subscription.create"))
    ).scalar_one()
    assert audit.tenant_id == tenant.id


async def test_founder_offer_zero_setup_no_billing(session: AsyncSession, tenant: Any) -> None:
    out = await service.create_subscription(session, tenant.id, "pro", offer_code="fundador")
    assert out.quote.setup_net_cop == 0
    assert out.subscription.notes == "fundador"
    assert (await session.execute(select(BillingRecord))).first() is None
    assert (await get_offer(session, "fundador")).redeemed == 1


async def test_sixth_founder_is_rejected(session: AsyncSession, make_tenant: Any) -> None:
    for _ in range(5):
        t = await make_tenant()
        await service.create_subscription(session, t.id, "basico", offer_code="fundador")
    t6 = await make_tenant()
    with pytest.raises(OfferUnavailableError):
        await service.create_subscription(session, t6.id, "basico", offer_code="fundador")
    assert await service.get_live_subscription(session, t6.id) is None


async def test_duplicate_and_unknown_tenant(session: AsyncSession, tenant: Any) -> None:
    await service.create_subscription(session, tenant.id, "basico")
    with pytest.raises(ConflictError):
        await service.create_subscription(session, tenant.id, "pro")
    with pytest.raises(NotFoundError):
        await service.create_subscription(session, uuid.uuid4(), "pro")


async def test_quote_for_validates_offer_without_consuming(session: AsyncSession) -> None:
    q = await service.quote_for(session, "pro", "fundador")
    assert q.setup_net_cop == 0
    assert (await get_offer(session, "fundador")).redeemed == 0
    with pytest.raises(NotFoundError):
        await service.quote_for(session, "pro", "nope")
    offer = await get_offer(session, "fundador")
    offer.is_active = False
    with pytest.raises(AppError):
        await service.quote_for(session, "pro", "fundador")


async def test_change_plan(session: AsyncSession, tenant: Any, owner_user: Any) -> None:
    await service.create_subscription(session, tenant.id, "basico")
    sub = await service.change_plan(session, tenant.id, "premium", actor=owner_user)
    pro = await service.get_plan(session, "premium")
    assert sub.plan_id == pro.id
    with pytest.raises(ConflictError):
        await service.change_plan(session, tenant.id, "premium")
    sub = await service.change_plan(session, tenant.id, None, custom_monthly_fee_cop=500_000)
    assert sub.custom_monthly_fee_cop == 500_000


async def test_change_plan_without_subscription(session: AsyncSession, tenant: Any) -> None:
    with pytest.raises(NotFoundError):
        await service.change_plan(session, tenant.id, "pro")
    with pytest.raises(NotFoundError):
        await service.set_status(session, tenant.id, "cancelled")


async def test_cancel_within_guarantee_releases_founder_slot(
    session: AsyncSession, tenant: Any
) -> None:
    await service.create_subscription(session, tenant.id, "basico", offer_code="fundador")
    sub = await service.set_status(session, tenant.id, "cancelled")
    assert sub.status == "cancelled" and sub.guarantee_status == "reclamada"
    assert (await get_offer(session, "fundador")).redeemed == 0
    # tras cancelar se puede contratar de nuevo
    await service.create_subscription(session, tenant.id, "pro")


async def test_cancel_after_guarantee_expires(session: AsyncSession, tenant: Any) -> None:
    with freeze_time("2026-01-01"):
        await service.create_subscription(session, tenant.id, "basico", offer_code="fundador")
    with freeze_time("2026-03-01"):
        sub = await service.set_status(session, tenant.id, "cancelled")
    assert sub.guarantee_status == "vencida"
    assert (await get_offer(session, "fundador")).redeemed == 1


async def test_invalid_status(session: AsyncSession, tenant: Any) -> None:
    with pytest.raises(AppError):
        await service.set_status(session, tenant.id, "trial")
