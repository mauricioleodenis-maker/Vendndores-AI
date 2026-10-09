"""Cliente LLM (dueno: B5).

Todo acceso a Claude pasa por aqui. ``LLMClient.complete``, los dataclasses y ``get_llm`` son
contrato (MAESTRO §5). ``FakeLLM()`` (sin argumentos) debe seguir siendo valido: lo usa
``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

import anthropic

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import get_logger

log = get_logger(__name__)

#: Separa el bloque estable del system prompt (cacheable) del bloque dinamico por turno.
SYSTEM_SPLIT = "\n<!--VAI-DYNAMIC-->\n"

_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
    anthropic.OverloadedError,  # 529: sobrecarga transitoria del proveedor
)


class LLMError(Exception):
    """Fallo no recuperable del proveedor LLM (sin detalles del mensaje: puede contener PII)."""


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: dict[str, int] = field(default_factory=dict)


class LLMClient(Protocol):
    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> LLMResponse: ...


def _system_blocks(system: str) -> list[dict[str, Any]]:
    """Bloque estable con ``cache_control`` efimero + bloque dinamico (sin cache)."""
    stable, sep, dynamic = system.partition(SYSTEM_SPLIT)
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}
    ]
    if sep and dynamic.strip():
        blocks.append({"type": "text", "text": dynamic})
    return blocks


def _parse_response(resp: Any) -> LLMResponse:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in resp.content or []:
        kind = getattr(block, "type", "")
        if kind == "text":
            texts.append(block.text)
        elif kind == "tool_use":
            calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input or {})))
    usage = getattr(resp, "usage", None)
    return LLMResponse(
        text="".join(texts).strip(),
        tool_calls=calls,
        stop_reason=str(getattr(resp, "stop_reason", "") or "end_turn"),
        usage={
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        },
    )


class AnthropicClient:
    """Cliente sobre el SDK oficial ``AsyncAnthropic`` con reintentos con backoff exponencial."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 20.0,
        max_retries: int = 2,
        client: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        # max_retries=0 en el SDK: los reintentos los controlamos aqui (jitter y limite propios).
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout, max_retries=0
        )
        self.model = model
        self._max_retries = max_retries
        self._sleep = sleep

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "system": _system_blocks(system),
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        attempt = 0
        while True:
            try:
                return _parse_response(await self._client.messages.create(**kwargs))
            except _RETRYABLE as exc:
                if attempt >= self._max_retries:
                    log.warning("llm.failed", error=type(exc).__name__, attempts=attempt + 1)
                    raise LLMError(type(exc).__name__) from exc
                delay = min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.25)  # noqa: S311
                log.info("llm.retry", error=type(exc).__name__, attempt=attempt + 1)
                attempt += 1
                await self._sleep(delay)
            except anthropic.APIError as exc:
                log.warning("llm.failed", error=type(exc).__name__)
                raise LLMError(type(exc).__name__) from exc


class FakeLLM:
    """LLM de pruebas: devuelve respuestas guionadas en orden (o un texto por defecto)."""

    def __init__(self, responses: list[LLMResponse | str] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def queue(self, *responses: LLMResponse | str) -> None:
        self._responses.extend(responses)

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> LLMResponse:
        self.calls.append(
            {"system": system, "messages": messages, "tools": tools, "tool_choice": tool_choice}
        )
        if self._responses:
            nxt = self._responses.pop(0)
            return LLMResponse(text=nxt) if isinstance(nxt, str) else nxt
        return LLMResponse(
            text="Respuesta de prueba", usage={"input_tokens": 1, "output_tokens": 1}
        )


@lru_cache
def _build_default() -> AnthropicClient:
    settings = get_settings()
    key = settings.anthropic_api_key.get_secret_value()
    if not key:
        raise AppError("llm_not_configured", "El asistente de IA no esta configurado", 503)
    return AnthropicClient(key, settings.anthropic_model)


def get_llm() -> LLMClient:
    """Dependencia FastAPI; los tests la sobreescriben con ``FakeLLM``."""
    return _build_default()
