"""Fabrica de bots (B4): ``build_bot`` (LLM con tool forzada), publicar, aprobar y rollback."""

from __future__ import annotations

import json
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import LLMClient, get_llm
from app.audit.service import log_event
from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.db.models.bots import BotConfig, LlmUsage
from app.db.models.catalog import KbDocument
from app.db.models.tenants import Tenant
from app.factory import review
from app.factory.grounding import fold, has_injection, verify_grounding
from app.factory.prompts import (
    GENERATOR_SYSTEM,
    PROMPT_VERSION,
    build_user_message,
)
from app.factory.schemas import (
    FactoryInput,
    FlagCfg,
    GeneratedBotConfig,
    HandoffAction,
    HandoffRule,
    ServiceCfg,
    emit_bot_config_tool,
    slugify_service,
)
from app.niches.loader import get_niche_template
from app.niches.schema import NicheTemplate, render_placeholders

__all__ = [
    "FactoryGenerationError",
    "FactoryInput",
    "approve_draft",
    "build_bot",
    "publish_bot",
    "rollback_bot",
]

log = get_logger(__name__)

MAX_KB_PAGES = 12
MAX_INPUT_CHARS = 48_000  # ~12k tokens de web; el tope total de entrada es holgado
DEFAULT_HANDOFFS: tuple[tuple[str, HandoffAction], ...] = (
    ("El cliente presenta una queja", "notify_human"),
    ("Urgencia o emergencia (dolor intenso, sangrado, accidente)", "escalate_urgent"),
    ("Pide factura, reembolso o devolucion", "notify_human"),
    ("Pide hablar con una persona", "notify_human"),
    ("Dos intentos fallidos de entender al cliente", "notify_human"),
)


FACTORY_TIMEOUT_S = 180.0  # generar el bot completo con razonamiento tarda mas que un chat


class FactoryGenerationError(AppError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "generation_failed",
            "No se pudo generar la configuración del bot. Edítala manualmente o reintenta.",
            502,
        )
        self.detail = detail


# --------------------------------------------------------------------------- entradas
def _owner_payload(inputs: FactoryInput) -> dict[str, Any]:
    return {
        "name": inputs.name,
        "niche": inputs.niche,
        "city": inputs.city,
        "address": inputs.address,
        "phone": inputs.phone,
        "website_url": inputs.website_url,
        "instagram_url": inputs.instagram_url,
        "services": inputs.services,
        "hours": inputs.hours,
        "notes": inputs.notes,
    }


def _owner_prices(inputs: FactoryInput) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for s in inputs.services:
        price = s.get("price_cop")
        out[fold(str(s.get("name", "")))] = price if isinstance(price, int) else None
    return out


def _template_summary(tpl: NicheTemplate) -> dict[str, Any]:
    return {
        "nicho": tpl.niche,
        "persona": {"tono": tpl.persona.tone, "trato": tpl.persona.address_form},
        "servicios_tipicos_precios_INDICATIVOS": [
            {
                "code": s.code,
                "name": s.name,
                "duration_min": s.duration_min,
                "rango_cop": [s.price_min_cop, s.price_max_cop],
                "requires_assessment": s.requires_assessment,
            }
            for s in tpl.typical_services
        ],
        "faq_seeds": [{"q": f.question, "a": f.answer} for f in tpl.faq_seeds],
        "reglas_agenda": {
            "slot_minutes": tpl.booking_rules.slot_minutes,
            "min_notice_hours": tpl.booking_rules.min_notice_hours,
            "max_days_ahead": tpl.booking_rules.max_days_ahead,
            "buffer_min": tpl.booking_rules.buffer_min,
        },
        "temas_prohibidos": tpl.forbidden_topics,
        "escalamiento": [r.name for r in tpl.escalation_triggers],
    }


async def load_kb(session: AsyncSession, tenant_id: uuid.UUID) -> list[KbDocument]:
    stmt = (
        select(KbDocument)
        .where(KbDocument.tenant_id == tenant_id)
        .order_by(KbDocument.fetched_at.desc())
        .limit(MAX_KB_PAGES)
    )
    return list((await session.execute(stmt)).scalars())


async def load_kb_preview(
    session: AsyncSession, tenant_id: uuid.UUID, chars: int = 1500
) -> list[Any]:
    """Filas (title, source_url, content recortado) sin traer el contenido completo."""
    stmt = (
        select(
            KbDocument.title,
            KbDocument.source_url,
            func.substr(KbDocument.content, 1, chars).label("content"),
        )
        .where(KbDocument.tenant_id == tenant_id)
        .order_by(KbDocument.fetched_at.desc())
        .limit(MAX_KB_PAGES)
    )
    return list((await session.execute(stmt)).all())


async def _scrape(session: AsyncSession, tenant_id: uuid.UUID, inputs: FactoryInput) -> str | None:
    """Rastrea la web del negocio. Devuelve un mensaje de error (o ``None``); nunca lanza."""
    if not (inputs.website_url or inputs.instagram_url):
        return None
    from app.scraping.service import scrape_and_store

    try:
        await scrape_and_store(
            session, tenant_id, inputs.website_url, instagram_url=inputs.instagram_url
        )
    except Exception as exc:  # el scraping es opcional: sigue con lo que haya
        log.warning("factory_scrape_failed", tenant_id=str(tenant_id), error=type(exc).__name__)
        return type(exc).__name__
    return None


# --------------------------------------------------------------------------- llamada al LLM
def _error_summary(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:12])


async def _generate(
    llm: LLMClient,
    *,
    owner: dict[str, Any],
    template_summary: dict[str, Any],
    pages: list[tuple[str, str]],
    instructions: str = "",
) -> tuple[GeneratedBotConfig, dict[str, int], int]:
    tool = emit_bot_config_tool()
    usage = {"tokens_in": 0, "tokens_out": 0}
    error: str | None = None
    for attempt in (1, 2):
        message = build_user_message(
            owner=owner,
            template_summary=template_summary,
            pages=pages,
            retry_error=error,
            instructions=instructions,
        )
        resp = await llm.complete(
            system=GENERATOR_SYSTEM,
            messages=[{"role": "user", "content": message}],
            tools=[tool],
            # Sonnet 5.5 rechaza tool_choice forzado: auto + instruccion + reintento
            tool_choice={"type": "auto"},
            max_tokens=16000,
            effort="medium",
            timeout=FACTORY_TIMEOUT_S,
        )
        usage["tokens_in"] += int(resp.usage.get("input_tokens", 0))
        usage["tokens_out"] += int(resp.usage.get("output_tokens", 0))
        call = next((c for c in resp.tool_calls if c.name == "emit_bot_config"), None)
        if call is None:
            error = "No llamaste la tool emit_bot_config."
            continue
        try:
            return GeneratedBotConfig.model_validate(call.input), usage, attempt
        except ValidationError as exc:
            error = _error_summary(exc)
    raise FactoryGenerationError(error or "")


# --------------------------------------------------------------------------- normalizacion
def _normalize(
    config: GeneratedBotConfig, inputs: FactoryInput, tpl: NicheTemplate
) -> GeneratedBotConfig:
    """Datos del dueno mandan; garantiza handoffs, ids unicos y servicios del dueno presentes."""
    biz = config.business.model_copy(
        update={
            "name": inputs.name[:120],
            "niche": tpl.niche,
            "city": inputs.city or config.business.city,
            "address": inputs.address or config.business.address,
            "phone": _phone(inputs.phone) or config.business.phone,
        }
    )
    owner_by_name = {fold(str(s.get("name", ""))): s for s in inputs.services}
    services: list[ServiceCfg] = []
    seen_names: set[str] = set()
    for svc in config.services:
        key = fold(svc.name)
        if key in seen_names:
            continue
        seen_names.add(key)
        declared = owner_by_name.get(key)
        if declared is not None:
            update: dict[str, Any] = {"source_ref": "owner"}
            if _valid_price(declared.get("price_cop")):
                update["price_cop"] = declared["price_cop"]
            dur = declared.get("duration_min")
            if isinstance(dur, int) and 5 <= dur <= 480:
                update["duration_min"] = dur
            svc = svc.model_copy(update=update)
        services.append(svc)
    for key, declared in owner_by_name.items():
        if key and key not in seen_names and len(services) < 30:
            name = str(declared["name"])[:120]
            price = declared.get("price_cop")
            dur = declared.get("duration_min")
            services.append(
                ServiceCfg(
                    id=slugify_service(name),
                    name=name,
                    description=str(declared.get("description") or "")[:300],
                    duration_min=dur if isinstance(dur, int) and 5 <= dur <= 480 else 30,
                    price_cop=price if _valid_price(price) else None,
                    source_ref="owner",
                )
            )
    taken: set[str] = set()
    for i, svc in enumerate(services):
        if svc.id in taken:
            services[i] = svc.model_copy(update={"id": review.unique_id(svc.name, taken)})
        else:
            taken.add(svc.id)
    handoffs = list(config.handoff_rules)
    have = {h.trigger.lower() for h in handoffs}
    for trig, action in DEFAULT_HANDOFFS:
        if trig.lower() not in have:
            handoffs.append(HandoffRule(trigger=trig, action=action))
    return config.model_copy(
        update={"business": biz, "services": services, "handoff_rules": handoffs}
    )


def _valid_price(value: Any) -> bool:
    """Entero (no bool) dentro de 0..50.000.000 COP."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 50_000_000


def _phone(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = "".join(c for c in raw if c.isdigit() or c in "+ ")
    digits = [c for c in cleaned if c.isdigit()]
    return cleaned.strip() if 7 <= len(digits) <= 20 else None


def _fill_values(config: GeneratedBotConfig) -> dict[str, str]:
    return {
        "negocio": config.business.name,
        "direccion": config.business.address or "",
        "telefono": config.business.phone or "",
    }


def _persisted_parts(
    config: GeneratedBotConfig, tpl: NicheTemplate, inputs: FactoryInput
) -> dict[str, Any]:
    values = _fill_values(config)

    def _fill(text: str) -> str:
        return render_placeholders(text, values)

    rules = tpl.booking_rules.model_dump()
    rules.update(
        slot_minutes=config.booking_rules.slot_minutes,
        min_notice_hours=config.booking_rules.min_notice_hours,
        max_days_ahead=config.booking_rules.max_days_ahead,
        buffer_min=config.booking_rules.buffer_minutes,
        required_fields=list(config.booking_rules.requires_fields),
        working_hours=review.working_hours(config),
        hours={
            d: review.hours_text(config, d) for d in config.hours.weekly if config.hours.weekly[d]
        },
    )
    guardrails = {
        "forbidden_topics": tpl.forbidden_topics,
        "out_of_scope_topics": config.out_of_scope_topics,
        "off_topic_response": _fill(tpl.off_topic_response),
        "sensitive_response": _fill(tpl.sensitive_response),
        "escalation": [r.model_dump() for r in tpl.escalation_triggers],
        "never_collect": tpl.booking_rules.never_collect,
        "consent_text": _fill(tpl.compliance.consent_text),
        "prompt_rules": [f"Evita: {a}" for a in tpl.persona.avoid],
        "operator_instructions": inputs.instructions.strip(),
    }
    templates = {k: _fill(v) for k, v in tpl.message_templates.items()}
    return {"booking_rules": rules, "guardrails": guardrails, "templates": templates}


# --------------------------------------------------------------------------- build_bot
async def build_bot(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    inputs: FactoryInput,
    *,
    scrape: bool = True,
    llm: LLMClient | None = None,
) -> BotConfig:
    """Genera un borrador de ``BotConfig`` (version nueva) y sincroniza servicios/FAQs.

    No hace commit (lo hace el llamador). Lanza ``FactoryGenerationError`` si el LLM no
    entrega una configuracion valida tras un reintento.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or tenant.deleted_at is not None:
        raise NotFoundError("Empresa no encontrada")
    tpl = get_niche_template(inputs.niche)
    scrape_error = await _scrape(session, tenant_id, inputs) if scrape else None
    docs = await load_kb(session, tenant_id)
    budget = MAX_INPUT_CHARS
    pages: list[tuple[str, str]] = []
    for doc in docs:
        text = f"{doc.title}\n{doc.content}"[:4000]
        if budget <= 0:
            break
        budget -= len(text)
        pages.append((doc.source_url or "sin-url", text))
    source_text = "\n".join(t for _, t in pages)

    owner = _owner_payload(inputs)
    client = llm or get_llm()
    raw, usage, attempts = await _generate(
        client,
        owner=owner,
        template_summary=_template_summary(tpl),
        pages=pages,
        instructions=inputs.instructions,
    )
    config = _normalize(raw, inputs, tpl)

    flags = list(config.flags)
    if has_injection(source_text) and not any(
        f.type == "prompt_injection_suspected" for f in flags
    ):
        flags.append(
            FlagCfg(type="prompt_injection_suspected", detail="Texto de la web con órdenes")
        )
    config = config.model_copy(update={"flags": flags})
    owner_text = json.dumps(
        {k: owner[k] for k in ("services", "hours", "notes", "address", "phone")},
        ensure_ascii=False,
        default=str,
    )
    report = verify_grounding(
        config,
        owner_text=owner_text,
        owner_prices=_owner_prices(inputs),
        source_text=source_text,
    )
    injected = any(f.type == "prompt_injection_suspected" for f in config.flags)
    # FAQ cuyo texto parece una orden al modelo -> a revision
    for i, faq in enumerate(config.faqs):
        if has_injection(faq.answer) or has_injection(faq.question):
            report.faq_indexes.add(i)
    # Descripciones/nombres de servicio (origen web) tambien llegan al prompt de runtime
    for svc in config.services:
        if has_injection(svc.description) or has_injection(svc.name):
            report.service_ids.add(svc.id)
            report.reasons.append(f"Servicio con texto sospechoso: {svc.name}")
    needs_review = report.needs_review or injected

    for old in await review.list_versions(session, tenant_id):
        if old.status == "draft":
            old.status = "archived"
    await session.flush()
    await review.sync_catalog_from_config(
        session,
        tenant_id,
        config,
        needs_review_services=report.service_ids,
        needs_review_faqs=report.faq_indexes,
    )

    model = get_settings().anthropic_model
    bot = BotConfig(
        tenant_id=tenant_id,
        version=await review.next_version(session, tenant_id),
        status="draft",
        config=config.model_dump(mode="json"),
        source_inputs=json.loads(json.dumps(owner, default=str)),
        model=model,
        generated_by="factory",
        **_persisted_parts(config, tpl, inputs),
    )
    review.rerender_prompt(bot, config)
    bot.generation_meta = {
        "prompt_version": PROMPT_VERSION,
        "template_version": tpl.version,
        "niche": tpl.niche,
        "attempts": attempts,
        "usage": usage,
        "kb_documents": len(pages),
        "scrape_error": scrape_error,
        "needs_review": needs_review,
        "sandbox_tested": False,
        "review": {
            "hours_ungrounded": report.hours_ungrounded,
            "reasons": report.reasons,
            "hours_confirmed": False,
            "flags_ack": False,
        },
    }
    session.add(bot)
    session.add(
        LlmUsage(
            tenant_id=tenant_id,
            purpose="generation",
            model=model,
            tokens_in=usage["tokens_in"],
            tokens_out=usage["tokens_out"],
        )
    )
    await session.flush()
    await log_event(
        session,
        actor="system",
        action="bot.generated",
        entity_type="bot_config",
        entity_id=bot.id,
        tenant_id=tenant_id,
        diff={"version": bot.version, "needs_review": needs_review, "attempts": attempts},
    )
    log.info(
        "bot_generated",
        tenant_id=str(tenant_id),
        version=bot.version,
        tokens_in=usage["tokens_in"],
        tokens_out=usage["tokens_out"],
        needs_review=needs_review,
    )
    return bot


# --------------------------------------------------------------------------- aprobar / publicar
async def approve_draft(
    session: AsyncSession, bot_config_id: uuid.UUID, *, actor: Any, require_sandbox: bool = False
) -> BotConfig:
    bot = await review.get_bot(session, bot_config_id)
    review.require_draft(bot)
    blockers = await review.publish_blockers(session, bot, require_sandbox=require_sandbox)
    if blockers:
        raise AppError("publish_blocked", " ".join(blockers), 422)
    info = review.review_info(bot)
    info.update(approved_by=str(getattr(actor, "id", actor)), approved_at=utcnow().isoformat())
    review.set_meta(bot, review=info)
    await session.flush()
    await log_event(
        session, actor=actor, action="bot.approved", entity_type="bot_config",
        entity_id=bot.id, tenant_id=bot.tenant_id, diff={"version": bot.version},
    )  # fmt: skip
    return bot


async def _make_published(session: AsyncSession, bot: BotConfig, *, actor: Any) -> None:
    for other in await review.list_versions(session, bot.tenant_id):
        if other.status == "published" and other.id != bot.id:
            other.status = "archived"
    await session.flush()  # libera el indice unico parcial antes de publicar la nueva
    bot.status = "published"
    bot.published_at = utcnow()
    actor_id = getattr(actor, "id", None)
    bot.published_by = actor_id if isinstance(actor_id, uuid.UUID) else None
    tenant = await session.get(Tenant, bot.tenant_id)
    if tenant is not None and tenant.status in ("draft", "building", "review"):
        tenant.status = "active"
    await session.flush()


async def publish_bot(
    session: AsyncSession,
    bot_config_id: uuid.UUID,
    *,
    actor: Any,
    require_sandbox: bool = False,
    enforce_review: bool = True,
) -> BotConfig:
    """Congela el borrador (catalogo + prompt) y lo deja como unica version publicada.

    ``enforce_review=False`` solo para flujos automaticos (p. ej. bot demo de un lead).
    """
    bot = await review.get_bot(session, bot_config_id)
    if bot.status != "draft":
        raise ConflictError("Solo se puede publicar un borrador")
    if enforce_review:
        blockers = await review.publish_blockers(session, bot, require_sandbox=require_sandbox)
        if blockers:
            raise AppError("publish_blocked", " ".join(blockers), 422)
    else:
        for row in await review.list_services(session, bot.tenant_id):
            row.needs_review = False
        for faq in await review.list_faqs(session, bot.tenant_id):
            faq.needs_review = False
    cfg = await review.snapshot_config(session, bot.tenant_id, review.parsed_config(bot))
    bot.config = cfg.model_dump(mode="json")
    review.rerender_prompt(bot, cfg)
    info = review.review_info(bot)
    info.setdefault("approved_by", str(getattr(actor, "id", actor)))
    review.set_meta(bot, review=info)
    await _make_published(session, bot, actor=actor)
    await log_event(
        session, actor=actor, action="bot.published", entity_type="bot_config",
        entity_id=bot.id, tenant_id=bot.tenant_id, diff={"version": bot.version},
    )  # fmt: skip
    return bot


async def rollback_bot(
    session: AsyncSession, tenant_id: uuid.UUID, version: int, *, actor: Any
) -> BotConfig:
    """Re-publica una version que ya estuvo publicada y restaura su catalogo."""
    stmt = select(BotConfig).where(BotConfig.tenant_id == tenant_id, BotConfig.version == version)
    bot = (await session.execute(stmt)).scalar_one_or_none()
    if bot is None:
        raise NotFoundError("Versión no encontrada")
    if bot.status == "published":
        raise ConflictError("Esa versión ya está publicada")
    if bot.published_at is None:
        raise ConflictError("Solo se puede volver a una versión que ya estuvo publicada")
    cfg = review.parsed_config(bot)
    blockers = review.rollback_blockers(bot, cfg)
    if blockers:
        raise AppError("rollback_blocked", " ".join(blockers), 422)
    await review.sync_catalog_from_config(session, tenant_id, cfg, replace=True)
    await _make_published(session, bot, actor=actor)
    await log_event(
        session, actor=actor, action="bot.rollback", entity_type="bot_config",
        entity_id=bot.id, tenant_id=tenant_id, diff={"version": version},
    )  # fmt: skip
    return bot
