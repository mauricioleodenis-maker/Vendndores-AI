"""Filtros Jinja en espanol (Colombia)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from app.core.clock import to_bogota

_MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
_DIAS = ("lun", "mar", "mie", "jue", "vie", "sab", "dom")

STATUS_LABELS: dict[str, tuple[str, str]] = {
    # estado -> (texto en espanol, clase css)
    "draft": ("Borrador", "neutral"),
    "building": ("Generando", "info"),
    "generating": ("Generando", "info"),
    "review": ("En revision", "warn"),
    "needs_review": ("Falta confirmar", "warn"),
    "active": ("Activo", "ok"),
    "paused": ("Pausado", "neutral"),
    "offboarded": ("Dado de baja", "neutral"),
    "published": ("Publicado", "ok"),
    "archived": ("Archivado", "neutral"),
    "open": ("Abierta", "info"),
    "handoff": ("Con humano", "warn"),
    "closed": ("Cerrada", "neutral"),
    "pending": ("Pendiente", "warn"),
    "confirmed": ("Confirmada", "ok"),
    "cancelled": ("Cancelada", "neutral"),
    "no_show": ("No asistio", "danger"),
    "done": ("Realizada", "ok"),
    "trial": ("Prueba", "info"),
    "past_due": ("En mora", "danger"),
    "pendiente": ("Pendiente", "warn"),
    "pagado": ("Pagado", "ok"),
    "vencido": ("Vencido", "danger"),
    "anulado": ("Anulado", "neutral"),
    "queued": ("En cola", "neutral"),
    "sent": ("Enviado", "info"),
    "delivered": ("Entregado", "ok"),
    "read": ("Leido", "ok"),
    "failed": ("Fallido", "danger"),
    "replied": ("Respondio", "ok"),
    "running": ("En curso", "info"),
    "completed": ("Completada", "ok"),
    "nuevo": ("Nuevo", "info"),
    "prueba_secreta": ("Prueba secreta", "info"),
    "contactado": ("Contactado", "info"),
    "demo": ("Demo", "warn"),
    "cerrado": ("Cerrado", "ok"),
    "perdido": ("Perdido", "neutral"),
    "no_contactar": ("No contactar", "danger"),
}

NICHE_LABELS = {
    "dentista": "Dentista",
    "clinica_estetica": "Clinica estetica",
    "taller": "Taller",
    "restaurante": "Restaurante",
    "otro": "Otro",
}


def cop(value: int | float | None) -> str:
    """1200000 -> '$1.200.000'."""
    if value is None:
        return "-"
    return "$" + f"{int(round(value)):,}".replace(",", ".")


def _as_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


def fecha(value: Any) -> str:
    dt = _as_dt(value)
    if dt is None:
        return "-"
    if not isinstance(value, datetime):
        return f"{dt.day:02d} {_MESES[dt.month - 1]} {dt.year}"
    local = to_bogota(dt)
    return f"{local.day:02d} {_MESES[local.month - 1]} {local.year}"


def fecha_hora(value: Any) -> str:
    dt = _as_dt(value)
    if dt is None:
        return "-"
    local = to_bogota(dt)
    hour12 = local.hour % 12 or 12
    suffix = "a. m." if local.hour < 12 else "p. m."
    return (
        f"{_DIAS[local.weekday()]} {local.day:02d} {_MESES[local.month - 1]} "
        f"{local.year}, {hour12}:{local.minute:02d} {suffix}"
    )


def hace(value: Any, now: datetime | None = None) -> str:
    dt = _as_dt(value)
    if dt is None:
        return "-"
    delta = int(((now or datetime.now(UTC)) - dt).total_seconds())
    if delta < 60:
        return "hace un momento"
    if delta < 3600:
        return f"hace {delta // 60} min"
    if delta < 86400:
        return f"hace {delta // 3600} h"
    return f"hace {delta // 86400} d"


def tel_mask(value: str | None) -> str:
    if not value:
        return "-"
    digits = "".join(c for c in value if c.isdigit())
    return f"+{digits[:2]} ••• ••• {digits[-4:]}" if len(digits) >= 8 else "•••"


def estado_es(value: str | None) -> str:
    return STATUS_LABELS.get(value or "", (value or "-", "neutral"))[0]


def estado_clase(value: str | None) -> str:
    return STATUS_LABELS.get(value or "", ("", "neutral"))[1]


def nicho_es(value: str | None) -> str:
    return NICHE_LABELS.get(value or "", value or "-")


def porcentaje(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.{digits}f} %".replace(".", ",")


FILTERS = {
    "cop": cop,
    "fecha": fecha,
    "fecha_hora": fecha_hora,
    "hace": hace,
    "tel_mask": tel_mask,
    "estado_es": estado_es,
    "estado_clase": estado_clase,
    "nicho_es": nicho_es,
    "porcentaje": porcentaje,
}
