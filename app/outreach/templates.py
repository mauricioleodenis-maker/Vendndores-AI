"""Plantillas de outreach: semilla (support/07 + pitch de support/06), render y variables."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.leads import Lead
from app.db.models.outreach import MessageTemplate

AGENCY_NAME = "Vendedores AI"
_PLACEHOLDER = re.compile(r"\{\{\s*(\d+)\s*\}\}")
_MAX_VAR_LEN = 60

NICHE_LABELS: dict[str, str] = {
    "dentista": "tu clinica",
    "clinica_estetica": "tu clinica",
    "taller": "tu taller",
    "restaurante": "tu restaurante",
}

# Claves de variable conocidas y su valor por defecto (None = obligatoria).
VARIABLE_DEFAULTS: dict[str, str | None] = {
    "contact_name": "hola",
    "agency": AGENCY_NAME,
    "business_type": "tu negocio",
    "business_name": None,
    "response_minutes": None,
    "sent_at_local": None,
    "scenario": "tu consulta",
}


class TemplateRenderError(ValueError):
    """Falta una variable obligatoria o la plantilla es inconsistente."""


@dataclass(frozen=True, slots=True)
class SeedTemplate:
    name: str
    kind: str
    category: str
    body: str
    variables: tuple[str, ...]


SEED_TEMPLATES: tuple[SeedTemplate, ...] = (
    SeedTemplate(
        name="lead_contacto_inicial",
        kind="pitch",
        category="marketing",
        body=(
            "Hola {{1}}, somos del equipo de {{2}}. Vimos que {{3}} podria ahorrar tiempo con un "
            "recepcionista automatico que responde a tus clientes por WhatsApp las 24 horas. "
            "Te interesa una demo corta? Responde SI para recibir informacion o NO para no "
            "recibir mas mensajes."
        ),
        variables=("contact_name", "agency", "business_type"),
    ),
    SeedTemplate(
        name="lead_prueba_secreta",
        kind="pitch",
        category="marketing",
        body=(
            "Hola {{1}}, somos de {{2}}. Hicimos una consulta de {{3}} por WhatsApp a {{4}} y la "
            "respuesta tardo {{5}} minutos. Un recepcionista con IA responde en segundos, las 24 "
            "horas. Te la mostramos en 2 minutos? Responde SI o NO para no recibir mas mensajes."
        ),
        variables=("contact_name", "agency", "scenario", "business_name", "response_minutes"),
    ),
    SeedTemplate(
        name="lead_seguimiento",
        kind="seguimiento",
        category="marketing",
        body=(
            "Hola {{1}}, te escribimos de {{2}} hace unos dias sobre un recepcionista con IA para "
            "{{3}}. Si te interesa verlo funcionando, responde SI. Si no, responde NO y no te "
            "escribimos mas."
        ),
        variables=("contact_name", "agency", "business_type"),
    ),
    SeedTemplate(
        name="optout_confirmacion",
        kind="optout_confirm",
        category="utility",
        body=(
            "Listo, {{1}}. Dejamos de enviarte mensajes de {{2}}. Si cambias de opinion, "
            "escribenos en cualquier momento."
        ),
        variables=("contact_name", "agency"),
    ),
)


async def seed_templates(session: AsyncSession) -> int:
    """Crea las plantillas semilla que falten (como borrador, sin aprobar). Idempotente."""
    existing = set(
        (
            await session.execute(
                select(MessageTemplate.name).where(MessageTemplate.scope == "outreach")
            )
        ).scalars()
    )
    created = 0
    for seed in SEED_TEMPLATES:
        if seed.name in existing:
            continue
        session.add(
            MessageTemplate(
                scope="outreach",
                name=seed.name,
                version=1,
                kind=seed.kind,
                category=seed.category,
                language="es_CO",
                body=seed.body,
                variables=list(seed.variables),
                approval_status="draft",
                approved=False,
            )
        )
        created += 1
    if created:
        await session.flush()
    return created


def is_sendable(template: MessageTemplate) -> bool:
    """Solo se envian plantillas aprobadas por Meta y con ``ContentSid`` registrado."""
    return bool(
        template.approved
        and template.approval_status == "approved"
        and template.twilio_content_sid
        and template.scope == "outreach"
    )


def placeholder_count(body: str) -> int:
    nums = {int(m) for m in _PLACEHOLDER.findall(body)}
    return max(nums) if nums else 0


def validate_template(body: str, variables: list[str]) -> None:
    """Los {{n}} deben ser 1..N, coincidir con ``variables`` y no estar en los extremos."""
    nums = sorted({int(m) for m in _PLACEHOLDER.findall(body)})
    if nums != list(range(1, len(variables) + 1)):
        raise TemplateRenderError("Las variables {{n}} no coinciden con la lista de variables")
    unknown = [v for v in variables if v not in VARIABLE_DEFAULTS]
    if unknown:
        raise TemplateRenderError(f"Variable desconocida: {unknown[0]}")
    stripped = body.strip()
    if stripped.startswith("{{") or stripped.endswith("}}"):
        raise TemplateRenderError("La plantilla no puede empezar ni terminar con una variable")


def _clean(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:_MAX_VAR_LEN]


def build_variables(
    template: MessageTemplate,
    lead: Lead,
    evidence: dict[str, Any] | None = None,
    *,
    agency: str = AGENCY_NAME,
) -> dict[str, str]:
    """Resuelve ``{"1": ..., "2": ...}`` para Twilio. La evidencia solo cita al propio negocio.

    Lanza ``TemplateRenderError`` si falta una variable obligatoria (p. ej. sin prueba secreta).
    """
    ev = evidence or {}
    values: dict[str, Any] = {
        "contact_name": "",
        "agency": agency,
        "business_type": NICHE_LABELS.get(lead.niche, "tu negocio"),
        "business_name": lead.name,
        "scenario": ev.get("scenario") or "",
        "response_minutes": ev.get("response_minutes") if ev.get("available") else None,
        "sent_at_local": ev.get("sent_at_local") if ev.get("available") else None,
    }
    out: dict[str, str] = {}
    for idx, key in enumerate(template.variables or [], start=1):
        raw = values.get(key)
        if raw is None or str(raw).strip() == "":
            default = VARIABLE_DEFAULTS.get(key)
            if default is None:
                raise TemplateRenderError(f"Falta la variable {key}")
            raw = default
        out[str(idx)] = _clean(raw)
    return out


def render_body(body: str, variables: dict[str, str]) -> str:
    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise TemplateRenderError(f"Falta el valor de {{{{{key}}}}}")
        return variables[key]

    return _PLACEHOLDER.sub(_sub, body)
