from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.plans.quote import pct_of, quote


@dataclass
class P:
    code: str = "pro"
    setup_fee_cop: int = 1_200_000
    monthly_fee_cop: int = 390_000


@dataclass
class O:  # noqa: E742
    code: str
    name: str
    discount_type: str
    value: int


def test_no_offer_no_iva() -> None:
    q = quote(P())
    assert q.total_first_invoice_cop == 1_590_000
    assert q.discounts == () and q.iva_cop == 0 and q.offer_code is None
    assert q.recurring_monthly_cop == 390_000


def test_iva_applies_to_net() -> None:
    q = quote(P(), include_iva=True)
    assert q.iva_cop == 302_100  # 19% de 1.590.000
    assert q.total_first_invoice_cop == 1_892_100


def test_founder_setup_free() -> None:
    q = quote(P(), O("fundador", "Fundador", "setup_pct", 100))
    assert q.setup_net_cop == 0
    assert q.discounts[0].amount_cop == 1_200_000
    assert q.total_first_invoice_cop == 390_000
    assert q.offer_code == "fundador"


def test_setup_pct_partial_with_iva() -> None:
    q = quote(P(), O("x", "X", "setup_pct", 50), include_iva=True)
    assert q.setup_net_cop == 600_000
    assert q.iva_cop == pct_of(990_000, 19)
    assert q.total_first_invoice_cop == 990_000 + q.iva_cop


def test_monthly_pct_applies_every_month() -> None:
    q = quote(P(), O("x", "X", "monthly_pct", 10))
    assert q.first_month_net_cop == q.recurring_monthly_cop == 351_000


def test_months_free() -> None:
    q = quote(P(), O("x", "X", "monthly_fixed_months_free", 2))
    assert q.first_month_net_cop == 0 and q.recurring_monthly_cop == 390_000
    assert q.free_months == 2 and q.total_first_invoice_cop == 1_200_000


def test_months_free_zero_is_noop() -> None:
    q = quote(P(), O("x", "X", "monthly_fixed_months_free", 0))
    assert q.discounts == () and q.total_first_invoice_cop == 1_590_000


def test_custom_monthly_fee() -> None:
    q = quote(P(), custom_monthly_fee_cop=300_000)
    assert q.monthly_cop == 300_000 and q.total_first_invoice_cop == 1_500_000


@pytest.mark.parametrize("kind,value", [("setup_pct", 101), ("monthly_pct", -1)])
def test_invalid_pct(kind: str, value: int) -> None:
    with pytest.raises(ValueError):
        quote(P(), O("x", "X", kind, value))


def test_unknown_type_and_negative_inputs() -> None:
    with pytest.raises(ValueError):
        quote(P(), O("x", "X", "raro", 1))
    with pytest.raises(ValueError):
        quote(P(), O("x", "X", "monthly_fixed_months_free", -1))
    with pytest.raises(ValueError):
        quote(P(setup_fee_cop=-1))


def test_pct_of_rounds_half_up() -> None:
    assert pct_of(1005, 50) == 503
    assert pct_of(0, 19) == 0
