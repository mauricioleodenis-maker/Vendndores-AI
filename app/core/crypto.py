"""Cifrado en reposo: AES-256-GCM con claves versionadas e indice ciego HMAC."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings, get_settings


class DecryptionError(Exception):
    """No se pudo descifrar (clave, AAD o datos incorrectos)."""


@dataclass(frozen=True, slots=True)
class EncryptedBlob:
    ciphertext: bytes
    nonce: bytes
    key_version: str


def make_aad(table: str, tenant_id: object, column: str) -> bytes:
    return f"{table}:{tenant_id}:{column}".encode()


class EnvelopeCrypto:
    def __init__(
        self, keys: dict[str, bytes], active: str, *, hmac_key: bytes | None = None
    ) -> None:
        if active not in keys:
            raise ValueError("La clave activa no esta en el llavero")
        for version, key in keys.items():
            if len(key) != 32:
                raise ValueError(f"La clave {version} debe tener 32 bytes")
        self._keys = dict(keys)
        self._active = active
        # Clave HMAC separada, derivada de la activa si no se entrega.
        self._hmac_key = hmac_key or hmac.new(keys[active], b"blind-index", hashlib.sha256).digest()

    @property
    def active_version(self) -> str:
        return self._active

    def encrypt(self, plaintext: bytes, *, aad: bytes) -> EncryptedBlob:
        nonce = os.urandom(12)
        ct = AESGCM(self._keys[self._active]).encrypt(nonce, plaintext, aad)
        return EncryptedBlob(ct, nonce, self._active)

    def decrypt(self, blob: EncryptedBlob, *, aad: bytes) -> bytes:
        key = self._keys.get(blob.key_version)
        if key is None:
            raise DecryptionError("Version de clave desconocida")
        try:
            return AESGCM(key).decrypt(blob.nonce, blob.ciphertext, aad)
        except InvalidTag as exc:
            raise DecryptionError("Fallo de autenticacion al descifrar") from exc

    def rotate(self, blob: EncryptedBlob, *, aad: bytes) -> EncryptedBlob:
        if blob.key_version == self._active:
            return blob
        return self.encrypt(self.decrypt(blob, aad=aad), aad=aad)

    def encrypt_str(self, text: str, *, aad: bytes) -> EncryptedBlob:
        return self.encrypt(text.encode(), aad=aad)

    def decrypt_str(self, blob: EncryptedBlob, *, aad: bytes) -> str:
        return self.decrypt(blob, aad=aad).decode()

    def blind_index(self, value: str, *, purpose: str) -> str:
        msg = f"{purpose}:{value}".encode()
        return hmac.new(self._hmac_key, msg, hashlib.sha256).hexdigest()


def _b64key(value: str) -> bytes:
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    return raw


def build_crypto(settings: Settings) -> EnvelopeCrypto:
    keys_b64 = settings.parsed_master_keys()
    if keys_b64:
        keys = {v: _b64key(k) for v, k in keys_b64.items()}
        active = next(iter(keys_b64))
    elif not settings.is_prod:
        # Clave de desarrollo derivada de secret_key (nunca en prod: el validador lo impide).
        seed = settings.secret_key.get_secret_value().encode()
        keys = {"dev": hashlib.sha256(b"vai-dev-master:" + seed).digest()}
        active = "dev"
    else:  # pragma: no cover - el validador de Settings ya lo impide
        raise ValueError("VAI_MASTER_KEYS requerido")
    phone_key = settings.phone_hash_key.get_secret_value().strip()
    hmac_key = phone_key.encode() if phone_key else None
    return EnvelopeCrypto(keys, active, hmac_key=hmac_key)


@lru_cache
def get_crypto() -> EnvelopeCrypto:
    return build_crypto(get_settings())


def generate_master_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def pack_blob(blob: EncryptedBlob) -> bytes:
    """Serializa a un solo ``bytes``: [len(version)][version][nonce 12][ciphertext]."""
    version = blob.key_version.encode()
    return bytes([len(version)]) + version + blob.nonce + blob.ciphertext


def unpack_blob(data: bytes) -> EncryptedBlob:
    n = data[0]
    version = data[1 : 1 + n].decode()
    nonce = data[1 + n : 13 + n]
    return EncryptedBlob(data[13 + n :], nonce, version)


def phone_hash(phone_e164: str) -> str:
    """Indice ciego unico de telefonos (contactos, leads, suppression_list) en TODA la app."""
    return get_crypto().blind_index(phone_e164.strip(), purpose="phone")
