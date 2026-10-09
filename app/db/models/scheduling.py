"""Jobs programados por tenant y eventos de webhook (dedupe)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    JSONType,
    TenantMixin,
    TimestampMixin,
    UTCDateTime,
    enum_check,
    utcnow,
    uuid_pk,
)

SCHEDULED_KINDS = (
    "reminder_24h",
    "reminder_2h",
    "confirm_request",
    "followup_unbooked",
    "followup_noshow",
    "followup_post_visit",
    "hold_expire",
    "reminder",
    "followup",
)
SCHEDULED_STATUSES = ("pending", "running", "done", "failed", "cancelled")


class ScheduledJob(TimestampMixin, TenantMixin, Base):
    __tablename__ = "scheduled_jobs"
    __table_args__ = (
        enum_check("kind", SCHEDULED_KINDS),
        enum_check("status", SCHEDULED_STATUSES),
        UniqueConstraint("dedupe_key", name="uq_scheduled_jobs_dedupe_key"),
        Index("ix_scheduled_jobs_status_run_at", "status", "run_at"),
        Index("ix_scheduled_jobs_appointment_id", "appointment_id"),
        Index("ix_scheduled_jobs_contact_id", "contact_id"),
        Index("ix_scheduled_jobs_tenant_status_run", "tenant_id", "status", "run_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    run_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    payload: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "provider", "kind", "provider_sid", "status_value", name="uq_webhook_events_dedupe"
        ),
        enum_check("kind", ("inbound", "status")),
        enum_check("status", ("received", "processed", "ignored", "failed")),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="twilio")
    provider_sid: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status_value: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="received")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
