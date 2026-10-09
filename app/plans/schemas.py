"""Esquemas Pydantic de entrada/salida de planes y suscripciones."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    description: str
    setup_fee_cop: int
    monthly_fee_cop: int
    features: list[str]
    limits: dict[str, Any]


class OfferOut(BaseModel):
    code: str
    name: str
    discount_type: str
    value: int
    max_redemptions: int | None
    redeemed: int
    remaining: int | None
    valid_until: date | None
    applies_to_plan_codes: list[str]
    conditions: str
    is_active: bool


class QuoteIn(BaseModel):
    plan_code: str = Field(min_length=1, max_length=40)
    offer_code: str | None = Field(default=None, max_length=40)
    include_iva: bool = False
    custom_monthly_fee_cop: int | None = Field(default=None, ge=0, le=100_000_000)


class DiscountOut(BaseModel):
    label: str
    amount_cop: int


class QuoteOut(BaseModel):
    plan_code: str
    offer_code: str | None
    setup_cop: int
    monthly_cop: int
    discounts: list[DiscountOut]
    setup_net_cop: int
    first_month_net_cop: int
    recurring_monthly_cop: int
    free_months: int
    include_iva: bool
    iva_cop: int
    total_first_invoice_cop: int


class SubscriptionIn(BaseModel):
    plan_code: str = Field(min_length=1, max_length=40)
    offer_code: str | None = Field(default=None, max_length=40)
    custom_monthly_fee_cop: int | None = Field(default=None, ge=0, le=100_000_000)
    include_iva: bool = False


class SubscriptionPatch(BaseModel):
    plan_code: str | None = Field(default=None, min_length=1, max_length=40)
    custom_monthly_fee_cop: int | None = Field(default=None, ge=0, le=100_000_000)
    status: Literal["active", "past_due", "cancelled"] | None = None


class SubscriptionOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    plan_code: str
    status: str
    offer_code: str | None
    started_at: datetime | None
    current_period_end: datetime | None
    custom_monthly_fee_cop: int | None
    guarantee_until: date | None
    guarantee_status: str | None
    quote: QuoteOut | None = None


class EntitlementsOut(BaseModel):
    plan_code: str
    status: str
    active: bool
    has_subscription: bool
    period: str
    limits: dict[str, Any]
    usage: dict[str, int]
    conversations_pct: int | None
    near_conversation_limit: bool
