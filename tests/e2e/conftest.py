"""Utilidades compartidas de los E2E: Twilio firmado, LLM guionado y fixtures JSON."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

import app.channels.jobs  # noqa: F401  (registra los jobs del canal)
import app.conversation.engine as engine_mod
import app.factory.service as factory_service
from app.ai.client import FakeLLM, LLMResponse, ToolCall
from app.channels.sender import sign_twilio
from app.core import jobs as core_jobs
from app.core.config import get_settings

DATA = Path(__file__).parent.parent / "fixtures" / "data"
E2E_TOKEN = "platform-token-e2e"
WA_URL = "http://test/webhooks/twilio/whatsapp"
HX = {"HX-Request": "true"}

Say = Callable[..., Awaitable[httpx.Response]]


def load_fixture(name: str) -> Any:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def tool_call(name: str, **args: Any) -> LLMResponse:
    return LLMResponse(text="", tool_calls=[ToolCall("t1", name, args)], stop_reason="tool_use")


@pytest.fixture(autouse=True)
def e2e_twilio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAI_TWILIO_AUTH_TOKEN", E2E_TOKEN)
    monkeypatch.setenv("VAI_TWILIO_ACCOUNT_SID", "ACplatform")
    monkeypatch.setenv("VAI_TWILIO_WHATSAPP_FROM", "+15550001111")
    get_settings.cache_clear()


@pytest.fixture
def e2e_llm(monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM) -> FakeLLM:
    """El mismo FakeLLM para la fabrica, el motor y los jobs (no usan dependency_overrides)."""
    monkeypatch.setattr(factory_service, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(engine_mod, "get_llm", lambda: fake_llm)
    return fake_llm


def make_say(client: httpx.AsyncClient, business: str) -> Say:
    """``await say("hola", sender="+57...")``: POST firmado + ejecuta los jobs encolados."""

    async def say(body: str, sender: str = "+573001234567") -> httpx.Response:
        params = {
            "MessageSid": f"SM{uuid.uuid4().hex}",
            "From": f"whatsapp:{sender}",
            "To": f"whatsapp:{business}",
            "Body": body,
            "NumMedia": "0",
            "ProfileName": "Paciente",
        }
        sig = sign_twilio(WA_URL, params, E2E_TOKEN)
        resp = await client.post(
            "/webhooks/twilio/whatsapp", data=params, headers={"X-Twilio-Signature": sig}
        )
        await core_jobs.run_enqueued()
        return resp

    return say
