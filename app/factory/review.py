"""Lectura y edicion de versiones del bot + catalogo (servicios/FAQs) durante la revision.

Las tablas ``services``/``faqs`` son la copia de trabajo; ``BotConfig.config`` es la foto
versionada que se congela al publicar.
"""

from __future__ import annotations

import copy
import re
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.audit.service import log_event
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models.bots import BotConfig
from app.db.models.catalog import Faq, Service
from app.db.models.tenants import Tenant
from app.factory.grounding import fold
from app.factory.prompts import render_system_prompt
from app.factory.schemas import (
    DAY_KEYS,
    FaqCfg,
    GeneratedBotConfig,
    ServiceCfg,
    TimeRange,
    slugify_service,
)

_RANGE_RE = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*[-–]\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")
FAQ_SOURCE_REF = {"scrape": "web", "template": "template", "manual": "owner"}


# --------------------------------------------------------------------------- consultas
async def get_bot(session: AsyncSession, bot_config_id: uuid.UUID) -> BotConfig:
    bot = await session.get(BotConfig, bot_config_id)
    if bot is None:
        raise NotFoundError("Versión del bot no encontrada")
    return bot


async def get_tenant_bot(
    session: AsyncSession, tenant_id: uuid.UUID, bot_config_id: uuid.UUID
) -> BotConfig:
    bot = await get_bot(session, bot_config_id)
    if bot.tenant_id != tenant_id:
        raise NotFoundError("Versión del bot no encontrada")
    return bot


async def list_versions(session: AsyncSession, tenant_id: uuid.UUID) -> list[BotConfig]:
    stmt = (
        select(BotConfig).where(BotConfig.tenant_id == tenant_id).order_by(BotConfig.version.desc())
    )
    return list((await session.execute(stmt)).scalars())


async def get_by_status(
    session: AsyncSession, tenant_id: uuid.UUID, status: str
) -> BotConfig | None:
    stmt = (
        select(BotConfig)
        .where(BotConfig.tenant_id == tenant_id, BotConfig.status == status)
        .order_by(BotConfig.version.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def current_draft(session: AsyncSession, tenant_id: uuid.UUID) -> BotConfig | None:
    return await get_by_status(session, tenant_id, "draft")


async def published_bot(session: AsyncSession, tenant_id: uuid.UUID) -> BotConfig | None:
    return await get_by_status(session, tenant_id, "published")


async def next_version(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    # Serializa creadores concurrentes de versiones del mismo tenant (no-op en SQLite)
    await session.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    top = await session.scalar(
        select(func.max(BotConfig.version)).where(BotConfig.tenant_id == tenant_id)
    )
    return int(top or 0) + 1


async def load_full(session: AsyncSession, tenant_id: uuid.UUID, bot_id: uuid.UUID) -> BotConfig:
    """Carga la version completa (recarga columnas diferidas por ``list_version_summaries``)."""
    stmt = (
        select(BotConfig)
        .where(BotConfig.id == bot_id, BotConfig.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    bot = (await session.execute(stmt)).scalar_one_or_none()
    if bot is None:
        raise NotFoundError("Versión del bot no encontrada")
    return bot


async def list_version_summaries(session: AsyncSession, tenant_id: uuid.UUID) -> list[BotConfig]:
    """Versiones sin cargar JSON pesado (config, prompt, meta): solo para listados."""
    stmt = (
        select(BotConfig)
        .options(
            defer(BotConfig.config),
            defer(BotConfig.system_prompt),
            defer(BotConfig.generation_meta),
            defer(BotConfig.source_inputs),
            defer(BotConfig.booking_rules),
            defer(BotConfig.guardrails),
            defer(BotConfig.templates),
        )
        .where(BotConfig.tenant_id == tenant_id)
        .order_by(BotConfig.version.desc())
    )
    return list((await session.execute(stmt)).scalars())


def rollback_blockers(bot: BotConfig, cfg: GeneratedBotConfig) -> list[str]:
    """Pendientes de revision de la version destino de un rollback."""
    out: list[str] = []
    info = review_info(bot)
    if cfg.open_questions:
        out.append(
            f"Esa versión tiene {len(cfg.open_questions)} pregunta(s) abiertas sin resolver."
        )
    if any(f.type == "prompt_injection_suspected" for f in cfg.flags) and not info.get("flags_ack"):
        out.append("Esa versión tiene una alerta de posible inyección sin revisar.")
    if info.get("hours_ungrounded") and not info.get("hours_confirmed"):
        out.append("Los horarios de esa versión no fueron confirmados.")
    return out


async def list_services(session: AsyncSession, tenant_id: uuid.UUID) -> list[Service]:
    stmt = (
        select(Service)
        .where(Service.tenant_id == tenant_id, Service.is_active.is_(True))
        .order_by(Service.sort, Service.name)
    )
    return list((await session.execute(stmt)).scalars())


async def list_faqs(session: AsyncSession, tenant_id: uuid.UUID) -> list[Faq]:
    stmt = (
        select(Faq)
        .where(Faq.tenant_id == tenant_id, Faq.is_active.is_(True))
        .order_by(Faq.sort, Faq.created_at)
    )
    return list((await session.execute(stmt)).scalars())


def parsed_config(bot: BotConfig) -> GeneratedBotConfig:
    try:
        return GeneratedBotConfig.model_validate(bot.config)
    except ValidationError as exc:
        raise AppError("config_invalid", "La configuración guardada no es válida", 422) from exc


def meta_of(bot: BotConfig) -> dict[str, Any]:
    return copy.deepcopy(dict(bot.generation_meta or {}))


def set_meta(bot: BotConfig, **changes: Any) -> None:
    """Reasigna el JSON completo (SQLAlchemy no detecta mutaciones in-place)."""
    meta = meta_of(bot)
    meta.update(changes)
    bot.generation_meta = meta


def review_info(bot: BotConfig) -> dict[str, Any]:
    return dict((bot.generation_meta or {}).get("review") or {})


# --------------------------------------------------------------------------- catalogo
async def sync_catalog_from_config(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    config: GeneratedBotConfig,
    *,
    needs_review_services: set[str] | None = None,
    needs_review_faqs: set[int] | None = None,
    replace: bool = False,
) -> None:
    """Escribe servicios/FAQs de ``config`` en las tablas.

    ``replace=False`` (build): los servicios existentes del dueno NO se pisan; las FAQs de
    plantilla/scrape se reemplazan y las manuales se conservan.
    ``replace=True`` (rollback): la foto manda; lo que no esta en ella se desactiva.
    """
    flagged = needs_review_services or set()
    flagged_faqs = needs_review_faqs or set()
    existing = {
        fold(s.name): s
        for s in (
            await session.execute(select(Service).where(Service.tenant_id == tenant_id))
        ).scalars()
    }
    kept: set[str] = set()
    for i, svc in enumerate(config.services):
        key = fold(svc.name)
        kept.add(key)
        row = existing.get(key)
        if row is None:
            session.add(
                Service(
                    tenant_id=tenant_id,
                    name=svc.name,
                    description=svc.description,
                    price_cop=svc.price_cop,
                    price_note=svc.price_note or "",
                    duration_min=svc.duration_min,
                    needs_review=svc.id in flagged,
                    sort=i,
                )
            )
        elif replace:
            row.description = svc.description
            row.price_cop = svc.price_cop
            row.price_note = svc.price_note or ""
            row.duration_min = svc.duration_min
            row.is_active = True
            row.needs_review = False
            row.sort = i
    if replace:
        for key, row in existing.items():
            if key not in kept:
                row.is_active = False
    if replace:
        await session.execute(delete(Faq).where(Faq.tenant_id == tenant_id))
    else:
        await session.execute(
            delete(Faq).where(Faq.tenant_id == tenant_id, Faq.source.in_(("template", "scrape")))
        )
    manual = {
        fold(q)
        for q in (
            await session.execute(
                select(Faq.question).where(Faq.tenant_id == tenant_id, Faq.source == "manual")
            )
        ).scalars()
    }
    for i, faq in enumerate(config.faqs):
        if fold(faq.question) in manual:
            continue
        ref = faq.source_ref
        source = "scrape" if ref.startswith("web") else "manual" if ref == "owner" else "template"
        session.add(
            Faq(
                tenant_id=tenant_id,
                question=faq.question,
                answer=faq.answer,
                source=source,
                needs_review=i in flagged_faqs,
                sort=i,
            )
        )
    await session.flush()


def unique_id(name: str, taken: set[str]) -> str:
    base = slugify_service(name)
    candidate, n = base, 2
    while candidate in taken:
        suffix = f"_{n}"
        candidate = base[: 40 - len(suffix)] + suffix
        n += 1
    taken.add(candidate)
    return candidate


async def snapshot_config(
    session: AsyncSession, tenant_id: uuid.UUID, config: GeneratedBotConfig
) -> GeneratedBotConfig:
    """``config`` con servicios/FAQs tomados de las tablas (lo que realmente se publica)."""
    refs = {fold(s.name): s for s in config.services}
    taken: set[str] = set()
    services: list[ServiceCfg] = []
    for row in (await list_services(session, tenant_id))[:30]:
        prev = refs.get(fold(row.name))
        services.append(
            ServiceCfg(
                id=prev.id if prev and prev.id not in taken else unique_id(row.name, taken),
                name=row.name[:120],
                description=(row.description or "")[:300],
                duration_min=min(max(row.duration_min, 5), 480),
                price_cop=row.price_cop,
                price_note=(row.price_note or None),
                requires_valuation=prev.requires_valuation if prev else False,
                source_ref=prev.source_ref if prev else "owner",
            )
        )
        taken.add(services[-1].id)
    faqs = [
        FaqCfg(
            question=f.question[:200],
            answer=f.answer[:600],
            source_ref=FAQ_SOURCE_REF.get(f.source, "owner"),
        )
        for f in (await list_faqs(session, tenant_id))[:25]
    ]
    data = config.model_copy(update={"services": services, "faqs": faqs})
    return GeneratedBotConfig.model_validate(data.model_dump())


def rerender_prompt(bot: BotConfig, config: GeneratedBotConfig) -> None:
    extra = list((bot.guardrails or {}).get("prompt_rules") or [])
    greeting = str((bot.templates or {}).get("saludo", ""))
    bot.system_prompt = render_system_prompt(config, persona_greeting=greeting, extra_rules=extra)


# --------------------------------------------------------------------------- bloqueos
async def publish_blockers(
    session: AsyncSession, bot: BotConfig, *, require_sandbox: bool = False
) -> list[str]:
    """Motivos (en espanol) por los que la version aun no se puede publicar."""
    out: list[str] = []
    cfg = parsed_config(bot)
    services = await list_services(session, bot.tenant_id)
    if not services:
        out.append("Agrega al menos un servicio.")
    pending_s = [s.name for s in services if s.needs_review]
    if pending_s:
        out.append("Confirma los servicios marcados: " + ", ".join(pending_s[:5]))
    pending_f = [f.question[:40] for f in await list_faqs(session, bot.tenant_id) if f.needs_review]
    if pending_f:
        out.append(f"Confirma {len(pending_f)} pregunta(s) frecuente(s) marcadas.")
    info = review_info(bot)
    if info.get("hours_ungrounded") and not info.get("hours_confirmed"):
        out.append("Confirma los horarios (no se encontraron en las fuentes).")
    if cfg.open_questions:
        out.append(f"Resuelve las {len(cfg.open_questions)} pregunta(s) abiertas.")
    if any(f.type == "prompt_injection_suspected" for f in cfg.flags) and not info.get("flags_ack"):
        out.append("Revisa y confirma la alerta de posible inyección de instrucciones.")
    if require_sandbox and not (bot.generation_meta or {}).get("sandbox_tested"):
        out.append("Prueba el bot en el chat de prueba al menos una vez.")
    return out


# --------------------------------------------------------------------------- ediciones
def require_draft(bot: BotConfig) -> None:
    if bot.status != "draft":
        raise ConflictError("Solo se puede editar un borrador. Crea un borrador nuevo primero.")


async def ensure_draft(session: AsyncSession, tenant_id: uuid.UUID, *, actor: Any) -> BotConfig:
    """Devuelve el borrador vigente o clona la version publicada en un borrador nuevo."""
    draft = await current_draft(session, tenant_id)
    if draft is not None:
        return draft
    base = await published_bot(session, tenant_id)
    if base is None:
        raise NotFoundError("Aún no hay un bot para esta empresa")
    meta = meta_of(base)
    meta.update(sandbox_tested=False, cloned_from=base.version)
    meta["review"] = {}
    draft = BotConfig(
        tenant_id=tenant_id,
        version=await next_version(session, tenant_id),
        status="draft",
        system_prompt=base.system_prompt,
        booking_rules=copy.deepcopy(base.booking_rules),
        guardrails=copy.deepcopy(base.guardrails),
        templates=copy.deepcopy(base.templates),
        config=copy.deepcopy(base.config),
        generation_meta=meta,
        source_inputs=copy.deepcopy(base.source_inputs),
        model=base.model,
        generated_by="manual",
    )
    session.add(draft)
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="bot.draft_cloned",
        entity_type="bot_config",
        entity_id=draft.id,
        tenant_id=tenant_id,
        diff={"from_version": base.version, "version": draft.version},
    )
    return draft


async def _service(session: AsyncSession, tenant_id: uuid.UUID, service_id: uuid.UUID) -> Service:
    row = await session.get(Service, service_id)
    if row is None or row.tenant_id != tenant_id:
        raise NotFoundError("Servicio no encontrado")
    return row


async def _faq(session: AsyncSession, tenant_id: uuid.UUID, faq_id: uuid.UUID) -> Faq:
    row = await session.get(Faq, faq_id)
    if row is None or row.tenant_id != tenant_id:
        raise NotFoundError("Pregunta no encontrada")
    return row


async def update_service(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    service_id: uuid.UUID,
    *,
    name: str,
    description: str,
    price_cop: int | None,
    price_note: str,
    duration_min: int,
    actor: Any,
) -> Service:
    """Edita y marca como confirmado por una persona."""
    if not 2 <= len(name.strip()) <= 120:
        raise AppError("invalid_name", "El nombre debe tener entre 2 y 120 caracteres", 422)
    if price_cop is not None and (isinstance(price_cop, bool) or not 0 <= price_cop <= 50_000_000):
        raise AppError("invalid_price", "Precio fuera de rango", 422)
    if not 5 <= duration_min <= 480:
        raise AppError("invalid_duration", "La duración debe estar entre 5 y 480 minutos", 422)
    row = await _service(session, tenant_id, service_id)
    clash = await session.scalar(
        select(Service.id).where(
            Service.tenant_id == tenant_id,
            Service.id != row.id,
            Service.is_active.is_(True),
            func.lower(Service.name) == name.strip().lower(),
        )
    )
    if clash is not None:
        raise AppError("duplicate_service", "Ya existe un servicio con ese nombre", 422)
    row.name = name.strip()
    row.description = description.strip()[:300]
    row.price_cop = price_cop
    row.price_note = price_note.strip()[:160]
    row.duration_min = duration_min
    row.needs_review = False
    await session.flush()
    await log_event(
        session, actor=actor, action="bot.service_confirmed", entity_type="service",
        entity_id=row.id, tenant_id=tenant_id,
    )  # fmt: skip
    return row


async def update_faq(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    faq_id: uuid.UUID,
    *,
    question: str,
    answer: str,
    actor: Any,
) -> Faq:
    if not 3 <= len(question.strip()) <= 200 or not 3 <= len(answer.strip()) <= 600:
        raise AppError("invalid_faq", "Pregunta (3-200) y respuesta (3-600) son obligatorias", 422)
    row = await _faq(session, tenant_id, faq_id)
    row.question = question.strip()
    row.answer = answer.strip()
    row.needs_review = False
    row.source = "manual"
    await session.flush()
    await log_event(
        session, actor=actor, action="bot.faq_confirmed", entity_type="faq",
        entity_id=row.id, tenant_id=tenant_id,
    )  # fmt: skip
    return row


async def discard_item(
    session: AsyncSession, tenant_id: uuid.UUID, kind: str, item_id: uuid.UUID, *, actor: Any
) -> None:
    row = (
        await _service(session, tenant_id, item_id)
        if kind == "service"
        else await _faq(session, tenant_id, item_id)
    )
    row.is_active = False
    row.needs_review = False
    await session.flush()
    await log_event(
        session, actor=actor, action=f"bot.{kind}_discarded", entity_type=kind,
        entity_id=item_id, tenant_id=tenant_id,
    )  # fmt: skip


async def _save_config(
    session: AsyncSession, bot: BotConfig, config: GeneratedBotConfig, *, action: str, actor: Any
) -> None:
    bot.config = config.model_dump(mode="json")
    rerender_prompt(bot, config)
    await session.flush()
    await log_event(
        session, actor=actor, action=action, entity_type="bot_config", entity_id=bot.id,
        tenant_id=bot.tenant_id, diff={"version": bot.version},
    )  # fmt: skip


async def update_tone(
    session: AsyncSession, bot: BotConfig, *, register: str, emoji_level: str, actor: Any
) -> None:
    require_draft(bot)
    if register not in ("tu", "usted") or emoji_level not in ("none", "low"):
        raise AppError("invalid_tone", "Tono no válido", 422)
    cfg = parsed_config(bot)
    cfg.tone = cfg.tone.model_copy(update={"address_form": register, "emoji_level": emoji_level})
    await _save_config(session, bot, cfg, action="bot.tone_updated", actor=actor)


def parse_hours_text(values: dict[str, str]) -> dict[str, list[TimeRange]]:
    """``{"mon": "09:00-12:00, 14:00-18:00"}`` -> rangos validados (dia vacio = cerrado)."""
    weekly: dict[str, list[TimeRange]] = {}
    for day in DAY_KEYS:
        raw = (values.get(day) or "").strip()
        if not raw:
            continue
        ranges: list[TimeRange] = []
        for part in raw.split(","):
            m = _RANGE_RE.match(part)
            if not m:
                raise AppError("invalid_hours", f"Horario no válido: «{part.strip()}»", 422)
            a, b = f"{int(m[1]):02d}:{m[2]}", f"{int(m[3]):02d}:{m[4]}"
            if a >= b:
                raise AppError(
                    "invalid_hours", "La hora de cierre debe ser mayor que la de apertura", 422
                )
            ranges.append(TimeRange(open=a, close=b))
        if len(ranges) > 3:
            raise AppError("invalid_hours", "Máximo 3 franjas por día", 422)
        weekly[day] = ranges
    return weekly


def hours_text(config: GeneratedBotConfig, day: str) -> str:
    return ", ".join(f"{r.open}-{r.close}" for r in config.hours.weekly.get(day, []))


def working_hours(config: GeneratedBotConfig) -> dict[str, list[list[str]]]:
    return {d: [[r.open, r.close] for r in rs] for d, rs in config.hours.weekly.items() if rs}


async def update_hours(
    session: AsyncSession, bot: BotConfig, values: dict[str, str], *, actor: Any
) -> None:
    require_draft(bot)
    cfg = parsed_config(bot)
    cfg.hours = cfg.hours.model_copy(update={"weekly": parse_hours_text(values)})
    rules = copy.deepcopy(dict(bot.booking_rules or {}))
    rules["working_hours"] = working_hours(cfg)
    rules["hours"] = {d: hours_text(cfg, d) for d in cfg.hours.weekly if cfg.hours.weekly[d]}
    bot.booking_rules = rules
    info = review_info(bot)
    info["hours_confirmed"] = True
    set_meta(bot, review=info)
    await _save_config(session, bot, cfg, action="bot.hours_confirmed", actor=actor)


async def resolve_open_question(
    session: AsyncSession, bot: BotConfig, index: int, *, actor: Any
) -> None:
    require_draft(bot)
    cfg = parsed_config(bot)
    if not 0 <= index < len(cfg.open_questions):
        raise NotFoundError("Pregunta abierta no encontrada")
    cfg.open_questions = [q for i, q in enumerate(cfg.open_questions) if i != index]
    await _save_config(session, bot, cfg, action="bot.question_resolved", actor=actor)


async def acknowledge_flags(session: AsyncSession, bot: BotConfig, *, actor: Any) -> None:
    require_draft(bot)
    info = review_info(bot)
    info["flags_ack"] = True
    set_meta(bot, review=info)
    await session.flush()
    await log_event(
        session, actor=actor, action="bot.flags_ack", entity_type="bot_config",
        entity_id=bot.id, tenant_id=bot.tenant_id,
    )  # fmt: skip


async def mark_sandbox_tested(session: AsyncSession, bot: BotConfig) -> None:
    if not (bot.generation_meta or {}).get("sandbox_tested"):
        set_meta(bot, sandbox_tested=True)
        await session.flush()
