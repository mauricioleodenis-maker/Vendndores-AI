"""Esquemas de entrada/salida y validacion de datos de empresas (Pydantic v2)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.db.models.tenants import NICHES, SECRET_KINDS

DAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABELS: dict[str, str] = {
    "mon": "Lunes",
    "tue": "Martes",
    "wed": "Miércoles",
    "thu": "Jueves",
    "fri": "Viernes",
    "sat": "Sábado",
    "sun": "Domingo",
}
WIZARD_NICHES: tuple[str, ...] = tuple(n for n in NICHES if n != "otro")
MAX_SERVICES = 60

_PHONE_RE = re.compile(r"^\+?\d{7,15}$")
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:(?!\d+(?:/|$))", re.I)
_HANDLE_RE = re.compile(r"^@?([A-Za-z0-9._]{1,30})$")

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


def clean_url(value: str | None, *, instagram: bool = False) -> str | None:
    """Solo http(s) con host; acepta ``@usuario`` para Instagram. Devuelve ``None`` si vacio."""
    raw = (value or "").strip()
    if not raw:
        return None
    if instagram and _HANDLE_RE.match(raw):
        return f"https://www.instagram.com/{raw.lstrip('@')}"
    if "://" not in raw:
        if _SCHEME_RE.match(raw):  # javascript:, data:, mailto: ...
            raise ValueError("URL no válida")
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.hostname or len(raw) > 500:
        raise ValueError("URL no válida")
    return raw


def clean_phone(value: str | None) -> str | None:
    raw = re.sub(r"[\s().-]", "", value or "")
    if not raw:
        return None
    if not _PHONE_RE.match(raw):
        raise ValueError("Teléfono no válido")
    return raw


def parse_ranges(text: str) -> list[list[str]]:
    """``"09:00-13:00, 14:00-18:00"`` -> ``[["09:00","13:00"],["14:00","18:00"]]``."""
    out: list[list[str]] = []
    for chunk in re.split(r"[,;]", text or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _RANGE_RE.match(chunk)
        if not m:
            raise ValueError(f"Horario no válido: {chunk[:20]}")
        h1, m1, h2, m2 = (int(g) for g in m.groups())
        if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
            raise ValueError(f"Horario no válido: {chunk[:20]}")
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if end <= start:
            raise ValueError("La hora de cierre debe ser posterior a la apertura")
        if out and start < int(out[-1][1][:2]) * 60 + int(out[-1][1][3:]):
            raise ValueError("Los rangos no pueden solaparse")
        out.append([f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"])
    return out


def hours_from_form(values: dict[str, str]) -> dict[str, list[list[str]]]:
    """Construye ``{"mon": [[...]], ...}`` solo con los dias abiertos."""
    return {d: r for d in DAY_KEYS if (r := parse_ranges(values.get(d, "")))}


def hours_to_text(hours: Any, day: str) -> str:
    ranges = (hours or {}).get(day) or []
    return ", ".join(f"{a}-{b}" for a, b in ranges)


class TenantIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: Annotated[str, Field(min_length=2, max_length=200)]
    niche: str
    city: Annotated[str, Field(max_length=100)] = ""
    address: Annotated[str, Field(max_length=300)] = ""
    phone_contact: str | None = None
    owner_name: Annotated[str, Field(max_length=200)] = ""
    website_url: str | None = None
    instagram_url: str | None = None

    @field_validator("niche")
    @classmethod
    def _niche(cls, v: str) -> str:
        if v not in NICHES:
            raise ValueError("Nicho no válido")
        return v

    @field_validator("phone_contact")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        return clean_phone(v)

    @field_validator("website_url")
    @classmethod
    def _web(cls, v: str | None) -> str | None:
        return clean_url(v)

    @field_validator("instagram_url")
    @classmethod
    def _ig(cls, v: str | None) -> str | None:
        return clean_url(v, instagram=True)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    niche: str
    status: str
    city: str
    address: str
    website_url: str | None
    instagram_url: str | None
    phone_contact: str | None
    owner_name: str
    created_at: datetime


class ServiceIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(max_length=2000)] = ""
    price_cop: Annotated[int, Field(ge=0, le=100_000_000)] | None = None
    price_note: Annotated[str, Field(max_length=200)] = ""
    duration_min: Annotated[int, Field(ge=5, le=600)] = 30
    requires_deposit: bool = False
    is_active: bool = True


class FaqIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    question: Annotated[str, Field(min_length=3, max_length=500)]
    answer: Annotated[str, Field(min_length=1, max_length=2000)]
    is_active: bool = True


class ChannelIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    phone_e164: str
    twilio_messaging_service_sid: Annotated[str, Field(max_length=40)] | None = None
    whatsapp_sender_status: str = "sandbox"
    voice_enabled: bool = False

    @field_validator("phone_e164")
    @classmethod
    def _e164(cls, v: str) -> str:
        v = re.sub(r"[\s().-]", "", v)
        if not _E164_RE.match(v):
            raise ValueError("Usa formato internacional, p. ej. +573001234567")
        return v

    @field_validator("whatsapp_sender_status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in {"sandbox", "pending", "approved", "rejected"}:
            raise ValueError("Estado no válido")
        return v

    @field_validator("twilio_messaging_service_sid")
    @classmethod
    def _sid(cls, v: str | None) -> str | None:
        return v or None


class SecretIn(BaseModel):
    kind: str
    value: Annotated[str, Field(min_length=1, max_length=4000)]

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in SECRET_KINDS:
            raise ValueError("Tipo de secreto no válido")
        return v


def error_map(exc: Any) -> dict[str, str]:
    """Convierte un ``ValidationError`` en ``{campo: mensaje}`` en espanol."""
    out: dict[str, str] = {}
    for err in exc.errors():
        field = str(err["loc"][0]) if err.get("loc") else "_"
        msg = str(err.get("msg", "Dato no válido"))
        if err.get("type") == "string_too_short":
            msg = "Es demasiado corto"
        elif err.get("type") == "string_too_long":
            msg = "Es demasiado largo"
        elif err.get("type") == "missing":
            msg = "Campo obligatorio"
        elif msg.startswith("Value error, "):
            msg = msg.removeprefix("Value error, ")
        out.setdefault(field, msg)
    return out


SECRET_LABELS: dict[str, str] = {
    "twilio_auth_token": "Twilio Auth Token",
    "twilio_sid": "Twilio Account SID",
    "google_oauth_refresh": "Google OAuth (refresh token)",
    "google_calendar_id": "Google Calendar ID",
    "custom_api": "API personalizada",
}
