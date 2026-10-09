"""Intentos de pago con pasarela (Wompi)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, TimestampMixin, enum_check, uuid_pk

PAYMENT_INTENT_STATUSES = ("pending", "approved", "declined", "voided", "error")


class PaymentIntent(TimestampMixin, Base):
    __tablename__ = "payment_intents"
    __table_args__ = (
        enum_check("status", PAYMENT_INTENT_STATUSES),
        Index("ix_payment_intents_billing_record", "billing_record_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    billing_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("billing_records.id", ondelete="RESTRICT"), nullable=False
    )
    reference: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="wompi")
    amount_cop: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    link_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_tx_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw_last_event: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
