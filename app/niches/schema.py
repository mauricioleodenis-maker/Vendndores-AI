"""Esquema pydantic de las plantillas de nicho (``extra=forbid``).

El YAML en ``app/niches/templates/`` es la fuente de verdad. Todo texto visible al cliente final
puede traer marcadores ``{{variable}}``: o bien variables de ejecucion (``RUNTIME_VARIABLES``) o
bien variables que la fabrica completa por negocio (``required_variables``).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NicheCode = Literal["dentista", "clinica_estetica", "taller", "restaurante"]

PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

# Variables que el motor rellena en cada mensaje (no las configura la fabrica).
RUNTIME_VARIABLES: frozenset[str] = frozenset(
    {"nombre", "servicio", "fecha", "hora", "fecha_hora", "personas", "numero", "total",
     "domicilio", "tiempo"}
)  # fmt: skip

REQUIRED_MESSAGE_TEMPLATES: frozenset[str] = frozenset(
    {"saludo", "confirmacion", "recordatorio_24h", "recordatorio_2h"}
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Persona(_Strict):
    tone: str = Field(min_length=3)
    address_form: str = Field(min_length=3)
    max_lines: int = Field(ge=1, le=10)
    emojis: str
    avoid: list[str] = Field(default_factory=list)
    greeting: str = Field(min_length=10)
    identity: str = Field(min_length=10)


class TypicalService(_Strict):
    code: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=3)
    duration_min: int | None = Field(default=None, ge=5, le=600)
    price_min_cop: int | None = Field(default=None, ge=0)
    price_max_cop: int | None = Field(default=None, ge=0)
    price_note: str = ""
    requires_assessment: bool = False
    requires_deposit: bool = False
    escalates: bool = False
    sessions: int = Field(default=1, ge=1, le=50)
    notes: str = ""

    @model_validator(mode="after")
    def _price_range(self) -> TypicalService:
        lo, hi = self.price_min_cop, self.price_max_cop
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"{self.code}: price_min_cop > price_max_cop")
        return self


class FaqSeed(_Strict):
    question: str = Field(min_length=5)
    answer: str = Field(min_length=5)


class BookingRules(_Strict):
    slot_minutes: int = Field(ge=5, le=240)
    buffer_min: int = Field(default=0, ge=0, le=120)
    min_notice_hours: int = Field(ge=0, le=168)
    max_days_ahead: int = Field(ge=1, le=365)
    required_fields: list[str] = Field(min_length=1)
    optional_fields: list[str] = Field(default_factory=list)
    never_collect: list[str] = Field(default_factory=list)
    cancellation_policy_hours: int = Field(ge=0, le=168)
    confirmation_required: bool = True
    reminder_hours_before: list[int] = Field(default_factory=lambda: [24, 2])
    hours: dict[str, str] = Field(default_factory=dict)
    assessment_first_services: list[str] = Field(default_factory=list)
    deposit_pct: int | None = Field(default=None, ge=0, le=100)
    deposit_services: list[str] = Field(default_factory=list)
    notes: dict[str, str] = Field(default_factory=dict)


class EscalationRule(_Strict):
    level: Literal[1, 2, 3]  # 1 emergencia, 2 urgente/queja, 3 consulta humana
    name: str
    triggers: list[str] = Field(min_length=1)
    action: str
    handoff: str


class Compliance(_Strict):
    legal_basis: str
    consent_text: str
    retention: str
    rights: str
    retention_days: int | None = Field(default=None, ge=1)
    notes: list[str] = Field(default_factory=list)


class NicheTemplate(_Strict):
    niche: NicheCode
    version: int = Field(ge=1)
    display_name_es: str = Field(min_length=3)
    country: str = "CO"
    base_city: str = "Cali"
    currency: Literal["COP"] = "COP"
    timezone: str = "America/Bogota"
    persona: Persona
    typical_services: list[TypicalService] = Field(min_length=1)
    faq_seeds: list[FaqSeed] = Field(min_length=3)
    booking_rules: BookingRules
    required_questions: list[str] = Field(min_length=1)
    forbidden_topics: list[str] = Field(min_length=1)
    escalation_triggers: list[EscalationRule] = Field(min_length=1)
    message_templates: dict[str, str]
    compliance: Compliance
    required_variables: list[str] = Field(min_length=1)
    off_topic_response: str = Field(min_length=10)
    sensitive_response: str = ""
    allowed_topics: list[str] = Field(default_factory=list)
    prompt_base: str = ""
    factory_inputs: dict[str, list[str]] = Field(default_factory=dict)
    extras: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistency(self) -> NicheTemplate:
        codes = [s.code for s in self.typical_services]
        if len(set(codes)) != len(codes):
            raise ValueError("codigos de servicio duplicados")
        known = set(codes)
        rules = self.booking_rules
        for ref in (*rules.assessment_first_services, *rules.deposit_services):
            if ref not in known:
                raise ValueError(f"servicio desconocido en reglas de agenda: {ref}")
        missing = REQUIRED_MESSAGE_TEMPLATES - self.message_templates.keys()
        if missing:
            raise ValueError(f"faltan plantillas de mensaje: {sorted(missing)}")
        allowed = RUNTIME_VARIABLES | set(self.required_variables)
        unknown = sorted(self.placeholders() - allowed)
        if unknown:
            raise ValueError(f"marcadores sin definir: {unknown}")
        return self

    def _texts(self) -> list[str]:
        return [
            self.persona.greeting,
            self.persona.identity,
            self.off_topic_response,
            self.sensitive_response,
            self.prompt_base,
            self.compliance.consent_text,
            self.compliance.rights,
            self.compliance.retention,
            *self.message_templates.values(),
            *(f.answer for f in self.faq_seeds),
            *(r.action for r in self.escalation_triggers),
        ]

    def placeholders(self) -> set[str]:
        """Todos los ``{{variable}}`` usados en textos de la plantilla."""
        found: set[str] = set()
        for text in self._texts():
            found.update(PLACEHOLDER_RE.findall(text))
        return found

    def service(self, code: str) -> TypicalService | None:
        return next((s for s in self.typical_services if s.code == code), None)


def render_placeholders(text: str, values: dict[str, str]) -> str:
    """Sustituye ``{{variable}}``; los desconocidos se dejan intactos (nunca falla)."""
    return PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)
