import pytest

from app.leads.normalize import (
    instagram_handle,
    name_key,
    normalize_website,
    phone_type,
    to_e164_co,
    website_domain,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("300 123 4567", ("+573001234567", "mobile")),
        ("+57 (310) 555-1234", ("+573105551234", "mobile")),
        ("573001234567", ("+573001234567", "mobile")),
        ("0057 3001234567", ("+573001234567", "mobile")),
        ("602 555 1234", ("+576025551234", "landline")),
        ("555 1234", ("+576025551234", "landline")),
        ("", (None, "unknown")),
        (None, (None, "unknown")),
        ("abc", (None, "unknown")),
        ("123", (None, "unknown")),
        ("1" * 20, (None, "unknown")),
    ],
)
def test_to_e164_co(raw, expected):
    assert to_e164_co(raw) == expected


def test_phone_type():
    assert phone_type("3001234567") == "mobile"
    assert phone_type("x") == "unknown"


def test_name_key():
    assert name_key("Clínica Dental  Sonrisas S.A.S.") == "clinica dental sonrisas"
    assert name_key("Taller & Pintura LTDA") == "taller y pintura"
    assert name_key(None) == ""


def test_website_domain():
    assert website_domain("https://www.Sonrisas.com.co/home?utm_source=x") == "sonrisas.com.co"
    assert website_domain("sonrisas.co") == "sonrisas.co"
    assert website_domain("https://instagram.com/foo") is None
    assert website_domain("https://wa.me/573001234567") is None
    assert website_domain("javascript:alert(1)") is None
    assert website_domain("nodot") is None
    assert website_domain(None) is None


def test_normalize_website_strips_tracking():
    out = normalize_website("http://Foo.com/a/?utm_x=1&fbclid=2&q=3#frag")
    assert out == "https://foo.com/a?q=3"


def test_instagram_handle():
    assert instagram_handle("https://www.instagram.com/Dental.Cali/?hl=es") == "dental.cali"
    assert instagram_handle("@dental") == "dental"
    assert instagram_handle("https://instagram.com/p/abc") is None
    assert instagram_handle("no es handle!!") is None
    assert instagram_handle("") is None
