"""Pruebas de endurecimiento (B19): limites de cuerpo, cabeceras y utilidades de seguridad."""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Request

from app.core.middleware import WebhookBodyLimitMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(WebhookBodyLimitMiddleware)

    @app.post("/webhooks/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"n": len(await request.body())}

    return app


async def test_chunked_webhook_body_over_limit_is_rejected() -> None:
    async def gen():  # sin Content-Length: Transfer-Encoding chunked
        for _ in range(10):
            yield b"x" * 10_000

    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/webhooks/echo", content=gen())
    assert resp.status_code == 413


async def test_small_chunked_webhook_body_passes() -> None:
    async def gen():
        yield b"abc"

    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/webhooks/echo", content=gen())
    assert resp.status_code == 200
    assert resp.json() == {"n": 3}


# --------------------------------------------------------------------------- rate limit LLM
async def test_sandbox_chat_is_rate_limited(
    authenticated_client, session, tenant, monkeypatch
) -> None:
    from app.ai.client import FakeLLM
    from app.factory import service
    from app.factory.schemas import FactoryInput
    from tests.factory.conftest import tool_response, valid_config

    inputs = FactoryInput(
        name="Clinica Demo", niche="dentista", city="Cali", address="Calle 5", phone="+573001234567",
        services=[{"name": "Valoracion", "price_cop": 60000, "duration_min": 30}],
        hours={"mon": [["08:00", "17:00"]]}, notes="",
    )  # fmt: skip
    bot = await service.build_bot(
        session, tenant.id, inputs, scrape=False, llm=FakeLLM([tool_response(valid_config())])
    )
    await session.commit()
    calls = 0

    async def fake_reply(*_a, **_k) -> str:
        nonlocal calls
        calls += 1
        return "ok"

    monkeypatch.setattr("app.conversation.engine.sandbox_reply", fake_reply)
    url = f"/admin/negocios/{tenant.id}/bot/{bot.id}/probar"
    for _ in range(35):
        await authenticated_client.post(url, data={"text": "hola", "history": "[]"})
    assert calls == 30  # el limite es 30 por minuto y usuario
