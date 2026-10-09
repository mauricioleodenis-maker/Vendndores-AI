"""Fixtures de B12: cliente de Places con respx, sin dormir ni tocar la red."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.core.config import get_settings
from app.leads.places import PlacesBudget, PlacesClient

DATA = Path(__file__).parent.parent / "fixtures" / "data"
SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FAKE_KEY = "AIza-FAKE-KEY-0123456789"


def load(name: str) -> dict[str, Any]:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


@pytest.fixture
def places_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("VAI_GOOGLE_PLACES_API_KEY", FAKE_KEY)
    get_settings.cache_clear()
    return FAKE_KEY


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def make_client(sleeps: list[float]) -> Callable[..., PlacesClient]:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def _make(**kw: Any) -> PlacesClient:
        kw.setdefault("budget", PlacesBudget(daily_usd=1000, monthly_usd=10_000))
        return PlacesClient(FAKE_KEY, http=httpx.AsyncClient(), qps=0, sleep=fake_sleep, **kw)

    return _make


@pytest.fixture
def mock_places() -> respx.MockRouter:
    with respx.mock(assert_all_called=False) as router:
        yield router


def paged_search(router: respx.MockRouter) -> respx.Route:
    """Primera pagina sin pageToken, segunda con pageToken."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        name = "places_search_page2.json" if body.get("pageToken") else "places_search_page1.json"
        return httpx.Response(200, json=load(name))

    return router.post(SEARCH_URL).mock(side_effect=handler)
