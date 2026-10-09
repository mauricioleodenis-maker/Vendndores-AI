"""Lead -> bot demo en un clic, chat web publico de demo y conversion a cliente."""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import LLMClient
from app.audit.service import log_event
from app.conversation.engine import sandbox_reply
from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.rate_limit import enforce
from app.db.models.bots import BotConfig
from app.db.models.leads import Lead
from app.db.models.tenants import Tenant
from app.db.models.users import User
from app.factory.schemas import FactoryInput
from app.factory.service import build_bot
from app.leads import pipeline
from app.plans.service import create_subscription

DEMO_NICHES = ("dentista", "clinica_estetica", "taller", "restaurante")
_SALT = "vai-demo-chat-v1"
DEMO_NOTICE = "Esto es una demostración: no es el asistente real del negocio."
MAX_TEXT = 500
MAX_HISTORY = 12
IP_MSGS_PER_MIN = 20

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DemoResult:
    tenant_id: uuid.UUID
    bot_config_id: uuid.UUID | None
    demo_link: str
    expires_at: datetime
    slug: str
    whatsapp_link: str | None = None
    reused: bool = False


@dataclass(frozen=True, slots=True)
class ConvertResult:
    tenant_id: uuid.UUID
    subscription_id: uuid.UUID
    plan_code: str
    setup_net_cop: int
    billing_record_created: bool


def slugify(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return (re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-") or "negocio")[:50]


async def _unique_slug(session: AsyncSession, name: str) -> str:
    base = slugify(name)
    candidate = base
    while (
        await session.execute(select(Tenant.id).where(Tenant.slug == candidate))
    ).first() is not None:
        candidate = f"{base}-{uuid.uuid4().hex[:6]}"
    return candidate


# --------------------------------------------------------------------------- tokens
def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key.get_secret_value(), salt=_SALT)


def make_demo_token(tenant_id: uuid.UUID) -> str:
    return _serializer().dumps({"t": str(tenant_id)})


def read_demo_token(token: str) -> uuid.UUID:
    """Valida firma y vigencia; lanza 404 generico ante cualquier fallo."""
    max_age = get_settings().demo_token_ttl_days * 86400
    try:
        data = _serializer().loads(token, max_age=max_age)
        return uuid.UUID(str(data["t"]))
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError) as exc:
        raise NotFoundError("Este enlace de demostración no es válido o ya venció") from exc


def demo_links(tenant: Tenant) -> tuple[str, str | None]:
    settings = get_settings()
    base = settings.public_base_url.rstrip("/")
    web = f"{base}/demo/{make_demo_token(tenant.id)}"
    number = settings.demo_whatsapp_number.lstrip("+").strip()
    wa = f"https://wa.me/{number}?text={quote('DEMO-' + tenant.slug)}" if number else None
    return web, wa


async def _latest_bot(session: AsyncSession, tenant_id: uuid.UUID) -> BotConfig | None:
    stmt = (
        select(BotConfig)
        .where(BotConfig.tenant_id == tenant_id)
        .order_by(BotConfig.version.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _factory_input(lead: Lead, niche: str) -> FactoryInput:
    notes: list[str] = []
    if lead.rating is not None:
        notes.append(f"Calificación en Google: {lead.rating} ({lead.review_count or 0} reseñas).")
    if lead.maps_url:
        notes.append(f"Ficha de Maps: {lead.maps_url}")
    return FactoryInput(
        name=lead.name,
        niche=niche,
        city=lead.city,
        address=lead.address,
        phone=lead.phone_e164,
        website_url=lead.website,
        instagram_url=lead.instagram,
        notes=" ".join(notes),
        instructions=lead.demo_instructions or "",
    )


async def lead_to_demo_bot(
    session: AsyncSession,
    lead_id: uuid.UUID,
    *,
    actor: User,
    scrape: bool = True,
    rebuild: bool = False,
    niche: str | None = None,
    llm: LLMClient | None = None,
) -> DemoResult:
    """Crea (o reutiliza) el tenant demo del lead y su bot; mueve el lead a ``demo``.

    Idempotente: si ya existe ``demo_tenant_id`` y no se pide ``rebuild`` devuelve el existente.
    No contacta al negocio: el operador comparte el enlace.
    """
    settings = get_settings()
    if not settings.demo_enabled:
        raise AppError("demo_disabled", "Las demos están deshabilitadas", 403)
    lead = await pipeline.get_lead(session, lead_id, for_update=True)
    if lead.disposition != "activo":
        raise ConflictError("El lead no está activo (perdido o no contactar)")
    if lead.converted_tenant_id is not None:
        raise ConflictError("El lead ya es cliente")
    expires = utcnow() + timedelta(days=settings.demo_token_ttl_days)

    tenant: Tenant | None = None
    if lead.demo_tenant_id is not None:
        tenant = await session.get(Tenant, lead.demo_tenant_id)
        if tenant is not None and tenant.deleted_at is not None:
            tenant = None
    if tenant is not None and not rebuild:
        bot = await _latest_bot(session, tenant.id)
        web, wa = demo_links(tenant)
        return DemoResult(tenant.id, bot.id if bot else None, web, expires, tenant.slug, wa, True)

    chosen = niche or (lead.niche if lead.niche in DEMO_NICHES else None)
    if chosen not in DEMO_NICHES:
        raise AppError("niche_required", "Elige el tipo de negocio para crear la demo", 422)
    if tenant is None:
        tenant = Tenant(
            slug=await _unique_slug(session, lead.name),
            name=lead.name[:200],
            niche=chosen,
            status="draft",
            city=lead.city,
            address=lead.address,
            website_url=lead.website,
            instagram_url=lead.instagram,
            phone_contact=lead.phone_e164,
            is_demo=True,
            created_by=actor.id,
        )
        session.add(tenant)
        await session.flush()
        lead.demo_tenant_id = tenant.id
    else:
        tenant.niche = chosen
    bot = await build_bot(session, tenant.id, _factory_input(lead, chosen), scrape=scrape, llm=llm)

    if lead.stage in ("nuevo", "prueba_secreta", "contactado"):
        # avanza paso a paso respetando ALLOWED (nuevo/prueba -> contactado -> demo)
        if lead.stage != "contactado":
            await pipeline.transition_stage(session, lead, "contactado", actor=actor)
        await pipeline.transition_stage(session, lead, "demo", actor=actor, note="demo creada")
    web, wa = demo_links(tenant)
    await pipeline.add_event(
        session,
        lead,
        "demo_created",
        {"tenant_id": str(tenant.id), "bot_config_id": str(bot.id), "rebuild": rebuild},
        actor=actor,
    )
    await log_event(
        session,
        actor=actor,
        action="lead.demo_bot",
        entity_type="lead",
        entity_id=lead.id,
        tenant_id=tenant.id,
        diff={"rebuild": rebuild, "niche": chosen},
    )
    return DemoResult(tenant.id, bot.id, web, expires, tenant.slug, wa, False)


# --------------------------------------------------------------------------- chat web
async def load_demo_tenant(session: AsyncSession, token: str) -> Tenant:
    tenant = await session.get(Tenant, read_demo_token(token))
    if tenant is None or not tenant.is_demo or tenant.deleted_at is not None:
        raise NotFoundError("Este enlace de demostración no es válido o ya venció")
    return tenant


def clean_history(history: Any) -> list[dict[str, str]]:
    """Normaliza el historial enviado por el navegador (no confiable)."""
    out: list[dict[str, str]] = []
    if not isinstance(history, list):
        return out
    for item in history[-MAX_HISTORY:]:
        if not isinstance(item, dict):
            continue
        role, content = item.get("role"), item.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            out.append({"role": str(role), "content": content[:MAX_TEXT]})
    return out


async def demo_chat_reply(
    session: AsyncSession,
    token: str,
    history: list[dict[str, str]],
    text: str,
    *,
    llm: LLMClient | None = None,
    tenant: Tenant | None = None,
    client_ip: str | None = None,
) -> str:
    """Una respuesta del chat de demo con tope de mensajes por demo (``demo_max_messages``).

    Ademas del tope por demo, limita por IP (anti-abuso de costo LLM) y convierte cualquier
    fallo inesperado del modelo en un error controlado (sin filtrar detalles).
    """
    settings = get_settings()
    if tenant is None:
        tenant = await load_demo_tenant(session, token)
    message = (text or "").strip()
    if not message:
        raise AppError("empty_message", "Escribe un mensaje", 422)
    await enforce(
        f"demo-msgs:{tenant.id}",
        limit=settings.demo_max_messages,
        window_s=settings.demo_token_ttl_days * 86400,
    )
    if client_ip:
        await enforce(f"demo-ip:{client_ip}", limit=IP_MSGS_PER_MIN, window_s=60)
    try:
        return await sandbox_reply(
            session, tenant.id, clean_history(history), message[:MAX_TEXT], llm=llm
        )
    except AppError:
        raise
    except Exception as exc:
        log.warning("demo.chat_failed", error_type=type(exc).__name__)
        raise AppError(
            "demo_unavailable", "La demostración no está disponible ahora. Intenta de nuevo.", 503
        ) from exc


# --------------------------------------------------------------------------- conversion
async def convert_lead(
    session: AsyncSession,
    lead_id: uuid.UUID,
    *,
    plan_code: str,
    offer_code: str | None = None,
    include_iva: bool = False,
    actor: User,
) -> ConvertResult:
    """Promueve el tenant demo a cliente: suscripcion + cobro de setup, lead -> ``cerrado``."""
    lead = await pipeline.get_lead(session, lead_id, for_update=True)
    if lead.disposition != "activo":
        raise ConflictError("El lead no está activo (perdido o no contactar)")
    if lead.converted_tenant_id is not None:
        raise ConflictError("El lead ya fue convertido en cliente")
    tenant = await session.get(Tenant, lead.demo_tenant_id) if lead.demo_tenant_id else None
    if tenant is None or tenant.deleted_at is not None:
        raise ConflictError("Primero crea la demo del negocio")
    outcome = await create_subscription(
        session,
        tenant.id,
        plan_code,
        offer_code=offer_code or None,
        include_iva=include_iva,
        actor=actor,
    )
    tenant.is_demo = False
    if tenant.status == "draft":
        tenant.status = "building"
    lead.converted_tenant_id = tenant.id
    await pipeline.transition_stage(
        session, lead, "cerrado", actor=actor, note=f"plan {plan_code}", system=True
    )
    await pipeline.add_event(
        session,
        lead,
        "converted",
        {"tenant_id": str(tenant.id), "plan": plan_code, "offer": offer_code},
        actor=actor,
    )
    await log_event(
        session,
        actor=actor,
        action="lead.convert",
        entity_type="lead",
        entity_id=lead.id,
        tenant_id=tenant.id,
        diff={"plan": plan_code, "offer": offer_code},
    )
    return ConvertResult(
        tenant_id=tenant.id,
        subscription_id=outcome.subscription.id,
        plan_code=outcome.plan.code,
        setup_net_cop=outcome.quote.setup_net_cop,
        billing_record_created=outcome.quote.setup_net_cop > 0,
    )
