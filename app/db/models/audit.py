"""Bitacora de auditoria append-only con cadena HMAC."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigIntPK, JSONType, UTCDateTime, enum_check, utcnow

ACTOR_TYPES = ("user", "system", "contact", "webhook")


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        enum_check("actor_type", ACTOR_TYPES),
        Index("ix_audit_log_ts", "ts"),
        Index("ix_audit_log_action", "action"),
        Index("ix_audit_log_tenant_id", "tenant_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    entity_id: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diff: Mapped[Any] = mapped_column(JSONType, nullable=True)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
