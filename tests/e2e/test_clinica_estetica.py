"""E2E clinica_estetica: fabrica -> revision -> publicacion -> conversacion (FakeLLM) con la regla
de valoracion previa y el rechazo de consejo medico. Sin red."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import FakeLLM
from app.conversation.engine import ConversationEngine, sandbox_reply
from app.conversation.guardrails import DEFAULT_REPLIES
from app.core.crypto import phone_hash
from app.db.models.bots import BotConfig
from app.db.models.catalog import Service
from app.db.models.contacts import Contact
from app.db.models.conversations import Conversation, Handoff
from app.db.models.tenants import Tenant
from app.factory import review, service
from app.factory.schemas import FactoryInput
from app.niches.loader import get_niche_template
from tests.conversation.conftest import FakeAgenda, agenda, text, tool_call  # noqa: F401
from tests.factory.conftest import tool_response, valid_config

PHONE = "+573005550011"


def _config() -> dict[str, Any]:
    cfg = valid_config()
    cfg["business"]["niche"] = "clinica_estetica"
    cfg["business"]["name"] = "Estetica Aura"
    cfg["services"] = [
        {
            "id": "valoracion",
            "name": "Valoracion estetica",
            "description": "Primera cita",
            "duration_min": 30,
            "price_cop": 50000,
            "price_note": None,
            "requires_valuation": False,
            "source_ref": "owner",
        },
        {
            "id": "botox",
            "name": "Botox",
            "description": "Toxina botulinica",
            "duration_min": 45,
            "price_cop": None,
            "price_note": "Segun zonas, se cotiza en valoracion",
            "requires_valuation": True,
            "source_ref": "template",
        },
    ]
    cfg["out_of_scope_topics"] = ["diagnosticos", "medicamentos"]
    return cfg


@pytest_asyncio.fixture
async def clinic(session: AsyncSession, make_tenant: Any) -> tuple[Tenant, BotConfig]:
    tenant: Tenant = await make_tenant("Estetica Aura", niche="clinica_estetica", status="draft")
    tenant.city = "Cali"
    tenant.address = "Calle 5 # 10-20"
    inputs = FactoryInput(
        name="Estetica Aura",
        niche="clinica_estetica",
        city="Cali",
        address="Calle 5 # 10-20",
        phone="+57 300 123 4567",
        services=[{"name": "Valoracion estetica", "price_cop": 50000, "duration_min": 30}],
        hours={"mon": [["08:00", "17:00"]]},
        notes="",
    )
    llm = FakeLLM([tool_response(_config())])
    bot = await service.build_bot(session, tenant.id, inputs, scrape=False, llm=llm)
    for s in await review.list_services(session, tenant.id):
        s.needs_review = False
    for f in await review.list_faqs(session, tenant.id):
        f.needs_review = False
    await session.flush()
    bot = await service.publish_bot(session, bot.id, actor="system")
    await session.commit()
    return tenant, bot


@pytest_asyncio.fixture
async def conv(session: AsyncSession, clinic: tuple[Tenant, BotConfig]) -> Conversation:
    tenant, _ = clinic
    contact = Contact(tenant_id=tenant.id, phone_hash=phone_hash(PHONE), source="inbound")
    session.add(contact)
    await session.flush()
    c = Conversation(tenant_id=tenant.id, contact_id=contact.id, channel="whatsapp", status="open")
    session.add(c)
    await session.commit()
    return c


async def _say(
    session: AsyncSession, llm: FakeLLM, tenant: Tenant, c: Conversation, msg: str
) -> list[str]:
    out = await ConversationEngine(llm).handle_inbound(
        session, tenant_id=tenant.id, conversation_id=c.id, text=msg
    )
    await session.commit()
    return out


async def test_niche_template_declares_assessment_first_rule() -> None:
    tpl = get_niche_template("clinica_estetica")
    rules = tpl.booking_rules
    assert "botox" in rules.assessment_first_services
    assert rules.notes["first_visit_type"] == "valoracion"
    assert "historia_clinica" in rules.never_collect and "diagnosticos" in rules.never_collect


async def test_factory_publishes_estetica_bot_with_valuation_rule(
    session: AsyncSession, clinic: tuple[Tenant, BotConfig]
) -> None:
    tenant, bot = clinic
    assert bot.status == "published"
    assert tenant.status == "active"
    assert "requiere valoracion previa" in bot.system_prompt
    assert bot.guardrails["never_collect"]
    assert bot.booking_rules["assessment_first_services"]
    names = {s.name for s in await review.list_services(session, tenant.id)}
    assert {"Valoracion estetica", "Botox"} <= names


async def test_medical_advice_is_refused_without_calling_llm(
    session: AsyncSession, clinic: tuple[Tenant, BotConfig], conv: Conversation
) -> None:
    tenant, _ = clinic
    llm = FakeLLM()
    out = await _say(
        session, llm, tenant, conv, "Que pastilla me recomiendas tomar para la inflamacion?"
    )
    assert llm.calls == []
    assert out == [DEFAULT_REPLIES["medical_reply"]]
    assert "valoraci" in out[0].lower() and "no puedo recomendarte medicamentos" in out[0].lower()


async def test_urgent_symptom_hands_off_to_human(
    session: AsyncSession, clinic: tuple[Tenant, BotConfig], conv: Conversation
) -> None:
    tenant, _ = clinic
    llm = FakeLLM()
    out = await _say(
        session, llm, tenant, conv, "Despues del relleno tengo la cara muy inflamada y dolor fuerte"
    )
    assert llm.calls == []
    assert out == [DEFAULT_REPLIES["urgent_reply"]]
    handoff = (await session.execute(select(Handoff))).scalar_one()
    assert handoff.reason == "medical_urgent"


async def test_system_prompt_lists_estetica_services_and_no_medical_promises(
    session: AsyncSession, clinic: tuple[Tenant, BotConfig], conv: Conversation
) -> None:
    tenant, _ = clinic
    llm = FakeLLM()
    llm.queue(text("Hola, soy el asistente virtual de Estetica Aura. Puedo agendar tu valoracion."))
    out = await _say(session, llm, tenant, conv, "Hola")
    assert "valoracion" in out[0]
    system = llm.calls[0]["system"]
    assert "Botox" in system and "Valoracion estetica" in system
    assert "Segun zonas, se cotiza en valoracion" in system


async def test_valuation_booking_flow_confirmed(
    session: AsyncSession,
    clinic: tuple[Tenant, BotConfig],
    conv: Conversation,
    agenda: FakeAgenda,  # noqa: F811
) -> None:
    tenant, _ = clinic
    val = (
        await session.execute(
            select(Service).where(Service.tenant_id == tenant.id, Service.name.like("Valoracion%"))
        )
    ).scalar_one()
    day = agenda.tomorrow().date().isoformat()
    slot = agenda.slot_times()[2]
    llm = FakeLLM()
    llm.queue(
        tool_call("check_availability", service="Valoracion estetica", date_from=day),
        text("Tengo mañana a las 3:00 p. m. ¿Te sirve?"),
    )
    await _say(session, llm, tenant, conv, "Quiero una valoracion para botox mañana")
    assert agenda.booked == []
    llm.queue(
        tool_call(
            "book_appointment",
            service=str(val.id),
            slot_start=slot.isoformat(),
            customer_name="Laura Gomez",
        ),
        text("Listo Laura, tu valoracion quedo confirmada."),
    )
    out = await _say(session, llm, tenant, conv, "Sí, confirmo")
    assert "confirmada" in out[0]
    assert len(agenda.booked) == 1 and agenda.booked[0].service_id == val.id


async def test_booking_requires_explicit_confirmation(
    session: AsyncSession,
    clinic: tuple[Tenant, BotConfig],
    conv: Conversation,
    agenda: FakeAgenda,  # noqa: F811
) -> None:
    tenant, _ = clinic
    val = (
        await session.execute(select(Service).where(Service.name.like("Valoracion%")))
    ).scalar_one()
    day = agenda.tomorrow().date().isoformat()
    llm = FakeLLM()
    llm.queue(
        tool_call("check_availability", service="Valoracion estetica", date_from=day),
        text("Tengo 3:00 p. m."),
    )
    await _say(session, llm, tenant, conv, "Quiero valoracion mañana")
    llm.queue(
        tool_call(
            "book_appointment",
            service=str(val.id),
            slot_start=agenda.slot_times()[2].isoformat(),
            customer_name="Laura",
        ),
        text("Necesito que confirmes."),
    )
    await _say(session, llm, tenant, conv, "Cuanto demora?")
    assert agenda.booked == []


@pytest.mark.xfail(
    strict=True,
    reason="BUG: la regla de valoracion previa no se aplica en codigo; book_appointment reserva "
    "servicios con requires_valuation sin cita de valoracion previa (ver integracion-pendientes).",
)
async def test_direct_botox_booking_is_blocked_until_valuation(
    session: AsyncSession,
    clinic: tuple[Tenant, BotConfig],
    conv: Conversation,
    agenda: FakeAgenda,  # noqa: F811
) -> None:
    tenant, _ = clinic
    botox = (await session.execute(select(Service).where(Service.name == "Botox"))).scalar_one()
    day = agenda.tomorrow().date().isoformat()
    llm = FakeLLM()
    llm.queue(
        tool_call("check_availability", service="Botox", date_from=day), text("Tengo 3:00 p. m.")
    )
    await _say(session, llm, tenant, conv, "Quiero botox mañana")
    llm.queue(
        tool_call(
            "book_appointment",
            service=str(botox.id),
            slot_start=agenda.slot_times()[2].isoformat(),
            customer_name="Laura",
        ),
        text("Listo."),
    )
    await _say(session, llm, tenant, conv, "Sí, confirmo")
    assert agenda.booked == []


async def test_sandbox_medical_refusal(
    session: AsyncSession, clinic: tuple[Tenant, BotConfig]
) -> None:
    tenant, _ = clinic
    llm = FakeLLM()
    reply = await sandbox_reply(
        session, tenant.id, [], "Que dosis de botox me pongo? me puedes recetar algo?", llm=llm
    )
    assert llm.calls == []
    assert reply == DEFAULT_REPLIES["medical_reply"]
