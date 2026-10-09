"""KPIs de Inicio, Conversaciones (lista, hilo, tomar control) y Citas entre empresas."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.billing import service as billing
from app.channels.sender import send_whatsapp_text
from app.channels.service import decrypt_phone
from app.conversation.handoff import bot_is_paused, open_handoff, resume_bot
from app.conversation.memory import decrypt_body, encrypt_body
from app.core.clock import ensure_utc, utcnow
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.db.models.booking import Appointment
from app.db.models.catalog import Service
from app.db.models.contacts import Contact
from app.db.models.conversations import Conversation, Handoff, Message
from app.db.models.leads import Lead
from app.db.models.tenants import Tenant
from app.db.models.users import User
from app.privacy.service import redact_pii

log = get_logger(__name__)
HOT_SCORE = 70
PAGE_SIZE = 25
MAX_REPLY_LEN = 1000
THREAD_LIMIT = 200
DUPLICATE_WINDOW_S = 10
OVERVIEW_LIMIT = 200
CONVERSATION_FILTERS = ("open", "handoff", "closed")


# --------------------------------------------------------------------------- Inicio
@dataclass(frozen=True, slots=True)
class Kpis:
    active_tenants: int
    conversations_month: int
    bot_appointments_month: int
    hot_leads: int
    demos: int
    pending_handoffs: int
    mrr_cop: int | None  # None si el usuario no puede ver facturacion
    overdue_cop: int | None


async def _count(session: AsyncSession, stmt) -> int:  # type: ignore[no-untyped-def]
    return int((await session.execute(stmt)).scalar_one())


async def compute_kpis(
    session: AsyncSession, *, include_billing: bool, now: datetime | None = None
) -> Kpis:
    now = now or utcnow()
    month_start, month_end = billing.month_bounds(billing.current_period(now))
    active = await _count(
        session,
        select(func.count())
        .select_from(Tenant)
        .where(Tenant.status == "active", Tenant.deleted_at.is_(None), Tenant.is_demo.is_(False)),
    )
    convs = await _count(
        session,
        select(func.count())
        .select_from(Conversation)
        .where(
            Conversation.created_at >= month_start,
            Conversation.created_at < month_end,
            Conversation.channel != "sandbox",
        ),
    )
    appts = await _count(
        session,
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.source == "bot",
            Appointment.status != "cancelled",
            Appointment.created_at >= month_start,
            Appointment.created_at < month_end,
        ),
    )
    hot = await _count(
        session,
        select(func.count())
        .select_from(Lead)
        .where(Lead.score >= HOT_SCORE, Lead.stage != "cerrado", Lead.disposition == "activo"),
    )
    demos = await _count(
        session,
        select(func.count())
        .select_from(Tenant)
        .where(Tenant.is_demo.is_(True), Tenant.deleted_at.is_(None)),
    )
    handoffs = await _count(
        session, select(func.count()).select_from(Handoff).where(Handoff.status == "open")
    )
    mrr = overdue = None
    if include_billing:
        s = await billing.summary(session, now=now)
        mrr, overdue = s.mrr_cop, s.overdue_cop
    return Kpis(active, convs, appts, hot, demos, handoffs, mrr, overdue)


# --------------------------------------------------------------------------- Conversaciones
@dataclass(frozen=True, slots=True)
class ConversationRow:
    id: uuid.UUID
    tenant_name: str
    phone: str | None
    status: str
    paused: bool
    last_message_at: datetime | None
    preview: str


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    id: uuid.UUID
    role: str
    direction: str
    body: str
    created_at: datetime
    status: str


async def list_conversations(
    session: AsyncSession,
    *,
    status: str | None = None,
    q: str | None = None,
    tenant_id: uuid.UUID | None = None,
    page: int = 1,
    actor: User | None = None,
) -> tuple[list[ConversationRow], bool]:
    """Lista (agencia: cartera completa por diseno, MAESTRO §5). La busqueda queda auditada."""
    page = max(1, page)
    stmt = (
        select(Conversation, Tenant.name, Contact)
        .join(Tenant, Tenant.id == Conversation.tenant_id)
        .outerjoin(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.channel != "sandbox")
        .order_by(Conversation.last_message_at.desc().nulls_last(), Conversation.created_at.desc())
        .limit(PAGE_SIZE + 1)
        .offset((page - 1) * PAGE_SIZE)
    )
    if status in CONVERSATION_FILTERS:
        stmt = stmt.where(Conversation.status == status)
    if tenant_id:
        stmt = stmt.where(Conversation.tenant_id == tenant_id)
    if q:
        needle = q.strip()[:100].replace("%", r"\%").replace("_", r"\_")
        stmt = stmt.where(
            exists().where(
                and_(
                    Message.conversation_id == Conversation.id,
                    Message.body_redacted.ilike(f"%{needle}%", escape="\\"),
                )
            )
        )
    if q and q.strip() and actor is not None:
        await log_event(
            session,
            actor=actor,
            action="conversation.search",
            entity_type="conversation",
            tenant_id=tenant_id,
            diff={"q_len": len(q.strip()[:100]), "estado": status or "todos", "page": page},
        )
    result = (await session.execute(stmt)).all()
    has_more = len(result) > PAGE_SIZE
    result = result[:PAGE_SIZE]
    previews = await _previews(session, [c.id for c, _, _ in result])
    rows = [
        ConversationRow(
            id=c.id,
            tenant_name=name,
            phone=decrypt_phone(c.tenant_id, contact) if contact else None,
            status=c.status,
            paused=bot_is_paused(c),
            last_message_at=c.last_message_at,
            preview=previews.get(c.id, ""),
        )
        for c, name, contact in result
    ]
    return rows, has_more


async def _previews(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    latest = (
        select(Message.conversation_id, func.max(Message.created_at).label("m"))
        .where(Message.conversation_id.in_(ids))
        .group_by(Message.conversation_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Message.conversation_id, Message.body_redacted).join(
                latest,
                and_(
                    Message.conversation_id == latest.c.conversation_id,
                    Message.created_at == latest.c.m,
                ),
            )
        )
    ).all()
    return {cid: (body or "")[:80] for cid, body in rows}


async def get_conversation(
    session: AsyncSession, conversation_id: uuid.UUID, *, for_update: bool = False
) -> Conversation:
    if for_update:
        # Serializa acciones concurrentes sobre la misma conversacion (no-op en SQLite).
        conv = (
            await session.execute(
                select(Conversation)
                .where(Conversation.id == conversation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    else:
        conv = await session.get(Conversation, conversation_id)
    if conv is None:
        raise NotFoundError("Conversación no encontrada")
    return conv


async def load_thread(
    session: AsyncSession, conv: Conversation, *, actor: User | None = None
) -> list[ThreadMessage]:
    """Transcript descifrado (ultimos ``THREAD_LIMIT``). La lectura queda auditada."""
    msgs = (
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conv.id, Message.tenant_id == conv.tenant_id)
                .order_by(Message.created_at.desc())
                .limit(THREAD_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    if actor is not None:
        await log_event(
            session,
            actor=actor,
            action="conversation.view",
            entity_type="conversation",
            entity_id=conv.id,
            tenant_id=conv.tenant_id,
        )
    return [
        ThreadMessage(
            m.id, m.role, m.direction, decrypt_body(conv.tenant_id, m), m.created_at, m.status
        )
        for m in reversed(msgs)
    ]


async def take_control(session: AsyncSession, conv: Conversation, *, actor: User) -> None:
    """Pasa la conversacion a un humano: el bot queda en silencio hasta devolverla."""
    handoff = await open_handoff(session, conv, "user_request", "Tomada por operador")
    handoff.status = "claimed"
    handoff.claimed_by = actor.id
    conv.bot_paused_until = None  # pausa indefinida mientras status == handoff
    await log_event(
        session,
        actor=actor,
        action="conversation.take_control",
        entity_type="conversation",
        entity_id=conv.id,
        tenant_id=conv.tenant_id,
    )


async def return_to_bot(session: AsyncSession, conv: Conversation, *, actor: User) -> None:
    resume_bot(conv)
    open_handoffs = (
        (
            await session.execute(
                select(Handoff).where(
                    Handoff.conversation_id == conv.id, Handoff.status.in_(("open", "claimed"))
                )
            )
        )
        .scalars()
        .all()
    )
    for h in open_handoffs:
        h.status = "resolved"
        h.resolved_at = utcnow()
    await log_event(
        session,
        actor=actor,
        action="conversation.return_to_bot",
        entity_type="conversation",
        entity_id=conv.id,
        tenant_id=conv.tenant_id,
    )


async def send_manual_reply(
    session: AsyncSession, conv: Conversation, text: str, *, actor: User
) -> Message:
    text = text.strip()
    if not text or len(text) > MAX_REPLY_LEN:
        raise AppError(
            "invalid_message", f"El mensaje debe tener entre 1 y {MAX_REPLY_LEN} caracteres", 422
        )
    if conv.status != "handoff":
        raise ConflictError("Toma el control de la conversación antes de responder")
    contact = await session.get(Contact, conv.contact_id) if conv.contact_id else None
    phone = decrypt_phone(conv.tenant_id, contact) if contact else None
    if phone is None:
        raise AppError("no_phone", "No hay un teléfono disponible para este contacto", 409)
    # Anti doble envio (doble clic / reintento): mismo texto del operador en pocos segundos.
    recent = (
        await session.execute(
            select(Message.id)
            .where(
                Message.conversation_id == conv.id,
                Message.tenant_id == conv.tenant_id,
                Message.role == "human_agent",
                Message.body_redacted == redact_pii(text)[:2000],
                Message.created_at >= utcnow() - timedelta(seconds=DUPLICATE_WINDOW_S),
            )
            .limit(1)
        )
    ).first()
    if recent is not None:
        raise ConflictError("Ese mensaje ya se envió hace unos segundos")
    now = utcnow()
    # Se persiste ANTES de enviar: si el commit falla despues, no hay mensaje fantasma.
    msg = Message(
        tenant_id=conv.tenant_id,
        conversation_id=conv.id,
        direction="out",
        role="human_agent",
        body_enc=encrypt_body(conv.tenant_id, text),
        body_redacted=redact_pii(text)[:2000],
        status="queued",
        processed_at=now,
    )
    session.add(msg)
    await session.flush()
    result = await send_whatsapp_text(session, tenant_id=conv.tenant_id, to_e164=phone, body=text)
    if not result.ok:
        detail = (
            "Fuera de la ventana de 24 h de WhatsApp"
            if result.error == "63016"
            else "No se pudo enviar el mensaje"
        )
        log.warning("dashboard.manual_reply_failed", error=result.error)
        raise AppError("send_failed", detail, 409)  # la transaccion se revierte: msg no queda
    msg.provider_sid = result.sid
    msg.status = "sent"
    conv.last_message_at = now
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="conversation.manual_reply",
        entity_type="conversation",
        entity_id=conv.id,
        tenant_id=conv.tenant_id,
    )
    return msg


def window_open(conv: Conversation) -> bool:
    last = conv.last_inbound_at
    return last is not None and utcnow() - ensure_utc(last) < timedelta(hours=24)


# ------------------------------------------------------------------ Citas (todas las empresas)
@dataclass(frozen=True, slots=True)
class AppointmentOverviewRow:
    id: uuid.UUID
    tenant_name: str
    service: str
    starts_at: datetime
    status: str
    source: str


async def appointments_overview(
    session: AsyncSession, *, days: int = 7, now: datetime | None = None
) -> tuple[list[AppointmentOverviewRow], dict[str, int]]:
    """Devuelve (filas acotadas a 200, conteo real por empresa ``nombre -> n``)."""
    now = now or utcnow()
    days = max(1, min(days, 60))
    cond = and_(
        Appointment.starts_at >= now - timedelta(hours=1),
        Appointment.starts_at < now + timedelta(days=days),
        Appointment.status.in_(("pending", "confirmed")),
        Tenant.deleted_at.is_(None),
    )
    rows = (
        await session.execute(
            select(Appointment, Tenant.name, Service.name)
            .join(Tenant, Tenant.id == Appointment.tenant_id)
            .outerjoin(Service, Service.id == Appointment.service_id)
            .where(cond)
            .order_by(Appointment.starts_at)
            .limit(OVERVIEW_LIMIT)
        )
    ).all()
    out = [
        AppointmentOverviewRow(a.id, tname, sname or "-", a.starts_at, a.status, a.source)
        for a, tname, sname in rows
    ]
    counts = (
        await session.execute(
            select(Tenant.id, Tenant.name, func.count())
            .select_from(Appointment)
            .join(Tenant, Tenant.id == Appointment.tenant_id)
            .where(cond)
            .group_by(Tenant.id, Tenant.name)
            .order_by(Tenant.name)
        )
    ).all()
    per_tenant: dict[str, int] = {}
    for _tid, name, n in counts:
        key = name if name not in per_tenant else f"{name} ({str(_tid)[:4]})"
        per_tenant[key] = int(n)
    return out, per_tenant
