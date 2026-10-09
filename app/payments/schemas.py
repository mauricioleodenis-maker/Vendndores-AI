"""Tipos compartidos del contrato Wompi."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

TransactionStatus = Literal["APPROVED", "DECLINED", "VOIDED", "ERROR", "PENDING"]


class PaymentLink(BaseModel):
    id: str
    url: str


class Transaction(BaseModel):
    id: str
    reference: str
    status: TransactionStatus
    amount_in_cents: int
    payment_method_type: str | None = None
