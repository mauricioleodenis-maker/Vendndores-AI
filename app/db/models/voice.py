"""Sesiones de llamada de voz (una por ``CallSid`` de Twilio)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UTCDateTime, enum_check, uuid_pk

CALL_STATUSES = ("active", "transferred", "completed", "rejected")


class CallSession(TimestampMixin, TenantMixin, Base):
    __tablename__ = "call_sessions"
    __table_args__ = (
        UniqueConstraint("call_sid", name="uq_call_sessions_call_sid"),
        enum_check("status", CALL_STATUSES),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    call_sid: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
