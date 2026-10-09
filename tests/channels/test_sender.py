from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels import sender
from app.channels.service import get_or_create_contact
from app.core.clock import utcnow
from app.core.config import get_settings
from app.db.models.conversations import Conversation
from app.db.models.tenants import ChannelAccount, Tenant
from app.privacy.service import apply_optout
from tests.channels.conftest import BIZ_NUMBER, CUSTOMER, TENANT_TOKEN


def test_signature_roundtrip_and_tamper() -> None:
    params = {"b": "2", "a": "1"}
    sig = sender.sign_twilio("http://x/y", params, "tok")
    assert sender.validate_twilio_signature("http://x/y", params, sig, "tok")
    assert not sender.validate_twilio_signature("http://x/y", {**params, "a": "9"}, sig, "tok")
    assert not sender.validate_twilio_signature("http://x/y", params, sig, "otro")
    assert not sender.validate_twilio_signature("http://x/y", params, "", "tok")
    assert not sender.validate_twilio_signature("http://x/y", params, sig, "")


def test_window_and_prefix_helpers() -> None:
    now = utcnow()
    assert sender.window_is_open(now - timedelta(hours=23), now)
    assert not sender.window_is_open(now - timedelta(hours=25), now)
    assert not sender.window_is_open(None)
    assert sender.to_whatsapp("+57300") == "whatsapp:+57300"
    assert sender.to_whatsapp("whatsapp:+57300") == "whatsapp:+57300"
    assert sender.from_whatsapp("whatsapp:+57 300") == "+57300"
    assert "ventana" in sender.failure_reason("63016")
    assert sender.failure_reason("99999").startswith("Error")
    assert sender.failure_reason(None) == ""


async def _open_window(session: AsyncSession, tenant: Tenant, hours_ago: float = 1) -> None:
    contact = await get_or_create_contact(session, tenant.id, CUSTOMER, None)
    session.add(
        Conversation(
            tenant_id=tenant.id,
            contact_id=contact.id,
            last_inbound_at=utcnow() - timedelta(hours=hours_ago),
        )
    )
    await session.commit()


async def test_text_dry_run_inside_window(
    session: AsyncSession, tenant: Tenant, channel: ChannelAccount
) -> None:
    await _open_window(session, tenant)
    res = await sender.send_whatsapp_text(
        session, tenant_id=tenant.id, to_e164=CUSTOMER, body="hola"
    )
    assert res.ok and res.dry_run and res.sid and res.sid.startswith("DRYRUN")


async def test_text_outside_window_blocked(
    session: AsyncSession, tenant: Tenant, channel: ChannelAccount
) -> None:
    await _open_window(session, tenant, hours_ago=30)
    res = await sender.send_whatsapp_text(
        session, tenant_id=tenant.id, to_e164=CUSTOMER, body="hola"
    )
    assert not res.ok and res.error == "63016"


async def test_text_unknown_contact_blocked(session: AsyncSession, tenant: Tenant) -> None:
    res = await sender.send_whatsapp_text(
        session, tenant_id=tenant.id, to_e164=CUSTOMER, body="hola"
    )
    assert not res.ok


async def test_suppressed_never_sent(
    session: AsyncSession, tenant: Tenant, channel: ChannelAccount
) -> None:
    await _open_window(session, tenant)
    await apply_optout(session, phone_e164=CUSTOMER, tenant_id=tenant.id, evidence="t")
    await session.commit()
    res = await sender.send_whatsapp_text(
        session, tenant_id=tenant.id, to_e164=CUSTOMER, body="hola"
    )
    assert not res.ok and res.status == "suppressed"
    ok = await sender.send_whatsapp_text(
        session, tenant_id=tenant.id, to_e164=CUSTOMER, body="baja", bypass_suppression=True
    )
    assert ok.ok
    tpl = await sender.send_whatsapp_template(
        session, tenant_id=None, to_e164=CUSTOMER, content_sid="HX1", variables={"1": "a"}
    )
    assert tpl.status == "suppressed"


async def test_template_dry_run(session: AsyncSession) -> None:
    res = await sender.send_whatsapp_template(
        session, tenant_id=None, to_e164=CUSTOMER, content_sid="HX1", variables={"1": "Ana"}
    )
    assert res.ok and res.dry_run


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAI_TWILIO_DRY_RUN", "false")
    get_settings.cache_clear()


async def test_live_text_uses_tenant_credentials(
    session: AsyncSession,
    tenant: Tenant,
    channel: ChannelAccount,
    add_tenant_token,  # type: ignore[no-untyped-def]
    live: None,
) -> None:
    await add_tenant_token("twilio_sid", "ACtenant")
    await add_tenant_token("twilio_auth_token", TENANT_TOKEN)
    await _open_window(session, tenant)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://api.twilio.com/2010-04-01/Accounts/ACtenant/Messages.json").mock(
            return_value=httpx.Response(201, json={"sid": "SM1", "status": "queued"})
        )
        res = await sender.send_whatsapp_text(
            session, tenant_id=tenant.id, to_e164=CUSTOMER, body="hola"
        )
    assert res.ok and res.sid == "SM1" and not res.dry_run
    req = route.calls.last.request
    form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
    assert form["From"] == f"whatsapp%3A%2B{BIZ_NUMBER[1:]}"
    assert "StatusCallback" in form
    assert req.headers["authorization"].startswith("Basic ")


async def test_live_template_platform_with_override(session: AsyncSession, live: None) -> None:
    with respx.mock() as mock:
        route = mock.post(
            "https://api.twilio.com/2010-04-01/Accounts/ACplatform/Messages.json"
        ).mock(return_value=httpx.Response(201, json={"sid": "SM2", "status": "accepted"}))
        res = await sender.send_whatsapp_template(
            session,
            tenant_id=None,
            to_e164=CUSTOMER,
            content_sid="HXabc",
            variables={"1": "Ana"},
            from_override="whatsapp:+15559998888",
        )
    assert res.ok
    body = route.calls.last.request.content.decode()
    assert "ContentSid=HXabc" in body and "15559998888" in body
    assert json.dumps({"1": "Ana"}).replace(" ", "") in body.replace("%22", '"').replace(
        "%3A", ":"
    ).replace("%7B", "{").replace("%7D", "}").replace("+", "")


async def test_live_client_error_not_retried(
    session: AsyncSession, live: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with respx.mock() as mock:
        route = mock.post(url__regex=r".*Messages.json").mock(
            return_value=httpx.Response(400, json={"code": 21211})
        )
        res = await sender.send_whatsapp_template(
            session, tenant_id=None, to_e164=CUSTOMER, content_sid="HX", variables={}
        )
    assert not res.ok and res.error == "21211" and route.call_count == 1


async def test_live_retries_then_fails(
    session: AsyncSession, live: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _nosleep(_: float) -> None:
        return None

    monkeypatch.setattr(sender.asyncio, "sleep", _nosleep)
    with respx.mock() as mock:
        route = mock.post(url__regex=r".*Messages.json").mock(
            side_effect=[
                httpx.ConnectError("x"),
                httpx.Response(503, text="no json"),
                httpx.Response(201, json={"sid": "SM9", "status": "queued"}),
            ]
        )
        res = await sender.send_whatsapp_template(
            session, tenant_id=None, to_e164=CUSTOMER, content_sid="HX", variables={}
        )
        assert res.ok and res.sid == "SM9" and route.call_count == 3
        mock.post(url__regex=r".*Messages.json").mock(return_value=httpx.Response(429, json={}))
        res2 = await sender.send_whatsapp_template(
            session, tenant_id=None, to_e164=CUSTOMER, content_sid="HX", variables={}
        )
    assert not res2.ok


async def test_live_not_configured(
    session: AsyncSession, tenant: Tenant, live: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tenant sin canal: nunca cae al numero de la agencia
    await _open_window(session, tenant)
    res = await sender.send_whatsapp_text(
        session, tenant_id=tenant.id, to_e164=CUSTOMER, body="hola"
    )
    assert not res.ok and res.error == "twilio_no_configurado"
