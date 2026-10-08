"""Lista de supresion (opt-out) consultada SIEMPRE antes de enviar."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, enum_check, uuid_pk


class SuppressionEntry(TimestampMixin, Base):
    """``suppression_list``. ``tenant_key`` es '' para global/agencia, o el id del tenant."""

    __tablename__ = "suppression_list"
    __table_args__ = (
        enum_check("reason", ("opt_out", "complaint", "manual", "bounced")),
        enum_check("scope", ("global", "tenant", "agency")),
        Index("uq_suppression_phone_scope", "phone_hash", "scope", "tenant_key", unique=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(20), nullable=False, default="opt_out")
    scope: Mapped[str] = mapped_column(String(10), nullable=False, default="global")
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    tenant_key: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    source_lead_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    suppressed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
