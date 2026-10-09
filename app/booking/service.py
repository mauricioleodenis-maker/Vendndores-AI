"""Reservas: busqueda de horarios, reserva idempotente sin doble booking, cancelar y mover."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.booking import availability
from app.booking.calendar_base import CalendarProvider
from app.booking.providers import get_provider
from app.core.clock import ensure_utc, to_bogota, utcnow
from app.core.errors import AppError, NotFoundError
from app.core.logging import get_logger
from app.db.models.booking import Appointment
from app.db.models.catalog import Service
from app.db.models.contacts import Contact
from app.db.models.tenants import Tenant
from app.reminders import scheduler

log = get_logger(__name__)

CANCEL_ACTORS = {"contact": "contact", "tenant": "tenant", "system": "system", "bot": "contact"}
MAX_IDEMPOTENCY_KEY = 100


class SlotTakenError(AppError):
    def __init__(self) -> None:
        super().__init__("slot_taken", "Ese horario ya no esta disponible", 409)


class SlotUnavailableError(AppError):
    def __init__(self, message: str = "Ese horario no esta disponible") -> None:
        super().__init__("slot_unavailable", message, 422)


@dataclass(frozen=True, slots=True)
class Slot:
    starts_at: datetime
    ends_at: datetime
    resource_key: str = "default"


class BookingService:
    def __init__(self, provider: CalendarProvider | None = None) -> None:
        self._provider = provider

    async def _provider_for(self, session: AsyncSession, tenant_id: uuid.UUID) -> CalendarProvider:
        return self._provider or await get_provider(session, tenant_id)

    # ------------------------------------------------------------------ consulta
    async def find_slots(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        service_id: uuid.UUID,
        date_from: date,
        date_to: date,
        *,
        limit: int = 6,
    ) -> list[Slot]:
        provider = await self._provider_for(session, tenant_id)
        pairs = await availability.compute_slots(
            session, tenant_id, service_id, date_from, date_to, provider=provider, max_slots=limit
        )
        return [Slot(s, e) for s, e in pairs]

    # ------------------------------------------------------------------ reservar
    async def book(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        *,
        contact_id: uuid.UUID,
        service_id: uuid.UUID,
        starts_at: datetime,
        idempotency_key: str,
        source: str = "bot",
    ) -> Appointment:
        if not idempotency_key or len(idempotency_key) > MAX_IDEMPOTENCY_KEY:
            raise AppError("invalid_idempotency_key", "Clave de idempotencia invalida", 422)
        if source not in ("bot", "manual"):
            raise AppError("invalid_source", "Origen de cita invalido", 422)
        existing = await self._by_idempotency(session, tenant_id, idempotency_key)
        if existing is not None:
            return existing

        starts_at = _aware_utc(starts_at)
        await self._lock_tenant(session, tenant_id)
        service = await self._service(session, tenant_id, service_id)
        contact = (
            await session.execute(
                select(Contact).where(Contact.id == contact_id, Contact.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if contact is None or contact.erased_at is not None:
            raise NotFoundError("Contacto no encontrado")
        ends_at = starts_at + timedelta(minutes=service.duration_min)
        rules = await availability.load_rules(session, tenant_id)

        if starts_at <= utcnow():
            raise SlotUnavailableError("Ese horario ya paso")
        if source == "bot":
            await self._require_offered(session, tenant_id, service, starts_at, rules)
            await self._check_contact_limits(
                session, tenant_id, contact_id, starts_at, ends_at, rules.max_active_per_contact
            )
        if await availability.active_appointments(session, tenant_id, starts_at, ends_at):
            raise SlotTakenError()

        appt = Appointment(
            tenant_id=tenant_id,
            contact_id=contact_id,
            service_id=service_id,
            resource_key="default",
            starts_at=starts_at,
            ends_at=ends_at,
            status="confirmed",
            source=source,
            idempotency_key=idempotency_key,
            sync_status="local_only",
        )
        try:
            async with session.begin_nested():
                session.add(appt)
                await session.flush()
        except IntegrityError as exc:
            # Carrera: otra transaccion gano el slot o la misma idempotency_key.
            winner = await self._by_idempotency(session, tenant_id, idempotency_key)
            if winner is not None:
                return winner
            raise SlotTakenError() from exc

        await self._push_event(session, tenant_id, appt, create=True)
        await scheduler.schedule_for_appointment(session, appt)
        await log_event(
            session,
            actor="contact" if source == "bot" else "system",
            action="appointment.create",
            entity_type="appointment",
            entity_id=appt.id,
            tenant_id=tenant_id,
            diff={"source": source},
        )
        return appt

    # ------------------------------------------------------------------ cancelar
    async def cancel(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        appointment_id: uuid.UUID,
        *,
        by: str,
        reason: str | None = None,
    ) -> Appointment:
        actor = CANCEL_ACTORS.get(by)
        if actor is None:
            raise AppError("invalid_actor", "Actor de cancelacion invalido", 422)
        appt = await self._appointment(session, tenant_id, appointment_id)
        if appt.status == "cancelled":
            return appt
        if appt.status not in availability.ACTIVE_STATUSES:
            raise AppError("not_cancellable", "Esta cita ya no se puede cancelar", 409)
        appt.status = "cancelled"
        appt.cancelled_by = actor
        appt.cancel_reason = (reason or "").strip()[:300] or None
        await session.flush()
        await scheduler.cancel_for_appointment(session, appt.id)
        await self._push_event(session, tenant_id, appt, delete=True)
        await log_event(
            session,
            actor=_audit_actor(actor),
            action="appointment.cancel",
            entity_type="appointment",
            entity_id=appt.id,
            tenant_id=tenant_id,
            diff={"by": actor},
        )
        return appt

    # ------------------------------------------------------------------ reprogramar
    async def reschedule(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        appointment_id: uuid.UUID,
        new_starts_at: datetime,
        *,
        by: str,
    ) -> Appointment:
        actor = CANCEL_ACTORS.get(by)
        if actor is None:
            raise AppError("invalid_actor", "Actor invalido", 422)
        await self._lock_tenant(session, tenant_id)
        appt = await self._appointment(session, tenant_id, appointment_id)
        if appt.status not in availability.ACTIVE_STATUSES:
            raise AppError("not_reschedulable", "Esta cita ya no se puede mover", 409)
        new_start = _aware_utc(new_starts_at)
        if new_start <= utcnow():
            raise SlotUnavailableError("Ese horario ya paso")
        if appt.service_id is None:
            raise AppError("no_service", "La cita no tiene servicio", 409)
        service = await self._service(session, tenant_id, appt.service_id)
        new_end = new_start + timedelta(minutes=service.duration_min)
        rules = await availability.load_rules(session, tenant_id)
        if actor != "tenant":
            await self._require_offered(session, tenant_id, service, new_start, rules, appt.id)
        if await availability.active_appointments(
            session, tenant_id, new_start, new_end, exclude_id=appt.id
        ):
            raise SlotTakenError()
        old = (appt.starts_at, appt.ends_at)
        appt.starts_at, appt.ends_at = new_start, new_end
        appt.reminder_sent_at = appt.reminder_24h_sent_at = appt.reminder_2h_sent_at = None
        appt.confirmed_by_contact_at = None
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError as exc:
            appt.starts_at, appt.ends_at = old
            raise SlotTakenError() from exc
        await scheduler.cancel_for_appointment(session, appt.id)
        await scheduler.schedule_for_appointment(session, appt)
        await self._push_event(session, tenant_id, appt)
        await log_event(
            session,
            actor=_audit_actor(actor),
            action="appointment.reschedule",
            entity_type="appointment",
            entity_id=appt.id,
            tenant_id=tenant_id,
            diff={"by": actor},
        )
        return appt

    # ------------------------------------------------------------------ helpers
    async def _lock_tenant(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        """Serializa reservas por tenant en Postgres (FOR UPDATE); no-op en SQLite."""
        await session.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())

    async def _by_idempotency(
        self, session: AsyncSession, tenant_id: uuid.UUID, key: str
    ) -> Appointment | None:
        return (
            await session.execute(
                select(Appointment).where(
                    Appointment.tenant_id == tenant_id, Appointment.idempotency_key == key
                )
            )
        ).scalar_one_or_none()

    async def _service(
        self, session: AsyncSession, tenant_id: uuid.UUID, service_id: uuid.UUID
    ) -> Service:
        service = (
            await session.execute(
                select(Service).where(
                    Service.id == service_id,
                    Service.tenant_id == tenant_id,
                    Service.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if service is None:
            raise NotFoundError("Servicio no encontrado")
        return service

    async def _appointment(
        self, session: AsyncSession, tenant_id: uuid.UUID, appointment_id: uuid.UUID
    ) -> Appointment:
        appt = (
            await session.execute(
                select(Appointment).where(
                    Appointment.id == appointment_id, Appointment.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if appt is None:
            raise NotFoundError("Cita no encontrada")
        return appt

    async def _require_offered(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        service: Service,
        starts_at: datetime,
        rules: availability.BookingRules,
        exclude_id: uuid.UUID | None = None,
    ) -> None:
        """El horario debe caer en la grilla calculada (horario, festivos, notice, buffer)."""
        provider = await self._provider_for(session, tenant_id)
        day = to_bogota(starts_at).date()
        tenant = (
            await session.execute(select(Tenant.timezone).where(Tenant.id == tenant_id))
        ).scalar_one_or_none()
        if tenant:
            day = starts_at.astimezone(availability.zone_for(tenant)).date()
        slots = await availability.compute_slots(
            session,
            tenant_id,
            service.id,
            day - timedelta(days=1),
            day + timedelta(days=1),
            provider=provider,
            rules=rules,
            max_slots=10_000,
            exclude_id=exclude_id,
        )
        if not any(s == starts_at for s, _ in slots):
            raise SlotUnavailableError()

    async def _check_contact_limits(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        contact_id: uuid.UUID,
        starts_at: datetime,
        ends_at: datetime,
        max_active: int,
    ) -> None:
        now = utcnow()
        active = (
            await session.execute(
                select(func.count())
                .select_from(Appointment)
                .where(
                    Appointment.tenant_id == tenant_id,
                    Appointment.contact_id == contact_id,
                    Appointment.status.in_(availability.ACTIVE_STATUSES),
                    Appointment.starts_at > now,
                )
            )
        ).scalar_one()
        if active >= max_active:
            raise AppError(
                "too_many_appointments",
                f"El contacto ya tiene {max_active} citas activas",
                409,
            )
        clash = (
            await session.execute(
                select(func.count())
                .select_from(Appointment)
                .where(
                    Appointment.tenant_id == tenant_id,
                    Appointment.contact_id == contact_id,
                    Appointment.status.in_(availability.ACTIVE_STATUSES),
                    Appointment.starts_at < ends_at,
                    Appointment.ends_at > starts_at,
                )
            )
        ).scalar_one()
        if clash:
            raise AppError("overlapping_contact", "El contacto ya tiene una cita a esa hora", 409)

    async def _push_event(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        appt: Appointment,
        *,
        create: bool = False,
        delete: bool = False,
    ) -> None:
        """Espeja en el calendario externo. Un fallo nunca revierte la cita local."""
        provider = await self._provider_for(session, tenant_id)
        try:
            if create:
                event_id = await provider.create_event(session, tenant_id, appt)
                if event_id:
                    appt.google_event_id = event_id
                    appt.sync_status = "synced"
            elif delete:
                await provider.delete_event(session, tenant_id, appt)
            else:
                await provider.update_event(session, tenant_id, appt)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "booking.calendar_sync_failed",
                appointment_id=str(appt.id),
                error_type=type(exc).__name__,
            )
            appt.sync_status = "pending_push"
        await session.flush()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise AppError("naive_datetime", "La fecha debe incluir zona horaria", 422)
    return ensure_utc(value).astimezone(UTC)


def _audit_actor(actor: str) -> str:
    """El audit_log solo admite user/system/webhook/contact; el staff queda como ``system``."""
    return "contact" if actor == "contact" else "system"
