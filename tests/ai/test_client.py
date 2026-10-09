"""AnthropicClient (SDK mockeado), FakeLLM, get_llm y registro de uso."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import client as client_mod
from app.ai.client import SYSTEM_SPLIT, AnthropicClient, FakeLLM, LLMError, get_llm
from app.ai.usage import estimate_cost_usd, record_llm_usage
from app.core.errors import AppError
from app.db.models.bots import LlmUsage
from app.db.models.plans import UsageCounter
from app.db.models.tenants import Tenant


def _resp(blocks: list[Any], stop: str = "end_turn") -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=11, output_tokens=7, cache_read_input_tokens=3)
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=usage)


class _Messages:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _make(outcomes: list[Any], **kw: Any) -> tuple[AnthropicClient, _Messages, list[float]]:
    msgs = _Messages(outcomes)
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    c = AnthropicClient(
        "k", "claude-test", client=SimpleNamespace(messages=msgs), sleep=fake_sleep, **kw
    )
    return c, msgs, sleeps


def _rate_limit() -> anthropic.RateLimitError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.RateLimitError(
        "slow down", response=httpx.Response(429, request=req), body=None
    )


async def test_parses_text_and_tool_use_and_usage() -> None:
    blocks = [
        SimpleNamespace(type="text", text="Hola "),
        SimpleNamespace(
            type="tool_use", id="t1", name="get_business_info", input={"topic": "precios"}
        ),
    ]
    c, msgs, _ = _make([_resp(blocks, "tool_use")])
    r = await c.complete(
        system="A" + SYSTEM_SPLIT + "B",
        messages=[{"role": "user", "content": "x"}],
        tools=[{"name": "t"}],
        tool_choice={"type": "auto"},
    )
    assert r.text == "Hola" and r.stop_reason == "tool_use"
    assert r.tool_calls[0].name == "get_business_info" and r.tool_calls[0].input == {
        "topic": "precios"
    }
    assert r.usage == {"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 3}
    call = msgs.calls[0]
    assert call["model"] == "claude-test"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["system"][1] == {"type": "text", "text": "B"}
    assert call["tool_choice"] == {"type": "auto"}


async def test_system_without_split_is_single_cached_block() -> None:
    c, msgs, _ = _make([_resp([SimpleNamespace(type="text", text="ok")])])
    await c.complete(system="solo", messages=[{"role": "user", "content": "x"}])
    assert len(msgs.calls[0]["system"]) == 1
    assert "tools" not in msgs.calls[0]


async def test_retries_rate_limit_then_succeeds() -> None:
    c, msgs, sleeps = _make(
        [_rate_limit(), _rate_limit(), _resp([SimpleNamespace(type="text", text="ok")])]
    )
    r = await c.complete(system="s", messages=[])
    assert r.text == "ok" and len(msgs.calls) == 3 and len(sleeps) == 2
    assert sleeps[1] > sleeps[0] - 0.3  # backoff creciente (con jitter)


async def test_gives_up_after_max_retries() -> None:
    c, msgs, _ = _make([_rate_limit()] * 3, max_retries=2)
    with pytest.raises(LLMError):
        await c.complete(system="s", messages=[])
    assert len(msgs.calls) == 3


async def test_non_retryable_api_error_fails_fast() -> None:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.BadRequestError("bad", response=httpx.Response(400, request=req), body=None)
    c, msgs, _ = _make([err])
    with pytest.raises(LLMError):
        await c.complete(system="s", messages=[])
    assert len(msgs.calls) == 1


async def test_fake_llm_scripted_and_default() -> None:
    f = FakeLLM(["uno"])
    f.queue("dos")
    assert (await f.complete(system="s", messages=[])).text == "uno"
    assert (await f.complete(system="s", messages=[])).text == "dos"
    assert (await f.complete(system="s", messages=[])).text == "Respuesta de prueba"
    assert len(f.calls) == 3


def test_get_llm_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    client_mod._build_default.cache_clear()
    monkeypatch.setenv("VAI_ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(AppError) as exc:
        get_llm()
    assert exc.value.code == "llm_not_configured"
    monkeypatch.setenv("VAI_ANTHROPIC_API_KEY", "sk-test")
    get_settings.cache_clear()
    client_mod._build_default.cache_clear()
    assert isinstance(get_llm(), AnthropicClient)
    client_mod._build_default.cache_clear()


async def test_record_llm_usage_writes_row_and_counter(
    session: AsyncSession, tenant: Tenant
) -> None:
    await record_llm_usage(
        session,
        tenant.id,
        {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 50},
    )
    await session.commit()
    row = (await session.execute(select(LlmUsage))).scalar_one()
    assert (row.tokens_in, row.tokens_out, row.cache_read) == (1000, 200, 50)
    assert row.cost_usd == estimate_cost_usd(1000, 200) > 0
    counter = (await session.execute(select(UsageCounter))).scalar_one()
    assert counter.llm_tokens_in == 1000 and counter.llm_tokens_out == 200


async def test_record_llm_usage_skips_empty_and_sandbox(
    session: AsyncSession, tenant: Tenant
) -> None:
    await record_llm_usage(session, tenant.id, {})
    await record_llm_usage(
        session,
        tenant.id,
        {"input_tokens": 5, "output_tokens": 5},
        purpose="sandbox",
        count_plan=False,
    )
    await session.commit()
    rows = (await session.execute(select(LlmUsage))).scalars().all()
    assert [r.purpose for r in rows] == ["sandbox"]
    assert (await session.execute(select(UsageCounter))).first() is None
