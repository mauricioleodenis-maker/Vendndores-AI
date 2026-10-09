"""Regresiones de endurecimiento: costo con reintentos, atascos, permisos y truncado."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core import jobs as core_jobs
from app.db.models.leads import LeadSource
from app.leads import places
from app.leads.places import PlacesBudget, PlacesError
from app.leads.search import (
    SearchParams,
    create_search_source,
    expire_stale_sources,
    get_source,
    run_search,
    split_neighborhoods,
)
from tests.leads.conftest import SEARCH_URL, load

HX = {"HX-Request": "true"}


async def test_retries_count_in_budget_and_cost(session, mock_places, make_client):
    page = load("places_search_page2.json")
    mock_places.post(SEARCH_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(503), httpx.Response(200, json=page)]
    )
    source = await create_search_source(
        session, SearchParams(niche="dentista", max_results=1), user_id=None
    )
    async with make_client() as client:
        stats = await run_search(session, source, client)
    assert client.requests_made == 3
    assert stats["http_requests"] == 3
    assert stats["cost_usd"] == pytest.approx(3 * places.COST_USD_PER_SEARCH_REQUEST, abs=1e-4)


async def test_retry_stops_when_budget_runs_out(mock_places, make_client):
    mock_places.post(SEARCH_URL).mock(return_value=httpx.Response(503))
    budget = PlacesBudget(daily_usd=places.COST_USD_PER_SEARCH_REQUEST * 2.5, monthly_usd=100)
    async with make_client(budget=budget) as client:
        with pytest.raises(places.PlacesBudgetExceededError):
            await client.text_search("a")
    assert client.requests_made == 2


@pytest.mark.parametrize("bad", ["a?x=1", "a#b", "..", "a b", "a%2Fb"])
async def test_get_details_rejects_unsafe_ids(make_client, bad):
    async with make_client() as client:
        with pytest.raises(PlacesError):
            await client.get_details(bad)


def test_split_neighborhoods_reports_dropped():
    used, dropped = split_neighborhoods(",".join(f"b{i}" for i in range(27)))
    assert len(used) == 20 and dropped == 7


async def test_stale_running_source_is_expired_on_read(session):
    source = await create_search_source(session, SearchParams(niche="taller"), user_id=None)
    source.stats = {**source.stats, "status": "running"}
    source.updated_at = datetime.now(UTC) - timedelta(minutes=30)
    await session.commit()
    got = await get_source(session, source.id)
    assert got.stats["status"] == "failed" and "tardo" in got.stats["error"]


async def test_expire_stale_sources_sweep(session):
    old = await create_search_source(session, SearchParams(niche="taller"), user_id=None)
    fresh = await create_search_source(session, SearchParams(niche="taller"), user_id=None)
    old.updated_at = datetime.now(UTC) - timedelta(hours=2)
    await session.commit()
    assert await expire_stale_sources(session) == 1
    await session.refresh(fresh)
    assert fresh.stats["status"] == "queued"


async def test_enqueue_failure_marks_source_failed(
    authenticated_client, session, places_key, monkeypatch
):
    async def boom(*_a, **_k):
        raise RuntimeError("redis caido")

    monkeypatch.setattr("app.leads.router.enqueue", boom)
    r = await authenticated_client.post(
        "/admin/leads/buscar", data={"niche": "taller", "modo": "buscar"}, headers=HX
    )
    assert r.status_code == 200 and "No se pudo iniciar" in r.text
    source = (await session.execute(select(LeadSource))).scalars().one()
    await session.refresh(source)
    assert source.stats["status"] == "failed"


async def test_operator_cannot_launch_paid_search(client, make_user, login, places_key):
    op = await make_user(role="operator")
    logged = await login(client, op)
    client.headers["X-CSRF-Token"] = logged.csrf
    r = await client.post("/api/leads/search", json={"niche": "taller"})
    assert r.status_code == 403
    assert core_jobs.ENQUEUED == []
    # estimar sigue permitido
    r = await client.post("/api/leads/search", json={"niche": "taller", "dry_run": True})
    assert r.status_code == 200


async def test_operator_cannot_read_foreign_source(client, make_user, login, session):
    owner = await make_user(role="admin")
    source = await create_search_source(session, SearchParams(niche="taller"), user_id=owner.id)
    await session.commit()
    op = await make_user(role="operator")
    await login(client, op)
    r = await client.get(f"/api/leads/sources/{source.id}/status")
    assert r.status_code == 404
    r = await client.get(f"/admin/leads/sources/{uuid.uuid4()}/status", headers=HX)
    assert r.status_code == 404


async def test_failed_status_renders_error_alert(authenticated_client, session):
    source = LeadSource(
        kind="places_api",
        label="x",
        params={},
        stats={"status": "failed", "error": "Se cayo"},
        created_by=authenticated_client.user.id,
    )
    session.add(source)
    await session.commit()
    r = await authenticated_client.get(f"/admin/leads/sources/{source.id}/status", headers=HX)
    assert "alert-error" in r.text and "Se cayo" in r.text


async def test_estimate_warns_when_neighborhoods_truncated(authenticated_client):
    r = await authenticated_client.post(
        "/admin/leads/buscar",
        data={"niche": "taller", "neighborhoods": ",".join(f"b{i}" for i in range(25))},
        headers=HX,
    )
    assert "se descartaron 5" in r.text
