"""Logica de empresas: CRUD, perfil/horarios, servicios, FAQs, secretos, canales y wizard.

Cada funcion recibe la ``session`` explicita; el commit lo hace la dependencia ``get_session``
(salvo en el job de generacion, que maneja su propia sesion). Todo acceso a tablas con
``tenant_id`` pasa por ``TenantScopedRepo``.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.core.clock import utcnow
from app.core.crypto import EncryptedBlob, get_crypto, make_aad
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.jobs import enqueue, register_job
from app.core.logging import get_logger
from app.db.models.catalog import Faq, Service
from app.db.models.tenants import (
    ChannelAccount,
    Tenant,
    TenantProfile,
    TenantSecret,
)
from app.db.repo import TenantScopedRepo
from app.db.session import get_sessionmaker
from app.factory import service as factory_service
from app.tenants.schemas import (
    MAX_SERVICES,
    ChannelIn,
    FaqIn,
    SecretIn,
    ServiceIn,
    TenantIn,
)

log = get_logger(__name__)

PAGE_SIZE = 25
MAX_PAGE = 1000  # tope de paginacion: evita OFFSET gigantes (DoS barato)
DPA_VERSION = "1.0"
GENERATE_JOB = "tenants.generate_bot"
STATUS_TRANSITIONS: dict[str, set[str]] = {
    "active": {"paused"},
    "paused": {"active"},
}


# --------------------------------------------------------------------------- helpers
def slugify(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")
    return (slug or "empresa")[:60]


async def _unique_slug(session: AsyncSession, name: str) -> str:
    base = slugify(name)
    candidate = base
    while (
        await session.execute(select(Tenant.id).where(Tenant.slug == candidate))
    ).first() is not None:
        candidate = f"{base}-{uuid.uuid4().hex[:6]}"
    return candidate


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------- empresas
async def get_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = (
        await session.execute(
            select(Tenant).where(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if tenant is None:
        raise NotFoundError("Empresa no encontrada")
    return tenant


async def list_tenants(
    session: AsyncSession,
    *,
    q: str = "",
    niche: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> tuple[list[Tenant], bool]:
    """Devuelve ``(filas, hay_mas)``."""
    stmt = select(Tenant).where(Tenant.deleted_at.is_(None))
    if q.strip():
        like = f"%{_escape_like(q.strip().lower())}%"
        stmt = stmt.where(
            or_(
                func.lower(Tenant.name).like(like, escape="\\"),
                func.lower(Tenant.city).like(like, escape="\\"),
            )
        )
    if niche:
        stmt = stmt.where(Tenant.niche == niche)
    if status:
        stmt = stmt.where(Tenant.status == status)
    page = min(max(page, 1), MAX_PAGE)
    stmt = (
        stmt.order_by(Tenant.created_at.desc(), Tenant.id)
        .limit(page_size + 1)
        .offset((page - 1) * page_size)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return rows[:page_size], len(rows) > page_size


async def create_tenant(session: AsyncSession, data: TenantIn, *, actor: Any) -> Tenant:
    tenant = Tenant(
        slug=await _unique_slug(session, data.name),
        name=data.name,
        niche=data.niche,
        status="draft",
        city=data.city,
        address=data.address,
        phone_contact=data.phone_contact,
        owner_name=data.owner_name,
        website_url=data.website_url,
        instagram_url=data.instagram_url,
        created_by=getattr(actor, "id", None),
    )
    session.add(tenant)
    await session.flush()
    session.add(TenantProfile(tenant_id=tenant.id, hours={}, tone="", languages=["es"]))
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="tenant.create",
        entity_type="tenant",
        entity_id=tenant.id,
        tenant_id=tenant.id,
        diff={"niche": tenant.niche},
    )
    return tenant


async def update_tenant(
    session: AsyncSession, tenant: Tenant, data: TenantIn, *, actor: Any
) -> Tenant:
    changed = [
        f for f in data.model_fields if getattr(tenant, f) != getattr(data, f) and f != "niche"
    ]
    if data.niche != tenant.niche:
        changed.append("niche")
    for field in data.model_fields:
        setattr(tenant, field, getattr(data, field))
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="tenant.update",
        entity_type="tenant",
        entity_id=tenant.id,
        tenant_id=tenant.id,
        diff={"fields": changed},
    )
    return tenant


async def set_status(session: AsyncSession, tenant: Tenant, new: str, *, actor: Any) -> Tenant:
    if new not in STATUS_TRANSITIONS.get(tenant.status, set()):
        raise ConflictError("No se puede cambiar el estado desde el actual")
    old, tenant.status = tenant.status, new
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="tenant.status",
        entity_type="tenant",
        entity_id=tenant.id,
        tenant_id=tenant.id,
        diff={"from": old, "to": new},
    )
    return tenant


async def delete_tenant(session: AsyncSession, tenant: Tenant, *, actor: Any) -> None:
    """Baja logica: conserva datos (retencion/auditoria) pero oculta la empresa."""
    tenant.deleted_at = utcnow()
    tenant.status = "offboarded"
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="tenant.delete",
        entity_type="tenant",
        entity_id=tenant.id,
        tenant_id=tenant.id,
    )


# --------------------------------------------------------------------------- perfil y horarios
async def get_profile(session: AsyncSession, tenant_id: uuid.UUID) -> TenantProfile:
    profile = await session.get(TenantProfile, tenant_id)
    if profile is None:
        profile = TenantProfile(tenant_id=tenant_id, hours={}, tone="", languages=["es"])
        session.add(profile)
        await session.flush()
    return profile


async def update_profile(
    session: AsyncSession,
    tenant: Tenant,
    *,
    hours: dict[str, Any] | None = None,
    tone: str | None = None,
    handoff_phone: str | None = None,
    set_handoff: bool = False,
    actor: Any,
) -> TenantProfile:
    profile = await get_profile(session, tenant.id)
    changed: list[str] = []
    if hours is not None:
        profile.hours = hours
        changed.append("hours")
    if tone is not None:
        profile.tone = tone[:1000]
        changed.append("tone")
    if set_handoff:
        profile.handoff_phone = handoff_phone
        changed.append("handoff_phone")
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="tenant.profile",
        entity_type="tenant",
        entity_id=tenant.id,
        tenant_id=tenant.id,
        diff={"fields": changed},
    )
    return profile


# --------------------------------------------------------------------------- servicios
async def list_services(session: AsyncSession, tenant_id: uuid.UUID) -> list[Service]:
    repo = TenantScopedRepo(session, Service, tenant_id)
    return await repo.list(order_by=[Service.sort, Service.created_at])


async def add_service(
    session: AsyncSession, tenant_id: uuid.UUID, data: ServiceIn, *, actor: Any
) -> Service:
    repo = TenantScopedRepo(session, Service, tenant_id)
    count = await repo.count()
    if count >= MAX_SERVICES:
        raise AppError("limit", f"Máximo {MAX_SERVICES} servicios por empresa", 422)
    svc = await repo.add(Service(**data.model_dump(), sort=count))
    await log_event(
        session,
        actor=actor,
        action="service.create",
        entity_type="service",
        entity_id=svc.id,
        tenant_id=tenant_id,
    )
    return svc


async def get_service(
    session: AsyncSession, tenant_id: uuid.UUID, service_id: uuid.UUID
) -> Service:
    svc = await TenantScopedRepo(session, Service, tenant_id).get(service_id)
    if svc is None:
        raise NotFoundError("Servicio no encontrado")
    return svc


async def update_service(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    service_id: uuid.UUID,
    data: ServiceIn,
    *,
    actor: Any,
) -> Service:
    svc = await get_service(session, tenant_id, service_id)
    for key, value in data.model_dump().items():
        setattr(svc, key, value)
    svc.needs_review = False  # el operador confirmo precio/datos
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="service.update",
        entity_type="service",
        entity_id=svc.id,
        tenant_id=tenant_id,
    )
    return svc


async def delete_service(
    session: AsyncSession, tenant_id: uuid.UUID, service_id: uuid.UUID, *, actor: Any
) -> None:
    svc = await get_service(session, tenant_id, service_id)
    await TenantScopedRepo(session, Service, tenant_id).delete(svc)
    await log_event(
        session,
        actor=actor,
        action="service.delete",
        entity_type="service",
        entity_id=service_id,
        tenant_id=tenant_id,
    )


async def replace_services(
    session: AsyncSession, tenant_id: uuid.UUID, items: list[ServiceIn], *, actor: Any
) -> list[Service]:
    """Reemplaza el catalogo completo (paso 2 del wizard, mientras la empresa es borrador)."""
    if len(items) > MAX_SERVICES:
        raise AppError("limit", f"Máximo {MAX_SERVICES} servicios por empresa", 422)
    repo = TenantScopedRepo(session, Service, tenant_id)
    # Borrado en lote (1 sentencia) en vez de N deletes ORM.
    await session.execute(delete(Service).where(Service.tenant_id == tenant_id))
    created = [await repo.add(Service(**it.model_dump(), sort=i)) for i, it in enumerate(items)]
    await log_event(
        session,
        actor=actor,
        action="service.replace",
        entity_type="tenant",
        entity_id=tenant_id,
        tenant_id=tenant_id,
        diff={"count": len(created)},
    )
    return created


# --------------------------------------------------------------------------- FAQs
async def list_faqs(session: AsyncSession, tenant_id: uuid.UUID) -> list[Faq]:
    return await TenantScopedRepo(session, Faq, tenant_id).list(order_by=[Faq.sort, Faq.created_at])


async def add_faq(session: AsyncSession, tenant_id: uuid.UUID, data: FaqIn, *, actor: Any) -> Faq:
    repo = TenantScopedRepo(session, Faq, tenant_id)
    faq = await repo.add(Faq(**data.model_dump(), source="manual", sort=await repo.count()))
    await log_event(
        session,
        actor=actor,
        action="faq.create",
        entity_type="faq",
        entity_id=faq.id,
        tenant_id=tenant_id,
    )
    return faq


async def _get_faq(session: AsyncSession, tenant_id: uuid.UUID, faq_id: uuid.UUID) -> Faq:
    faq = await TenantScopedRepo(session, Faq, tenant_id).get(faq_id)
    if faq is None:
        raise NotFoundError("Pregunta frecuente no encontrada")
    return faq


async def update_faq(
    session: AsyncSession, tenant_id: uuid.UUID, faq_id: uuid.UUID, data: FaqIn, *, actor: Any
) -> Faq:
    faq = await _get_faq(session, tenant_id, faq_id)
    for key, value in data.model_dump().items():
        setattr(faq, key, value)
    faq.needs_review = False
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="faq.update",
        entity_type="faq",
        entity_id=faq.id,
        tenant_id=tenant_id,
    )
    return faq


async def delete_faq(
    session: AsyncSession, tenant_id: uuid.UUID, faq_id: uuid.UUID, *, actor: Any
) -> None:
    faq = await _get_faq(session, tenant_id, faq_id)
    await TenantScopedRepo(session, Faq, tenant_id).delete(faq)
    await log_event(
        session,
        actor=actor,
        action="faq.delete",
        entity_type="faq",
        entity_id=faq_id,
        tenant_id=tenant_id,
    )


# --------------------------------------------------------------------------- secretos
@dataclass(frozen=True, slots=True)
class SecretMeta:
    """Lo unico que la UI puede ver de un secreto."""

    kind: str
    last4: str
    rotated_at: Any


def _secret_aad(tenant_id: uuid.UUID, kind: str) -> bytes:
    return make_aad("tenant_secrets", tenant_id, kind)


async def set_secret(
    session: AsyncSession, tenant_id: uuid.UUID, data: SecretIn, *, actor: Any
) -> SecretMeta:
    """Cifra (AES-GCM, AAD ligado a tenant+tipo) y guarda; crea o rota."""
    blob = get_crypto().encrypt_str(data.value, aad=_secret_aad(tenant_id, data.kind))
    repo = TenantScopedRepo(session, TenantSecret, tenant_id)
    row = (await repo.list(TenantSecret.kind == data.kind))[:1]
    last4 = data.value[-4:]
    if row:
        secret, action = row[0], "secret.rotate"
        secret.ciphertext, secret.nonce, secret.key_version = (
            blob.ciphertext,
            blob.nonce,
            blob.key_version,
        )
        secret.last4, secret.rotated_at = last4, utcnow()
    else:
        secret, action = (
            await repo.add(
                TenantSecret(
                    kind=data.kind,
                    ciphertext=blob.ciphertext,
                    nonce=blob.nonce,
                    key_version=blob.key_version,
                    last4=last4,
                )
            ),
            "secret.create",
        )
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action=action,
        entity_type="tenant_secret",
        entity_id=secret.id,
        tenant_id=tenant_id,
        diff={"kind": data.kind},
    )
    return SecretMeta(secret.kind, secret.last4, secret.rotated_at)


async def list_secret_meta(session: AsyncSession, tenant_id: uuid.UUID) -> list[SecretMeta]:
    rows = await TenantScopedRepo(session, TenantSecret, tenant_id).list(order_by=TenantSecret.kind)
    return [SecretMeta(r.kind, r.last4, r.rotated_at) for r in rows]


async def get_secret_value(session: AsyncSession, tenant_id: uuid.UUID, kind: str) -> str | None:
    """Descifra para uso interno (envio Twilio, Google). Nunca se expone en la UI."""
    rows = await TenantScopedRepo(session, TenantSecret, tenant_id).list(TenantSecret.kind == kind)
    if not rows:
        return None
    row = rows[0]
    return get_crypto().decrypt_str(
        EncryptedBlob(row.ciphertext, row.nonce, row.key_version),
        aad=_secret_aad(tenant_id, kind),
    )


async def delete_secret(
    session: AsyncSession, tenant_id: uuid.UUID, kind: str, *, actor: Any
) -> None:
    repo = TenantScopedRepo(session, TenantSecret, tenant_id)
    rows = await repo.list(TenantSecret.kind == kind)
    if not rows:
        raise NotFoundError("Secreto no encontrado")
    await repo.delete(rows[0])
    await log_event(
        session,
        actor=actor,
        action="secret.delete",
        entity_type="tenant_secret",
        entity_id=kind,
        tenant_id=tenant_id,
        diff={"kind": kind},
    )


# --------------------------------------------------------------------------- canales
async def list_channels(session: AsyncSession, tenant_id: uuid.UUID) -> list[ChannelAccount]:
    return await TenantScopedRepo(session, ChannelAccount, tenant_id).list(
        order_by=ChannelAccount.created_at
    )


async def add_channel(
    session: AsyncSession, tenant_id: uuid.UUID, data: ChannelIn, *, actor: Any
) -> ChannelAccount:
    repo = TenantScopedRepo(session, ChannelAccount, tenant_id)
    try:
        async with session.begin_nested():
            account = await repo.add(
                ChannelAccount(channel="whatsapp", provider="twilio", **data.model_dump())
            )
    except IntegrityError as exc:
        raise ConflictError("Ese número ya está registrado en otra empresa") from exc
    await log_event(
        session,
        actor=actor,
        action="channel.create",
        entity_type="channel_account",
        entity_id=account.id,
        tenant_id=tenant_id,
    )
    return account


async def update_channel(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    channel_id: uuid.UUID,
    data: ChannelIn,
    *,
    actor: Any,
) -> ChannelAccount:
    account = await TenantScopedRepo(session, ChannelAccount, tenant_id).get(channel_id)
    if account is None:
        raise NotFoundError("Canal no encontrado")
    try:
        async with session.begin_nested():
            for key, value in data.model_dump().items():
                setattr(account, key, value)
            await session.flush()
    except IntegrityError as exc:
        raise ConflictError("Ese número ya está registrado en otra empresa") from exc
    await log_event(
        session,
        actor=actor,
        action="channel.update",
        entity_type="channel_account",
        entity_id=account.id,
        tenant_id=tenant_id,
    )
    return account


async def delete_channel(
    session: AsyncSession, tenant_id: uuid.UUID, channel_id: uuid.UUID, *, actor: Any
) -> None:
    repo = TenantScopedRepo(session, ChannelAccount, tenant_id)
    account = await repo.get(channel_id)
    if account is None:
        raise NotFoundError("Canal no encontrado")
    await repo.delete(account)
    await log_event(
        session,
        actor=actor,
        action="channel.delete",
        entity_type="channel_account",
        entity_id=channel_id,
        tenant_id=tenant_id,
    )


# --------------------------------------------------------------------------- wizard / generacion
async def build_factory_input(
    session: AsyncSession, tenant: Tenant, notes: str = ""
) -> factory_service.FactoryInput:
    profile = await get_profile(session, tenant.id)
    services = [
        {
            "name": s.name,
            "price_cop": s.price_cop,
            "duration_min": s.duration_min,
            "description": s.description,
        }
        for s in await list_services(session, tenant.id)
        if s.is_active
    ]
    return factory_service.FactoryInput(
        name=tenant.name,
        niche=tenant.niche,
        city=tenant.city,
        address=tenant.address,
        phone=tenant.phone_contact,
        website_url=tenant.website_url,
        instagram_url=tenant.instagram_url,
        services=services,
        hours=dict(profile.hours or {}),
        notes=notes[:2000],
    )


async def start_generation(
    session: AsyncSession, tenant: Tenant, *, actor: Any, notes: str = "", consent: bool
) -> None:
    """Valida consentimiento, pasa a ``building`` y encola el job de generacion."""
    if not consent:
        raise AppError("consent_required", "Debes autorizar el uso de datos del negocio", 422)
    if tenant.status not in {"draft", "review"}:
        raise ConflictError("Esta empresa ya tiene un bot generado o está en proceso")
    # Reclamo atomico: dos clics/peticiones concurrentes no encolan dos generaciones.
    claimed = await session.execute(
        update(Tenant)
        .where(Tenant.id == tenant.id, Tenant.status.in_(("draft", "review")))
        .values(status="building")
    )
    if claimed.rowcount == 0:
        raise ConflictError("Esta empresa ya tiene un bot generado o está en proceso")
    tenant.status = "building"
    tenant.dpa_version = DPA_VERSION
    tenant.dpa_accepted_at = utcnow()
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="tenant.generate",
        entity_type="tenant",
        entity_id=tenant.id,
        tenant_id=tenant.id,
    )
    await session.commit()  # el job corre en otra sesion y debe ver el estado
    await enqueue(
        GENERATE_JOB,
        str(tenant.id),
        notes=notes[:2000],
        actor_id=str(getattr(actor, "id", "")) or None,
    )


@register_job(GENERATE_JOB)
async def generate_bot_job(
    ctx: dict[str, Any], tenant_id: str, *, notes: str = "", actor_id: str | None = None
) -> None:
    """Job puro: ``factory.build_bot`` y deja la empresa en ``review`` (``draft`` si falla)."""
    tid = uuid.UUID(tenant_id)
    maker = get_sessionmaker()
    try:
        async with maker() as session:
            tenant = await get_tenant(session, tid)
            inputs = await build_factory_input(session, tenant, notes)
            await factory_service.build_bot(
                session, tid, inputs, scrape=bool(tenant.website_url), llm=ctx.get("llm")
            )
            await session.refresh(tenant)
            if tenant.status == "building":
                tenant.status = "review"
            await log_event(
                session,
                actor=uuid.UUID(actor_id) if actor_id else "system",
                action="tenant.generated",
                entity_type="tenant",
                entity_id=tid,
                tenant_id=tid,
            )
            await session.commit()
    except Exception as exc:
        log.error("tenant_generation_failed", tenant_id=tenant_id, error_type=type(exc).__name__)
        async with maker() as session:
            failed = (
                await session.execute(select(Tenant).where(Tenant.id == tid))
            ).scalar_one_or_none()
            if failed is not None and failed.status == "building":
                failed.status = "draft"
                await log_event(
                    session,
                    actor="system",
                    action="tenant.generate_failed",
                    entity_type="tenant",
                    entity_id=tid,
                    tenant_id=tid,
                    diff={"error_type": type(exc).__name__},
                )
                await session.commit()
