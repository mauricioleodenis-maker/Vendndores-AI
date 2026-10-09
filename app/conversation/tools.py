"""Herramientas del recepcionista. El backend inyecta tenant/contacto/conversacion: el LLM nunca
los envia. Argumentos validados con pydantic (``extra=forbid``); efectos limitados al contacto."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.service import BookingService
from app.conversation.context import BusinessContext, format_when, price_text
from app.conversation.guardrails import fold, is_affirmation, sanitize_config_text
from app.conversation.handoff import LLM_REASONS, normalize_reason, open_handoff
from app.core.clock import BOGOTA, to_bogota, utcnow
from app.core.crypto import get_crypto, make_aad, pack_blob
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models.booking import Appointment
from app.db.models.catalog import Service
from app.db.models.contacts import Contact
from app.db.models.conversations import Conversation
from app.privacy.service import redact_pii

log = get_logger(__name__)

MAX_SLOTS = 6
MAX_RANGE_DAYS = 7
Period = Literal["manana", "tarde", "noche"]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CheckAvailabilityArgs(_Args):
    service: str = Field(min_length=1, max_length=80, description="ID o nombre del servicio")
    date_from: date
    date_to: date | None = None
    preferred_period: Period | None = None


class BookAppointmentArgs(_Args):
    service: str = Field(min_length=1, max_length=80)
    slot_start: datetime = Field(
        description="ISO 8601 con offset, exactamente como lo devolvió check_availability"
    )
    customer_name: str = Field(min_length=2, max_length=80)
    notes: str | None = Field(default=None, max_length=200)


class CancelAppointmentArgs(_Args):
    appointment_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=140)


class BusinessInfoArgs(_Args):
    topic: Literal["servicios", "precios", "horarios", "ubicacion", "politicas"]


class HandoffArgs(_Args):
    reason: Literal[
        "user_request", "low_confidence", "medical_urgent", "complaint",
        "out_of_scope_repeated", "tool_failure",
        # alias aceptados por compatibilidad con support/13
        "asked_for_human", "urgent_medical", "out_of_scope", "refund_or_billing", "unclear",
        "price_unknown",
    ]  # fmt: skip
    summary: str = Field(default="", max_length=300)
    urgency: bool = False


TOOL_MODELS: dict[str, type[_Args]] = {
    "check_availability": CheckAvailabilityArgs,
    "book_appointment": BookAppointmentArgs,
    "cancel_appointment": CancelAppointmentArgs,
    "get_business_info": BusinessInfoArgs,
    "handoff_to_human": HandoffArgs,
}
_DESCRIPTIONS = {
    "check_availability": (
        "Devuelve hasta 6 horarios libres de un servicio entre date_from y date_to (máx 7 días). "
        "Llamar antes de ofrecer cualquier horario."
    ),
    "book_appointment": (
        "Agenda una cita en un horario devuelto por check_availability. Solo tras la confirmación "
        "explícita del cliente (sí/confirmo) en su último mensaje."
    ),
    "cancel_appointment": (
        "Cancela una cita activa de ESTE cliente (id de la lista de citas activas). Solo tras "
        "su confirmación explícita."
    ),
    "get_business_info": (
        "Información aprobada del negocio: servicios, precios, horarios, ubicación, políticas."
    ),
    "handoff_to_human": (
        "Transfiere la conversación a una persona del equipo y pausa el bot. Usar si el cliente lo "
        "pide, en urgencias, quejas, o cuando falte información."
    ),
}


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    schema["additionalProperties"] = False
    return schema


def tool_definitions() -> list[dict[str, Any]]:
    out = []
    for name, model in TOOL_MODELS.items():
        schema = _schema(model)
        if name == "handoff_to_human":
            schema["properties"]["reason"]["enum"] = list(LLM_REASONS)
        out.append({"name": name, "description": _DESCRIPTIONS[name], "input_schema": schema})
    return out


@dataclass(slots=True)
class ToolContext:
    session: AsyncSession
    tenant_id: uuid.UUID
    bc: BusinessContext
    last_user_text: str
    conversation: Conversation | None = None
    contact: Contact | None = None
    sandbox: bool = False
    booking: BookingService = field(default_factory=BookingService)
    offered: set[str] = field(default_factory=set)
    failures: int = 0
    handoff_reason: str | None = None
    tools_used: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ToolOutcome:
    content: dict[str, Any]
    is_error: bool = False

    def dumps(self) -> str:
        return json.dumps(self.content, ensure_ascii=False, default=str)


def _err(message: str) -> ToolOutcome:
    return ToolOutcome({"error": message}, is_error=True)


def slot_key(service_id: uuid.UUID, starts_at: datetime) -> str:
    return f"{service_id}|{starts_at.astimezone(UTC).replace(microsecond=0).isoformat()}"


def resolve_service(bc: BusinessContext, ref: str) -> Service | None:
    """Servicio activo por UUID o por nombre (exacto o contenido, si es unico)."""
    try:
        return bc.service_by_id.get(uuid.UUID(ref.strip()))
    except ValueError:
        pass
    needle = fold(ref)
    exact = [s for s in bc.services if fold(s.name) == needle]
    if len(exact) == 1:
        return exact[0]
    partial = [s for s in bc.services if needle in fold(s.name) or fold(s.name) in needle]
    return partial[0] if len(partial) == 1 else None


def _in_period(dt: datetime, period: Period | None) -> bool:
    if period is None:
        return True
    hour = to_bogota(dt).hour
    return {"manana": 5 <= hour < 12, "tarde": 12 <= hour < 18, "noche": hour >= 18}[period]


def _idem_key(ctx: ToolContext, key: str) -> str:
    base = f"{ctx.conversation.id if ctx.conversation else 'sandbox'}|{key}"
    return hashlib.sha256(base.encode()).hexdigest()[:48]


# --------------------------------------------------------------------------- handlers
async def check_availability(ctx: ToolContext, args: CheckAvailabilityArgs) -> ToolOutcome:
    service = resolve_service(ctx.bc, args.service)
    if service is None:
        return _err("Servicio no encontrado. Usa uno de la lista de servicios del negocio.")
    today = to_bogota(utcnow()).date()
    if args.date_from < today:
        return _err("La fecha ya pasó. Pide una fecha de hoy en adelante.")
    max_days = int(ctx.bc.booking_rules.get("max_days_ahead", 60) or 60)
    if args.date_from > today + timedelta(days=max_days):
        return _err(f"Solo se agenda hasta {max_days} días hacia adelante.")
    date_to = min(
        args.date_to or args.date_from, args.date_from + timedelta(days=MAX_RANGE_DAYS - 1)
    )
    if date_to < args.date_from:
        return _err("date_to no puede ser anterior a date_from.")
    slots = await ctx.booking.find_slots(
        ctx.session, ctx.tenant_id, service.id, args.date_from, date_to, limit=MAX_SLOTS * 4
    )
    chosen = [s for s in slots if _in_period(s.starts_at, args.preferred_period)][:MAX_SLOTS]
    for s in chosen:
        ctx.offered.add(slot_key(service.id, s.starts_at))
    return ToolOutcome(
        {
            "service": service.name,
            "slots": [
                {
                    "start": s.starts_at.astimezone(BOGOTA).isoformat(),
                    "label": format_when(s.starts_at),
                }
                for s in chosen
            ],
            "note": "" if chosen else "Sin disponibilidad en ese rango; ofrece otra fecha.",
        }
    )


async def _slot_still_free(ctx: ToolContext, service: Service, starts_at: datetime) -> bool:
    day = to_bogota(starts_at).date()
    slots = await ctx.booking.find_slots(
        ctx.session, ctx.tenant_id, service.id, day, day, limit=200
    )
    return any(s.starts_at.astimezone(UTC) == starts_at for s in slots)


async def book_appointment(ctx: ToolContext, args: BookAppointmentArgs) -> ToolOutcome:
    service = resolve_service(ctx.bc, args.service)
    if service is None:
        return _err("Servicio no encontrado.")
    if args.slot_start.tzinfo is None:
        return _err("slot_start debe incluir zona horaria (ej. 2026-10-09T15:00:00-05:00).")
    starts_at = args.slot_start.astimezone(UTC)
    if starts_at <= utcnow():
        return _err("Ese horario ya pasó.")
    if not is_affirmation(ctx.last_user_text):
        return _err(
            "Falta la confirmación explícita del cliente. Resume servicio, fecha y hora y pregunta "
            "si confirma; luego llama de nuevo."
        )
    key = slot_key(service.id, starts_at)
    # El horario debe venir de un check_availability reciente. En sandbox (sin estado entre
    # turnos) se acepta si la agenda real aun lo muestra libre.
    if key not in ctx.offered and not (
        ctx.sandbox and await _slot_still_free(ctx, service, starts_at)
    ):
        return _err(
            "Ese horario no fue ofrecido. Llama check_availability y elige uno de los resultados."
        )
    if ctx.sandbox:
        return ToolOutcome(
            {
                "status": "confirmed",
                "simulated": True,
                "when": format_when(starts_at),
                "service": service.name,
            }
        )
    if ctx.contact is None:
        return _err("No se puede agendar sin un contacto identificado.")
    try:
        appt = await ctx.booking.book(
            ctx.session,
            ctx.tenant_id,
            contact_id=ctx.contact.id,
            service_id=service.id,
            starts_at=starts_at,
            idempotency_key=_idem_key(ctx, key),
            source="bot",
        )
    except AppError as exc:
        ctx.failures += 1
        return _err(f"No se pudo agendar: {exc.message}. Ofrece otro horario.")
    except NotImplementedError:
        ctx.failures += 1
        return _err("La agenda no está disponible en este momento.")
    await _remember_customer(ctx, args, appt)
    return ToolOutcome(
        {
            "status": appt.status,
            "appointment_id": str(appt.id),
            "service": service.name,
            "when": format_when(appt.starts_at),
        }
    )


async def _remember_customer(
    ctx: ToolContext, args: BookAppointmentArgs, appt: Appointment
) -> None:
    """Guarda (cifrado) el nombre del contacto y las notas generales de la cita."""
    crypto = get_crypto()
    if ctx.contact is not None and not ctx.contact.display_name_enc:
        aad = make_aad("contacts", ctx.tenant_id, "display_name_enc")
        ctx.contact.display_name_enc = pack_blob(crypto.encrypt_str(args.customer_name, aad=aad))
    if args.notes:
        aad = make_aad("appointments", ctx.tenant_id, "notes_enc")
        appt.notes_enc = pack_blob(crypto.encrypt_str(redact_pii(args.notes), aad=aad))
    await ctx.session.flush()


async def cancel_appointment(ctx: ToolContext, args: CancelAppointmentArgs) -> ToolOutcome:
    if not is_affirmation(ctx.last_user_text, allow_cancel=True):
        return _err("Falta la confirmación explícita del cliente para cancelar. Pídela primero.")
    if ctx.sandbox:
        return _err("En modo de prueba no hay citas reales para cancelar.")
    if ctx.contact is None:
        return _err("No se puede cancelar sin un contacto identificado.")
    appt = (
        await ctx.session.execute(
            select(Appointment).where(
                Appointment.id == args.appointment_id,
                Appointment.tenant_id == ctx.tenant_id,
                Appointment.contact_id == ctx.contact.id,
            )
        )
    ).scalar_one_or_none()
    if appt is None or appt.status not in ("pending", "confirmed"):
        return _err("No encontré una cita activa con ese id asociada a este número.")
    try:
        done = await ctx.booking.cancel(
            ctx.session, ctx.tenant_id, appt.id, by="contact", reason=args.reason
        )
    except AppError as exc:
        return _err(f"No se pudo cancelar: {exc.message}")
    except NotImplementedError:
        ctx.failures += 1
        return _err("La agenda no está disponible en este momento.")
    return ToolOutcome({"status": done.status, "appointment_id": str(done.id)})


async def get_business_info(ctx: ToolContext, args: BusinessInfoArgs) -> ToolOutcome:
    bc, tenant = ctx.bc, ctx.bc.tenant
    if args.topic == "servicios":
        data: Any = [
            {
                "id": str(s.id),
                "name": sanitize_config_text(s.name, limit=120),
                "duration_min": s.duration_min,
                # descripcion sin revisar (scrape/IA) no llega al LLM
                "description": (
                    "" if s.needs_review else sanitize_config_text(s.description or "", limit=400)
                ),
            }
            for s in bc.services
        ]
    elif args.topic == "precios":
        data = [
            {
                "name": sanitize_config_text(s.name, limit=120),
                "price": sanitize_config_text(price_text(s), limit=120),
            }
            for s in bc.services
        ]
    elif args.topic == "horarios":
        data = sanitize_config_text(bc.hours_text, limit=400)
    elif args.topic == "ubicacion":
        data = {
            "city": sanitize_config_text(tenant.city or "", limit=80),
            "address": sanitize_config_text(tenant.address or "", limit=240),
            "phone": tenant.phone_contact,
        }
    else:
        rules = ctx.bc.booking_rules
        policies = [
            sanitize_config_text(f.answer, limit=600)
            for f in bc.faqs
            if any(k in fold(f.question) for k in ("polit", "cancel", "pago", "garant"))
        ]
        data = {"reglas_de_agenda": rules, "politicas": policies}
    return ToolOutcome(
        {"topic": args.topic, "data": data or "Sin información publicada; pasa con el equipo."}
    )


async def handoff_to_human(ctx: ToolContext, args: HandoffArgs) -> ToolOutcome:
    reason = normalize_reason("medical_urgent" if args.urgency else args.reason)
    ctx.handoff_reason = reason
    if not ctx.sandbox and ctx.conversation is not None:
        await open_handoff(ctx.session, ctx.conversation, reason, args.summary)
    return ToolOutcome({"status": "handoff_created", "simulated": ctx.sandbox})


_HANDLERS = {
    "check_availability": check_availability,
    "book_appointment": book_appointment,
    "cancel_appointment": cancel_appointment,
    "get_business_info": get_business_info,
    "handoff_to_human": handoff_to_human,
}


async def run_tool(ctx: ToolContext, name: str, raw: dict[str, Any]) -> ToolOutcome:
    """Ejecuta una herramienta pedida por el LLM. Nunca lanza: devuelve el error como resultado."""
    model = TOOL_MODELS.get(name)
    if model is None:
        log.warning("tool.unknown", tool=name[:40])
        ctx.failures += 1
        return _err("Herramienta no disponible.")
    try:
        args = model.model_validate(raw)
    except ValidationError as exc:
        ctx.failures += 1
        fields = ", ".join(str(e["loc"][0]) if e["loc"] else "args" for e in exc.errors())
        return _err(
            f"Argumentos inválidos ({fields}). Corrígelos o pregunta al cliente el dato faltante."
        )
    ctx.tools_used.append(name)
    try:
        outcome = await _HANDLERS[name](ctx, args)  # type: ignore[operator]
    except AppError as exc:
        ctx.failures += 1
        return _err(exc.message)
    except Exception as exc:  # noqa: BLE001 - un fallo de tool no debe tumbar el turno
        log.error("tool.failed", tool=name, error=type(exc).__name__)
        ctx.failures += 1
        return _err("La herramienta falló. Intenta de nuevo o pasa con el equipo.")
    return outcome
