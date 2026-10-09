"""Cotizador puro: sin DB ni reloj, solo enteros COP."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.plans.catalog import IVA_PCT

PCT_TYPES = ("setup_pct", "monthly_pct")


class PlanLike(Protocol):
    code: str
    setup_fee_cop: int
    monthly_fee_cop: int


class OfferLike(Protocol):
    code: str
    name: str
    discount_type: str
    value: int


@dataclass(frozen=True, slots=True)
class Discount:
    label: str
    amount_cop: int  # siempre positivo; se resta del total


@dataclass(frozen=True, slots=True)
class Quote:
    plan_code: str
    offer_code: str | None
    setup_cop: int  # precio de lista
    monthly_cop: int  # precio de lista (o personalizado)
    discounts: tuple[Discount, ...]
    setup_net_cop: int
    first_month_net_cop: int  # mensualidad de la primera factura
    recurring_monthly_cop: int  # mensualidad del segundo mes en adelante (sin IVA)
    free_months: int
    include_iva: bool
    iva_cop: int
    total_first_invoice_cop: int


def pct_of(amount: int, pct: int) -> int:
    """``pct`` % de ``amount`` redondeado al peso (mitad hacia arriba, enteros)."""
    return (amount * pct + 50) // 100


def quote(
    plan: PlanLike,
    offer: OfferLike | None = None,
    include_iva: bool = False,
    *,
    custom_monthly_fee_cop: int | None = None,
) -> Quote:
    """Cotiza la primera factura (setup + primera mensualidad).

    * ``setup_pct``: % de descuento sobre el setup.
    * ``monthly_pct``: % de descuento sobre la mensualidad (todos los meses).
    * ``monthly_fixed_months_free``: ``value`` meses gratis al inicio.
    """
    setup = plan.setup_fee_cop
    monthly = plan.monthly_fee_cop if custom_monthly_fee_cop is None else custom_monthly_fee_cop
    if setup < 0 or monthly < 0:
        raise ValueError("Los precios no pueden ser negativos")

    discounts: list[Discount] = []
    setup_net = setup
    first_month = monthly
    recurring = monthly
    free_months = 0

    if offer is not None:
        kind, value = offer.discount_type, offer.value
        if kind in PCT_TYPES and not 0 <= value <= 100:
            raise ValueError("El porcentaje de descuento debe estar entre 0 y 100")
        if kind == "setup_pct":
            amount = pct_of(setup, value)
            setup_net -= amount
            discounts.append(
                Discount(f"{offer.name}: {value}% de descuento en instalación", amount)
            )
        elif kind == "monthly_pct":
            amount = pct_of(monthly, value)
            first_month -= amount
            recurring -= amount
            discounts.append(Discount(f"{offer.name}: {value}% de descuento mensual", amount))
        elif kind == "monthly_fixed_months_free":
            if value < 0:
                raise ValueError("Los meses gratis no pueden ser negativos")
            free_months = value
            if value >= 1:
                first_month = 0
                discounts.append(
                    Discount(f"{offer.name}: {value} mes(es) gratis", monthly),
                )
        else:
            raise ValueError(f"Tipo de descuento desconocido: {kind}")

    net = setup_net + first_month
    iva = pct_of(net, IVA_PCT) if include_iva else 0
    return Quote(
        plan_code=plan.code,
        offer_code=offer.code if offer is not None else None,
        setup_cop=setup,
        monthly_cop=monthly,
        discounts=tuple(discounts),
        setup_net_cop=setup_net,
        first_month_net_cop=first_month,
        recurring_monthly_cop=recurring,
        free_months=free_months,
        include_iva=include_iva,
        iva_cop=iva,
        total_first_invoice_cop=net + iva,
    )
