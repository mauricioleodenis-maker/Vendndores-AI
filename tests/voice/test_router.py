from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

import app.voice.router as vrouter
from app.channels.sender import sign_twilio
from app.db.models.tenants import ChannelAccount, Tenant
from app.plans.entitlements import Entitlements
from tests.channels.conftest import PLATFORM_TOKEN, _twilio_env  # noqa: F401

NUMBER = "+576015550199"
IN_URL = "http://test/webhooks/twilio/voice/incoming"
GA_URL = "http://test/webhooks/twilio/voice/gather"


@pytest_asyncio.fixture
async def voice_channel(session: AsyncSession, tenant: Tenant) -> ChannelAccount:
    acc = ChannelAccount(
        tenant_id=tenant.id, channel="voice", phone_e164=NUMBER, voice_enabled=True
    )
    session.add(acc)
    await session.commit()
    return acc


@pytest.fixture(autouse=True)
def _wire(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    app.include_router(vrouter.router)
    state: dict[str, Any] = {"voice": True, "calls": [], "reply": None}

    async def fake_ent(session: Any, tenant_id: Any) -> Entitlements:
        return Entitlements(limits={"voice_enabled": state["voice"]})

    async def fake_turn(session: Any, **kw: Any) -> Any:
        state["calls"].append(kw)
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return state["reply"]

    monkeypatch.setattr(vrouter, "get_entitlements", fake_ent)
    monkeypatch.setitem(sys.modules, "app.voice.bridge", SimpleNamespace(voice_turn=fake_turn))
    return state


def reply(text: str, end: bool = False, transfer: str | None = None) -> Any:
    return SimpleNamespace(text=text, end_call=end, transfer_to=transfer)


def params(**extra: str) -> dict[str, str]:
    return {"CallSid": "CA1", "From": "+573001234567", "To": NUMBER, **extra}


async def post(
    client: httpx.AsyncClient, path: str, url: str, p: dict[str, str], sig: bool = True
) -> httpx.Response:
    headers = {"X-Twilio-Signature": sign_twilio(url, p, PLATFORM_TOKEN)} if sig else {}
    return await client.post(path, data=p, headers=headers)


def root(r: httpx.Response) -> ElementTree.Element:
    assert r.headers["content-type"].startswith("application/xml")
    return ElementTree.fromstring(r.text)  # noqa: S314


async def test_incoming_gather_with_notice(
    client: httpx.AsyncClient, voice_channel: ChannelAccount
) -> None:
    r = await post(client, "/webhooks/twilio/voice/incoming", IN_URL, params())
    assert r.status_code == 200
    g = root(r).find("Gather")
    assert g is not None and g.get("input") == "speech" and g.get("language") == "es-CO"
    assert g.get("action") == GA_URL
    says = g.findall("Say")
    assert says[0].get("language") == "es-MX" and "registra" in (says[0].text or "")
    assert "Clinica Demo" in (says[1].text or "")


async def test_bad_or_missing_signature_403(
    client: httpx.AsyncClient, voice_channel: ChannelAccount
) -> None:
    p = params()
    r = await client.post(
        "/webhooks/twilio/voice/incoming", data=p, headers={"X-Twilio-Signature": "x"}
    )
    assert r.status_code == 403
    r = await post(client, "/webhooks/twilio/voice/gather", GA_URL, p, sig=False)
    assert r.status_code == 403


async def test_unknown_number_and_non_voice_channel_403(
    client: httpx.AsyncClient, session: AsyncSession, tenant: Tenant
) -> None:
    session.add(ChannelAccount(tenant_id=tenant.id, channel="whatsapp", phone_e164=NUMBER))
    await session.commit()
    r = await post(client, "/webhooks/twilio/voice/incoming", IN_URL, params())
    assert r.status_code == 403
    p = params(To="+570000000000")
    r = await post(client, "/webhooks/twilio/voice/incoming", IN_URL, p)
    assert r.status_code == 403


async def test_plan_without_voice_hangs_up(
    client: httpx.AsyncClient, voice_channel: ChannelAccount, _wire: dict[str, Any]
) -> None:
    _wire["voice"] = False
    for path, url in (("incoming", IN_URL), ("gather", GA_URL)):
        r = await post(client, f"/webhooks/twilio/voice/{path}", url, params(SpeechResult="hola"))
        x = root(r)
        assert x.find("Gather") is None and x.find("Hangup") is not None
    assert not _wire["calls"]


async def test_gather_turn_continues_and_escapes(
    client: httpx.AsyncClient, voice_channel: ChannelAccount, _wire: dict[str, Any]
) -> None:
    _wire["reply"] = reply("Cuesta <b>50 mil</b> & más. ¿Algo más?")
    p = params(SpeechResult="cuánto cuesta")
    r = await post(client, "/webhooks/twilio/voice/gather", GA_URL, p)
    assert "&lt;b&gt;" in r.text and "&amp;" in r.text
    g = root(r).find("Gather")
    assert g is not None and (g.find("Say").text or "").startswith("Cuesta <b>")  # type: ignore[union-attr]
    call = _wire["calls"][0]
    assert call["call_sid"] == "CA1" and call["caller_e164"] == "+573001234567"
    assert call["utterance"] == "cuánto cuesta" and call["tenant_id"] == voice_channel.tenant_id


async def test_gather_end_call(
    client: httpx.AsyncClient, voice_channel: ChannelAccount, _wire: dict[str, Any]
) -> None:
    _wire["reply"] = reply("Listo, hasta pronto.", end=True)
    r = await post(client, "/webhooks/twilio/voice/gather", GA_URL, params(SpeechResult="adiós"))
    x = root(r)
    assert x.find("Hangup") is not None and x.find("Gather") is None


async def test_gather_transfer_dial(
    client: httpx.AsyncClient, voice_channel: ChannelAccount, _wire: dict[str, Any]
) -> None:
    _wire["reply"] = reply("Te comunico.", transfer="+57 300 111 2233")
    r = await post(client, "/webhooks/twilio/voice/gather", GA_URL, params(SpeechResult="humano"))
    assert root(r).find("Dial").text == "+573001112233"  # type: ignore[union-attr]
    _wire["reply"] = reply("Te comunico.", transfer="<x>")
    r = await post(client, "/webhooks/twilio/voice/gather", GA_URL, params(SpeechResult="humano"))
    assert root(r).find("Dial") is None


async def test_empty_speech_reprompts_then_hangs_up(
    client: httpx.AsyncClient, voice_channel: ChannelAccount, _wire: dict[str, Any]
) -> None:
    r = await post(client, "/webhooks/twilio/voice/gather", GA_URL, params())
    g = root(r).find("Gather")
    assert g is not None and g.get("action").endswith("?r=1")  # type: ignore[union-attr]
    url2 = GA_URL + "?r=2"
    r = await post(client, "/webhooks/twilio/voice/gather?r=2", url2, params())
    assert root(r).find("Hangup") is not None and root(r).find("Gather") is None
    assert not _wire["calls"]


async def test_engine_failure_apologizes(
    client: httpx.AsyncClient, voice_channel: ChannelAccount, _wire: dict[str, Any]
) -> None:
    _wire["reply"] = RuntimeError("boom")
    r = await post(client, "/webhooks/twilio/voice/gather", GA_URL, params(SpeechResult="hola"))
    assert r.status_code == 200 and root(r).find("Hangup") is not None
