"""Contactos finales del tenant y sus consentimientos (Ley 1581)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UTCDateTime, enum_check, utcnow, uuid_pk


class Contact(TimestampMixin, TenantMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "phone_hash", name="uq_contacts_tenant_phone_hash"),
        enum_check("source", ("inbound", "import", "demo")),
        enum_check("opt_out_source", ("keyword", "manual", "complaint"), nullable=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    phone_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    display_name_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    opted_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    opted_out_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    opt_out_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    opt_out_keyword: Mapped[str | None] = mapped_column(String(60), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="inbound")
    erased_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Consent(TimestampMixin, TenantMixin, Base):
    __tablename__ = "consents"
    __table_args__ = (
        enum_check("purpose", ("atencion", "recordatorios", "marketing")),
        Index("ix_consents_tenant_contact", "tenant_id", "contact_id"),
        Index(
            "uq_consents_active",
            "tenant_id",
            "contact_id",
            "purpose",
            unique=True,
            sqlite_where=text("revoked_at IS NULL"),
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(20), nullable=False, default="1")
    granted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="whatsapp")
