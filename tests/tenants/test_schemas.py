from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.tenants.schemas import (
    ChannelIn,
    TenantIn,
    clean_phone,
    clean_url,
    error_map,
    hours_from_form,
    parse_ranges,
)


def test_clean_url_variants() -> None:
    assert clean_url("") is None
    assert clean_url("ejemplo.com") == "https://ejemplo.com"
    assert clean_url("@mi_negocio", instagram=True) == "https://www.instagram.com/mi_negocio"
    with pytest.raises(ValueError):
        clean_url("javascript:alert(1)")
    with pytest.raises(ValueError):
        clean_url("ftp://x.com")


def test_clean_phone() -> None:
    assert clean_phone("+57 300 123 4567") == "+573001234567"
    assert clean_phone(None) is None
    with pytest.raises(ValueError):
        clean_phone("abc")


def test_parse_ranges_ok_and_errors() -> None:
    assert parse_ranges("9:00-13:00, 14:00-18:00") == [["09:00", "13:00"], ["14:00", "18:00"]]
    assert parse_ranges("") == []
    for bad in ("25:00-26:00", "10:00-09:00", "foo", "09:00-13:00, 12:00-15:00"):
        with pytest.raises(ValueError):
            parse_ranges(bad)


def test_hours_from_form_skips_closed_days() -> None:
    assert hours_from_form({"mon": "09:00-18:00", "tue": ""}) == {"mon": [["09:00", "18:00"]]}


def test_tenant_in_validation_and_error_map() -> None:
    with pytest.raises(ValidationError) as exc:
        TenantIn(name="a", niche="nada", phone_contact="x")
    errs = error_map(exc.value)
    assert {"name", "niche", "phone_contact"} <= set(errs)


def test_channel_in_requires_e164() -> None:
    assert ChannelIn(phone_e164="+57 300 123 4567").phone_e164 == "+573001234567"
    with pytest.raises(ValidationError):
        ChannelIn(phone_e164="3001234567")
    with pytest.raises(ValidationError):
        ChannelIn(phone_e164="+573001234567", whatsapp_sender_status="x")
