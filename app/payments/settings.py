"""Configuracion de Wompi (env ``VAI_WOMPI_*``)."""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

SANDBOX_URL = "https://sandbox.wompi.co/v1"
PRODUCTION_URL = "https://production.wompi.co/v1"


class WompiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_WOMPI_", extra="ignore")

    public_key: str = ""
    private_key: SecretStr = SecretStr("")
    events_secret: SecretStr = SecretStr("")
    integrity_secret: SecretStr = SecretStr("")
    sandbox: bool = True
    base_url: str = ""
    timeout_s: float = 10.0

    @property
    def enabled(self) -> bool:
        return bool(self.public_key and self.private_key.get_secret_value())

    @property
    def effective_base_url(self) -> str:
        return self.base_url or (SANDBOX_URL if self.sandbox else PRODUCTION_URL)


def get_wompi_settings() -> WompiSettings:
    return WompiSettings()
