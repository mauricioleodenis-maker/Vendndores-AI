"""Agenda: recursos, horarios, ausencias, citas y conexiones de calendario."""

from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, TenantMixin, TimestampMixin, UTCDateTime, enum_check, uuid_pk

APPOINTMENT_STATUSES = ("pending", "confirmed", "cancelled", "no_show", "done")
ACTIVE_APPOINTMENT_SQL = "status IN ('pending', 'confirmed')"


class Resource(TimestampMixin, TenantMixin, Base):
    __tablename__ = "resources"

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(60), nullable=False, default="default")
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="Principal")
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="staff")
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_resources_tenant_key"),)


class WorkingHours(TimestampMixin, TenantMixin, Base):
    __tablename__ = "working_hours"
    __table_args__ = (Index("ix_working_hours_tenant_weekday", "tenant_id", "weekday"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("resources.id", ondelete="CASCADE"), nullable=True
    )
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 0=lunes .. 6=domingo
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)


class TimeOff(TimestampMixin, TenantMixin, Base):
    __tablename__ = "time_off"
    __table_args__ = (Index("ix_time_off_tenant_starts", "tenant_id", "starts_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("resources.id", ondelete="CASCADE"), nullable=True
    )
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class Appointment(TimestampMixin, TenantMixin, Base):
    __tablename__ = "appointments"
    __table_args__ = (
        enum_check("status", APPOINTMENT_STATUSES),
        enum_check("source", ("bot", "manual")),
        enum_check("sync_status", ("local_only", "synced", "pending_push", "failed")),
        enum_check("cancelled_by", ("contact", "tenant", "system"), nullable=True),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_appointments_tenant_idem"),
        # Anti doble reserva portable; en Postgres la migracion agrega ademas EXCLUDE gist.
        Index(
            "uq_appointments_active_slot",
            "tenant_id",
            "resource_key",
            "starts_at",
            unique=True,
            sqlite_where=text(ACTIVE_APPOINTMENT_SQL),
            postgresql_where=text(ACTIVE_APPOINTMENT_SQL),
        ),
        Index("ix_appointments_tenant_starts", "tenant_id", "starts_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="RESTRICT"), nullable=True
    )
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("services.id", ondelete="RESTRICT"), nullable=True
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("resources.id", ondelete="RESTRICT"), nullable=True
    )
    resource_key: Mapped[str] = mapped_column(String(60), nullable=False, default="default")
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="bot")
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    hold_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    google_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    google_event_etag: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sync_status: Mapped[str] = mapped_column(String(20), nullable=False, default="local_only")
    cancelled_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    confirmed_by_contact_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reminder_24h_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reminder_2h_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    notes_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class CalendarConnection(TimestampMixin, TenantMixin, Base):
    __tablename__ = "calendar_connections"
    __table_args__ = (
        enum_check("provider", ("google", "local")),
        enum_check("status", ("active", "needs_reauth", "revoked", "error")),
        Index(
            "uq_calendar_connections_active",
            "tenant_id",
            unique=True,
            sqlite_where=text("status <> 'revoked'"),
            postgresql_where=text("status <> 'revoked'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="local")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    google_calendar_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    google_account_email_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    scopes: Mapped[Any] = mapped_column(JSONType, nullable=False, default=list)
    sync_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    watch_channel_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    watch_resource_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    watch_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
