from __future__ import annotations

import json

import httpx
import pytest

from app.core import rate_limit
from app.leads import places
from app.leads.places import (
    PlacesBudget,
    PlacesBudgetExceededError,
    PlacesClient,
    PlacesError,
    PlacesNotConfiguredError,
    QpsLimiter,
    SearchStats,
    build_queries,
    estimate_search,
    parse_place,
)
from tests.leads.conftest import FAKE_KEY, SEARCH_URL, load, paged_search


def test_build_queries_variants_times_neighborhoods():
    qs = build_queries("dentista", "Cali", ["Granada", " ", "San Fernando"])
    assert len(qs) == 3 * 2
    assert "dentista en Granada, Cali, Colombia" in qs
    assert build_queries("restaurante", "Cali") == ["restaurante en Cali, Colombia"]
    assert build_queries("otro_nicho", "Cali") == ["otro nicho en Cali, Colombia"]


def test_estimate_is_upper_bound_without_calls():
    est = estimate_search("dentista", "Cali", ["A", "B"], max_results=100)
    assert est.variants == 3 and est.areas == 2
    assert est.max_requests == 6 * 3  # 6 consultas x 3 paginas (tope)
    assert est.max_cost_usd == round(18 * places.COST_USD_PER_SEARCH_REQUEST, 4)
    small = estimate_search("restaurante", "Cali", max_results=10)
    assert small.max_requests == 1


def test_parse_place_handles_missing_fields():
    assert parse_place({"displayName": {"text": "x"}}) is None
    p = parse_place(
        {
            "id": "abc",
            "displayName": {"text": " Dental "},
            "nationalPhoneNumber": "300 1",
            "rating": 4,
            "userRatingCount": 10,
            "priceLevel": "PRICE_LEVEL_MODERATE",
        }
    )
    assert p is not None
    assert (p.name, p.phone_raw, p.rating, p.review_count, p.price_level) == (
        "Dental",
        "300 1",
        4.0,
        10,
        2,
    )


async def test_requires_api_key(monkeypatch):
    monkeypatch.setenv("VAI_GOOGLE_PLACES_API_KEY", "")
    with pytest.raises(PlacesNotConfiguredError):
        PlacesClient()


async def test_key_from_settings_and_repr_hides_it(places_key):
    client = PlacesClient(budget=PlacesBudget(daily_usd=10, monthly_usd=100))
    assert places_key not in repr(client)
    await client.aclose()


async def test_text_search_sends_mask_headers_and_body(mock_places, make_client):
    route = mock_places.post(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load("places_search_page1.json"))
    )
    async with make_client() as client:
        page = await client.text_search("dentista en Cali, Colombia")
    req = route.calls.last.request
    assert req.headers["X-Goog-Api-Key"] == FAKE_KEY
    assert req.headers["X-Goog-FieldMask"] == places.SEARCH_FIELD_MASK
    assert "places.internationalPhoneNumber" in req.headers["X-Goog-FieldMask"]
    assert "reviews" not in req.headers["X-Goog-FieldMask"]
    body = json.loads(req.content)
    assert body["textQuery"] == "dentista en Cali, Colombia"
    assert body["regionCode"] == "CO" and body["languageCode"] == "es"
    assert body["pageSize"] == 20 and "pageToken" not in body
    assert len(page.places) == 5 and page.next_page_token is not None
    assert page.places[0].phone_raw == "+57 300 000 1101"


async def test_text_search_empty_result_is_not_an_error(mock_places, make_client):
    mock_places.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={}))
    async with make_client() as client:
        page = await client.text_search("nada")
    assert page.places == [] and page.next_page_token is None


async def test_search_all_paginates_and_dedupes_by_place_id(mock_places, make_client):
    route = paged_search(mock_places)
    stats = SearchStats()
    async with make_client() as client:
        found = [p async for p in client.search_all("dentista", "Cali", stats=stats)]
    ids = [p.place_id for p in found]
    assert len(ids) == len(set(ids)) == 8
    # 3 variantes x 2 paginas
    assert route.call_count == 6 and stats.requests == 6
    assert stats.found == 8 and stats.duplicates == 16
    assert len(stats.queries) == 3


async def test_search_all_respects_max_results(mock_places, make_client):
    route = paged_search(mock_places)
    async with make_client() as client:
        found = [p async for p in client.search_all("dentista", "Cali", max_results=3)]
    assert len(found) == 3 and route.call_count == 1


async def test_search_all_filters_closed_and_low_reviews(mock_places, make_client):
    data = load("places_search_page1.json")
    data["nextPageToken"] = None
    data["places"][0]["businessStatus"] = "CLOSED_PERMANENTLY"
    mock_places.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=data))
    stats = SearchStats()
    async with make_client() as client:
        found = [
            p
            async for p in client.search_all(
                "restaurante", "Cali", min_reviews=100, stats=stats
            )
        ]
    assert {p.place_id for p in found} == {"ChIJFAKE000003", "ChIJFAKE000005"}
    assert stats.rejected == 3


async def test_retry_on_429_then_success(mock_places, make_client, sleeps):
    route = mock_places.post(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}),
            httpx.Response(503),
            httpx.Response(200, json=load("places_search_page2.json")),
        ]
    )
    async with make_client() as client:
        page = await client.text_search("q")
    assert route.call_count == 3 and len(page.places) == 3
    assert len(sleeps) == 2 and sleeps[1] > 0


async def test_gives_up_after_max_attempts(mock_places, make_client, sleeps):
    route = mock_places.post(SEARCH_URL).mock(return_value=httpx.Response(500))
    async with make_client() as client:
        with pytest.raises(PlacesError) as exc:
            await client.text_search("q")
    assert route.call_count == places.MAX_ATTEMPTS
    assert exc.value.code == "places_unavailable"
    assert len(sleeps) == places.MAX_ATTEMPTS - 1


async def test_persistent_429_maps_to_rate_limited(mock_places, make_client):
    mock_places.post(SEARCH_URL).mock(return_value=httpx.Response(429))
    async with make_client() as client:
        with pytest.raises(PlacesError) as exc:
            await client.text_search("q")
    assert exc.value.code == "places_rate_limited" and exc.value.status == 429


@pytest.mark.parametrize("status", [400, 403, 404])
async def test_no_retry_on_client_errors_and_key_not_leaked(
    mock_places, make_client, sleeps, status
):
    route = mock_places.post(SEARCH_URL).mock(
        return_value=httpx.Response(status, json={"error": {"status": "PERMISSION_DENIED"}})
    )
    async with make_client() as client:
        with pytest.raises(PlacesError) as exc:
            await client.text_search("q")
    assert route.call_count == 1 and sleeps == []
    assert FAKE_KEY not in exc.value.message and FAKE_KEY not in str(exc.value)


async def test_network_error_is_wrapped_without_retry(mock_places, make_client):
    route = mock_places.post(SEARCH_URL).mock(side_effect=httpx.ConnectTimeout("x"))
    async with make_client() as client:
        with pytest.raises(PlacesError) as exc:
            await client.text_search("q")
    assert exc.value.code == "places_network" and route.call_count == 1


async def test_invalid_json_body(mock_places, make_client):
    mock_places.post(SEARCH_URL).mock(return_value=httpx.Response(200, text="<html>"))
    async with make_client() as client:
        with pytest.raises(PlacesError) as exc:
            await client.text_search("q")
    assert exc.value.code == "places_bad_response"


async def test_budget_counts_once_per_logical_call_and_stops(mock_places, make_client):
    mock_places.post(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load("places_search_page2.json"))
    )
    budget = PlacesBudget(daily_usd=places.COST_USD_PER_SEARCH_REQUEST * 2.5, monthly_usd=100)
    async with make_client(budget=budget) as client:
        await client.text_search("a")
        await client.text_search("b")
        with pytest.raises(PlacesBudgetExceededError) as exc:
            await client.text_search("c")
    assert exc.value.status == 429 and "diario" in exc.value.message
    assert client.requests_made == 2


async def test_budget_monthly_cap():
    budget = PlacesBudget(daily_usd=100, monthly_usd=places.COST_USD_PER_SEARCH_REQUEST)
    await budget.consume()
    with pytest.raises(PlacesBudgetExceededError, match="mensual"):
        await budget.consume()
    await rate_limit.get_rate_limiter().reset("places:budget:m:" + budget._now().strftime("%Y%m"))


async def test_budget_from_settings(monkeypatch):
    monkeypatch.setenv("VAI_GOOGLE_PLACES_BUDGET_USD_MONTH", "3.5")
    from app.core.config import get_settings

    get_settings.cache_clear()
    budget = PlacesBudget.from_settings()
    assert budget._monthly == int(3.5 / places.COST_USD_PER_SEARCH_REQUEST)


async def test_qps_limiter_spaces_calls():
    now = [0.0]
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    limiter = QpsLimiter(5, clock=lambda: now[0], sleep=fake_sleep)
    for _ in range(3):
        await limiter.wait()
    assert slept == [pytest.approx(0.2), pytest.approx(0.2)]
    await QpsLimiter(0).wait()  # sin limite: no duerme


async def test_get_details_parses_reviews_and_hours(mock_places, make_client):
    data = load("places_details_ChIJFAKE000001.json")
    route = mock_places.get("https://places.googleapis.com/v1/places/ChIJFAKE000001").mock(
        return_value=httpx.Response(200, json=data)
    )
    async with make_client() as client:
        d = await client.get_details("ChIJFAKE000001")
    assert route.calls.last.request.headers["X-Goog-FieldMask"] == places.DETAILS_FIELD_MASK
    assert d.name.startswith("Clínica Dental") and d.review_count == 128
    assert len(d.reviews) >= 2 and d.reviews[0].published_at is not None
    assert d.weekday_hours[0].startswith("lunes")


async def test_get_details_rejects_bad_ids(make_client):
    async with make_client() as client:
        for bad in ("", "a/b", "x" * 201):
            with pytest.raises(PlacesError):
                await client.get_details(bad)
