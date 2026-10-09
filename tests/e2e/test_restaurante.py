"""E2E restaurante: fabrica -> revision -> publicacion -> conversacion (FakeLLM): reserva con
numero de personas, domicilios (info) y alergias como dato sensible cifrado/redactado. Sin red."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import FakeLLM
from app.conversation.engine import ConversationEngine, sandbox_reply
from app.core.crypto import phone_hash
from app.db.models.booking import Appointment
from app.db.models.bots import BotConfig
from app.db.models.catalog import Service
from app.db.models.contacts import Contact
from app.db.models.conversations import Conversation
from app.db.models.tenants import Tenant
from app.factory import review, service
from app.factory.schemas import FactoryInput
from app.niches.loader import get_niche_template
from tests.conversation.conftest import FakeAgenda, agenda, text, tool_call  # noqa: F401, I001
from tests.factory.conftest import tool_response, valid_config

PHONE = "+573005550033"


def _config() -> dict[str, Any]:
    cfg = valid_config()
    cfg["business"]["niche"] = "restaurante"
    cfg["business"]["name"] = "Restaurante La Brasa"
    cfg["services"] = [
        {
            "id": "reserva_mesa",
            "name": "Reserva de mesa",
            "description": "Mesa para el numero de personas indicado",
            "duration_min": 90,
            "price_cop": None,
            "price_note": "Sin costo",
            "requires_valuation": False,
            "source_ref": "template",
        },
    ]
    cfg["out_of_scope_topics"] = ["diagnosticar alergias"]
    return cfg


@pytest_asyncio.fixture
async def shop(session: AsyncSession, make_tenant: Any) -> tuple[Tenant, BotConfig]:
    tenant: Tenant = await make_tenant("Restaurante La Brasa", niche="restaurante", status="draft")
    tenant.city = "Cali"
    tenant.address = "Carrera 8 # 20-30"
    inputs = FactoryInput(
        name="Restaurante La Brasa",
        niche="restaurante",
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


async def test_niche_template_declares_party_size_delivery_and_sensitive_allergies() -> None:
    tpl = get_niche_template("restaurante")
    rules = tpl.booking_rules
    assert {"nombre", "celular", "numero_personas", "fecha", "hora"} <= set(rules.required_fields)
    assert "alergias_o_restricciones" in rules.optional_fields
    assert "alergias_o_restricciones" not in rules.required_fields
    assert "datos sensibles" in tpl.compliance.legal_basis
    assert "ingredientes" in tpl.sensitive_response
    assert "domicilio" in tpl.extras["domicilios"]["flujo"][1] or tpl.extras["domicilios"]["activo"]
    assert any("alergenos" in t for t in tpl.forbidden_topics)


async def test_factory_publishes_restaurant_bot(
    session: AsyncSession, shop: tuple[Tenant, BotConfig]
) -> None:
    tenant, bot = shop
    assert bot.status == "published"
    assert tenant.status == "active"
    names = {s.name for s in await review.list_services(session, tenant.id)}
    assert "Reserva de mesa" in names


async def test_prompt_lists_services_and_greets_as_assistant(
    session: AsyncSession, shop: tuple[Tenant, BotConfig], conv: Conversation
) -> None:
    tenant, _ = shop
    llm = FakeLLM()
    llm.queue(text("Hola, soy el asistente virtual de Restaurante La Brasa."))
    out = await _say(session, llm, tenant, conv, "Hola")
    assert "asistente virtual" in out[0]
    assert "Reserva de mesa" in llm.calls[0]["system"]


async def _reserve(
    session: AsyncSession,
    llm: FakeLLM,
    tenant: Tenant,
    conv: Conversation,
    agenda: FakeAgenda,  # noqa: F811
    notes: str,
    confirm_text: str = "Sí, confirmo",
) -> list[str]:
    table = (
        await session.execute(select(Service).where(Service.name == "Reserva de mesa"))
    ).scalar_one()
    day = agenda.tomorrow().date().isoformat()
    llm.queue(
        tool_call("check_availability", service="Reserva de mesa", date_from=day),
        text("Tengo mañana a las 3:00 p. m. ¿Te sirve?"),
    )
    await _say(session, llm, tenant, conv, "Mesa para 6 personas mañana")
    llm.queue(
        tool_call(
            "book_appointment",
            service=str(table.id),
            slot_start=agenda.slot_times()[2].isoformat(),
            customer_name="Laura Gomez",
            notes=notes,
        ),
        text("Listo Laura, tu reserva quedo confirmada."),
    )
    return await _say(session, llm, tenant, conv, confirm_text)


async def test_reservation_with_party_size_and_allergy_note_is_encrypted(
    session: AsyncSession,
    shop: tuple[Tenant, BotConfig],
    conv: Conversation,
    agenda: FakeAgenda,  # noqa: F811
) -> None:
    tenant, _ = shop
    llm = FakeLLM()
    out = await _reserve(
        session, llm, tenant, conv, agenda, "6 personas; alergia a los mariscos y nueces"
    )
    assert "confirmada" in out[0]
    assert len(agenda.booked) == 1
    appts = (await session.execute(select(Appointment))).scalars().all()
    assert len(appts) == 1
    blob = appts[0].notes_enc
    assert blob is not None
    assert b"mariscos" not in blob and b"nueces" not in blob
    assert b"6 personas" not in blob


async def test_allergy_is_redacted_before_encryption(
    session: AsyncSession,
    shop: tuple[Tenant, BotConfig],
    conv: Conversation,
    agenda: FakeAgenda,  # noqa: F811
) -> None:
    from app.core.crypto import get_crypto, make_aad, unpack_blob

    tenant, _ = shop
    await _reserve(session, FakeLLM(), tenant, conv, agenda, "4 personas, alergia a los mariscos")
    appt = (await session.execute(select(Appointment))).scalar_one()
    assert appt.notes_enc is not None
    aad = make_aad("appointments", tenant.id, "notes_enc")
    plain = get_crypto().decrypt_str(unpack_blob(appt.notes_enc), aad=aad)
    assert "mariscos" not in plain
    assert "[salud]" in plain
    assert "4 personas" in plain


async def test_booking_requires_explicit_confirmation(
    session: AsyncSession,
    shop: tuple[Tenant, BotConfig],
    conv: Conversation,
    agenda: FakeAgenda,  # noqa: F811
) -> None:
    tenant, _ = shop
    table = (
        await session.execute(select(Service).where(Service.name == "Reserva de mesa"))
    ).scalar_one()
    day = agenda.tomorrow().date().isoformat()
    llm = FakeLLM()
    llm.queue(
        tool_call("check_availability", service="Reserva de mesa", date_from=day),
        text("Tengo 3:00 p. m."),
    )
    await _say(session, llm, tenant, conv, "Mesa mañana para 2")
    llm.queue(
        tool_call(
            "book_appointment",
            service=str(table.id),
            slot_start=agenda.slot_times()[2].isoformat(),
            customer_name="Laura",
        ),
        text("Necesito que confirmes."),
    )
    await _say(session, llm, tenant, conv, "Tienen parqueadero?")
    assert agenda.booked == []


async def test_delivery_question_is_answered_without_booking(
    session: AsyncSession,
    shop: tuple[Tenant, BotConfig],
    conv: Conversation,
    agenda: FakeAgenda,  # noqa: F811
) -> None:
    tenant, _ = shop
    llm = FakeLLM()
    llm.queue(text("Si hacemos domicilios. Dime tu barrio y te confirmo cobertura y costo."))
    out = await _say(session, llm, tenant, conv, "Hacen domicilios?")
    assert "domicilios" in out[0]
    assert agenda.booked == []


async def test_allergy_reaction_escalates_to_human(
    session: AsyncSession, shop: tuple[Tenant, BotConfig], conv: Conversation
) -> None:
    """'intoxic' esta cubierto por el guardrail de urgencia: no se llama al LLM."""
    tenant, _ = shop
    llm = FakeLLM()
    out = await _say(session, llm, tenant, conv, "Quedé intoxicado con lo que comí ayer, ayuda")
    assert llm.calls == []
    assert out != []


@pytest.mark.xfail(
    strict=True,
    reason="BUG: el guardrail de urgencia no cubre 'reaccion alergica' (escalation nivel 1 de la "
    "plantilla restaurante) (ver integracion-pendientes).",
)
async def test_allergic_reaction_text_escalates_to_human(
    session: AsyncSession, shop: tuple[Tenant, BotConfig], conv: Conversation
) -> None:
    tenant, _ = shop
    llm = FakeLLM()
    await _say(session, llm, tenant, conv, "Tuve una reaccion alergica y se me hincha la garganta")
    assert llm.calls == []


async def test_sandbox_reply_works_for_restaurant(
    session: AsyncSession, shop: tuple[Tenant, BotConfig]
) -> None:
    tenant, _ = shop
    llm = FakeLLM()
    llm.queue(text("Con gusto, ¿para cuantas personas?"))
    reply = await sandbox_reply(session, tenant.id, [], "Hola", llm=llm)
    assert reply
