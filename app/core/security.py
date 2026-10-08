"""Hash de contrasenas (argon2id), tokens y comparaciones en tiempo constante."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LEN = 12

_hasher = PasswordHasher()  # argon2id por defecto
# Hash ficticio para igualar tiempos cuando el usuario no existe.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing-equalization")


class WeakPasswordError(ValueError):
    pass


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise WeakPasswordError(f"La contrasena debe tener al menos {MIN_PASSWORD_LEN} caracteres")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise WeakPasswordError("La contrasena debe combinar letras y numeros")


def hash_password(password: str) -> str:
    validate_password_strength(password)
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Verifica en tiempo uniforme; si no hay hash gasta el mismo trabajo con uno ficticio."""
    if password_hash is None:
        try:
            _hasher.verify(_DUMMY_HASH, password)
        except (VerificationError, InvalidHashError):
            pass
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_csrf_token(expected: str | None, provided: str | None) -> bool:
    if not expected or not provided:
        return False
    return constant_time_equals(expected, provided)
