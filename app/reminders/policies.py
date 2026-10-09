"""Politicas de recordatorios: offsets, horario de silencio (08-20) y formato es-CO."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.clock import BOGOTA, ensure_utc

QUIET_START = time(8, 0)  # no se envia antes
QUIET_END = time(20, 0)  # ni a partir de esta hora

REMINDER_24H = "reminder_24h"
REMINDER_2H = "reminder_2h"
FOLLOWUP_NOSHOW = "followup_noshow"
FOLLOWUP_UNBOOKED = "followup_unbooked"

OFFSETS: dict[str, timedelta] = {
    REMINDER_24H: timedelta(hours=24),
    REMINDER_2H: timedelta(hours=2),
}
NOSHOW_DELAY = timedelta(hours=2)
UNBOOKED_DELAYS: tuple[timedelta, ...] = (timedelta(hours=24), timedelta(days=3))
MAX_UNBOOKED_FOLLOWUPS = 2
MAX_ATTEMPTS = 3
RETRY_BACKOFF = (timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2))
STALE_LOCK = timedelta(minutes=10)

# Plantillas aprobadas (support/07) por tipo de job.
TEMPLATE_NAMES: dict[str, str] = {
    REMINDER_24H: "cita_recordatorio_24h",
    REMINDER_2H: "cita_recordatorio_2h",
    FOLLOWUP_NOSHOW: "cita_no_show",
}
# Clave en ``BotConfig.templates`` que el tenant puede personalizar.
BOT_TEMPLATE_KEYS: dict[str, str] = {
    REMINDER_24H: "recordatorio_24h",
    REMINDER_2H: "recordatorio_2h",
    FOLLOWUP_NOSHOW: "seguimiento_no_show",
    FOLLOWUP_UNBOOKED: "seguimiento_no_agendo",
}
DEFAULT_TEXTS: dict[str, str] = {
    REMINDER_24H: (
        "Hola {nombre}, te recordamos tu cita de mañana, {fecha}, a las {hora} en {negocio}. "
        "Responde 1 para confirmar o 2 para reprogramar."
    ),
    REMINDER_2H: (
        "Hola {nombre}, en 2 horas tienes tu cita en {negocio} a las {hora}. "
        "Si no puedes asistir, responde 2 para reprogramar."
    ),
    FOLLOWUP_NOSHOW: (
        "Hola {nombre}, no alcanzamos a verte en tu cita de {negocio} el {fecha}. "
        "Si quieres agendar un nuevo horario, responde a este mensaje."
    ),
    FOLLOWUP_UNBOOKED: (
        "Hola {nombre}, soy el asistente de {negocio}. ¿Te ayudo a agendar tu cita? "
        "Responde a este mensaje cuando quieras."
    ),
}
# Variables posicionales de cada plantilla de Twilio Content.
TEMPLATE_VARS: dict[str, tuple[str, ...]] = {
    REMINDER_24H: ("nombre", "fecha", "hora", "negocio"),
    REMINDER_2H: ("nombre", "negocio", "hora"),
    FOLLOWUP_NOSHOW: ("nombre", "negocio", "fecha"),
}

_DAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MONTHS = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)  # fmt: skip


def tz_of(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name) if name else BOGOTA
    except Exception:  # noqa: BLE001 - zona invalida => zona del negocio por defecto
        return BOGOTA


def in_quiet_hours(now: datetime, tz: ZoneInfo = BOGOTA) -> bool:
    local = ensure_utc(now).astimezone(tz).time()
    return not (QUIET_START <= local < QUIET_END)


def next_allowed(now: datetime, tz: ZoneInfo = BOGOTA) -> datetime:
    """Primer instante (UTC) dentro de la ventana de envio, >= ``now``."""
    local = ensure_utc(now).astimezone(tz)
    if not in_quiet_hours(now, tz):
        return ensure_utc(now)
    day = local.date() if local.time() < QUIET_START else local.date() + timedelta(days=1)
    start = datetime.combine(day, QUIET_START, tzinfo=tz)
    return start.astimezone(ensure_utc(now).tzinfo)


def format_date_es(dt: datetime, tz: ZoneInfo = BOGOTA) -> str:
    local = ensure_utc(dt).astimezone(tz)
    return f"{_DAYS[local.weekday()]} {local.day} de {_MONTHS[local.month - 1]}"


def format_time_es(dt: datetime, tz: ZoneInfo = BOGOTA) -> str:
    local = ensure_utc(dt).astimezone(tz)
    hour12 = local.hour % 12 or 12
    return f"{hour12}:{local.minute:02d} {'a. m.' if local.hour < 12 else 'p. m.'}"


def render_text(template: str, values: dict[str, str]) -> str:
    """Sustituye ``{clave}``/``{{clave}}`` sin ``str.format`` (plantillas no confiables)."""
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", value).replace("{" + key + "}", value)
    return out
