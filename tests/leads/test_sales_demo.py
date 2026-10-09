from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rate_limit
from app.core.config import get_settings
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models.bots import BotConfig
from app.db.models.leads import LeadEvent
from app.db.models.plans import BillingRecord, Subscription
from app.db.models.tenants import Tenant
from app.leads import demo
from app.plans import catalog
from tests.leads.test_sales_pipeline import make_lead


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"build": [], "chat": []}

    async def fake_build(session: AsyncSession, tenant_id: uuid.UUID, inputs: Any, **kw: Any):
        calls["build"].append((inputs, kw))
        n = len((await session.execute(select(BotConfig))).scalars().all()) + 1
        bot = BotConfig(tenant_id=tenant_id, version=n, status="draft")
        session.add(bot)
        await session.flush()
        return bot

    async def fake_sandbox(session: Any, tenant_id: Any, history: Any, text: str, **kw: Any):
        calls["chat"].append((history, text))
        return "Hola, soy el asistente"

    monkeypatch.setattr(demo, "build_bot", fake_build)
    monkeypatch.setattr(demo, "sandbox_reply", fake_sandbox)
    rate_limit.set_rate_limiter(rate_limit.MemoryRateLimiter())
    return calls


@pytest_asyncio.fixture
async def seeded(session: AsyncSession) -> None:
    await catalog.seed(session)
    await session.commit()


def test_slugify() -> None:
    assert demo.slugify("Clínica Sonrisa & Co.") == "clinica-sonrisa-co"
    assert demo.slugify("!!!") == "negocio"


def test_token_roundtrip_and_tamper() -> None:
    tid = uuid.uuid4()
    assert demo.read_demo_token(demo.make_demo_token(tid)) == tid
    with pytest.raises(NotFoundError):
        demo.read_demo_token("basura")
    bad = URLSafeTimedSerializer("otra-clave-distinta", salt="vai-demo-chat-v1").dumps({"t": "x"})
    with pytest.raises(NotFoundError):
        demo.read_demo_token(bad)
    wrong_payload = URLSafeTimedSerializer(
        get_settings().secret_key.get_secret_value(), salt="vai-demo-chat-v1"
    ).dumps({"t": "no-uuid"})
    with pytest.raises(NotFoundError):
        demo.read_demo_token(wrong_payload)


def test_token_expires() -> None:
    from freezegun import freeze_time

    token = demo.make_demo_token(uuid.uuid4())
    days = get_settings().demo_token_ttl_days
    with freeze_time(datetime.now(UTC) + timedelta(days=days + 1)), pytest.raises(NotFoundError):
        demo.read_demo_token(token)


async def test_lead_to_demo_bot_creates_everything(
    session: AsyncSession, owner_user: Any, fakes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "demo_whatsapp_number", "+573000000000")
    lead = await make_lead(
        session,
        phone_e164="+573001112233",
        website="https://sonrisa.co",
        rating=4.2,
        review_count=30,
    )
    res = await demo.lead_to_demo_bot(session, lead.id, actor=owner_user, scrape=False)
    tenant = await session.get(Tenant, res.tenant_id)
    assert tenant and tenant.is_demo and tenant.status == "draft"
    assert tenant.slug == "clinica-sonrisa" and tenant.phone_contact == "+573001112233"
    assert lead.stage == "demo" and lead.demo_tenant_id == tenant.id
    assert "/demo/" in res.demo_link
    assert res.whatsapp_link == "https://wa.me/573000000000?text=DEMO-clinica-sonrisa"
    inputs, kw = fakes["build"][0]
    assert inputs.niche == "dentista" and "4.2" in inputs.notes and kw["scrape"] is False
    kinds = {e.kind for e in (await session.execute(select(LeadEvent))).scalars()}
    assert "demo_created" in kinds
    # idempotente
    again = await demo.lead_to_demo_bot(session, lead.id, actor=owner_user)
    assert again.reused and again.tenant_id == tenant.id and len(fakes["build"]) == 1
    # rebuild genera nueva version en el mismo tenant
    rebuilt = await demo.lead_to_demo_bot(session, lead.id, actor=owner_user, rebuild=True)
    assert rebuilt.tenant_id == tenant.id and not rebuilt.reused and len(fakes["build"]) == 2


async def test_demo_requires_niche_and_valid_state(
    session: AsyncSession, owner_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    lead = await make_lead(session, niche="otro")
    with pytest.raises(AppError) as exc:
        await demo.lead_to_demo_bot(session, lead.id, actor=owner_user)
    assert exc.value.code == "niche_required"
    res = await demo.lead_to_demo_bot(session, lead.id, actor=owner_user, niche="taller")
    assert (await session.get(Tenant, res.tenant_id)).niche == "taller"  # type: ignore[union-attr]
    lost = await make_lead(session, name="Perdido", disposition="perdido")
    with pytest.raises(ConflictError):
        await demo.lead_to_demo_bot(session, lost.id, actor=owner_user)
    monkeypatch.setattr(get_settings(), "demo_enabled", False)
    with pytest.raises(AppError):
        await demo.lead_to_demo_bot(session, lead.id, actor=owner_user)


async def test_demo_from_prueba_secreta_stage(session: AsyncSession, owner_user: Any) -> None:
    lead = await make_lead(session, stage="prueba_secreta")
    await demo.lead_to_demo_bot(session, lead.id, actor=owner_user)
    assert lead.stage == "demo"


async def test_chat_reply_and_cap(
    session: AsyncSession, owner_user: Any, fakes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    lead = await make_lead(session)
    res = await demo.lead_to_demo_bot(session, lead.id, actor=owner_user)
    token = res.demo_link.rsplit("/", 1)[1]
    monkeypatch.setattr(get_settings(), "demo_max_messages", 2)
    hist = [{"role": "user", "content": "hola"}, {"role": "system", "content": "x"}, "basura"]
    assert await demo.demo_chat_reply(session, token, hist, " precio? ") == "Hola, soy el asistente"  # type: ignore[arg-type]
    assert fakes["chat"][0][0] == [{"role": "user", "content": "hola"}]
    await demo.demo_chat_reply(session, token, [], "otra")
    with pytest.raises(AppError) as exc:
        await demo.demo_chat_reply(session, token, [], "tercera")
    assert exc.value.status == 429
    with pytest.raises(AppError):
        await demo.demo_chat_reply(session, token, [], "   ")


def test_clean_history_limits() -> None:
    assert demo.clean_history("x") == []
    big = [{"role": "user", "content": "a" * 900}] * 30
    out = demo.clean_history(big)
    assert len(out) == demo.MAX_HISTORY and len(out[0]["content"]) == demo.MAX_TEXT


async def test_load_demo_tenant_rejects_real_tenant(session: AsyncSession, tenant: Tenant) -> None:
    with pytest.raises(NotFoundError):
        await demo.load_demo_tenant(session, demo.make_demo_token(tenant.id))
    with pytest.raises(NotFoundError):
        await demo.load_demo_tenant(session, demo.make_demo_token(uuid.uuid4()))


@pytest.mark.usefixtures("seeded")
async def test_convert_lead(session: AsyncSession, owner_user: Any) -> None:
    lead = await make_lead(session)
    with pytest.raises(ConflictError):
        await demo.convert_lead(session, lead.id, plan_code="pro", actor=owner_user)
    res = await demo.lead_to_demo_bot(session, lead.id, actor=owner_user)
    out = await demo.convert_lead(session, lead.id, plan_code="pro", actor=owner_user)
    assert out.billing_record_created and out.setup_net_cop == 1_200_000
    tenant = await session.get(Tenant, res.tenant_id)
    assert tenant and not tenant.is_demo and tenant.status == "building"
    assert lead.stage == "cerrado" and lead.converted_tenant_id == tenant.id
    rec = (await session.execute(select(BillingRecord))).scalar_one()
    assert rec.kind == "setup" and rec.status == "pendiente"
    assert (await session.execute(select(Subscription))).scalar_one().tenant_id == tenant.id
    kinds = {e.kind for e in (await session.execute(select(LeadEvent))).scalars()}
    assert "converted" in kinds
    with pytest.raises(ConflictError):
        await demo.convert_lead(session, lead.id, plan_code="pro", actor=owner_user)
    with pytest.raises(NotFoundError):  # el enlace de demo deja de servir
        await demo.load_demo_tenant(session, demo.make_demo_token(tenant.id))
    with pytest.raises(ConflictError):
        await demo.lead_to_demo_bot(session, lead.id, actor=owner_user)


@pytest.mark.usefixtures("seeded")
async def test_convert_founder_offer_no_setup_record(
    session: AsyncSession, owner_user: Any
) -> None:
    lead = await make_lead(session)
    await demo.lead_to_demo_bot(session, lead.id, actor=owner_user)
    out = await demo.convert_lead(
        session, lead.id, plan_code="basico", offer_code="fundador", actor=owner_user
    )
    assert out.setup_net_cop == 0 and not out.billing_record_created


async def test_convert_blocked_for_inactive(session: AsyncSession, owner_user: Any) -> None:
    lead = await make_lead(session, disposition="no_contactar")
    with pytest.raises(ConflictError):
        await demo.convert_lead(session, lead.id, plan_code="pro", actor=owner_user)


async def test_demo_blocked_for_do_not_contact(session: AsyncSession, owner_user: Any) -> None:
    lead = await make_lead(session, disposition="no_contactar")
    with pytest.raises(ConflictError):
        await demo.lead_to_demo_bot(session, lead.id, actor=owner_user)


async def test_demo_uses_lead_instructions(
    session: AsyncSession, owner_user: Any, fakes: dict[str, Any]
) -> None:
    from app.leads import listing

    lead = await make_lead(session)
    await listing.set_demo_instructions(
        session, lead, "  Resalta el blanqueamiento. Tono de tú.  ", owner_user
    )
    assert lead.demo_instructions == "Resalta el blanqueamiento. Tono de tú."
    await demo.lead_to_demo_bot(session, lead.id, actor=owner_user, scrape=False)
    inputs, _ = fakes["build"][0]
    assert inputs.instructions == "Resalta el blanqueamiento. Tono de tú."
