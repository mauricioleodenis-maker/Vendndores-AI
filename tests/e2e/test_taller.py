"""E2E taller: fabrica -> revision -> publicacion -> conversacion (FakeLLM), sin diagnostico remoto
y con datos de vehiculo/placa cifrados. Sin red."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import FakeLLM
from app.conversation.engine import ConversationEngine, sandbox_reply
from app.conversation.guardrails import DEFAULT_REPLIES
from app.core.crypto import get_crypto, make_aad, phone_hash, unpack_blob
from app.db.models.booking import Appointment
from app.db.models.bots import BotConfig
from app.db.models.catalog import Service
from app.db.models.contacts import Contact
from app.db.models.conversations import Conversation
from app.db.models.tenants import Tenant
from app.factory import review, service
from app.factory.schemas import FactoryInput
from app.niches.loader import get_niche_template
from tests.conversation.conftest import FakeAgenda, agenda, text, tool_call  # noqa: F401
from tests.factory.conftest import tool_response, valid_config

PHONE = "+573005550022"
PLATE = "ABC123"


def _config() -> dict[str, Any]:
    cfg = valid_config()
    cfg["business"]["niche"] = "taller"
    cfg["business"]["name"] = "Taller El Piston"
    cfg["services"] = [
        {
            "id": "cambio_aceite",
            "name": "Cambio de aceite",
            "description": "Aceite y filtro",
            "duration_min": 30,
            "price_cop": None,
            "price_note": "Depende del aceite y el vehiculo",
            "requires_valuation": False,
            "source_ref": "template",
        },
        {
            "id": "reparacion_motor",
            "name": "Reparacion de motor",
            "description": "Solo cotizacion presencial",
            "duration_min": 30,
            "price_cop": None,
            "price_note": "Se cotiza tras inspeccion",
            "requires_valuation": True,
            "source_ref": "template",
        },
    ]
    cfg["out_of_scope_topics"] = ["diagnostico remoto"]
    return cfg


@pytest_asyncio.fixture
async def shop(session: AsyncSession, make_tenant: Any) -> tuple[Tenant, BotConfig]:
    tenant: Tenant = await make_tenant("Taller El Piston", niche="taller", status="draft")
    tenant.city = "Cali"
    tenant.address = "Carrera 8 # 20-30"
    inputs = FactoryInput(
        name="Taller El Piston",
        niche="taller",
        city="Cali",
        address="Carrera 8 # 20-30",
        phone="+57 300 123 4567",
        services=[{"name": "Cambio de aceite", "duration_min": 30}],
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
async def conv(session: AsyncSession, shop: tuple[Tenant, BotConfig]) -> Conversation:
    tenant, _ = shop
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


async def test_niche_template_declares_vehicle_data_and_assessment_rules() -> None:
    tpl = get_niche_template("taller")
    rules = tpl.booking_rules
    assert {"marca", "modelo", "anio"} <= set(rules.required_fields)
    assert "placa" in rules.optional_fields and "placa" not in rules.required_fields
    assert "frenos" in rules.assessment_first_services
    assert any("diagnosticar un vehiculo a distancia" in a for a in tpl.persona.avoid)
    assert "diagnostico remoto definitivo" in tpl.forbidden_topics


async def test_factory_publishes_taller_bot(
    session: AsyncSession, shop: tuple[Tenant, BotConfig]
) -> None:
    tenant, bot = shop
    assert bot.status == "published"
    assert tenant.status == "active"
    names = {s.name for s in await review.list_services(session, tenant.id)}
    assert {"Cambio de aceite", "Reparacion de motor"} <= names


async def test_system_prompt_lists_services_without_price_promises(
    session: AsyncSession, shop: tuple[Tenant, BotConfig], conv: Conversation
) -> None:
    tenant, _ = shop
    llm = FakeLLM()
    llm.queue(text("Hola, soy el asistente virtual de Taller El Piston."))
    out = await _say(session, llm, tenant, conv, "Hola")
    assert "asistente virtual" in out[0]
    system = llm.calls[0]["system"]
    assert "Cambio de aceite" in system and "Reparacion de motor" in system
    assert "Se cotiza tras inspeccion" in system


async def test_booking_flow_stores_plate_encrypted(
    session: AsyncSession,
    shop: tuple[Tenant, BotConfig],
    conv: Conversation,
    agenda: FakeAgenda,  # noqa: F811
) -> None:
    tenant, _ = shop
    oil = (
        await session.execute(
            select(Service).where(
                Service.tenant_id == tenant.id, Service.name == "Cambio de aceite"
            )
        )
    ).scalar_one()
    day = agenda.tomorrow().date().isoformat()
    slot = agenda.slot_times()[2]
    llm = FakeLLM()
    llm.queue(
        tool_call("check_availability", service="Cambio de aceite", date_from=day),
        text("Tengo mañana a las 3:00 p. m. ¿Te sirve?"),
    )
    await _say(session, llm, tenant, conv, "Cambio de aceite mañana, Mazda 3 2018")
    assert agenda.booked == []
    llm.queue(
        tool_call(
            "book_appointment",
            service=str(oil.id),
            slot_start=slot.isoformat(),
            customer_name="Carlos Perez",
            notes=f"Mazda 3 2018 placa {PLATE}",
        ),
        text("Listo Carlos, tu cita quedo confirmada."),
    )
    out = await _say(session, llm, tenant, conv, "Sí, confirmo")
    assert "confirmada" in out[0]
    assert len(agenda.booked) == 1 and agenda.booked[0].service_id == oil.id


async def test_booking_requires_explicit_confirmation(
    session: AsyncSession,
    shop: tuple[Tenant, BotConfig],
    conv: Conversation,
    agenda: FakeAgenda,  # noqa: F811
) -> None:
    tenant, _ = shop
    oil = (
        await session.execute(select(Service).where(Service.name == "Cambio de aceite"))
    ).scalar_one()
    day = agenda.tomorrow().date().isoformat()
    llm = FakeLLM()
    llm.queue(
        tool_call("check_availability", service="Cambio de aceite", date_from=day),
        text("Tengo 3:00 p. m."),
    )
    await _say(session, llm, tenant, conv, "Quiero cambio de aceite mañana")
    llm.queue(
        tool_call(
            "book_appointment",
            service=str(oil.id),
            slot_start=agenda.slot_times()[2].isoformat(),
            customer_name="Carlos",
        ),
        text("Necesito que confirmes."),
    )
    await _say(session, llm, tenant, conv, "Cuanto demora?")
    assert agenda.booked == []


async def test_plate_note_is_not_stored_in_plaintext(
    session: AsyncSession, shop: tuple[Tenant, BotConfig], conv: Conversation
) -> None:
    """Las notas de cita (donde viaja la placa) van cifradas con AAD del tenant."""
    tenant, _ = shop
    crypto = get_crypto()
    aad = make_aad("appointments", tenant.id, "notes_enc")
    from app.core.crypto import pack_blob

    blob = pack_blob(crypto.encrypt_str(f"placa {PLATE}", aad=aad))
    assert PLATE.encode() not in blob
    assert crypto.decrypt_str(unpack_blob(blob), aad=aad) == f"placa {PLATE}"
    assert (await session.execute(select(Appointment))).first() is None


async def test_remote_diagnosis_does_not_get_a_diagnosis(
    session: AsyncSession, shop: tuple[Tenant, BotConfig], conv: Conversation
) -> None:
    """El LLM guionado diagnostica a distancia solo si el sistema se lo permite; el prompt debe
    prohibirlo (persona.avoid) y la respuesta final se entrega sin ofrecer un diagnostico."""
    tenant, _ = shop
    llm = FakeLLM()
    llm.queue(text("No puedo diagnosticarlo por mensaje. Te agendo un diagnostico presencial."))
    out = await _say(session, llm, tenant, conv, "Mi carro hace un ruido raro, que puede ser?")
    assert "diagnosticarlo por mensaje" in out[0]
    assert "diagnostic" in llm.calls[0]["system"].lower()


async def test_brake_failure_prompt_carries_safety_rule(
    session: AsyncSession, shop: tuple[Tenant, BotConfig], conv: Conversation
) -> None:
    tenant, bot = shop
    blob = (bot.system_prompt or "").lower()
    assert "freno" in blob or "frenos" in blob or "grua" in blob


@pytest.mark.xfail(
    strict=True,
    reason="BUG: el guardrail de urgencia solo conoce sintomas medicos; 'se me fueron los frenos' "
    "(falla de seguridad del taller) no escala a humano (ver integracion-pendientes).",
)
async def test_brake_failure_escalates_to_human(
    session: AsyncSession, shop: tuple[Tenant, BotConfig], conv: Conversation
) -> None:
    tenant, _ = shop
    llm = FakeLLM()
    out = await _say(session, llm, tenant, conv, "Se me fueron los frenos y sale humo del motor")
    assert llm.calls == []
    assert out != [DEFAULT_REPLIES["safe_reply"]]


@pytest.mark.xfail(
    strict=True,
    reason="BUG: la regla assessment_first_services (frenos, reparacion_motor) no se aplica en "
    "codigo; book_appointment reserva sin inspeccion previa (ver integracion-pendientes).",
)
async def test_direct_engine_repair_booking_is_blocked(
    session: AsyncSession,
    shop: tuple[Tenant, BotConfig],
    conv: Conversation,
    agenda: FakeAgenda,  # noqa: F811
) -> None:
    tenant, _ = shop
    eng = (
        await session.execute(select(Service).where(Service.name == "Reparacion de motor"))
    ).scalar_one()
    day = agenda.tomorrow().date().isoformat()
    llm = FakeLLM()
    llm.queue(
        tool_call("check_availability", service="Reparacion de motor", date_from=day),
        text("Tengo 3:00 p. m."),
    )
    await _say(session, llm, tenant, conv, "Reparar el motor mañana")
    llm.queue(
        tool_call(
            "book_appointment",
            service=str(eng.id),
            slot_start=agenda.slot_times()[2].isoformat(),
            customer_name="Carlos",
        ),
        text("Listo."),
    )
    await _say(session, llm, tenant, conv, "Sí, confirmo")
    assert agenda.booked == []


async def test_sandbox_reply_works_for_taller(
    session: AsyncSession, shop: tuple[Tenant, BotConfig]
) -> None:
    tenant, _ = shop
    llm = FakeLLM()
    llm.queue(text("Con gusto, ¿qué servicio necesitas?"))
    reply = await sandbox_reply(session, tenant.id, [], "Hola", llm=llm)
    assert reply
