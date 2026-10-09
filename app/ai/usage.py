"""Registro de consumo de LLM (``llm_usage``) y contadores del plan."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.bots import LlmUsage
from app.plans.entitlements import record_usage

# USD por millon de tokens (entrada, salida). Estimacion para el panel de costos.
_PRICE_PER_MTOK = (3.0, 15.0)


def estimate_cost_usd(tokens_in: int, tokens_out: int) -> float:
    return round((tokens_in * _PRICE_PER_MTOK[0] + tokens_out * _PRICE_PER_MTOK[1]) / 1_000_000, 6)


def merge_usage(total: dict[str, int], usage: Mapping[str, int]) -> None:
    for key, value in usage.items():
        total[key] = total.get(key, 0) + int(value)


async def record_llm_usage(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    usage: Mapping[str, int],
    *,
    purpose: str = "conversation",
    model: str | None = None,
    count_plan: bool = True,
) -> None:
    """Una fila ``llm_usage`` y (opcional) el contador mensual de tokens del plan."""
    tokens_in = int(usage.get("input_tokens", 0))
    tokens_out = int(usage.get("output_tokens", 0))
    if not (tokens_in or tokens_out):
        return
    session.add(
        LlmUsage(
            tenant_id=tenant_id,
            purpose=purpose,
            model=model or get_settings().anthropic_model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_read=int(usage.get("cache_read_input_tokens", 0)),
            cost_usd=estimate_cost_usd(tokens_in, tokens_out),
        )
    )
    if count_plan:
        await record_usage(session, tenant_id, tokens_in=tokens_in, tokens_out=tokens_out)
