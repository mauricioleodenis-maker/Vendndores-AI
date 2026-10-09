import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


def test_defaults_are_safe_for_outreach():
    s = Settings(_env_file=None)
    assert s.twilio_dry_run is True
    assert s.outreach_enabled is False
    assert s.anthropic_model == "claude-sonnet-5-5"


def test_prod_requires_strong_config():
    with pytest.raises(ValidationError) as exc:
        Settings(env="prod", secret_key="weak", master_keys="", cookie_secure=True, _env_file=None)
    msg = str(exc.value)
    assert "VAI_SECRET_KEY" in msg and "VAI_MASTER_KEYS" in msg


def test_prod_rejects_insecure_cookie():
    with pytest.raises(ValidationError):
        Settings(
            env="prod",
            secret_key="x" * 40,
            master_keys='{"v1": "abc"}',
            phone_hash_key="k",
            cookie_secure=False,
            _env_file=None,
        )


def test_prod_valid():
    s = Settings(
        env="prod",
        secret_key="x" * 40,
        master_keys='{"v1": "abc"}',
        phone_hash_key="k",
        cookie_secure=True,
        _env_file=None,
    )
    assert s.is_prod and s.parsed_master_keys() == {"v1": "abc"}


def test_secrets_not_in_repr():
    s = Settings(anthropic_api_key="sk-ant-secret-value", _env_file=None)
    assert "sk-ant-secret-value" not in repr(s)


def test_bad_master_keys_json():
    with pytest.raises(ValueError):
        Settings(master_keys="[]", _env_file=None).parsed_master_keys()


def test_get_settings_cached():
    assert get_settings() is get_settings()
