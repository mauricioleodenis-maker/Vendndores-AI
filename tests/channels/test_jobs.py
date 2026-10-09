from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import FakeLLM
from app.channels import jobs
from app.conversation import memory
from app.conversation.engine import ConversationEngine
from app.core import jobs as core_jobs
from app.core.clock import utcnow
from app.db.models.contacts import Consent, Contact
from app.db.models.conversations import Conversation, Message
from app.db.models.privacy import SuppressionEntry
from app.db.models.tenants import ChannelAccount
from tests.channels.conftest import inbound_params, signed

PATH = "/webhooks/twilio/whatsapp"


async def _receive(client: httpx.AsyncClient, body: str, **extra: str) -> None:
    p = inbound_params(body=body, **extra)
    r = await client.post(PATH, data=p, headers=signed(p))
    assert r.status_code == 200


async def _outs(session: AsyncSession) -> list[Message]:
    rows = await session.execute(
        select(Message).where(Message.direction == "out").order_by(Message.created_at, Message.id)
    )
    return list(rows.scalars())


def _bodies(session_msgs: list[Message], tenant_id: Any) -> list[str]:
    return [memory.decrypt_body(tenant_id, m) for m in session_msgs]


async def test_full_flow_first_message_sends_notice_then_reply(
    client: httpx.AsyncClient,
    session: AsyncSession,
    channel: ChannelAccount,
    tid: uuid.UUID,
    _engine_llm: None,
    fake_llm: FakeLLM,
) -> None:
    fake_llm.queue("Claro, ¿para qué día?")
    await _receive(client, "Quiero una cita")
    assert await core_jobs.run_enqueued() == 1
    session.expire_all()
    outs = await _outs(session)
    bodies = _bodies(outs, tid)
    assert len(outs) == 2
    assert "asistente virtual" in bodies[0] and "STOP" in bodies[0]
    assert outs[0].template_name == "privacy_notice"
    assert bodies[1] == "Claro, ¿para qué día?"
    assert all(m.provider_sid and m.provider_sid.startswith("DRYRUN") for m in outs)
    inbound = (await session.execute(select(Message).where(Message.direction == "in"))).scalar_one()
    assert inbound.processed_at is not None


async def test_consent_si_records_and_notice_not_repeated(
    client: httpx.AsyncClient,
    session: AsyncSession,
    channel: ChannelAccount,
    tid: uuid.UUID,
    _engine_llm: None,
    fake_llm: FakeLLM,
) -> None:
    fake_llm.queue("Hola, ¿en qué te ayudo?", "Perfecto")
    await _receive(client, "Hola")
    await core_jobs.run_enqueued()
    await _receive(client, "SI")
    await core_jobs.run_enqueued()
    session.expire_all()
    consents = (await session.execute(select(Consent))).scalars().all()
    assert len(consents) == 1 and consents[0].purpose == "atencion"
    bodies = _bodies(await _outs(session), tid)
    assert any("Usaremos tus datos" in b for b in bodies)
    await _receive(client, "Quiero agendar")
    await core_jobs.run_enqueued()
    session.expire_all()
    notices = [m for m in await _outs(session) if m.template_name == "privacy_notice"]
    assert len(notices) == 1


async def test_optout_flow(
    client: httpx.AsyncClient,
    session: AsyncSession,
    channel: ChannelAccount,
    tid: uuid.UUID,
    _engine_llm: None,
    fake_llm: FakeLLM,
) -> None:
    await _receive(client, "STOP")
    await core_jobs.run_enqueued()
    session.expire_all()
    contact = (await session.execute(select(Contact))).scalar_one()
    assert contact.opted_out and contact.opt_out_source == "keyword"
    assert (await session.execute(select(SuppressionEntry))).scalars().first() is not None
    bodies = _bodies(await _outs(session), tid)
    assert len(bodies) == 1 and "no recibirás" in bodies[0]
    assert fake_llm.calls == []
    # mensajes posteriores: silencio total
    await _receive(client, "hola?")
    await core_jobs.run_enqueued()
    session.expire_all()
    assert len(await _outs(session)) == 1
    assert fake_llm.calls == []


async def test_media_only_message_gets_text_request(
    client: httpx.AsyncClient,
    session: AsyncSession,
    channel: ChannelAccount,
    tid: uuid.UUID,
    _engine_llm: None,
    fake_llm: FakeLLM,
) -> None:
    await _receive(client, "", NumMedia="1")
    await core_jobs.run_enqueued()
    session.expire_all()
    bodies = _bodies(await _outs(session), tid)
    assert len(bodies) == 2 and bodies[1] == jobs.MEDIA_REPLY  # aviso + peticion de texto
    assert fake_llm.calls == []


async def test_job_is_idempotent(
    client: httpx.AsyncClient,
    session: AsyncSession,
    channel: ChannelAccount,
    tid: uuid.UUID,
    _engine_llm: None,
    fake_llm: FakeLLM,
) -> None:
    await _receive(client, "Hola")
    name, args, kwargs = core_jobs.ENQUEUED[0]
    await core_jobs.run_enqueued()
    n_calls = len(fake_llm.calls)
    assert await jobs.process_inbound({}, *args, **kwargs) == 0
    assert len(fake_llm.calls) == n_calls


async def test_engine_failure_does_not_crash(
    client: httpx.AsyncClient,
    session: AsyncSession,
    channel: ChannelAccount,
    tid: uuid.UUID,
    _engine_llm: None,
) -> None:
    class Boom(ConversationEngine):
        async def handle_inbound(self, *a: Any, **k: Any) -> list[str]:
            raise RuntimeError("boom")

    await _receive(client, "Hola")
    _, (t, c, m), _ = core_jobs.ENQUEUED[0]
    import uuid

    sent = await jobs.process(session, uuid.UUID(t), uuid.UUID(c), uuid.UUID(m), Boom())
    assert sent == 1  # mensaje de respaldo, el paciente no queda sin respuesta
    inbound = (await session.execute(select(Message).where(Message.direction == "in"))).scalar_one()
    assert inbound.processed_at is not None


async def test_missing_phone_or_erased_contact_skipped(
    client: httpx.AsyncClient,
    session: AsyncSession,
    channel: ChannelAccount,
    tid: uuid.UUID,
    _engine_llm: None,
    fake_llm: FakeLLM,
) -> None:
    await _receive(client, "Hola")
    contact = (await session.execute(select(Contact))).scalar_one()
    contact.erased_at = utcnow()
    contact.phone_enc = None
    await session.commit()
    assert await core_jobs.run_enqueued() == 1
    assert fake_llm.calls == []
    assert await _outs(session) == []


async def test_optin_after_optout_resumes(
    client: httpx.AsyncClient,
    session: AsyncSession,
    channel: ChannelAccount,
    tid: uuid.UUID,
    _engine_llm: None,
    fake_llm: FakeLLM,
) -> None:
    await _receive(client, "STOP")
    await core_jobs.run_enqueued()
    await _receive(client, "START")
    await core_jobs.run_enqueued()
    session.expire_all()
    contact = (await session.execute(select(Contact))).scalar_one()
    assert contact.opted_out is False


def test_notice_text_and_yes_detection() -> None:
    assert "*Clinica*" in jobs.privacy_notice("Clinica")
    assert jobs._is_yes("Sí") and jobs._is_yes(" OK! ") and not jobs._is_yes("sí quiero cita")


async def test_conversation_closed_creates_new(
    client: httpx.AsyncClient,
    session: AsyncSession,
    channel: ChannelAccount,
    tid: uuid.UUID,
    _engine_llm: None,
) -> None:
    await _receive(client, "uno")
    conv = (await session.execute(select(Conversation))).scalar_one()
    conv.status = "closed"
    conv.last_inbound_at = utcnow() - timedelta(days=3)
    await session.commit()
    await _receive(client, "dos")
    assert len((await session.execute(select(Conversation))).scalars().all()) == 2


async def test_rights_and_erase_keywords_get_privacy_reply(
    client: httpx.AsyncClient,
    session: AsyncSession,
    channel: ChannelAccount,
    tid: uuid.UUID,
    _engine_llm: None,
    fake_llm: FakeLLM,
) -> None:
    for word in ("DERECHOS", "BORRAR MIS DATOS"):
        await _receive(client, word)
    await core_jobs.run_enqueued()
    session.expire_all()
    bodies = _bodies(await _outs(session), tid)
    assert len(bodies) == 2 and all("/privacidad" in b for b in bodies)
    assert not fake_llm.calls


async def test_media_first_message_sends_privacy_notice(
    client: httpx.AsyncClient,
    session: AsyncSession,
    channel: ChannelAccount,
    tid: uuid.UUID,
    _engine_llm: None,
) -> None:
    await _receive(client, "", NumMedia="1")
    await core_jobs.run_enqueued()
    session.expire_all()
    outs = await _outs(session)
    assert outs[0].template_name == "privacy_notice" and len(outs) == 2
