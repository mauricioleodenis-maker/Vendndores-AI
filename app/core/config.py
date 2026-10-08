"""Configuracion central (variables de entorno con prefijo ``VAI_``)."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "dev-only-secret-key-change-me-0123456789abcdef"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="VAI_", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    debug: bool = False

    # Base de datos / colas
    database_url: str = "sqlite+aiosqlite:///./vendedores.db"
    redis_url: str | None = None

    # Seguridad
    secret_key: SecretStr = SecretStr(_DEV_SECRET)
    master_keys: SecretStr = SecretStr("")  # JSON {"v2": "<b64 32B>", "v1": "..."}; la primera es la activa
    phone_hash_key: SecretStr = SecretStr("")
    session_ttl_min: int = 480
    session_absolute_ttl_days: int = 7
    cookie_secure: bool = True
    allowed_hosts: list[str] = Field(default_factory=lambda: ["*"])
    login_max_attempts: int = 5
    login_window_min: int = 15
    lockout_min: int = 15
    public_base_url: str = "http://localhost:8000"

    # Regionalizacion
    default_tz: str = "America/Bogota"
    msg_retention_days: int = 90

    # Anthropic
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-sonnet-5-5"

    # Twilio (cuenta de la plataforma/agencia)
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_whatsapp_from: str = ""
    twilio_messaging_service_sid: str = ""
    twilio_dry_run: bool = True
    outreach_enabled: bool = False

    # Google
    google_places_api_key: SecretStr = SecretStr("")
    google_places_budget_usd_month: float = 50.0
    google_oauth_client_id: str = ""
    google_oauth_client_secret: SecretStr = SecretStr("")

    # Demo
    demo_enabled: bool = True
    demo_token_ttl_days: int = 7
    demo_max_messages: int = 30
    demo_whatsapp_number: str = ""

    @model_validator(mode="after")
    def _validate_prod(self) -> Settings:
        if self.env != "prod":
            return self
        problems: list[str] = []
        secret = self.secret_key.get_secret_value()
        if len(secret) < 32 or secret == _DEV_SECRET:
            problems.append("VAI_SECRET_KEY debil (min 32 caracteres, no el valor por defecto)")
        if not self.cookie_secure:
            problems.append("VAI_COOKIE_SECURE debe ser true en prod")
        if not self.master_keys.get_secret_value().strip():
            problems.append("VAI_MASTER_KEYS requerido en prod")
        if not self.phone_hash_key.get_secret_value().strip():
            problems.append("VAI_PHONE_HASH_KEY requerido en prod")
        if self.debug:
            problems.append("VAI_DEBUG debe ser false en prod")
        if problems:
            raise ValueError("Configuracion de produccion invalida: " + "; ".join(problems))
        return self

    def parsed_master_keys(self) -> dict[str, str]:
        raw = self.master_keys.get_secret_value().strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict) or not data:
            raise ValueError("VAI_MASTER_KEYS debe ser un objeto JSON no vacio")
        return {str(k): str(v) for k, v in data.items()}

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
