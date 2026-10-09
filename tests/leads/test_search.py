from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.jobs import JOB_REGISTRY
from app.db.models.leads import Lead, LeadEvent, LeadSource
from app.leads import places
from app.leads.places import PlacesBudget, PlaceSummary
from app.leads.search import (
    JOB_NAME,
    SearchParams,
    create_search_source,
    get_source,
    parse_neighborhoods,
    place_to_candidate,
    plan_search,
    run_search,
    search_places_job,
    source_status,
)
from tests.leads.conftest import SEARCH_URL, paged_search


def test_params_validation_and_cleanup():
    p = SearchParams(niche=" Dentista ", city="  Cali  ", neighborhoods=["A", "A", " B "])
    assert (p.niche, p.city, p.neighborhoods) == ("dentista", "Cali", ["A", "B"])
    with pytest.raises(ValidationError):
        SearchParams(niche="astronauta")
    with pytest.raises(ValidationError):
        SearchParams(niche="dentista", max_results=500)
    with pytest.raises(ValidationError):
        SearchParams(niche="dentista", neighborhoods=[f"b{i}" for i in range(21)])


def test_parse_neighborhoods():
    assert parse_neighborhoods("Granada, Ciudad Jardín\nSan Fernando,, Granada") == [
        "Granada",
        "Ciudad Jardín",
        "San Fernando",
    ]
    assert len(parse_neighborhoods(",".join(str(i) for i in range(50)))) == 20


def test_place_to_candidate_maps_fields():
    c = place_to_candidate(
        PlaceSummary(
            "ChIJX",
            "  Dental  Norte ",
            "Cra 1",
            "+57 300 000 1101",
            "https://www.dental.co/?utm_source=x",
            4.5,
            80,
            "https://maps.google.com/?cid=1",
            "OPERATIONAL",
            "dentist",
        ),
        row=3,
        niche="dentista",
        city="Cali",
    )
    assert c.name == "Dental Norte" and c.phone_e164 == "+573000001101"
    assert c.website_domain == "dental.co" and str(c.rating) == "4.5"
    assert c.place_id == "ChIJX" and c.category == "dentist" and c.row == 3
    bare = place_to_candidate(PlaceSummary("Z", "N"), row=1, niche="taller", city="Cali")
    assert bare.phone_e164 is None and bare.rating is None


async def test_run_search_stores_scored_deduped_leads(session, mock_places, make_client):
    paged_search(mock_places)
    params = SearchParams(niche="dentista", city="Cali", max_results=50)
    source = await create_search_source(session, params, user_id=None)
    assert source.stats["status"] == "queued" and source.stats["max_requests"] == 9
    async with make_client() as client:
        stats = await run_search(session, source, client)
    await session.commit()

    assert stats["status"] == "done" and stats["found"] == 8
    assert stats["created"] == 8 and stats["requests"] == 6
    assert stats["api_duplicates"] == 16
    leads = (await session.execute(select(Lead))).scalars().all()
    assert len(leads) == 8
    assert all(lead.source_id == source.id and lead.place_id for lead in leads)
    assert all(lead.phone_enc and lead.phone_hash for lead in leads)
    events = await session.scalar(select(func.count()).select_from(LeadEvent))
    assert events == 8

    # segunda corrida: todo es duplicado por place_id, no se crean leads
    again = await create_search_source(session, params, user_id=None)
    async with make_client() as client:
        stats2 = await run_search(session, again, client)
    assert stats2["created"] == 0 and stats2["merged"] == 8
    assert await session.scalar(select(func.count()).select_from(Lead)) == 8


async def test_run_search_partial_when_budget_runs_out(session, mock_places, make_client):
    paged_search(mock_places)
    source = await create_search_source(
        session, SearchParams(niche="dentista", max_results=50), user_id=None
    )
    budget = PlacesBudget(daily_usd=places.COST_USD_PER_SEARCH_REQUEST * 2.5, monthly_usd=100)
    async with make_client(budget=budget) as client:
        stats = await run_search(session, source, client)
    assert stats["status"] == "partial" and "Presupuesto" in stats["error"]
    assert stats["created"] == 8  # lo encontrado antes del corte se guarda


async def test_run_search_failed_when_google_rejects(session, mock_places, make_client):
    mock_places.post(SEARCH_URL).mock(return_value=httpx.Response(403))
    source = await create_search_source(session, SearchParams(niche="taller"), user_id=None)
    async with make_client() as client:
        stats = await run_search(session, source, client)
    assert stats["status"] == "failed" and stats["created"] == 0
    assert "clave" in stats["error"]


async def test_job_runs_with_injected_client(session, mock_places, make_client):
    paged_search(mock_places)
    source = await create_search_source(
        session, SearchParams(niche="restaurante", max_results=5), user_id=None
    )
    await session.commit()
    assert JOB_REGISTRY[JOB_NAME] is search_places_job
    async with make_client() as client:
        out = await search_places_job({"places_client": client}, str(source.id))
    assert out is not None and out["status"] == "done" and out["created"] == 5
    await session.refresh(source)
    assert source_status(source)["finished"] is True
    # idempotente: una fuente que ya corrio no se repite
    assert await search_places_job({}, str(source.id)) is None


async def test_job_marks_failed_without_api_key(session, monkeypatch):
    monkeypatch.setenv("VAI_GOOGLE_PLACES_API_KEY", "")
    source = await create_search_source(session, SearchParams(niche="dentista"), user_id=None)
    await session.commit()
    out = await search_places_job({}, str(source.id))
    assert out is not None and out["status"] == "failed"
    await session.refresh(source)
    assert "Google Places" in source.stats["error"]


async def test_job_marks_failed_and_reraises_on_crash(session, make_client):
    source = await create_search_source(session, SearchParams(niche="dentista"), user_id=None)
    await session.commit()

    class Boom:
        def search_all(self, *a, **k):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await search_places_job({"places_client": Boom()}, str(source.id))
    await session.refresh(source)
    assert source.stats["status"] == "failed"


async def test_job_ignores_unknown_or_other_sources(session):
    import uuid

    assert await search_places_job({}, str(uuid.uuid4())) is None
    other = LeadSource(kind="csv_import", params={}, stats={})
    session.add(other)
    await session.commit()
    assert await search_places_job({}, str(other.id)) is None


async def test_get_source_404(session):
    import uuid

    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await get_source(session, uuid.uuid4())


def test_plan_search_matches_params():
    est = plan_search(SearchParams(niche="taller", neighborhoods=["Centro"], max_results=20))
    assert est.max_requests == 3 and est.queries[0].startswith("taller mecanico en Centro")
