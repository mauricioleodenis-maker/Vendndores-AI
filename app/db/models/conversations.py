"""Conversaciones, mensajes y traspasos a humano."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Uuid,
)
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

CONVERSATION_STATUSES = ("open", "handoff", "closed")
MESSAGE_STATUSES = ("received", "queued", "sent", "delivered", "read", "failed", "undelivered")
HANDOFF_REASONS = (
    "user_request",
    "low_confidence",
    "out_of_scope_repeated",
    "medical_urgent",
    "complaint",
    "injection_suspected",
    "tool_failure",
)


class Conversation(TimestampMixin, TenantMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        enum_check("status", CONVERSATION_STATUSES),
        enum_check("channel", ("whatsapp", "voice", "sandbox", "web")),
        Index("ix_conversations_tenant_last", "tenant_id", "last_message_at"),
        Index("ix_conversations_tenant_contact", "tenant_id", "contact_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="RESTRICT"), nullable=True
    )
    channel_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("channel_accounts.id", ondelete="SET NULL"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="whatsapp")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary_upto_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    handoff_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    locale: Mapped[str] = mapped_column(String(10), nullable=False, default="es_CO")
    bot_paused_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Message(TimestampMixin, TenantMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        enum_check("direction", ("in", "out")),
        enum_check("role", ("user", "assistant", "human_agent", "system")),
        enum_check("status", MESSAGE_STATUSES),
        Index("ix_messages_tenant_conv_created", "tenant_id", "conversation_id", "created_at"),
        Index("ix_messages_purge_after", "purge_after"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    provider_sid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    body_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    body_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="received")
    error_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    template_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    window_exempt: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    llm_usage: Mapped[Any] = mapped_column(JSONType, nullable=True)
    guardrail_flags: Mapped[Any] = mapped_column(JSONType, nullable=True)
    purge_after: Mapped[date | None] = mapped_column(Date, nullable=True)


class Handoff(TimestampMixin, TenantMixin, Base):
    __tablename__ = "handoffs"
    __table_args__ = (
        enum_check("reason", HANDOFF_REASONS),
        enum_check("status", ("open", "claimed", "resolved")),
        Index("ix_handoffs_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    claimed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
