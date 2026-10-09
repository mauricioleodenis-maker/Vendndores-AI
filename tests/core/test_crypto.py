import os

import pytest

from app.core.config import Settings
from app.core.crypto import (
    DecryptionError,
    EnvelopeCrypto,
    build_crypto,
    generate_master_key,
    make_aad,
    pack_blob,
    phone_hash,
    unpack_blob,
)

K1, K2 = os.urandom(32), os.urandom(32)


def test_roundtrip_and_pack():
    c = EnvelopeCrypto({"v1": K1}, "v1")
    aad = make_aad("messages", "t1", "body_enc")
    blob = c.encrypt(b"hola", aad=aad)
    assert blob.ciphertext != b"hola"
    assert c.decrypt(unpack_blob(pack_blob(blob)), aad=aad) == b"hola"
    assert c.decrypt_str(c.encrypt_str("ñandú", aad=aad), aad=aad) == "ñandú"


def test_wrong_aad_fails():
    c = EnvelopeCrypto({"v1": K1}, "v1")
    blob = c.encrypt(b"x", aad=b"a")
    with pytest.raises(DecryptionError):
        c.decrypt(blob, aad=b"b")


def test_nonce_unique():
    c = EnvelopeCrypto({"v1": K1}, "v1")
    assert c.encrypt(b"x", aad=b"a").nonce != c.encrypt(b"x", aad=b"a").nonce


def test_rotation():
    old = EnvelopeCrypto({"v1": K1}, "v1")
    blob = old.encrypt(b"secreto", aad=b"a")
    new = EnvelopeCrypto({"v2": K2, "v1": K1}, "v2")
    rotated = new.rotate(blob, aad=b"a")
    assert rotated.key_version == "v2"
    assert new.decrypt(rotated, aad=b"a") == b"secreto"
    assert new.rotate(rotated, aad=b"a") is rotated
    with pytest.raises(DecryptionError):
        EnvelopeCrypto({"v1": K1}, "v1").decrypt(rotated, aad=b"a")


def test_blind_index_deterministic_and_purpose_bound():
    c = EnvelopeCrypto({"v1": K1}, "v1")
    assert c.blind_index("+573001112233", purpose="phone") == c.blind_index(
        "+573001112233", purpose="phone"
    )
    assert c.blind_index("x", purpose="phone") != c.blind_index("x", purpose="email")
    assert EnvelopeCrypto({"v1": K2}, "v1").blind_index("x", purpose="phone") != c.blind_index(
        "x", purpose="phone"
    )


def test_invalid_keys():
    with pytest.raises(ValueError):
        EnvelopeCrypto({"v1": b"short"}, "v1")
    with pytest.raises(ValueError):
        EnvelopeCrypto({"v1": K1}, "v9")


def test_build_from_settings_and_dev_fallback():
    s = Settings(master_keys=f'{{"v2": "{generate_master_key()}"}}', _env_file=None)
    assert build_crypto(s).active_version == "v2"
    assert build_crypto(Settings(_env_file=None)).active_version == "dev"


def test_phone_hash_stable():
    assert phone_hash("+573001112233") == phone_hash(" +573001112233 ")
