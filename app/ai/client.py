"""Contrato del cliente LLM. STUB de la fundacion; el dueno (B5) completa AnthropicClient.

La firma de ``LLMClient.complete``, los dataclasses y ``get_llm`` NO deben cambiar.
``FakeLLM()`` (sin argumentos) debe seguir siendo valido: lo usa ``tests/conftest.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


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
        return LLMResponse(text="Respuesta de prueba", usage={"input_tokens": 1, "output_tokens": 1})


def get_llm() -> LLMClient:
    """Dependencia FastAPI. STUB: B5 devuelve ``AnthropicClient`` configurado."""
    raise NotImplementedError("get_llm: pendiente de B5 (app/ai/client.py)")
