"""Tenants (negocios clientes), perfil, secretos cifrados y canales."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, LargeBinary, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    JSONType,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
    UTCDateTime,
    enum_check,
    uuid_pk,
)

NICHES = ("dentista", "clinica_estetica", "taller", "restaurante", "otro")
TENANT_STATUSES = ("draft", "building", "review", "active", "paused", "offboarded")
SECRET_KINDS = (
    "twilio_auth_token",
    "twilio_sid",
    "google_oauth_refresh",
    "google_calendar_id",
    "custom_api",
)
CHANNELS = ("whatsapp", "voice")


class Tenant(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "tenants"
    __table_args__ = (
        enum_check("niche", NICHES),
        enum_check("status", TENANT_STATUSES),
        Index("ix_tenants_status", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    niche: Mapped[str] = mapped_column(String(30), nullable=False, default="otro")
    niche_template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("niche_templates.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    city: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    address: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    timezone: Mapped[str] = mapped_column(String(50), nullable=False, default="America/Bogota")
    website_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    instagram_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    phone_contact: Mapped[str | None] = mapped_column(String(30), nullable=True)
    owner_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dpa_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dpa_accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class TenantProfile(TimestampMixin, Base):
    __tablename__ = "tenant_profiles"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    hours: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    tone: Mapped[str] = mapped_column(Text, nullable=False, default="")
    languages: Mapped[Any] = mapped_column(JSONType, nullable=False, default=lambda: ["es"])
    handoff_phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    scraped_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")


class TenantSecret(TimestampMixin, TenantMixin, Base):
    """Secreto cifrado (AES-GCM). Solo ``last4`` se puede mostrar."""

    __tablename__ = "tenant_secrets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", name="uq_tenant_secrets_tenant_kind"),
        enum_check("kind", SECRET_KINDS),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[str] = mapped_column(String(20), nullable=False)
    last4: Mapped[str] = mapped_column(String(4), nullable=False, default="")
    rotated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ChannelAccount(TimestampMixin, TenantMixin, Base):
    __tablename__ = "channel_accounts"
    __table_args__ = (
        enum_check("channel", CHANNELS),
        enum_check(
            "whatsapp_sender_status", ("sandbox", "pending", "approved", "rejected"), nullable=True
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="whatsapp")
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="twilio")
    phone_e164: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    twilio_messaging_service_sid: Mapped[str | None] = mapped_column(String(40), nullable=True)
    twilio_subaccount_sid: Mapped[str | None] = mapped_column(String(40), nullable=True)
    whatsapp_sender_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default="sandbox"
    )
    voice_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    webhook_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
