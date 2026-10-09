"""Planes, ofertas, suscripciones, cobros y contadores de uso."""

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
    PrimaryKeyConstraint,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    JSONType,
    TenantMixin,
    TimestampMixin,
    UTCDateTime,
    enum_check,
    uuid_pk,
)

OFFER_DISCOUNT_TYPES = ("setup_pct", "monthly_pct", "monthly_fixed_months_free")
SUBSCRIPTION_STATUSES = ("trial", "active", "past_due", "cancelled")
GUARANTEE_STATUSES = ("vigente", "reclamada", "vencida")
BILLING_KINDS = ("setup", "mensualidad", "ajuste", "reembolso", "descuento")
BILLING_STATUSES = ("pendiente", "pagado", "vencido", "anulado")
PAYMENT_METHODS = ("transferencia", "nequi", "daviplata", "pse", "efectivo", "otro")


class Plan(TimestampMixin, Base):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    setup_fee_cop: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    monthly_fee_cop: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    limits: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    features: Mapped[Any] = mapped_column(JSONType, nullable=False, default=list)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Offer(TimestampMixin, Base):
    __tablename__ = "offers"
    __table_args__ = (enum_check("discount_type", OFFER_DISCOUNT_TYPES),)

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    discount_type: Mapped[str] = mapped_column(String(40), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_redemptions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redeemed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    applies_to_plan_codes: Mapped[Any] = mapped_column(JSONType, nullable=False, default=list)
    conditions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Subscription(TimestampMixin, TenantMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        enum_check("status", SUBSCRIPTION_STATUSES),
        enum_check("guarantee_status", GUARANTEE_STATUSES, nullable=True),
        Index("ix_subscriptions_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False
    )
    offer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("offers.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="trial")
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    custom_monthly_fee_cop: Mapped[int | None] = mapped_column(Integer, nullable=True)
    guarantee_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    guarantee_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


class BillingRecord(TimestampMixin, TenantMixin, Base):
    __tablename__ = "billing_records"
    __table_args__ = (
        enum_check("kind", BILLING_KINDS),
        enum_check("status", BILLING_STATUSES),
        enum_check("method", PAYMENT_METHODS, nullable=True),
        Index(
            "uq_billing_monthly",
            "subscription_id",
            "kind",
            "period",
            unique=True,
            sqlite_where=text("kind = 'mensualidad'"),
            postgresql_where=text("kind = 'mensualidad'"),
        ),
        Index("ix_billing_records_status_due", "status", "due_date"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    period: Mapped[str | None] = mapped_column(String(7), nullable=True)
    amount_cop: Mapped[int] = mapped_column(Integer, nullable=False)
    iva_cop: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pendiente")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reference: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    external_invoice_no: Mapped[str | None] = mapped_column(String(60), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class UsageCounter(TenantMixin, Base):
    __tablename__ = "usage_counters"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "period", name="pk_usage_counters"),)

    period: Mapped[str] = mapped_column(String(7), nullable=False)
    conversations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    templates_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
