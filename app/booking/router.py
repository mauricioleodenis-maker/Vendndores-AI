"""UI de Citas (/admin/citas): agenda por dia, crear/cancelar manual y editor de horarios."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking import availability
from app.booking.service import BookingService
from app.channels.service import get_or_create_contact
from app.core.clock import ensure_utc, utcnow
from app.core.crypto import get_crypto, make_aad, unpack_blob
from app.core.deps import current_user, get_session, require_role
from app.core.errors import AppError, NotFoundError
from app.core.logging import get_logger
from app.db.models.booking import Appointment, TimeOff, WorkingHours
from app.db.models.catalog import Service
from app.db.models.contacts import Contact
from app.db.models.tenants import Tenant
from app.db.models.users import User
from app.leads.normalize import to_e164_co
from app.web.templating import render

log = get_logger(__name__)
router = APIRouter(tags=["citas"])
admin_only = require_role("admin")

WEEKDAYS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
MAX_WINDOWS_PER_DAY = 2
MAX_DAYS_AHEAD = 730  # tope de fecha aceptada (evita OverflowError y citas absurdas)
MAX_TIME_OFF_DAYS = 366
MIN_YEAR = 2000
SERVICE = BookingService()
_CLAVE_RE = re.compile(r"[0-9a-f]{32}")


@dataclass(slots=True)
class AppointmentRow:
    id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    service: str
    contact: str
    status: str
    source: str
    sync_status: str
    can_cancel: bool


def _redirect(url: str, *, ok: str | None = None, error: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if ok:
        url += f"{sep}ok={quote(ok)}"
    elif error:
        url += f"{sep}error={quote(error)}"
    return RedirectResponse(url, status_code=303)


def _flash(request: Request) -> dict[str, str] | None:
    if msg := request.query_params.get("ok"):
        return {"kind": "ok", "message": msg[:200]}
    if msg := request.query_params.get("error"):
        return {"kind": "error", "message": msg[:200]}
    return None


async def _tenants(session: AsyncSession) -> list[Tenant]:
    rows = await session.execute(
        select(Tenant).where(Tenant.deleted_at.is_(None)).order_by(Tenant.name)
    )
    return list(rows.scalars().all())


async def _pick_tenant(
    session: AsyncSession, tenant_id: uuid.UUID | None
) -> tuple[Tenant | None, list[Tenant]]:
    tenants = await _tenants(session)
    if tenant_id is not None:
        chosen = next((t for t in tenants if t.id == tenant_id), None)
        if chosen is None:
            raise NotFoundError("Empresa no encontrada")
        return chosen, tenants
    return (tenants[0] if tenants else None), tenants


def _contact_label(tenant_id: uuid.UUID, contact: Contact | None) -> str:
    if contact is None or contact.erased_at is not None:
        return "Contacto eliminado"
    if contact.display_name_enc:
        try:
            aad = make_aad("contacts", tenant_id, "display_name_enc")
            return get_crypto().decrypt_str(unpack_blob(contact.display_name_enc), aad=aad)
        except Exception as exc:  # noqa: BLE001 - clave rotada/corrupta
            log.warning("booking.contact_decrypt_failed", error_type=type(exc).__name__)
            return "Contacto"
    return "Contacto sin nombre"


def _today(tz_name: str) -> date:
    return utcnow().astimezone(availability.zone_for(tz_name)).date()


def _valid_date(d: date, tz_name: str) -> bool:
    """Rango sano: evita OverflowError al sumar dias y fechas absurdas."""
    return d.year >= MIN_YEAR and d <= _today(tz_name) + timedelta(days=MAX_DAYS_AHEAD)


def _parse_day(raw: str | None, tz_name: str) -> date:
    if raw:
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            d = None
        if d is not None and _valid_date(d, tz_name):
            return d
    return _today(tz_name)


def _day_bounds(day: date, tz_name: str) -> tuple[datetime, datetime]:
    tz = availability.zone_for(tz_name)
    return (
        datetime.combine(day, time.min, tz).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(UTC),
    )


async def _rows_for_range(
    session: AsyncSession, tenant: Tenant, start: datetime, end: datetime
) -> list[AppointmentRow]:
    stmt = (
        select(Appointment, Service.name, Contact)
        .outerjoin(Service, Service.id == Appointment.service_id)
        .outerjoin(Contact, Contact.id == Appointment.contact_id)
        .where(
            Appointment.tenant_id == tenant.id,
            Appointment.starts_at >= start,
            Appointment.starts_at < end,
        )
        .order_by(Appointment.starts_at)
    )
    out: list[AppointmentRow] = []
    for appt, svc_name, contact in (await session.execute(stmt)).all():
        out.append(
            AppointmentRow(
                id=appt.id,
                starts_at=ensure_utc(appt.starts_at),
                ends_at=ensure_utc(appt.ends_at),
                service=svc_name or "Sin servicio",
                contact=_contact_label(tenant.id, contact),
                status=appt.status,
                source=appt.source,
                sync_status=appt.sync_status,
                can_cancel=appt.status in availability.ACTIVE_STATUSES,
            )
        )
    return out


# --------------------------------------------------------------------------- agenda
@router.get("/admin/citas", response_class=HTMLResponse)
async def citas_page(
    request: Request,
    tenant_id: uuid.UUID | None = Query(None),
    dia: str | None = Query(None, max_length=10),
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    tenant, tenants = await _pick_tenant(session, tenant_id)
    ctx: dict[str, Any] = {"tenants": tenants, "tenant": tenant, "flash": _flash(request)}
    if tenant is None:
        return render(request, "booking/citas.html", ctx)
    day = _parse_day(dia, tenant.timezone)
    week_start = day - timedelta(days=day.weekday())
    w_start, _ = _day_bounds(week_start, tenant.timezone)
    _, w_end = _day_bounds(week_start + timedelta(days=6), tenant.timezone)
    week_rows = await _rows_for_range(session, tenant, w_start, w_end)
    tz = availability.zone_for(tenant.timezone)
    week = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        count = sum(
            1
            for r in week_rows
            if r.status in availability.ACTIVE_STATUSES and r.starts_at.astimezone(tz).date() == d
        )
        week.append(
            {
                "date": d,
                "label": WEEKDAYS[i][:3],
                "count": count,
                "selected": d == day,
                "holiday": availability.holiday_name(d),
            }
        )
    services = (
        (
            await session.execute(
                select(Service)
                .where(Service.tenant_id == tenant.id, Service.is_active.is_(True))
                .order_by(Service.sort, Service.name)
            )
        )
        .scalars()
        .all()
    )
    ctx.update(
        {
            "day": day,
            "prev_day": day - timedelta(days=1),
            "next_day": day + timedelta(days=1),
            "week": week,
            "rows": [r for r in week_rows if r.starts_at.astimezone(tz).date() == day],
            "services": services,
            "tz": tz,
            "holiday": availability.holiday_name(day),
            "form_key": uuid.uuid4().hex,
        }
    )
    return render(request, "booking/citas.html", ctx)


@router.post("/admin/citas/nueva")
async def crear_cita(
    tenant_id: Annotated[uuid.UUID, Form()],
    service_id: Annotated[uuid.UUID, Form()],
    fecha: Annotated[str, Form(max_length=10)],
    hora: Annotated[str, Form(max_length=5)],
    telefono: Annotated[str, Form(max_length=30)],
    nombre: Annotated[str, Form(max_length=120)] = "",
    clave: Annotated[str, Form(max_length=32)] = "",
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    tenant, _ = await _pick_tenant(session, tenant_id)
    assert tenant is not None  # noqa: S101 - _pick_tenant levanta si no existe
    back = f"/admin/citas?tenant_id={tenant.id}&dia={quote(fecha)}"
    e164, _kind = to_e164_co(telefono)
    if e164 is None:
        return _redirect(back, error="El teléfono no es válido")
    try:
        day = date.fromisoformat(fecha)
        starts_local = datetime.combine(
            day, time.fromisoformat(hora), availability.zone_for(tenant.timezone)
        )
    except ValueError:
        return _redirect(back, error="Fecha u hora inválida")
    if not _valid_date(day, tenant.timezone):
        return _redirect(back, error="La fecha está fuera del rango permitido")
    try:
        contact = await get_or_create_contact(session, tenant.id, e164, nombre.strip() or None)
        key = f"manual:{clave if _CLAVE_RE.fullmatch(clave) else uuid.uuid4().hex}"
        await SERVICE.book(
            session,
            tenant.id,
            contact_id=contact.id,
            service_id=service_id,
            starts_at=starts_local,
            idempotency_key=key,
            source="manual",
        )
    except AppError as exc:
        await session.rollback()
        return _redirect(back, error=exc.message)
    return _redirect(back, ok="Cita creada")


@router.post("/admin/citas/{appointment_id}/cancelar")
async def cancelar_cita(
    appointment_id: uuid.UUID,
    tenant_id: Annotated[uuid.UUID, Form()],
    motivo: Annotated[str, Form(max_length=300)] = "",
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    appt = (
        await session.execute(
            select(Appointment).where(
                Appointment.id == appointment_id, Appointment.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if appt is None:
        raise NotFoundError("Cita no encontrada")
    tenant_tz = (
        await session.execute(select(Tenant.timezone).where(Tenant.id == tenant_id))
    ).scalar_one_or_none() or "America/Bogota"
    local_day = ensure_utc(appt.starts_at).astimezone(availability.zone_for(tenant_tz)).date()
    back = f"/admin/citas?tenant_id={tenant_id}&dia={local_day.isoformat()}"
    try:
        await SERVICE.cancel(session, tenant_id, appointment_id, by="tenant", reason=motivo)
    except AppError as exc:
        await session.rollback()
        return _redirect(back, error=exc.message)
    return _redirect(back, ok="Cita cancelada")


# --------------------------------------------------------------------------- horarios
def _parse_hhmm(raw: str) -> time | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return time.fromisoformat(raw)
    except ValueError as exc:
        raise AppError("invalid_time", f"Hora inválida: {raw[:8]}", 422) from exc


@router.get("/admin/citas/horarios", response_class=HTMLResponse)
async def horarios_page(
    request: Request,
    tenant_id: uuid.UUID | None = Query(None),
    _user: User = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> Response:
    tenant, tenants = await _pick_tenant(session, tenant_id)
    ctx: dict[str, Any] = {"tenants": tenants, "tenant": tenant, "flash": _flash(request)}
    if tenant is not None:
        hours = (
            (
                await session.execute(
                    select(WorkingHours)
                    .where(WorkingHours.tenant_id == tenant.id, WorkingHours.resource_id.is_(None))
                    .order_by(WorkingHours.weekday, WorkingHours.start_time)
                )
            )
            .scalars()
            .all()
        )
        windows: dict[int, list[WorkingHours]] = {i: [] for i in range(7)}
        for h in hours:
            windows.setdefault(int(h.weekday), []).append(h)
        days = []
        for i in range(7):
            slots = windows[i][:MAX_WINDOWS_PER_DAY]
            slots += [None] * (MAX_WINDOWS_PER_DAY - len(slots))  # type: ignore[list-item]
            days.append({"index": i, "name": WEEKDAYS[i], "windows": slots})
        offs = (
            (
                await session.execute(
                    select(TimeOff)
                    .where(TimeOff.tenant_id == tenant.id, TimeOff.ends_at >= utcnow())
                    .order_by(TimeOff.starts_at)
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        for o in offs:  # ends_at es exclusivo (medianoche siguiente): mostrar el ultimo dia
            o.last_day = (ensure_utc(o.ends_at) - timedelta(seconds=1)).astimezone(  # type: ignore[attr-defined]
                availability.zone_for(tenant.timezone)
            )
        ctx.update({"days": days, "time_off": offs, "tz": availability.zone_for(tenant.timezone)})
    return render(request, "booking/horarios.html", ctx)


@router.post("/admin/citas/horarios")
async def guardar_horarios(
    request: Request,
    tenant_id: Annotated[uuid.UUID, Form()],
    _user: User = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    tenant, _ = await _pick_tenant(session, tenant_id)
    assert tenant is not None  # noqa: S101
    back = f"/admin/citas/horarios?tenant_id={tenant.id}"
    form = await request.form()
    new_rows: list[WorkingHours] = []
    try:
        for wd in range(7):
            for n in (1, 2):
                start = _parse_hhmm(str(form.get(f"d{wd}_ini{n}", "")))
                end = _parse_hhmm(str(form.get(f"d{wd}_fin{n}", "")))
                if start is None and end is None:
                    continue
                if start is None or end is None or end <= start:
                    raise AppError(
                        "invalid_window",
                        f"{WEEKDAYS[wd]}: la hora de cierre debe ser posterior a la de apertura",
                        422,
                    )
                new_rows.append(
                    WorkingHours(tenant_id=tenant.id, weekday=wd, start_time=start, end_time=end)
                )
        # La UI solo muestra MAX_WINDOWS_PER_DAY tramos por dia: los tramos extra ya
        # guardados (3.o en adelante) se conservan en vez de borrarse en silencio.
        stored = (
            (
                await session.execute(
                    select(WorkingHours)
                    .where(WorkingHours.tenant_id == tenant.id, WorkingHours.resource_id.is_(None))
                    .order_by(WorkingHours.weekday, WorkingHours.start_time)
                )
            )
            .scalars()
            .all()
        )
        per_day: dict[int, list[WorkingHours]] = {}
        for h in stored:
            per_day.setdefault(int(h.weekday), []).append(h)
        hidden = [h for rows in per_day.values() for h in rows[MAX_WINDOWS_PER_DAY:]]
        _check_no_overlap(
            new_rows
            + [
                WorkingHours(
                    tenant_id=tenant.id,
                    weekday=h.weekday,
                    start_time=h.start_time,
                    end_time=h.end_time,
                )
                for h in hidden
            ]
        )
    except AppError as exc:
        return _redirect(back, error=exc.message)
    hidden_ids = {h.id for h in hidden}
    await session.execute(
        delete(WorkingHours).where(
            WorkingHours.tenant_id == tenant.id,
            WorkingHours.resource_id.is_(None),
            WorkingHours.id.not_in(hidden_ids) if hidden_ids else True,
        )
    )
    session.add_all(new_rows)
    await session.flush()
    return _redirect(back, ok="Horario guardado")


def _check_no_overlap(rows: list[WorkingHours]) -> None:
    for wd in range(7):
        day = sorted((r.start_time, r.end_time) for r in rows if r.weekday == wd)
        for (_, prev_end), (nxt_start, _) in zip(day, day[1:], strict=False):
            if nxt_start < prev_end:
                raise AppError(
                    "overlapping_windows", f"{WEEKDAYS[wd]}: los tramos se traslapan", 422
                )


@router.post("/admin/citas/ausencias")
async def crear_ausencia(
    tenant_id: Annotated[uuid.UUID, Form()],
    desde: Annotated[str, Form(max_length=10)],
    hasta: Annotated[str, Form(max_length=10)],
    motivo: Annotated[str, Form(max_length=200)] = "",
    _user: User = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    tenant, _ = await _pick_tenant(session, tenant_id)
    assert tenant is not None  # noqa: S101
    back = f"/admin/citas/horarios?tenant_id={tenant.id}"
    try:
        d_from, d_to = date.fromisoformat(desde), date.fromisoformat(hasta)
    except ValueError:
        return _redirect(back, error="Fechas inválidas")
    if d_to < d_from:
        return _redirect(back, error="La fecha final no puede ser anterior a la inicial")
    if not (_valid_date(d_from, tenant.timezone) and _valid_date(d_to, tenant.timezone)):
        return _redirect(back, error="Las fechas están fuera del rango permitido")
    if (d_to - d_from).days > MAX_TIME_OFF_DAYS:
        return _redirect(back, error="La ausencia no puede superar un año")
    starts, _ = _day_bounds(d_from, tenant.timezone)
    _, ends = _day_bounds(d_to, tenant.timezone)
    session.add(
        TimeOff(tenant_id=tenant.id, starts_at=starts, ends_at=ends, reason=motivo.strip()[:200])
    )
    await session.flush()
    return _redirect(back, ok="Ausencia registrada")


@router.post("/admin/citas/ausencias/{off_id}/eliminar")
async def eliminar_ausencia(
    off_id: uuid.UUID,
    tenant_id: Annotated[uuid.UUID, Form()],
    _user: User = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    count = (
        await session.execute(
            select(func.count())
            .select_from(TimeOff)
            .where(TimeOff.id == off_id, TimeOff.tenant_id == tenant_id)
        )
    ).scalar_one()
    if not count:
        raise NotFoundError("Ausencia no encontrada")
    await session.execute(
        delete(TimeOff).where(TimeOff.id == off_id, TimeOff.tenant_id == tenant_id)
    )
    return _redirect(f"/admin/citas/horarios?tenant_id={tenant_id}", ok="Ausencia eliminada")


from app.booking.oauth import router as google_oauth_router  # noqa: E402

routers = [google_oauth_router]
