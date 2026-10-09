from __future__ import annotations

import uuid
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels import service
from app.core import jobs as core_jobs
from app.db.models.contacts import Contact
from app.db.models.conversations import Conversation, Message
from app.db.models.scheduling import WebhookEvent
from app.db.models.tenants import ChannelAccount
from tests.channels.conftest import (
    BIZ_NUMBER as BIZ,
)
from tests.channels.conftest import (
    PLATFORM_TOKEN,
    TENANT_TOKEN,
    WA_URL,
    inbound_params,
    signed,
)

PATH = "/webhooks/twilio/whatsapp"


async def test_valid_inbound_persists_and_enqueues(
    client: httpx.AsyncClient, session: AsyncSession, channel: ChannelAccount
) -> None:
    params = inbound_params(body="Quiero una cita")
    r = await client.post(PATH, data=params, headers=signed(params))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert "<Response/>" in r.text
    msgs = (await session.execute(select(Message))).scalars().all()
    assert len(msgs) == 1 and msgs[0].provider_sid == params["MessageSid"]
    assert msgs[0].processed_at is None and msgs[0].direction == "in"
    assert b"Quiero" not in (msgs[0].body_enc or b"")  # cifrado
    contact = (await session.execute(select(Contact))).scalar_one()
    assert contact.phone_enc and contact.display_name_enc
    conv = (await session.execute(select(Conversation))).scalar_one()
    assert conv.last_inbound_at is not None
    assert [j[0] for j in core_jobs.ENQUEUED] == ["channels.process_inbound"]


async def test_invalid_signature_403_nothing_stored(
    client: httpx.AsyncClient, session: AsyncSession, channel: ChannelAccount
) -> None:
    params = inbound_params()
    r = await client.post(PATH, data=params, headers={"X-Twilio-Signature": "mala"})
    assert r.status_code == 403
    r2 = await client.post(PATH, data=params)
    assert r2.status_code == 403
    assert (await session.execute(select(func.count(Message.id)))).scalar_one() == 0
    assert not core_jobs.ENQUEUED


async def test_unknown_number_403(client: httpx.AsyncClient, channel: ChannelAccount) -> None:
    params = {**inbound_params(), "To": "whatsapp:+570000000"}
    r = await client.post(PATH, data=params, headers=signed(params))
    assert r.status_code == 403


async def test_duplicate_sid_is_idempotent(
    client: httpx.AsyncClient, session: AsyncSession, channel: ChannelAccount
) -> None:
    params = inbound_params()
    for _ in range(3):
        r = await client.post(PATH, data=params, headers=signed(params))
        assert r.status_code == 200
    assert (await session.execute(select(func.count(Message.id)))).scalar_one() == 1
    assert (await session.execute(select(func.count(WebhookEvent.id)))).scalar_one() == 1
    assert len(core_jobs.ENQUEUED) == 1


async def test_tenant_token_takes_precedence(
    client: httpx.AsyncClient, channel: ChannelAccount, add_tenant_token: Any
) -> None:
    await add_tenant_token("twilio_auth_token", TENANT_TOKEN)
    params = inbound_params()
    bad = await client.post(PATH, data=params, headers=signed(params, PLATFORM_TOKEN))
    assert bad.status_code == 403
    ok = await client.post(PATH, data=params, headers=signed(params, TENANT_TOKEN))
    assert ok.status_code == 200


async def test_second_message_reuses_conversation(
    client: httpx.AsyncClient, session: AsyncSession, channel: ChannelAccount
) -> None:
    for body in ("uno", "dos"):
        p = inbound_params(body=body)
        await client.post(PATH, data=p, headers=signed(p))
    assert (await session.execute(select(func.count(Conversation.id)))).scalar_one() == 1
    assert (await session.execute(select(func.count(Contact.id)))).scalar_one() == 1


async def test_media_only_and_oversized_body(
    client: httpx.AsyncClient, session: AsyncSession, channel: ChannelAccount
) -> None:
    p = inbound_params(body="", NumMedia="2")
    assert (await client.post(PATH, data=p, headers=signed(p))).status_code == 200
    p2 = inbound_params(body="x" * 9000, NumMedia="zz")
    assert (await client.post(PATH, data=p2, headers=signed(p2))).status_code == 200
    rows = (await session.execute(select(Message).order_by(Message.created_at))).scalars().all()
    assert rows[0].media_count == 2 and rows[1].media_count == 0


async def test_missing_sid_ignored(client: httpx.AsyncClient, channel: ChannelAccount) -> None:
    p = {k: v for k, v in inbound_params().items() if k != "MessageSid"}
    r = await client.post(PATH, data=p, headers=signed(p))
    assert r.status_code == 400 and not core_jobs.ENQUEUED


# --------------------------------------------------------------------------- status
STATUS = "/webhooks/twilio/status"
STATUS_URL = "http://test/webhooks/twilio/status"


async def _outbound(session: AsyncSession, tid: uuid.UUID, sid: str) -> Message:
    conv = Conversation(tenant_id=tid, channel="whatsapp")
    session.add(conv)
    await session.flush()
    m = Message(
        tenant_id=tid,
        conversation_id=conv.id,
        direction="out",
        role="assistant",
        provider_sid=sid,
        status="sent",
    )
    session.add(m)
    await session.commit()
    return m


def _status(sid: str, status: str, **extra: str) -> dict[str, str]:
    return {
        "MessageSid": sid,
        "MessageStatus": status,
        "From": "whatsapp:+576015550100",
        "To": "whatsapp:+573001234567",
        **extra,
    }


async def test_status_monotonic(
    client: httpx.AsyncClient, session: AsyncSession, channel: ChannelAccount, tid: uuid.UUID
) -> None:
    m = await _outbound(session, tid, "SMout1")
    for st in ("delivered", "read", "sent", "queued"):
        p = _status("SMout1", st)
        r = await client.post(STATUS, data=p, headers=signed(p, url=STATUS_URL))
        assert r.status_code == 200
    await session.refresh(m)
    assert m.status == "read"


async def test_status_failed_stores_error_code(
    client: httpx.AsyncClient, session: AsyncSession, channel: ChannelAccount, tid: uuid.UUID
) -> None:
    m = await _outbound(session, tid, "SMout2")
    p = _status("SMout2", "failed", ErrorCode="63016")
    await client.post(STATUS, data=p, headers=signed(p, url=STATUS_URL))
    await session.refresh(m)
    assert m.status == "failed" and m.error_code == "63016"


async def test_status_21610_marks_optout(
    client: httpx.AsyncClient, session: AsyncSession, channel: ChannelAccount, tid: uuid.UUID
) -> None:
    contact = await service.get_or_create_contact(session, tid, "+573001234567", None)
    conv = Conversation(tenant_id=tid, contact_id=contact.id)
    session.add(conv)
    await session.flush()
    session.add(
        Message(
            tenant_id=tid,
            conversation_id=conv.id,
            direction="out",
            role="assistant",
            provider_sid="SMout3",
            status="sent",
        )
    )
    await session.commit()
    p = _status("SMout3", "undelivered", ErrorCode="21610")
    await client.post(STATUS, data=p, headers=signed(p, url=STATUS_URL))
    await session.refresh(contact)
    assert contact.opted_out is True


async def test_status_duplicate_and_unknown_and_bad_status(
    client: httpx.AsyncClient, session: AsyncSession, channel: ChannelAccount, tid: uuid.UUID
) -> None:
    await _outbound(session, tid, "SMout4")
    for p in (
        _status("SMout4", "delivered"),
        _status("SMout4", "delivered"),
        _status("SMnone", "delivered"),
        _status("SMout4", "weird"),
    ):
        r = await client.post(STATUS, data=p, headers=signed(p, url=STATUS_URL))
        # SID desconocido: 503 para que Twilio reintente cuando el SID se confirme
        assert r.status_code == (503 if p["MessageSid"] == "SMnone" else 200)


async def test_status_bad_signature(
    client: httpx.AsyncClient, channel: ChannelAccount, tid: uuid.UUID
) -> None:
    p = _status("SMx", "sent")
    assert (
        await client.post(STATUS, data=p, headers={"X-Twilio-Signature": "x"})
    ).status_code == 403


async def test_status_platform_fallback(client: httpx.AsyncClient) -> None:
    seen: list[dict[str, str]] = []

    async def handler(_s: AsyncSession, params: dict[str, str]) -> None:
        seen.append(params)

    service.STATUS_FALLBACKS.append(handler)
    try:
        p = {**_status("SMagency", "delivered"), "From": "whatsapp:+15550001111"}
        ok = await client.post(STATUS, data=p, headers=signed(p, url=STATUS_URL))
        assert ok.status_code == 200 and seen and seen[0]["MessageSid"] == "SMagency"
        bad = await client.post(STATUS, data=p, headers={"X-Twilio-Signature": "x"})
        assert bad.status_code == 403
    finally:
        service.STATUS_FALLBACKS.remove(handler)


# --------------------------------------------------------------------------- voz
async def test_voice_stub(client: httpx.AsyncClient, channel: ChannelAccount) -> None:
    url = "http://test/webhooks/twilio/voice"
    p = {"CallSid": "CA1", "From": "+573001234567", "To": "+576015550100"}
    r = await client.post("/webhooks/twilio/voice", data=p, headers=signed(p, url=url))
    assert r.status_code == 200 and "<Say" in r.text and "<Hangup/>" in r.text
    assert (await client.post("/webhooks/twilio/voice", data=p)).status_code == 403
    s_url = "http://test/webhooks/twilio/voice/status"
    sp = {**p, "CallStatus": "completed"}
    ok = await client.post("/webhooks/twilio/voice/status", data=sp, headers=signed(sp, url=s_url))
    assert ok.status_code == 200
    assert (await client.post("/webhooks/twilio/voice/status", data=sp)).status_code == 403
    unknown = {**p, "To": "+570000"}
    assert (
        await client.post("/webhooks/twilio/voice", data=unknown, headers=signed(unknown, url=url))
    ).status_code == 403


async def test_signature_uses_query_string(
    client: httpx.AsyncClient, channel: ChannelAccount
) -> None:
    p = inbound_params()
    r = await client.post(PATH + "?x=1", data=p, headers=signed(p, url=WA_URL + "?x=1"))
    assert r.status_code == 200


async def test_status_before_sid_is_not_claimed(
    client: httpx.AsyncClient, session: AsyncSession, channel: ChannelAccount
) -> None:
    params = {
        "MessageSid": "SMlate",
        "MessageStatus": "failed",
        "ErrorCode": "21610",
        "From": f"whatsapp:{BIZ}",
    }
    url = "http://test/webhooks/twilio/status"
    r = await client.post("/webhooks/twilio/status", data=params, headers=signed(params, url=url))
    assert r.status_code == 503
    assert (await session.execute(select(func.count(WebhookEvent.id)))).scalar_one() == 0


async def test_enqueue_failure_releases_inbound_for_retry(
    client: httpx.AsyncClient,
    session: AsyncSession,
    channel: ChannelAccount,
    monkeypatch: Any,
) -> None:
    from app.channels import router as ch_router

    async def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("redis caido")

    monkeypatch.setattr(ch_router, "enqueue", boom)
    params = inbound_params()
    r = await client.post(PATH, data=params, headers=signed(params))
    assert r.status_code == 503
    assert (await session.execute(select(func.count(Message.id)))).scalar_one() == 0
    monkeypatch.undo()
    r2 = await client.post(PATH, data=params, headers=signed(params))
    assert r2.status_code == 200
    assert (await session.execute(select(func.count(Message.id)))).scalar_one() == 1
