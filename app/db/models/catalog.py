"""Catalogo del tenant: servicios, FAQs y documentos de conocimiento."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UTCDateTime, enum_check, utcnow, uuid_pk


class Service(TimestampMixin, TenantMixin, Base):
    __tablename__ = "services"
    __table_args__ = (Index("ix_services_tenant_active", "tenant_id", "is_active"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    price_cop: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_note: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    requires_deposit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Faq(TimestampMixin, TenantMixin, Base):
    __tablename__ = "faqs"
    __table_args__ = (enum_check("source", ("template", "scrape", "manual")),)

    id: Mapped[uuid.UUID] = uuid_pk()
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class KbDocument(TimestampMixin, TenantMixin, Base):
    __tablename__ = "kb_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "content_hash", name="uq_kb_documents_tenant_hash"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
