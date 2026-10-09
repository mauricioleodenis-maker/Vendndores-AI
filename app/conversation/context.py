"""Contexto del negocio (BotConfig publicada + catalogo) y construccion del system prompt."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import SYSTEM_SPLIT
from app.conversation.guardrails import (
    OutputContext,
    canary_for,
    digits_only,
    fold,
    parse_prices,
    reply_for,
    sanitize_config_text,
)
from app.core.clock import to_bogota, utcnow
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.db.models.booking import Appointment
from app.db.models.bots import BotConfig
from app.db.models.catalog import Faq, Service
from app.db.models.tenants import Tenant, TenantProfile

PROMPT_VERSION = "runtime@1.0.0"
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "ai" / "prompts"
_env = Environment(
    loader=FileSystemLoader(_PROMPTS_DIR),
    autoescape=False,  # noqa: S701 - texto plano para un LLM, no HTML
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=False,
    keep_trailing_newline=True,
)

NICHE_LABELS = {
    "dentista": "consultorio odontológico",
    "clinica_estetica": "clínica estética",
    "taller": "taller automotriz",
    "restaurante": "restaurante",
}
_DAYS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MONTHS_ES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "octubre", "noviembre", "diciembre",
)  # fmt: skip


@dataclass(slots=True)
class BusinessContext:
    tenant: Tenant
    bot: BotConfig | None
    services: list[Service]
    faqs: list[Faq]
    profile: TenantProfile | None
    config: dict[str, Any]
    templates: dict[str, Any]
    hours_text: str
    canary: str
    rules_text: str = ""
    service_by_id: dict[uuid.UUID, Service] = field(default_factory=dict)

    @property
    def booking_rules(self) -> dict[str, Any]:
        rules = self.bot.booking_rules if self.bot else {}
        return rules if isinstance(rules, dict) else {}

    @property
    def register(self) -> str:
        tone = self.config.get("tone")
        if isinstance(tone, dict) and tone.get("register") in {"tu", "usted"}:
            return str(tone["register"])
        profile_tone = fold(self.profile.tone) if self.profile else ""
        return "tú" if "tu" in profile_tone.split() or "tuteo" in profile_tone else "usted"


def format_hm(value: Any) -> str:
    return str(value)[:5]


def format_hours(raw: Any) -> str:
    """Formatea horarios tolerando las formas ``{mon:[{open,close}]}`` o ``{lunes:"9-18"}``."""
    if not isinstance(raw, dict) or not raw:
        return ""
    weekly = raw.get("weekly", raw)
    if not isinstance(weekly, dict):
        return ""
    parts: list[str] = []
    for idx, key in enumerate(_DAY_KEYS):
        value = weekly.get(key, weekly.get(_DAYS_ES[idx], weekly.get(str(idx))))
        if not value:
            continue
        if isinstance(value, str):
            ranges = value
        elif isinstance(value, list):
            chunks = []
            for item in value:
                if isinstance(item, dict):
                    chunks.append(
                        f"{format_hm(item.get('open', item.get('start', '')))}-"
                        f"{format_hm(item.get('close', item.get('end', '')))}"
                    )
                else:
                    chunks.append(str(item))
            ranges = ", ".join(chunks)
        else:
            continue
        parts.append(f"{_DAYS_ES[idx]} {ranges}")
    return "; ".join(parts)


def format_when(dt: datetime) -> str:
    local = to_bogota(dt)
    hour12 = local.hour % 12 or 12
    suffix = "a. m." if local.hour < 12 else "p. m."
    return (
        f"{_DAYS_ES[local.weekday()]} {local.day} de {_MONTHS_ES[local.month - 1]}, "
        f"{hour12}:{local.minute:02d} {suffix}"
    )


def price_text(service: Service) -> str:
    if service.price_cop is not None and not service.needs_review:
        return f"${service.price_cop:,}".replace(",", ".") + " COP"
    return service.price_note.strip() or "precio por confirmar con el equipo"


async def load_business_context(
    session: AsyncSession, tenant_id: uuid.UUID, bot_config_id: uuid.UUID | None = None
) -> BusinessContext:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFoundError("Negocio no encontrado")
    stmt = select(BotConfig).where(BotConfig.tenant_id == tenant_id)
    stmt = (
        stmt.where(BotConfig.id == bot_config_id)
        if bot_config_id
        else stmt.where(BotConfig.status == "published")
    )
    bot = (await session.execute(stmt)).scalars().first()
    if bot_config_id and bot is None:
        raise NotFoundError("Configuración no encontrada")
    services = list(
        (
            await session.execute(
                select(Service)
                .where(Service.tenant_id == tenant_id, Service.is_active.is_(True))
                .order_by(Service.sort, Service.name)
            )
        ).scalars()
    )
    faqs = list(
        (
            await session.execute(
                select(Faq)
                .where(
                    Faq.tenant_id == tenant_id,
                    Faq.is_active.is_(True),
                    Faq.needs_review.is_(False),
                )
                .order_by(Faq.sort)
            )
        ).scalars()
    )
    profile = (
        await session.execute(select(TenantProfile).where(TenantProfile.tenant_id == tenant_id))
    ).scalar_one_or_none()
    config = bot.config if bot is not None and isinstance(bot.config, dict) else {}
    templates = bot.templates if bot is not None and isinstance(bot.templates, dict) else {}
    hours_text = format_hours(config.get("hours")) or format_hours(
        profile.hours if profile else None
    )
    secret = get_settings().secret_key.get_secret_value()
    return BusinessContext(
        tenant=tenant,
        bot=bot,
        services=services,
        faqs=faqs,
        profile=profile,
        config=config,
        templates=templates,
        hours_text=hours_text or "consultar con el equipo",
        canary=canary_for(secret, str(tenant_id)),
        service_by_id={s.id: s for s in services},
    )


async def active_appointments(
    session: AsyncSession, tenant_id: uuid.UUID, contact_id: uuid.UUID | None, *, limit: int = 5
) -> list[Appointment]:
    if contact_id is None:
        return []
    rows = await session.execute(
        select(Appointment)
        .where(
            Appointment.tenant_id == tenant_id,
            Appointment.contact_id == contact_id,
            Appointment.status.in_(("pending", "confirmed")),
            Appointment.starts_at >= utcnow(),
        )
        .order_by(Appointment.starts_at)
        .limit(limit)
    )
    return list(rows.scalars())


def build_system_prompt(
    bc: BusinessContext,
    *,
    appointments: list[Appointment] | None = None,
    summary: str = "",
) -> tuple[str, str]:
    """Devuelve ``(system, rules_text)``; ``rules_text`` es el bloque fijo (para detectar fugas)."""
    tenant = bc.tenant
    emoji = (
        bc.config.get("tone", {}).get("emoji_level")
        if isinstance(bc.config.get("tone"), dict)
        else None
    )
    services = [
        {
            "id": s.id,
            "name": sanitize_config_text(s.name, limit=120),
            "duration_min": s.duration_min,
            "price_text": sanitize_config_text(price_text(s), limit=120),
        }
        for s in bc.services
    ]
    faqs = [
        {
            "question": sanitize_config_text(f.question, limit=200),
            "answer": sanitize_config_text(f.answer, limit=600),
        }
        for f in bc.faqs
    ]
    appts = []
    for a in appointments or []:
        svc = bc.service_by_id.get(a.service_id) if a.service_id else None
        appts.append(
            {"id": a.id, "service": svc.name if svc else "Cita", "when": format_when(a.starts_at)}
        )
    now = to_bogota(utcnow())
    values: dict[str, Any] = {
        "business_name": sanitize_config_text(tenant.name, limit=120),
        "niche_label": NICHE_LABELS.get(tenant.niche, "negocio"),
        "city": sanitize_config_text(tenant.city, limit=80),
        "address": sanitize_config_text(tenant.address, limit=240),
        "phone": sanitize_config_text(tenant.phone_contact or "", limit=30),
        "register": bc.register,
        "emoji_rule": ", sin emojis" if emoji == "none" else ", máximo 1 emoji",
        "canary": bc.canary,
        "split": SYSTEM_SPLIT,
        "hours_text": sanitize_config_text(bc.hours_text, limit=400),
        "now_text": f"{format_when(now)} (hora de Bogotá)",
        "out_of_scope_reply": reply_for(bc.templates, "out_of_scope_reply"),
        "services": services,
        "faqs": faqs,
        "notes": sanitize_config_text(bc.bot.system_prompt, limit=2500) if bc.bot else "",
        "appointments": appts,
        "summary": sanitize_config_text(summary, limit=1500),
    }
    system = _env.get_template("runtime.md.j2").render(**values)
    rules_text = system.split(SYSTEM_SPLIT, 1)[0]
    return system, rules_text


def build_output_context(
    bc: BusinessContext, rules_text: str, extra_phones: set[str]
) -> OutputContext:
    """Listas blancas para ``check_output`` a partir del catalogo aprobado."""
    knowledge_parts = [bc.hours_text, bc.tenant.address]
    allowed_prices: set[int] = set()
    for s in bc.services:
        if s.price_cop is not None and not s.needs_review:
            allowed_prices.add(int(s.price_cop))
        knowledge_parts += [s.name, s.description, s.price_note]
    for f in bc.faqs:
        knowledge_parts += [f.question, f.answer]
    knowledge_parts += [
        reply_for(bc.templates, "out_of_scope_reply"),
        str(bc.config.get("welcome", "")),
    ]
    knowledge = "\n".join(p for p in knowledge_parts if p)
    allowed_prices |= parse_prices(knowledge)
    phones = {digits_only(p) for p in (bc.tenant.phone_contact, "123", *extra_phones) if p}
    if bc.profile and bc.profile.handoff_phone:
        phones.add(digits_only(bc.profile.handoff_phone))
    hosts = {
        h
        for h in (
            _host_of(bc.tenant.website_url),
            _host_of(bc.tenant.instagram_url),
            *_hosts_in(knowledge),
        )
        if h
    }
    return OutputContext(
        canary=bc.canary,
        rules_text=rules_text,
        allowed_prices=allowed_prices,
        allowed_hosts=hosts,
        allowed_phones={p for p in phones if p},
        knowledge_folded=fold(knowledge),
        templates=bc.templates,
    )


def _host_of(url: str | None) -> str:
    if not url:
        return ""
    return (
        fold(url).replace("https://", "").replace("http://", "").removeprefix("www.").split("/")[0]
    )


def _hosts_in(text: str) -> list[str]:
    import re

    return [_host_of(u) for u in re.findall(r"(?:https?://|www\.)[^\s)>\]]+", text, flags=re.I)]
