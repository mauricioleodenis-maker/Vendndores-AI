"""Esquemas de la fabrica: entrada del operador y ``GeneratedBotConfig`` (tool ``emit_bot_config``).

El JSON schema de ``GeneratedBotConfig`` es el ``input_schema`` de la tool forzada; todo lo que
devuelve el LLM se valida con estos modelos antes de tocar la base de datos.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TIME_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
DAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SCHEMA_VERSION = "1"

HandoffAction = Literal["notify_human", "pause_bot", "escalate_urgent"]
FlagType = Literal[
    "prompt_injection_suspected",
    "missing_price",
    "medical_claim_removed",
    "pii_found",
    "low_confidence",
]


class FactoryInput(BaseModel):
    """Datos que escribe el operador (fuente ``owner``, maxima prioridad)."""

    model_config = ConfigDict(extra="allow")

    name: str
    niche: str
    city: str = ""
    address: str = ""
    phone: str | None = None
    website_url: str | None = None
    instagram_url: str | None = None
    services: list[dict[str, Any]] = []
    hours: dict[str, Any] = {}
    notes: str = ""
    instructions: str = ""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BusinessInfo(_Strict):
    name: Annotated[str, Field(max_length=120)]
    niche: Literal["dentista", "clinica_estetica", "taller", "restaurante"]
    city: Annotated[str, Field(max_length=100)] = ""
    address: Annotated[str | None, Field(max_length=240)] = None
    phone: Annotated[str | None, Field(pattern=r"^\+?[0-9 ]{7,20}$")] = None
    timezone: Literal["America/Bogota"] = "America/Bogota"


class ServiceCfg(_Strict):
    id: Annotated[str, Field(pattern=r"^[a-z0-9_]{2,40}$")]
    name: Annotated[str, Field(min_length=2, max_length=120)]
    description: Annotated[str, Field(max_length=300)] = ""
    duration_min: Annotated[int, Field(ge=5, le=480)]
    price_cop: Annotated[int | None, Field(ge=0, le=50_000_000)] = None
    price_note: Annotated[str | None, Field(max_length=160)] = None
    requires_valuation: bool = False
    source_ref: Annotated[str, Field(max_length=300)] = "template"


class TimeRange(_Strict):
    open: Annotated[str, Field(pattern=TIME_RE)]
    close: Annotated[str, Field(pattern=TIME_RE)]


class HoursCfg(_Strict):
    weekly: dict[str, Annotated[list[TimeRange], Field(max_length=3)]] = Field(default_factory=dict)

    @field_validator("weekly")
    @classmethod
    def _days(cls, v: dict[str, list[TimeRange]]) -> dict[str, list[TimeRange]]:
        bad = sorted(set(v) - set(DAY_KEYS))
        if bad:
            raise ValueError(f"dias no validos: {bad}")
        return v


BookingField = Literal["name", "phone", "service", "reason_general", "email"]


def _default_fields() -> list[BookingField]:
    return ["name", "phone", "service"]


class BookingRulesCfg(_Strict):
    slot_minutes: Literal[15, 20, 30, 60] = 30
    min_notice_hours: Annotated[int, Field(ge=0, le=72)] = 2
    max_days_ahead: Annotated[int, Field(ge=1, le=90)] = 30
    buffer_minutes: Annotated[int, Field(ge=0, le=60)] = 0
    requires_fields: list[BookingField] = Field(default_factory=_default_fields)


class FaqCfg(_Strict):
    question: Annotated[str, Field(min_length=3, max_length=200)]
    answer: Annotated[str, Field(min_length=3, max_length=600)]
    source_ref: Annotated[str, Field(max_length=300)] = "template"


class ToneCfg(_Strict):
    address_form: Literal["tu", "usted"] = "usted"
    emoji_level: Literal["none", "low"] = "low"


class HandoffRule(_Strict):
    trigger: Annotated[str, Field(min_length=3, max_length=200)]
    action: HandoffAction = "notify_human"


class FlagCfg(_Strict):
    type: FlagType
    detail: Annotated[str, Field(max_length=240)] = ""


class GeneratedBotConfig(_Strict):
    schema_version: Literal["1"] = "1"
    business: BusinessInfo
    services: Annotated[list[ServiceCfg], Field(max_length=30)]
    hours: HoursCfg
    booking_rules: BookingRulesCfg
    faqs: Annotated[list[FaqCfg], Field(max_length=25)]
    tone: ToneCfg
    handoff_rules: list[HandoffRule]
    out_of_scope_topics: list[Annotated[str, Field(max_length=120)]]
    open_questions: Annotated[list[Annotated[str, Field(max_length=240)]], Field(max_length=20)]
    flags: list[FlagCfg]
    sources: list[Annotated[str, Field(max_length=300)]]

    @field_validator("services")
    @classmethod
    def _unique_ids(cls, v: list[ServiceCfg]) -> list[ServiceCfg]:
        ids = [s.id for s in v]
        if len(set(ids)) != len(ids):
            raise ValueError("ids de servicio duplicados")
        return v


def emit_bot_config_tool() -> dict[str, Any]:
    """Definicion de la tool forzada (formato Anthropic)."""
    return {
        "name": "emit_bot_config",
        "description": "Entrega la configuracion completa del recepcionista virtual del negocio.",
        "input_schema": GeneratedBotConfig.model_json_schema(),
    }


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify_service(name: str) -> str:
    base = _SLUG_RE.sub("_", name.lower().strip()).strip("_")[:40]
    return base if len(base) >= 2 else "servicio"
