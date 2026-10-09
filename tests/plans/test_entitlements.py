from __future__ import annotations

from typing import Any

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.booking import CalendarConnection
from app.db.models.catalog import Service
from app.db.models.plans import UsageCounter
from app.plans import service
from app.plans.entitlements import (
    Entitlements,
    PlanLimitExceeded,
    assert_within_limit,
    current_period,
    get_entitlements,
    record_usage,
)

pytestmark = pytest.mark.usefixtures("seeded")


async def test_no_subscription_defaults_to_basico(session: AsyncSession, tenant: Any) -> None:
    ent = await get_entitlements(session, tenant.id)
    assert ent.plan_code == "basico" and ent.active and not ent.has_subscription
    assert ent.limit("conversations") == 500
    assert not ent.has_feature("followups")


async def test_plan_limits_follow_subscription(session: AsyncSession, tenant: Any) -> None:
    await service.create_subscription(session, tenant.id, "premium")
    ent = await get_entitlements(session, tenant.id)
    assert ent.plan_code == "premium" and ent.status == "active"
    assert ent.limit("conversations") == 4000
    assert ent.limit("services") is None
    assert ent.has_feature("followups") and ent.has_feature("voice")


async def test_record_usage_accumulates(session: AsyncSession, tenant: Any) -> None:
    await record_usage(session, tenant.id, conversations=1, messages_in=2, tokens_in=100)
    await record_usage(session, tenant.id, messages_out=3, tokens_out=50, conversations=1)
    await record_usage(session, tenant.id)  # no-op
    row = (await session.execute(select(UsageCounter))).scalar_one()
    assert (row.conversations, row.messages_in, row.messages_out) == (2, 2, 3)
    assert (row.llm_tokens_in, row.llm_tokens_out) == (100, 50)
    ent = await get_entitlements(session, tenant.id)
    assert ent.usage["conversations"] == 2 and ent.period == current_period()


async def test_negative_usage_rejected(session: AsyncSession, tenant: Any) -> None:
    with pytest.raises(ValueError):
        await record_usage(session, tenant.id, conversations=-1)


async def test_conversation_limit_enforced(session: AsyncSession, tenant: Any) -> None:
    await assert_within_limit(session, tenant.id, "conversations")
    await record_usage(session, tenant.id, conversations=499)
    await assert_within_limit(session, tenant.id, "conversations")
    await record_usage(session, tenant.id, conversations=1)
    with pytest.raises(PlanLimitExceeded) as exc:
        await assert_within_limit(session, tenant.id, "conversations")
    assert exc.value.metric == "conversations" and exc.value.status == 402


async def test_usage_resets_each_month(session: AsyncSession, tenant: Any) -> None:
    with freeze_time("2026-03-15 12:00:00"):
        await record_usage(session, tenant.id, conversations=500)
        with pytest.raises(PlanLimitExceeded):
            await assert_within_limit(session, tenant.id, "conversations")
    with freeze_time("2026-04-02 12:00:00"):
        await assert_within_limit(session, tenant.id, "conversations")
        assert current_period() == "2026-04"


async def test_period_uses_bogota_month(session: AsyncSession) -> None:
    with freeze_time("2026-05-01 03:00:00"):  # 22:00 del 30-abr en Bogota
        assert current_period() == "2026-04"


async def test_soft_limit_warning(session: AsyncSession, tenant: Any) -> None:
    await record_usage(session, tenant.id, conversations=399)
    assert not (await get_entitlements(session, tenant.id)).near_limit("conversations")
    await record_usage(session, tenant.id, conversations=1)
    ent = await get_entitlements(session, tenant.id)
    assert ent.near_limit("conversations") and ent.usage_pct("conversations") == 80


async def test_services_limit_counts_active_only(session: AsyncSession, tenant: Any) -> None:
    for i in range(10):
        session.add(Service(tenant_id=tenant.id, name=f"S{i}", is_active=i != 0))
    await session.flush()
    await assert_within_limit(session, tenant.id, "services")  # 9 activos de 10
    session.add(Service(tenant_id=tenant.id, name="extra"))
    await session.flush()
    with pytest.raises(PlanLimitExceeded):
        await assert_within_limit(session, tenant.id, "services")


async def test_calendars_limit_ignores_revoked(session: AsyncSession, tenant: Any) -> None:
    session.add(CalendarConnection(tenant_id=tenant.id, provider="local", status="revoked"))
    await session.flush()
    await assert_within_limit(session, tenant.id, "calendars")
    session.add(CalendarConnection(tenant_id=tenant.id, provider="local", status="active"))
    await session.flush()
    with pytest.raises(PlanLimitExceeded):
        await assert_within_limit(session, tenant.id, "calendars")


async def test_unlimited_metric_always_passes(session: AsyncSession, tenant: Any) -> None:
    await service.create_subscription(session, tenant.id, "premium")
    for i in range(15):
        session.add(Service(tenant_id=tenant.id, name=f"S{i}"))
    await session.flush()
    await assert_within_limit(session, tenant.id, "services")


async def test_cancelled_subscription_blocks(session: AsyncSession, tenant: Any) -> None:
    await service.create_subscription(session, tenant.id, "pro")
    await service.set_status(session, tenant.id, "cancelled")
    ent = await get_entitlements(session, tenant.id)
    assert not ent.active and ent.status == "cancelled"
    with pytest.raises(PlanLimitExceeded):
        await assert_within_limit(session, tenant.id, "conversations")


async def test_past_due_still_serves(session: AsyncSession, tenant: Any) -> None:
    await service.create_subscription(session, tenant.id, "pro")
    await service.set_status(session, tenant.id, "past_due")
    await assert_within_limit(session, tenant.id, "conversations")


async def test_unknown_metric(session: AsyncSession, tenant: Any) -> None:
    with pytest.raises(ValueError):
        await assert_within_limit(session, tenant.id, "magia")


def test_entitlements_helpers_without_db() -> None:
    ent = Entitlements(limits={"max_conversations_month": 0, "export_csv": True, "x": True})
    assert ent.usage_pct("conversations") is None
    assert ent.limit("x") is None  # bool no cuenta como tope
    assert ent.has_feature("export_csv") and not ent.has_feature("voice")


async def test_cancelled_only_reports_inactive_and_live_wins(
    session: AsyncSession, tenant: Any
) -> None:
    await service.create_subscription(session, tenant.id, "premium")
    await service.set_status(session, tenant.id, "cancelled")
    ent = await get_entitlements(session, tenant.id)
    assert not ent.active and ent.status == "cancelled"
    await service.create_subscription(session, tenant.id, "basico")
    ent = await get_entitlements(session, tenant.id)
    assert ent.active and ent.plan_code == "basico"


async def test_get_plan_seeds_lazily_and_unknown_is_404(session: AsyncSession) -> None:
    from app.core.errors import NotFoundError

    plan = await service.get_plan(session, "basico")
    assert plan.code == "basico"
    with pytest.raises(NotFoundError):
        await service.get_plan(session, "no-existe")
