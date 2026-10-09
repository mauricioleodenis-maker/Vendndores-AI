from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.jobs import ENQUEUED, run_enqueued
from app.db.models.audit import AuditLog
from app.db.models.tenants import Tenant, TenantSecret
from app.tenants import service
from app.tenants.schemas import ChannelIn, FaqIn, SecretIn, ServiceIn, TenantIn


def _data(**kw: Any) -> TenantIn:
    base = {"name": "Clínica Sonrisa Ñandú", "niche": "dentista", "city": "Cali"}
    return TenantIn(**{**base, **kw})


async def test_create_slug_unique_and_profile(session: AsyncSession, owner_user: Any) -> None:
    a = await service.create_tenant(session, _data(), actor=owner_user)
    b = await service.create_tenant(session, _data(), actor=owner_user)
    assert a.slug == "clinica-sonrisa-nandu"
    assert b.slug != a.slug and b.slug.startswith("clinica-sonrisa-nandu-")
    assert a.status == "draft"
    assert (await service.get_profile(session, a.id)).hours == {}
    actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert actions.count("tenant.create") == 2


async def test_list_filters_and_escape(session: AsyncSession, owner_user: Any) -> None:
    await service.create_tenant(
        session, _data(name="Taller 100%", niche="taller"), actor=owner_user
    )
    await service.create_tenant(session, _data(name="Dental Uno"), actor=owner_user)
    rows, more = await service.list_tenants(session, q="100%")
    assert [t.name for t in rows] == ["Taller 100%"] and not more
    rows, _ = await service.list_tenants(session, niche="dentista")
    assert len(rows) == 1
    rows, _ = await service.list_tenants(session, q="%")
    assert [t.name for t in rows] == ["Taller 100%"]
    rows, more = await service.list_tenants(session, page_size=1)
    assert len(rows) == 1 and more


async def test_update_status_delete(session: AsyncSession, owner_user: Any) -> None:
    t = await service.create_tenant(session, _data(), actor=owner_user)
    await service.update_tenant(
        session, t, _data(name="Nuevo Nombre", niche="taller"), actor=owner_user
    )
    assert t.name == "Nuevo Nombre" and t.niche == "taller"
    with pytest.raises(ConflictError):
        await service.set_status(session, t, "paused", actor=owner_user)
    t.status = "active"
    await service.set_status(session, t, "paused", actor=owner_user)
    assert t.status == "paused"
    await service.delete_tenant(session, t, actor=owner_user)
    with pytest.raises(NotFoundError):
        await service.get_tenant(session, t.id)
    with pytest.raises(NotFoundError):
        await service.get_tenant(session, uuid.uuid4())


async def test_profile_update(session: AsyncSession, owner_user: Any) -> None:
    t = await service.create_tenant(session, _data(), actor=owner_user)
    p = await service.update_profile(
        session, t, hours={"mon": [["09:00", "18:00"]]}, tone="cercano",
        handoff_phone="+573001112233", set_handoff=True, actor=owner_user,
    )  # fmt: skip
    assert p.hours["mon"] == [["09:00", "18:00"]] and p.handoff_phone == "+573001112233"
    orphan = uuid.uuid4()
    other = await service.get_profile(session, t.id)
    assert other is p and orphan != t.id


async def test_services_crud_and_isolation(
    session: AsyncSession, owner_user: Any, make_tenant: Any
) -> None:
    t1, t2 = await make_tenant(), await make_tenant()
    s = await service.add_service(
        session, t1.id, ServiceIn(name="Limpieza", price_cop=80000), actor=owner_user
    )
    s.needs_review = True
    await service.update_service(
        session, t1.id, s.id, ServiceIn(name="Limpieza dental", price_cop=90000), actor=owner_user
    )
    assert s.price_cop == 90000 and s.needs_review is False
    with pytest.raises(NotFoundError):  # IDOR: otro tenant
        await service.update_service(session, t2.id, s.id, ServiceIn(name="x"), actor=owner_user)
    with pytest.raises(NotFoundError):
        await service.delete_service(session, t2.id, s.id, actor=owner_user)
    assert await service.list_services(session, t2.id) == []
    await service.replace_services(
        session, t1.id, [ServiceIn(name="A"), ServiceIn(name="B", price_cop=None)], actor=owner_user
    )
    assert [x.name for x in await service.list_services(session, t1.id)] == ["A", "B"]
    await service.delete_service(
        session, t1.id, (await service.list_services(session, t1.id))[0].id, actor=owner_user
    )
    with pytest.raises(AppError):
        await service.replace_services(session, t1.id, [ServiceIn(name="x")] * 61, actor=owner_user)


async def test_faqs_crud(session: AsyncSession, owner_user: Any, make_tenant: Any) -> None:
    t, other = await make_tenant(), await make_tenant()
    f = await service.add_faq(
        session, t.id, FaqIn(question="¿Dónde?", answer="Cali"), actor=owner_user
    )
    await service.update_faq(
        session,
        t.id,
        f.id,
        FaqIn(question="¿Dónde están?", answer="Cali", is_active=False),
        actor=owner_user,
    )
    assert f.is_active is False and f.source == "manual"
    with pytest.raises(NotFoundError):
        await service.delete_faq(session, other.id, f.id, actor=owner_user)
    await service.delete_faq(session, t.id, f.id, actor=owner_user)
    assert await service.list_faqs(session, t.id) == []


async def test_secrets_encrypted_roundtrip_and_rotate(
    session: AsyncSession, owner_user: Any, make_tenant: Any
) -> None:
    t, other = await make_tenant(), await make_tenant()
    meta = await service.set_secret(
        session, t.id, SecretIn(kind="twilio_auth_token", value="supersecret1234"), actor=owner_user
    )
    assert meta.last4 == "1234"
    row = (await session.execute(select(TenantSecret))).scalar_one()
    assert b"supersecret" not in row.ciphertext
    assert await service.get_secret_value(session, t.id, "twilio_auth_token") == "supersecret1234"
    assert await service.get_secret_value(session, other.id, "twilio_auth_token") is None
    await service.set_secret(
        session, t.id, SecretIn(kind="twilio_auth_token", value="otro-valor-9999"), actor=owner_user
    )
    assert (await service.list_secret_meta(session, t.id))[0].last4 == "9999"
    assert await service.get_secret_value(session, t.id, "twilio_auth_token") == "otro-valor-9999"
    # el valor nunca llega a la auditoria
    diffs = (await session.execute(select(AuditLog.diff))).scalars().all()
    assert "supersecret" not in str(diffs) and "9999" not in str(diffs)
    await service.delete_secret(session, t.id, "twilio_auth_token", actor=owner_user)
    with pytest.raises(NotFoundError):
        await service.delete_secret(session, t.id, "twilio_auth_token", actor=owner_user)


async def test_channels_unique_phone(
    session: AsyncSession, owner_user: Any, make_tenant: Any
) -> None:
    t1, t2 = await make_tenant(), await make_tenant()
    c = await service.add_channel(
        session, t1.id, ChannelIn(phone_e164="+573001234567"), actor=owner_user
    )
    with pytest.raises(ConflictError):
        await service.add_channel(
            session, t2.id, ChannelIn(phone_e164="+573001234567"), actor=owner_user
        )
    c2 = await service.add_channel(
        session, t2.id, ChannelIn(phone_e164="+573009999999"), actor=owner_user
    )
    with pytest.raises(ConflictError):
        await service.update_channel(
            session, t2.id, c2.id, ChannelIn(phone_e164="+573001234567"), actor=owner_user
        )
    await service.update_channel(
        session,
        t1.id,
        c.id,
        ChannelIn(phone_e164="+573001234567", voice_enabled=True),
        actor=owner_user,
    )
    assert c.voice_enabled
    with pytest.raises(NotFoundError):
        await service.delete_channel(session, t2.id, c.id, actor=owner_user)
    await service.delete_channel(session, t1.id, c.id, actor=owner_user)
    assert await service.list_channels(session, t1.id) == []
    with pytest.raises(NotFoundError):
        await service.update_channel(
            session, t1.id, uuid.uuid4(), ChannelIn(phone_e164="+573001234567"), actor=owner_user
        )


async def test_generation_success(
    session: AsyncSession, owner_user: Any, fake_build_bot: list[Any]
) -> None:
    t = await service.create_tenant(session, _data(website_url="https://x.com"), actor=owner_user)
    await service.replace_services(
        session, t.id, [ServiceIn(name="Limpieza", price_cop=1000)], actor=owner_user
    )
    with pytest.raises(AppError):
        await service.start_generation(session, t, actor=owner_user, consent=False)
    await service.start_generation(session, t, actor=owner_user, notes="hola", consent=True)
    assert t.status == "building" and t.dpa_accepted_at is not None
    assert ENQUEUED[0][0] == service.GENERATE_JOB
    with pytest.raises(ConflictError):
        await service.start_generation(session, t, actor=owner_user, consent=True)
    assert await run_enqueued() == 1
    assert len(fake_build_bot) == 1
    _tid, inputs, kw = fake_build_bot[0]
    assert inputs.services[0]["name"] == "Limpieza" and inputs.notes == "hola" and kw["scrape"]
    await session.refresh(t)
    assert t.status == "review"


async def test_generation_failure_returns_to_draft(
    session: AsyncSession, owner_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.factory import service as factory_service

    async def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("fallo")

    monkeypatch.setattr(factory_service, "build_bot", boom)
    t = await service.create_tenant(session, _data(), actor=owner_user)
    await service.start_generation(session, t, actor=owner_user, consent=True)
    await run_enqueued()
    await session.refresh(t)
    assert t.status == "draft"
    actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert "tenant.generate_failed" in actions


async def test_tenant_model_slug_helper() -> None:
    assert service.slugify("  ¡Hola Mundo! ") == "hola-mundo"
    assert service.slugify("###") == "empresa"
    assert isinstance(Tenant.__tablename__, str)


async def test_start_generation_claim_is_atomic(
    session: AsyncSession, owner_user: Any, fake_build_bot: list[Any]
) -> None:
    """Regresion: si otra peticion ya paso a ``building``, no se encola de nuevo."""
    from sqlalchemy import update

    from app.db.models.tenants import Tenant

    t = await service.create_tenant(session, _data(), actor=owner_user)
    await session.execute(update(Tenant).where(Tenant.id == t.id).values(status="building"))
    before = len(ENQUEUED)
    with pytest.raises(ConflictError):
        await service.start_generation(session, t, actor=owner_user, consent=True)
    assert len(ENQUEUED) == before
