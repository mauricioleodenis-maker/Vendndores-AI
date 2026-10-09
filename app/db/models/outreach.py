"""Plantillas de mensaje, campanas y mensajes de outreach."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, TimestampMixin, UTCDateTime, enum_check, uuid_pk

CAMPAIGN_STATUSES = ("draft", "ready", "running", "paused", "completed", "cancelled")
OUTREACH_STATUSES = ("queued", "sent", "delivered", "read", "failed", "replied", "skipped")


class MessageTemplate(TimestampMixin, Base):
    __tablename__ = "message_templates"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_message_templates_name_version"),
        enum_check("scope", ("outreach", "tenant")),
        enum_check(
            "kind",
            ("pitch", "seguimiento", "optout_confirm", "prueba_secreta_hint", "general"),
        ),
        enum_check("category", ("utility", "marketing", "authentication"), nullable=True),
        enum_check("approval_status", ("draft", "submitted", "approved", "rejected")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    scope: Mapped[str] = mapped_column(String(10), nullable=False, default="outreach")
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, default="general")
    category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    niche: Mapped[str | None] = mapped_column(String(30), nullable=True)
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="es_CO")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[Any] = mapped_column(JSONType, nullable=False, default=list)
    twilio_content_sid: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approval_status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        enum_check("status", CAMPAIGN_STATUSES),
        enum_check(
            "stage_target", ("nuevo", "prueba_secreta", "contactado", "demo"), nullable=True
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    niche: Mapped[str | None] = mapped_column(String(30), nullable=True)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("message_templates.id", ondelete="RESTRICT"), nullable=True
    )
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    send_window: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    audience_filter: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    stage_target: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    paused_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    stats: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class CampaignTarget(TimestampMixin, Base):
    __tablename__ = "campaign_targets"
    __table_args__ = (
        UniqueConstraint("campaign_id", "lead_id", name="uq_campaign_targets_campaign_lead"),
        Index("ix_campaign_targets_lead_id", "lead_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")


class OutreachMessage(TimestampMixin, Base):
    __tablename__ = "outreach_messages"
    __table_args__ = (
        enum_check("status", OUTREACH_STATUSES),
        UniqueConstraint("idempotency_key", name="uq_outreach_messages_idem"),
        Index("ix_outreach_messages_provider_sid", "provider_sid"),
        Index("ix_outreach_messages_campaign_status", "campaign_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="whatsapp")
    template_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    step: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    content_variables: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    provider_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    delivery_error_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
