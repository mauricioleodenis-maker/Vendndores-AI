from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.factory import service as factory_service


@pytest.fixture
def fake_build_bot(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Sustituye ``build_bot`` (B4) por un fake que registra las llamadas."""
    calls: list[Any] = []

    async def _fake(session: Any, tenant_id: Any, inputs: Any, **kw: Any) -> None:
        calls.append((tenant_id, inputs, kw))

    monkeypatch.setattr(factory_service, "build_bot", _fake)
    return calls


@pytest.fixture
def admin_headers() -> Callable[[Any], dict[str, str]]:
    return lambda c: {"X-CSRF-Token": c.csrf_token}
