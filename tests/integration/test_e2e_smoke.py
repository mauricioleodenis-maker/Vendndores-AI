"""Humo de punta a punta: wizard -> bot -> publicar -> WhatsApp firmado -> cita; CSV -> score -> demo."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.channels.jobs  # noqa: F401
import app.conversation.engine as engine_mod
import app.factory.service as factory_service
from app.ai.client import FakeLLM, LLMResponse, ToolCall
from app.booking.service import BookingService
from app.channels.sender import sign_twilio
from app.core import jobs as core_jobs
from app.core.clock import to_bogota, utcnow
from app.core.config import get_settings
from app.db.models.booking import Appointment
from app.db.models.catalog import Service
from app.db.models.conversations import Message
from app.db.models.leads import Lead
from app.db.models.tenants import ChannelAccount, Tenant
from app.db.models.users import User
from app.factory.service import publish_bot
from tests.factory.conftest import tool_response, valid_config
from tests.leads.test_router import CSV, csv_files

H = {"HX-Request": "true"}
BIZ = "+576015550199"
CUSTOMER = "+573001234567"
TOKEN = "platform-token-e2e"


@pytest.fixture(autouse=True)
def _twilio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAI_TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("VAI_TWILIO_ACCOUNT_SID", "ACplatform")
    monkeypatch.setenv("VAI_TWILIO_WHATSAPP_FROM", "+15550001111")
    get_settings.cache_clear()


def _tc(name: str, **args: Any) -> LLMResponse:
    return LLMResponse(text="", tool_calls=[ToolCall("t1", name, args)], stop_reason="tool_use")


async def test_wizard_to_booking(
    authenticated_client: httpx.AsyncClient,
    session: AsyncSession,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = authenticated_client
    uid = c.user.id  # type: ignore[attr-defined]
    monkeypatch.setattr(factory_service, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(engine_mod, "get_llm", lambda: fake_llm)
    # 1) wizard
    r = await c.post(
        "/admin/negocios/nuevo/paso1",
        data={"name": "Clinica Demo", "niche": "dentista", "city": "Cali",
              "phone_contact": CUSTOMER, "website_url": "", "instagram_url": ""},
        headers=H,
    )  # fmt: skip
    tid = re.search(r"/admin/negocios/([0-9a-f-]{36})/paso2", r.text).group(1)  # type: ignore[union-attr]
    step2 = {
        "service_name": ["Limpieza dental"],
        "service_price": ["99.000"],
        "service_duration": ["45"],
        "hours_mon": "08:00-17:00",
        "hours_tue": "08:00-17:00",
        "hours_wed": "08:00-17:00",
        "hours_thu": "08:00-17:00",
        "hours_fri": "08:00-17:00",
        "notes": "",
    }
    assert (await c.post(f"/admin/negocios/{tid}/paso2", data=step2, headers=H)).status_code == 200
    # 2) build_bot con FakeLLM (job del worker)
    cfg = valid_config()
    cfg["services"] = [
        {"id": "limpieza", "name": "Limpieza dental", "description": "", "duration_min": 45,
         "price_cop": 99000, "price_note": None, "requires_valuation": False,
         "source_ref": "owner"}
    ]  # fmt: skip
    cfg["hours"] = {
        "weekly": {
            d: [{"open": "08:00", "close": "17:00"}] for d in ("mon", "tue", "wed", "thu", "fri")
        }
    }
    fake_llm.queue(tool_response(cfg))
    r = await c.post(
        f"/admin/negocios/{tid}/generar", data={"consent": "on", "notes": ""}, headers=H
    )
    assert r.status_code == 200
    await core_jobs.run_enqueued()
    session.expire_all()
    tenant = (await session.execute(select(Tenant).where(Tenant.id == uuid.UUID(tid)))).scalar_one()
    assert tenant.status == "review"
    tenant_id = tenant.id
    # 3) publicar
    from app.db.models.bots import BotConfig

    bot = (
        (await session.execute(select(BotConfig).where(BotConfig.tenant_id == tenant.id)))
        .scalars()
        .one()
    )
    await publish_bot(session, bot.id, actor=await session.get(User, uid), enforce_review=False)  # type: ignore[attr-defined]
    session.add(ChannelAccount(tenant_id=tenant.id, phone_e164=BIZ))
    await session.commit()
    await session.refresh(tenant)
    assert tenant.status == "active"
    # 4) horarios disponibles reales
    service = (
        (await session.execute(select(Service).where(Service.tenant_id == tenant.id)))
        .scalars()
        .first()
    )
    assert service is not None
    today = utcnow().date()
    slots = await BookingService().find_slots(
        session, tenant.id, service.id, today + timedelta(days=1), today + timedelta(days=14)
    )
    assert slots, "debe haber horarios libres"
    slot = slots[0]

    async def say(body: str) -> None:
        p = {"MessageSid": f"SM{abs(hash(body))}", "From": f"whatsapp:{CUSTOMER}",
             "To": f"whatsapp:{BIZ}", "Body": body, "NumMedia": "0", "ProfileName": "Ana"}  # fmt: skip
        sig = sign_twilio("http://test/webhooks/twilio/whatsapp", p, TOKEN)
        resp = await c.post(
            "/webhooks/twilio/whatsapp", data=p, headers={"X-Twilio-Signature": sig}
        )
        assert resp.status_code == 200
        await core_jobs.run_enqueued()

    fake_llm.queue(
        _tc("check_availability", service="Limpieza dental",
            date_from=to_bogota(slot.starts_at).date().isoformat()),
        "Tengo disponible ese dia, ¿confirmas?",
    )  # fmt: skip
    await say("Quiero una limpieza")
    fake_llm.queue(
        _tc("book_appointment", service=str(service.id), slot_start=slot.starts_at.isoformat(),
            customer_name="Ana Perez", notes=""),
        "Listo Ana, tu cita quedo confirmada.",
    )  # fmt: skip
    await say("Si, confirmo")
    session.expire_all()
    from app.conversation import memory

    session.expire_all()
    dbg = [
        memory.decrypt_body(tenant_id, m)
        for m in (await session.execute(select(Message).order_by(Message.created_at))).scalars()
    ]
    appt = (await session.execute(select(Appointment))).scalars().first()
    assert appt is not None, (dbg, [str(x["messages"][-1])[:400] for x in fake_llm.calls])
    assert appt.status == "confirmed" and appt.tenant_id == tenant_id
    sent = (
        (await session.execute(select(Message).where(Message.direction == "out"))).scalars().all()
    )
    assert sent and all((m.provider_sid or "").startswith("DRYRUN") for m in sent)


async def test_csv_import_score_demo(
    authenticated_client: httpx.AsyncClient,
    session: AsyncSession,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.leads import demo
    from app.leads.csv_import import file_sha256

    c = authenticated_client
    monkeypatch.setattr(factory_service, "get_llm", lambda: fake_llm)
    base = {"sha256": file_sha256(CSV), "niche": "dentista", "city": "Cali"}
    await c.post(
        "/admin/leads/importar", files=csv_files(), data={"niche": "dentista", "city": "Cali"}
    )
    done = await c.post("/admin/leads/importar/confirmar", data={**base, "col_name": "Titulo"})
    assert "Importación terminada" in done.text
    leads = (
        (await session.execute(select(Lead).where(Lead.disposition != "perdido"))).scalars().all()
    )
    assert leads and all(lead.score is not None for lead in leads)
    lead = leads[0]
    uid = c.user.id  # type: ignore[attr-defined]
    fake_llm.queue(tool_response(valid_config()))
    res = await demo.lead_to_demo_bot(
        session, lead.id, actor=await session.get(User, uid), scrape=False, niche="dentista"
    )  # type: ignore[attr-defined]
    await session.commit()
    tenant = await session.get(Tenant, res.tenant_id)
    assert tenant is not None and tenant.is_demo
    assert await session.scalar(select(func.count()).select_from(Tenant)) >= 1
    page = await c.get(res.demo_link.replace("http://test", ""))
    assert page.status_code == 200
