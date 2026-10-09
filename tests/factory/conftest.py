"""Fixtures de la fabrica: configuracion valida del LLM y FactoryInput del dueno."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.ai.client import LLMResponse, ToolCall
from app.factory.schemas import FactoryInput


def valid_config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "schema_version": "1",
        "business": {
            "name": "Clinica Demo",
            "niche": "dentista",
            "city": "Cali",
            "address": "Calle 5 # 10-20",
            "phone": "+57 300 123 4567",
            "timezone": "America/Bogota",
        },
        "services": [
            {
                "id": "valoracion",
                "name": "Valoracion odontologica",
                "description": "Revision general",
                "duration_min": 30,
                "price_cop": 60000,
                "price_note": None,
                "requires_valuation": False,
                "source_ref": "owner",
            },
            {
                "id": "limpieza",
                "name": "Limpieza dental",
                "description": "",
                "duration_min": 45,
                "price_cop": 99000,
                "price_note": None,
                "requires_valuation": False,
                "source_ref": "template",
            },
        ],
        "hours": {"weekly": {"mon": [{"open": "08:00", "close": "17:00"}]}},
        "booking_rules": {
            "slot_minutes": 30,
            "min_notice_hours": 2,
            "max_days_ahead": 30,
            "buffer_minutes": 0,
            "requires_fields": ["name", "phone", "service"],
        },
        "faqs": [
            {"question": "Donde quedan?", "answer": "En Cali, Calle 5.", "source_ref": "owner"}
        ],
        "tone": {"address_form": "usted", "emoji_level": "low"},
        "handoff_rules": [{"trigger": "El cliente pide una persona", "action": "notify_human"}],
        "out_of_scope_topics": ["diagnosticos"],
        "open_questions": [],
        "flags": [],
        "sources": ["owner"],
    }
    cfg.update(copy.deepcopy(overrides))
    return cfg


def tool_response(config: dict[str, Any], name: str = "emit_bot_config") -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id="tc1", name=name, input=config)],
        stop_reason="tool_use",
        usage={"input_tokens": 1200, "output_tokens": 800},
    )


@pytest.fixture
def owner_input() -> FactoryInput:
    return FactoryInput(
        name="Clinica Demo",
        niche="dentista",
        city="Cali",
        address="Calle 5 # 10-20",
        phone="+57 300 123 4567",
        services=[{"name": "Valoracion odontologica", "price_cop": 60000, "duration_min": 30}],
        hours={"mon": [["08:00", "17:00"]]},
        notes="Atendemos solo lunes",
    )
