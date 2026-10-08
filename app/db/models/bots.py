"""Configuraciones de bot versionadas y consumo de LLM."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, text
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

BOT_STATUSES = ("draft", "published", "archived")


class BotConfig(TimestampMixin, TenantMixin, Base):
    __tablename__ = "bot_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_bot_configs_tenant_version"),
        enum_check("status", BOT_STATUSES),
        enum_check("generated_by", ("factory", "manual")),
        Index(
            "uq_bot_configs_one_published",
            "tenant_id",
            unique=True,
            sqlite_where=text("status = 'published'"),
            postgresql_where=text("status = 'published'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    booking_rules: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    guardrails: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    templates: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    config: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    generation_meta: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    source_inputs: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    model: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    generated_by: Mapped[str] = mapped_column(String(20), nullable=False, default="factory")
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class LlmUsage(TenantMixin, Base):
    """Consumo de LLM por llamada (alimenta limites por plan y costos)."""

    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    purpose: Mapped[str] = mapped_column(String(30), nullable=False, default="conversation")
    model: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
