from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.models.tenants import Tenant
from app.leads import demo, demo_whatsapp


@pytest.fixture
async def demo_tenant(session: AsyncSession) -> Tenant:
    t = Tenant(slug="taller-rayo", name="Taller Rayo", niche="taller", status="draft", is_demo=True)
    session.add(t)
    await session.flush()
    return t


async def test_demo_code_links_phone_then_bot_answers(
    session: AsyncSession, demo_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []

    async def fake_reply(session: Any, token: str, history: Any, text: str, **kw: Any) -> str:
        calls.append((history, text, kw["tenant"].id))
        return "Claro, la revisión vale $80.000"

    monkeypatch.setattr(demo, "demo_chat_reply", fake_reply)
    sender = "whatsapp:+573001234567"
    assert await demo_whatsapp.handle_inbound(session, sender, "hola") == demo_whatsapp.UNKNOWN_TEXT
    welcome = await demo_whatsapp.handle_inbound(session, sender, "DEMO-taller-rayo")
    assert "Taller Rayo" in welcome
    reply = await demo_whatsapp.handle_inbound(session, sender, "¿Cuánto vale la revisión?")
    assert reply.startswith("Claro") and calls[0][2] == demo_tenant.id
    await demo_whatsapp.handle_inbound(session, sender, "¿Y el sábado?")
    assert len(calls[1][0]) == 2  # historial: pregunta + respuesta anteriores
    assert "no existe" in await demo_whatsapp.handle_inbound(session, sender, "DEMO-no-existe")


async def test_limit_message(session: AsyncSession, demo_tenant: Tenant, monkeypatch) -> None:
    async def limited(*a: Any, **kw: Any) -> str:
        raise AppError("rate_limited", "x", 429)

    monkeypatch.setattr(demo, "demo_chat_reply", limited)
    s = "whatsapp:+573001234568"
    await demo_whatsapp.handle_inbound(session, s, "DEMO-taller-rayo")
    assert "límite" in await demo_whatsapp.handle_inbound(session, s, "hola")


def test_twiml_escapes_and_number_match(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "&lt;b&gt;" in demo_whatsapp.message_twiml("<b>")
    monkeypatch.setattr(get_settings(), "demo_whatsapp_number", "+573000000000")
    assert demo_whatsapp.is_demo_number("whatsapp:+573000000000")
    assert not demo_whatsapp.is_demo_number("whatsapp:+573000000001")


async def test_webhook_routes_demo_number(client, session, demo_tenant, monkeypatch) -> None:
    from app.channels import router as ch

    monkeypatch.setattr(get_settings(), "demo_whatsapp_number", "+573000000000")
    monkeypatch.setattr(ch, "_signature_ok", lambda *a, **k: True)
    r = await client.post(
        "/webhooks/twilio/whatsapp",
        data={
            "To": "whatsapp:+573000000000",
            "From": "whatsapp:+573001112299",
            "Body": "DEMO-taller-rayo",
            "MessageSid": "SM1",
        },
    )
    assert r.status_code == 200 and "Taller Rayo" in r.text and "<Message>" in r.text
