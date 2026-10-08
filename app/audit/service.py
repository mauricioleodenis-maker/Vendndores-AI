"""``log_event``: bitacora append-only encadenada con HMAC-SHA256."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.core.config import get_settings
from app.db.models.audit import AuditLog
from app.db.models.users import User

_SENSITIVE = re.compile(
    r"pass|secret|token|key|cookie|auth|phone|tel|email|correo|body|mensaje|text|nombre|name|"
    r"address|direccion|cipher",
    re.I,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_ADVISORY_LOCK_ID = 7_340_001


def _chain_key() -> bytes:
    secret = get_settings().secret_key.get_secret_value().encode()
    return hmac.new(secret, b"audit-chain-v1", hashlib.sha256).digest()


def sanitize_diff(diff: Any) -> Any:
    """Quita valores sensibles: deja nombres de campos y valores no sensibles cortos."""
    if isinstance(diff, dict):
        out: dict[str, Any] = {}
        for k, v in diff.items():
            if _SENSITIVE.search(str(k)) and not isinstance(v, list | dict):
                out[str(k)] = "[redacted]"
            else:
                out[str(k)] = sanitize_diff(v)
        return out
    if isinstance(diff, list | tuple):
        return [sanitize_diff(v) for v in diff]
    if isinstance(diff, str):
        return _PHONE.sub("[tel]", _EMAIL.sub("[email]", diff))[:300]
    return diff


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True)


def compute_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hmac.new(
        _chain_key(), (prev_hash + _canonical(payload)).encode(), hashlib.sha256
    ).hexdigest()


def _payload(entry: AuditLog) -> dict[str, Any]:
    return {
        "ts": entry.ts.isoformat(),
        "actor_user_id": str(entry.actor_user_id) if entry.actor_user_id else None,
        "actor_type": entry.actor_type,
        "tenant_id": str(entry.tenant_id) if entry.tenant_id else None,
        "action": entry.action,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "ip": entry.ip,
        "request_id": entry.request_id,
        "diff": entry.diff,
    }


async def log_event(
    session: AsyncSession,
    *,
    actor: User | uuid.UUID | str | None,
    action: str,
    entity_type: str = "",
    entity_id: object = "",
    tenant_id: uuid.UUID | None = None,
    diff: dict[str, Any] | None = None,
    ip: str | None = None,
    request_id: str | None = None,
    actor_type: str | None = None,
) -> AuditLog:
    """Registra un evento en la MISMA transaccion del llamador.

    ``actor``: ``User``/``UUID`` (usuario), ``"system"``/``"webhook"``/``"contact"`` o ``None``.
    """
    actor_user_id: uuid.UUID | None = None
    resolved_type = actor_type
    if isinstance(actor, User):
        actor_user_id = actor.id
        resolved_type = resolved_type or "user"
    elif isinstance(actor, uuid.UUID):
        actor_user_id = actor
        resolved_type = resolved_type or "user"
    elif isinstance(actor, str):
        resolved_type = resolved_type or actor
    resolved_type = resolved_type or "system"

    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _ADVISORY_LOCK_ID})
    # autoflush esta apagado: volcar pendientes para ver la cola real de la cadena
    await session.flush()
    last = (
        await session.execute(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1))
    ).scalar_one_or_none()

    entry = AuditLog(
        ts=utcnow(),
        actor_user_id=actor_user_id,
        actor_type=resolved_type,
        tenant_id=tenant_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        ip=ip,
        request_id=request_id,
        diff=sanitize_diff(diff) if diff is not None else None,
        prev_hash=last or "",
    )
    entry.hash = compute_hash(entry.prev_hash, _payload(entry))
    session.add(entry)
    await session.flush()
    return entry


async def verify_chain(session: AsyncSession, *, limit: int | None = None) -> tuple[bool, int | None]:
    """Recalcula la cadena. Devuelve ``(ok, id_primer_registro_alterado)``."""
    stmt = select(AuditLog).order_by(AuditLog.id)
    if limit:
        stmt = stmt.limit(limit)
    prev = ""
    first = True
    for entry in (await session.execute(stmt)).scalars():
        if first and limit is None and entry.prev_hash != "":
            return False, entry.id
        if not first and entry.prev_hash != prev:
            return False, entry.id
        if entry.hash != compute_hash(entry.prev_hash, _payload(entry)):
            return False, entry.id
        prev = entry.hash
        first = False
    return True, None


async def list_events(
    session: AsyncSession,
    *,
    action: str | None = None,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    if tenant_id:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
    if actor_user_id:
        stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
    if since:
        stmt = stmt.where(AuditLog.ts >= since)
    if until:
        stmt = stmt.where(AuditLog.ts < until)
    return list((await session.execute(stmt.limit(limit).offset(offset))).scalars().all())
