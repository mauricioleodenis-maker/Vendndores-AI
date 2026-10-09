import pytest

from app.core.security import (
    WeakPasswordError,
    constant_time_equals,
    hash_password,
    hash_token,
    new_token,
    password_needs_rehash,
    validate_password_strength,
    verify_csrf_token,
    verify_password,
)


def test_hash_is_argon2id_and_verifies():
    h = hash_password("Una-clave-segura-123")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "Una-clave-segura-123")
    assert not verify_password(h, "otra-clave-segura-1")
    assert not password_needs_rehash(h)


def test_unknown_user_does_dummy_work_and_fails():
    assert verify_password(None, "lo-que-sea-12345") is False


def test_invalid_hash_is_false():
    assert verify_password("no-es-un-hash", "x") is False


@pytest.mark.parametrize("pw", ["corta1", "sololetrasqueson12", "123456789012345"])
def test_weak_passwords(pw):
    if pw == "sololetrasqueson12":
        pw = "sololetrasquesonmuchas"
    with pytest.raises(WeakPasswordError):
        validate_password_strength(pw)


def test_tokens():
    a, b = new_token(), new_token()
    assert a != b and len(a) >= 43
    assert hash_token(a) == hash_token(a) and len(hash_token(a)) == 64
    assert constant_time_equals("x", "x") and not constant_time_equals("x", "y")


def test_csrf_compare():
    assert verify_csrf_token("abc", "abc")
    assert not verify_csrf_token("abc", "abd")
    assert not verify_csrf_token("abc", None)
    assert not verify_csrf_token(None, "abc")
