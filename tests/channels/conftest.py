from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.sender import sign_twilio
from app.core.config import get_settings
from app.core.crypto import get_crypto, make_aad
from app.db.models.tenants import ChannelAccount, Tenant, TenantSecret

BIZ_NUMBER = "+576015550100"
CUSTOMER = "+573001234567"
PLATFORM_TOKEN = "platform-token-xyz"
TENANT_TOKEN = "tenant-token-abc"
WA_URL = "http://test/webhooks/twilio/whatsapp"


@pytest.fixture(autouse=True)
def _twilio_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAI_TWILIO_AUTH_TOKEN", PLATFORM_TOKEN)
    monkeypatch.setenv("VAI_TWILIO_ACCOUNT_SID", "ACplatform")
    monkeypatch.setenv("VAI_TWILIO_WHATSAPP_FROM", "+15550001111")
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def channel(session: AsyncSession, tenant: Tenant) -> ChannelAccount:
    acc = ChannelAccount(tenant_id=tenant.id, phone_e164=BIZ_NUMBER)
    session.add(acc)
    await session.commit()
    return acc


@pytest_asyncio.fixture
async def add_tenant_token(session: AsyncSession, tenant: Tenant) -> Callable[..., Any]:
    async def _add(kind: str, value: str) -> None:
        blob = get_crypto().encrypt_str(value, aad=make_aad("tenant_secrets", tenant.id, kind))
        session.add(
            TenantSecret(
                tenant_id=tenant.id,
                kind=kind,
                ciphertext=blob.ciphertext,
                nonce=blob.nonce,
                key_version=blob.key_version,
                last4=value[-4:],
            )
        )
        await session.commit()

    return _add


def inbound_params(sid: str | None = None, body: str = "Hola", **extra: str) -> dict[str, str]:
    return {
        "MessageSid": sid or f"SM{uuid.uuid4().hex}",
        "From": f"whatsapp:{CUSTOMER}",
        "To": f"whatsapp:{BIZ_NUMBER}",
        "Body": body,
        "NumMedia": "0",
        "ProfileName": "Maria",
        **extra,
    }


def signed(
    params: dict[str, str], token: str = PLATFORM_TOKEN, url: str = WA_URL
) -> dict[str, str]:
    return {"X-Twilio-Signature": sign_twilio(url, params, token)}


@pytest.fixture
def _engine_llm(monkeypatch: pytest.MonkeyPatch, fake_llm: Any) -> None:
    """Los jobs no pasan por dependency_overrides: el motor toma el FakeLLM."""
    import app.conversation.engine as engine_mod

    monkeypatch.setattr(engine_mod, "get_llm", lambda: fake_llm)


@pytest_asyncio.fixture
async def tid(session: AsyncSession, tenant: Tenant, channel: ChannelAccount) -> uuid.UUID:
    """Id del tenant ya materializado (evita lazy-load tras commits)."""
    await session.refresh(tenant)
    return tenant.id
