from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import FakeLLM
from app.db.models.bots import BotConfig
from app.db.models.tenants import Tenant
from app.factory import review, service
from app.factory.schemas import FactoryInput
from tests.factory.conftest import tool_response, valid_config

HX = {"HX-Request": "true"}


async def _draft(session: AsyncSession, tenant: Tenant, inputs: FactoryInput) -> BotConfig:
    llm = FakeLLM([tool_response(valid_config())])
    bot = await service.build_bot(session, tenant.id, inputs, scrape=False, llm=llm)
    await session.commit()
    return bot


def _url(tenant: Tenant, suffix: str = "") -> str:
    return f"/admin/negocios/{tenant.id}/bot{suffix}"


async def test_requires_login(client: httpx.AsyncClient, tenant: Tenant) -> None:
    r = await client.get(_url(tenant), headers={"accept": "application/json"})
    assert r.status_code == 401


async def test_operator_forbidden(
    client: httpx.AsyncClient,
    tenant: Tenant,
    make_user: Callable[..., Any],
    login: Callable[..., Any],
) -> None:
    await login(client, await make_user("operator"))
    assert (await client.get(_url(tenant))).status_code == 403


async def test_empty_state_and_page_with_draft(
    authenticated_client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    owner_input: FactoryInput,
) -> None:
    c = authenticated_client
    r = await c.get(_url(tenant))
    assert r.status_code == 200 and "Aún no hay un bot" in r.text
    bot = await _draft(session, tenant, owner_input)
    r = await c.get(_url(tenant))
    assert r.status_code == 200
    assert "Propuesta de la IA" in r.text and "Limpieza dental" in r.text
    assert "Falta confirmar" in r.text and "Chat de prueba" in r.text
    assert "Aprobar y publicar" in r.text and "disabled" in r.text
    assert (await c.get(_url(tenant) + f"?v={bot.version}&ok=Hecho")).status_code == 200
    assert (await c.get(_url(tenant) + "?error=Algo")).status_code == 200
    assert (await c.get(_url(tenant) + "?v=99")).status_code == 200


async def test_unknown_tenant_404(authenticated_client: httpx.AsyncClient) -> None:
    r = await authenticated_client.get(
        "/admin/negocios/00000000-0000-0000-0000-000000000001/bot",
        headers={"accept": "application/json"},
    )
    assert r.status_code == 404


async def test_full_flow_edit_publish_rollback(
    authenticated_client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    owner_input: FactoryInput,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = authenticated_client
    bot = await _draft(session, tenant, owner_input)
    flagged = [s for s in await review.list_services(session, tenant.id) if s.needs_review]
    assert len(flagged) == 1
    r = await c.post(
        _url(tenant, f"/servicios/{flagged[0].id}"),
        data={
            "name": "Limpieza dental",
            "price": "$99.000",
            "price_note": "",
            "duration_min": "45",
            "description": "Con ultrasonido",
        },
    )
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    await session.refresh(flagged[0])
    assert flagged[0].price_cop == 99000 and flagged[0].needs_review is False

    # publicar sin probar el chat -> bloqueado con mensaje
    r = await c.post(_url(tenant, f"/{bot.id}/publicar"))
    assert r.status_code == 303 and "error=" in r.headers["location"]

    async def fake_reply(
        session_: Any, tenant_id: Any, history: list[dict[str, Any]], text: str, **kw: Any
    ) -> str:
        assert kw["bot_config_id"] == bot.id
        return f"Eco: {text}"

    monkeypatch.setattr("app.conversation.engine.sandbox_reply", fake_reply)
    r = await c.post(
        _url(tenant, f"/{bot.id}/probar"),
        data={"text": "Hola <b>", "history": "[]"},
        headers=HX,
    )
    assert r.status_code == 200 and "Eco: Hola &lt;b&gt;" in r.text
    hist = re.search(r'name="history" value="([^"]*)"', r.text)
    assert hist is not None
    r2 = await c.post(
        _url(tenant, f"/{bot.id}/probar"),
        data={"text": "Otra", "history": hist.group(1).replace("&#34;", '"')},
        headers=HX,
    )
    assert r2.text.count('class="fb-msg') == 4

    r = await c.post(_url(tenant, f"/{bot.id}/publicar"))
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    await session.refresh(bot)
    assert bot.status == "published"

    page = await c.get(_url(tenant))
    assert "Crear borrador para editar" in page.text and "Publicado" in page.text

    # nuevo borrador, publicar, y volver a v1
    assert (await c.post(_url(tenant, "/borrador"))).status_code == 303
    draft = await review.current_draft(session, tenant.id)
    assert draft is not None and draft.version == 2
    await service.publish_bot(session, draft.id, actor="system", enforce_review=False)
    await session.commit()
    r = await c.post(_url(tenant, "/rollback/1"))
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    r = await c.post(_url(tenant, "/rollback/1"))
    assert "error=" in r.headers["location"]
    r = await c.post(_url(tenant, "/rollback/1"), headers={"X-CSRF-Token": ""})
    assert r.status_code == 403


async def test_edit_endpoints(
    authenticated_client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    owner_input: FactoryInput,
) -> None:
    c = authenticated_client
    bot = await _draft(session, tenant, owner_input)
    faq = (await review.list_faqs(session, tenant.id))[0]
    svc = (await review.list_services(session, tenant.id))[0]

    r = await c.post(
        _url(tenant, f"/faqs/{faq.id}"), data={"question": "Cual?", "answer": "Respuesta"}
    )
    assert "ok=" in r.headers["location"]
    r = await c.post(_url(tenant, f"/faqs/{faq.id}"), data={"question": "", "answer": ""})
    assert r.status_code == 422  # campos obligatorios de formulario
    r = await c.post(_url(tenant, f"/faqs/{faq.id}"), data={"question": "ab", "answer": "xyz"})
    assert "error=" in r.headers["location"]
    r = await c.post(
        _url(tenant, f"/servicios/{svc.id}"),
        data={"name": "Nombre", "price": "abc", "duration_min": "30"},
    )
    assert "error=" in r.headers["location"]

    r = await c.post(_url(tenant, f"/{bot.id}/horarios"), data={"mon": "09:00-12:00, 14:00-18:00"})
    assert "ok=" in r.headers["location"]
    r = await c.post(_url(tenant, f"/{bot.id}/horarios"), data={"mon": "mal"})
    assert "error=" in r.headers["location"]
    r = await c.post(
        _url(tenant, f"/{bot.id}/tono"), data={"address_form": "tu", "emoji_level": "none"}
    )
    assert "ok=" in r.headers["location"]
    r = await c.post(
        _url(tenant, f"/{bot.id}/tono"), data={"address_form": "vos", "emoji_level": "none"}
    )
    assert "error=" in r.headers["location"]
    r = await c.post(_url(tenant, f"/{bot.id}/preguntas/3"))
    assert "error=" in r.headers["location"]
    r = await c.post(_url(tenant, f"/{bot.id}/alertas"))
    assert "ok=" in r.headers["location"]
    r = await c.post(_url(tenant, f"/descartar/faq/{faq.id}"))
    assert "ok=" in r.headers["location"]
    r = await c.post(_url(tenant, f"/descartar/service/{svc.id}"))
    assert "ok=" in r.headers["location"]
    r = await c.post(
        _url(tenant, f"/descartar/otro/{svc.id}"), headers={"accept": "application/json"}
    )
    assert r.status_code == 404
    r = await c.post(_url(tenant, "/borrador"))  # ya hay borrador vigente
    assert "ok=" in r.headers["location"]


async def test_other_tenant_bot_is_not_accessible(
    authenticated_client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    make_tenant: Callable[..., Any],
    owner_input: FactoryInput,
) -> None:
    bot = await _draft(session, tenant, owner_input)
    other = await make_tenant("Otro")
    r = await authenticated_client.post(
        _url(other, f"/{bot.id}/tono"),
        data={"address_form": "tu", "emoji_level": "none"},
        headers={"accept": "application/json"},
    )
    assert r.status_code == 404
    r = await authenticated_client.post(
        _url(other, f"/{bot.id}/probar"),
        data={"text": "hola"},
        headers={"accept": "application/json"},
    )
    assert r.status_code == 404


async def test_sandbox_handles_unavailable_engine_and_bad_history(
    authenticated_client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    owner_input: FactoryInput,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(*a: Any, **k: Any) -> str:
        raise NotImplementedError

    monkeypatch.setattr("app.conversation.engine.sandbox_reply", unavailable)
    bot = await _draft(session, tenant, owner_input)
    hist = json.dumps(
        [{"role": "system", "content": "x"}, {"role": "user", "content": "ok"}, "basura"]
    )
    r = await authenticated_client.post(
        _url(tenant, f"/{bot.id}/probar"), data={"text": "hola", "history": hist}, headers=HX
    )
    assert r.status_code == 200 and "aún no está disponible" in r.text
    assert "basura" not in r.text and "&#34;system&#34;" not in r.text
    r = await authenticated_client.post(
        _url(tenant, f"/{bot.id}/probar"), data={"text": "  ", "history": "no-json"}, headers=HX
    )
    assert r.status_code == 200


async def test_regenerate_uses_llm_dependency(
    authenticated_client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    fake_llm: FakeLLM,
) -> None:
    fake_llm.queue(tool_response(valid_config()))
    r = await authenticated_client.post(_url(tenant, "/regenerar"))
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    assert (await review.current_draft(session, tenant.id)) is not None
    # LLM sin salida valida -> mensaje de error, sin 500
    fake_llm.queue(tool_response({"x": 1}), tool_response({"x": 2}))
    r = await authenticated_client.post(_url(tenant, "/regenerar"))
    assert r.status_code == 303 and "error=" in r.headers["location"]


async def test_sandbox_generic_error_is_shown_not_500(
    authenticated_client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    owner_input: FactoryInput,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("secret-internal-detail")

    monkeypatch.setattr("app.conversation.engine.sandbox_reply", boom)
    bot = await _draft(session, tenant, owner_input)
    r = await authenticated_client.post(
        _url(tenant, f"/{bot.id}/probar"), data={"text": "hola", "history": "[]"}, headers=HX
    )
    assert r.status_code == 200 and "No pudimos obtener respuesta" in r.text
    assert "secret-internal-detail" not in r.text


async def test_non_numeric_duration_is_flash_not_422_json(
    authenticated_client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    owner_input: FactoryInput,
) -> None:
    await _draft(session, tenant, owner_input)
    svc = (await review.list_services(session, tenant.id))[0]
    r = await authenticated_client.post(
        _url(tenant, f"/servicios/{svc.id}"),
        data={"name": "Valido", "price": "1000", "duration_min": "abc"},
    )
    assert r.status_code == 303 and "error=" in r.headers["location"]
