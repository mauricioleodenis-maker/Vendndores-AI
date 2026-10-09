# ruff: noqa: F811
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.voice.bridge as bridge
from app.ai.client import FakeLLM
from app.conversation.engine import ConversationEngine
from app.db.models.conversations import Conversation
from app.db.models.tenants import TenantProfile
from app.db.models.voice import CallSession
from app.plans.entitlements import Entitlements
from tests.conversation.conftest import Biz, agenda, biz, llm, text, tool_call  # noqa: F401

PHONE = "+573001234567"


@pytest.fixture
def voice_on(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    state = {"voice": True}

    async def fake_ent(session: Any, tenant_id: Any) -> Entitlements:
        return Entitlements(limits={"voice_enabled": state["voice"]})

    monkeypatch.setattr(bridge, "get_entitlements", fake_ent)
    return state


async def _turn(session: AsyncSession, biz: Biz, llm: FakeLLM, msg: str, sid: str = "CA1") -> Any:
    out = await bridge.voice_turn(
        session,
        tenant_id=biz.tenant.id,
        call_sid=sid,
        caller_e164=PHONE,
        utterance=msg,
        engine=ConversationEngine(llm),
    )
    await session.commit()
    return out


async def test_plan_without_voice_ends_call(
    session: AsyncSession, biz: Biz, llm: FakeLLM, voice_on: dict[str, bool]
) -> None:
    voice_on["voice"] = False
    out = await _turn(session, biz, llm, "Hola")
    assert out.end_call and "WhatsApp" in out.text
    assert (await session.execute(select(CallSession))).first() is None


async def test_turn_creates_voice_conversation_and_session(
    session: AsyncSession, biz: Biz, llm: FakeLLM, voice_on: dict[str, bool]
) -> None:
    llm.queue(text("Claro. Tenemos limpieza dental. ¿Te interesa agendar? También hay valoración."))
    out = await _turn(session, biz, llm, "Hola, qué servicios tienen")
    assert not out.end_call and out.transfer_to is None
    assert out.text.count(".") + out.text.count("?") <= 2
    assert "valoración" not in out.text
    call = (await session.execute(select(CallSession))).scalar_one()
    assert call.turns == 1 and call.status == "active" and call.consent_at is not None
    conv = await session.get(Conversation, call.conversation_id)
    assert conv is not None and conv.channel == "voice"


async def test_same_call_reuses_conversation(
    session: AsyncSession, biz: Biz, llm: FakeLLM, voice_on: dict[str, bool]
) -> None:
    llm.queue(text("Hola, ¿en qué te ayudo?"), text("Con gusto."))
    await _turn(session, biz, llm, "Hola")
    await _turn(session, biz, llm, "Precio")
    calls = (await session.execute(select(CallSession))).scalars().all()
    assert len(calls) == 1 and calls[0].turns == 2
    convs = (
        (await session.execute(select(Conversation).where(Conversation.channel == "voice")))
        .scalars()
        .all()
    )
    assert len(convs) == 1


async def test_handoff_transfers_to_tenant_phone(
    session: AsyncSession, biz: Biz, llm: FakeLLM, voice_on: dict[str, bool]
) -> None:
    session.add(TenantProfile(tenant_id=biz.tenant.id, handoff_phone="+573105550000"))
    await session.commit()
    llm.queue(tool_call("handoff_to_human", reason="human_requested"), text("Te paso con alguien."))
    out = await _turn(session, biz, llm, "Quiero hablar con una persona")
    assert out.transfer_to == "+573105550000"
    call = (await session.execute(select(CallSession))).scalar_one()
    assert call.status == "transferred"


async def test_handoff_without_phone_ends_call(
    session: AsyncSession, biz: Biz, llm: FakeLLM, voice_on: dict[str, bool]
) -> None:
    llm.queue(tool_call("handoff_to_human", reason="human_requested"), text("Te paso con alguien."))
    out = await _turn(session, biz, llm, "Quiero hablar con una persona")
    assert out.end_call and out.transfer_to is None


async def test_max_turns_escalates(
    session: AsyncSession,
    biz: Biz,
    llm: FakeLLM,
    voice_on: dict[str, bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session.add(TenantProfile(tenant_id=biz.tenant.id, handoff_phone="+573105550000"))
    session.add(CallSession(tenant_id=biz.tenant.id, call_sid="CA9", turns=bridge.MAX_TURNS))
    await session.commit()
    out = await _turn(session, biz, llm, "otra vez", sid="CA9")
    assert out.transfer_to == "+573105550000"


async def test_finished_call_is_not_processed(
    session: AsyncSession, biz: Biz, llm: FakeLLM, voice_on: dict[str, bool]
) -> None:
    session.add(CallSession(tenant_id=biz.tenant.id, call_sid="CA8", status="completed"))
    await session.commit()
    out = await _turn(session, biz, llm, "hola", sid="CA8")
    assert out.end_call


def test_shorten_limits_sentences_and_strips_markdown() -> None:
    out = bridge.shorten("**Hola.** Uno. Dos. Tres.")
    assert out == "Hola. Uno."
