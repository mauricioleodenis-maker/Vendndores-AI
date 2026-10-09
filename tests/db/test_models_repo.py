import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.base import Base
from app.db.models import (
    Appointment,
    BotConfig,
    Contact,
    Lead,
    Service,
    SuppressionEntry,
    Tenant,
    User,
)
from app.db.repo import TenantScopedRepo, tenant_select

EXPECTED_TABLES = {
    "users", "sessions", "audit_log", "plans", "offers", "subscriptions", "billing_records",
    "usage_counters", "niche_templates", "tenants", "tenant_profiles", "tenant_secrets",
    "channel_accounts", "services", "faqs", "kb_documents", "bot_configs", "contacts", "consents",
    "conversations", "messages", "handoffs", "appointments", "calendar_connections",
    "working_hours", "time_off", "scheduled_jobs", "webhook_events", "suppression_list",
    "lead_sources", "leads", "review_signals", "lead_events", "secret_shop_tests",
    "message_templates", "campaigns", "campaign_targets", "outreach_messages", "llm_usage",
}  # fmt: skip


def test_all_maestro_tables_exist():
    assert set(Base.metadata.tables) >= EXPECTED_TABLES


def test_tenant_tables_have_tenant_id_indexed():
    for name in ("services", "contacts", "messages", "appointments", "bot_configs", "faqs"):
        col = Base.metadata.tables[name].c.tenant_id
        assert not col.nullable and col.index


async def mk_tenants(session):
    a, b = Tenant(slug="a", name="A"), Tenant(slug="b", name="B")
    session.add_all([a, b])
    await session.flush()
    return a, b


async def test_repo_isolates_tenants(session):
    a, b = await mk_tenants(session)
    repo_a = TenantScopedRepo(session, Service, a.id)
    repo_b = TenantScopedRepo(session, Service, b.id)
    s = await repo_a.add(Service(name="Limpieza"))
    await repo_b.add(Service(name="Otra"))
    assert s.tenant_id == a.id
    assert await repo_b.get(s.id) is None
    assert [x.name for x in await repo_a.list()] == ["Limpieza"]
    assert await repo_a.count() == 1
    assert [
        x.name
        for x in await repo_a.list(Service.name == "Limpieza", order_by=Service.name, limit=5)
    ] == ["Limpieza"]
    assert len(await repo_a.list(order_by=[Service.name])) == 1
    assert len((await session.execute(tenant_select(Service, b.id))).scalars().all()) == 1


async def test_repo_rejects_foreign_objects_and_bad_models(session):
    a, b = await mk_tenants(session)
    repo_a = TenantScopedRepo(session, Service, a.id)
    with pytest.raises(PermissionError):
        await repo_a.add(Service(name="x", tenant_id=b.id))
    foreign = await TenantScopedRepo(session, Service, b.id).add(Service(name="y"))
    with pytest.raises(PermissionError):
        await repo_a.delete(foreign)
    with pytest.raises(TypeError):
        TenantScopedRepo(session, User, a.id)  # type: ignore[type-var]
    with pytest.raises(ValueError):
        TenantScopedRepo(session, Service, None)  # type: ignore[arg-type]
    mine = await repo_a.add(Service(name="z"))
    await repo_a.delete(mine)
    assert await repo_a.get(mine.id) is None


async def test_fk_enforced_for_tenant(session):
    session.add(Service(tenant_id=uuid.uuid4(), name="huerfano"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_enum_check_constraint(session):
    session.add(Tenant(slug="x", name="X", niche="inexistente"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_contact_unique_per_tenant(session):
    a, b = await mk_tenants(session)
    session.add_all(
        [Contact(tenant_id=a.id, phone_hash="h"), Contact(tenant_id=b.id, phone_hash="h")]
    )
    await session.flush()
    session.add(Contact(tenant_id=a.id, phone_hash="h"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_double_booking_blocked_but_cancelled_slot_reusable(session):
    a, _ = await mk_tenants(session)
    t0 = datetime(2026, 5, 4, 15, tzinfo=UTC)

    def appt(status, key):
        return Appointment(
            tenant_id=a.id,
            starts_at=t0,
            ends_at=t0.replace(hour=16),
            status=status,
            idempotency_key=key,
        )

    session.add(appt("confirmed", "k1"))
    await session.flush()
    session.add(appt("pending", "k2"))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_cancelled_does_not_block(session):
    a, _ = await mk_tenants(session)
    t0 = datetime(2026, 5, 4, 15, tzinfo=UTC)
    session.add_all(
        [
            Appointment(
                tenant_id=a.id, starts_at=t0, ends_at=t0, status="cancelled", idempotency_key="1"
            ),
            Appointment(
                tenant_id=a.id, starts_at=t0, ends_at=t0, status="confirmed", idempotency_key="2"
            ),
        ]
    )
    await session.flush()


async def test_single_published_bot_per_tenant(session):
    a, _ = await mk_tenants(session)
    session.add(BotConfig(tenant_id=a.id, version=1, status="published"))
    session.add(BotConfig(tenant_id=a.id, version=2, status="draft"))
    await session.flush()
    session.add(BotConfig(tenant_id=a.id, version=3, status="published"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_datetimes_roundtrip_aware_utc(session):
    a, _ = await mk_tenants(session)
    await session.commit()
    row = (await session.execute(select(Tenant).where(Tenant.id == a.id))).scalar_one()
    assert row.created_at.tzinfo is UTC
    with pytest.raises(Exception):
        session.add(Tenant(slug="naive", name="n", dpa_accepted_at=datetime(2026, 1, 1)))
        await session.flush()


async def test_lead_unique_active_phone_hash(session):
    session.add(Lead(name="A", phone_hash="h1"))
    await session.flush()
    session.add(Lead(name="B", phone_hash="h1"))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
    session.add(Lead(name="C", phone_hash="h1", disposition="perdido"))
    session.add(Lead(name="D"))
    session.add(Lead(name="E"))
    await session.flush()


async def test_suppression_unique_by_scope(session):
    session.add(SuppressionEntry(phone_hash="h", scope="global"))
    session.add(SuppressionEntry(phone_hash="h", scope="agency"))
    await session.flush()
    session.add(SuppressionEntry(phone_hash="h", scope="global"))
    with pytest.raises(IntegrityError):
        await session.flush()
