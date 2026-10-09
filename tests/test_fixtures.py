"""Verifica las fixtures compartidas que usaran los demas modulos."""

import pytest

from app.ai.client import LLMResponse, ToolCall, get_llm


async def test_fake_llm_override_and_scripting(app, fake_llm):
    llm = app.dependency_overrides[get_llm]()
    assert llm is fake_llm
    fake_llm.queue("uno", LLMResponse(text="", tool_calls=[ToolCall("1", "book", {"a": 1})]))
    assert (await llm.complete(system="s", messages=[])).text == "uno"
    second = await llm.complete(system="s", messages=[], tools=[{"name": "book"}])
    assert second.tool_calls[0].name == "book"
    assert (await llm.complete(system="s", messages=[])).text == "Respuesta de prueba"
    assert len(fake_llm.calls) == 3


def test_real_get_llm_requires_api_key():
    from app.core.errors import AppError

    get_llm.__globals__["_build_default"].cache_clear()
    with pytest.raises(AppError):
        get_llm()


async def test_network_is_blocked():
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(RuntimeError, match="Red bloqueada"):
        s.connect(("93.184.216.34", 80))
    s.close()
    with pytest.raises(RuntimeError, match="DNS bloqueado"):
        socket.getaddrinfo("example.com", 80)


async def test_authenticated_client_has_csrf_and_cookie(authenticated_client):
    assert authenticated_client.headers["X-CSRF-Token"] == authenticated_client.csrf_token
    assert authenticated_client.user.role == "owner"


async def test_make_tenant_and_user_factories(make_tenant, make_user):
    a, b = await make_tenant(), await make_tenant(niche="taller")
    assert a.slug != b.slug and b.niche == "taller"
    assert (await make_user("admin")).role == "admin"


async def test_contracts_import():
    from app.booking.service import BookingService
    from app.channels.sender import send_whatsapp_template
    from app.conversation.engine import ConversationEngine, sandbox_reply
    from app.factory.service import build_bot, publish_bot
    from app.leads.normalize import to_e164_co
    from app.niches.loader import get_niche_template, list_niches
    from app.plans.entitlements import PlanLimitExceeded, assert_within_limit, record_usage
    from app.privacy.service import is_optout_message, redact_pii
    from app.reminders.scheduler import cancel_for_appointment, schedule_for_appointment

    assert get_niche_template("dentista") is not None
    assert "dentista" in list_niches()
    assert PlanLimitExceeded("m").status == 402
    _ = (BookingService, build_bot, send_whatsapp_template, ConversationEngine, sandbox_reply,
         publish_bot, assert_within_limit, record_usage, schedule_for_appointment,
         cancel_for_appointment)  # fmt: skip
    assert is_optout_message("STOP") and not is_optout_message("cancelar mi cita")
    assert "[email]" in redact_pii("a@b.co")
    assert to_e164_co("300 111 2233") == ("+573001112233", "mobile")
    assert to_e164_co("abc") == (None, "unknown") and to_e164_co(None) == (None, "unknown")


def test_twilio_signature_validation():
    import base64
    import hashlib
    import hmac

    from app.channels.sender import validate_twilio_signature

    url, params, token = "https://x.co/hook", {"B": "2", "A": "1"}, "tok"
    sig = base64.b64encode(
        hmac.new(b"tok", b"https://x.co/hookA1B2", hashlib.sha1).digest()
    ).decode()
    assert validate_twilio_signature(url, params, sig, token)
    assert not validate_twilio_signature(url, params, "mala", token)
    assert not validate_twilio_signature(url, {**params, "A": "9"}, sig, token)


async def test_privacy_stub_suppression(session):
    from app.privacy.service import apply_optout, is_suppressed, record_consent

    assert not await is_suppressed(session, "+573001112233")
    await apply_optout(session, phone_e164="+573001112233", evidence="STOP")
    await apply_optout(session, phone_e164="+573001112233", evidence="STOP")  # idempotente
    assert await is_suppressed(session, "+573001112233")
    assert not await is_suppressed(session, "+573009998877")
    import uuid

    from app.db.models.contacts import Contact
    from app.db.models.tenants import Tenant

    t = Tenant(slug="s", name="S")
    session.add(t)
    await session.flush()
    c = Contact(tenant_id=t.id, phone_hash="h")
    session.add(c)
    await session.flush()
    await record_consent(session, t.id, c.id, "atencion", "hola")
    await apply_optout(session, phone_e164="+573001110000", tenant_id=t.id, evidence="baja")
    assert uuid  # noqa
