"""Reloj inyectable (UTC) para facilitar pruebas."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

BOGOTA = ZoneInfo("America/Bogota")


def utcnow() -> datetime:
    """Hora actual en UTC con zona horaria (compatible con freezegun)."""
    return datetime.now(UTC)


def to_bogota(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(BOGOTA)


def ensure_utc(dt: datetime) -> datetime:
    """Normaliza datetimes naive (SQLite) a UTC aware."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
