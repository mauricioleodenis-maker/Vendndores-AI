"""Cliente de Google Places API (New): Text Search y Details con field mask obligatorio.

- Reintenta solo 429 y 5xx (backoff exponencial con jitter, max 4 intentos); 400/403/404 no.
- Limitador de QPS y tope de gasto (diario y mensual) sobre el rate limiter compartido.
- La API key nunca se loguea ni aparece en los mensajes de error.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.rate_limit import get_rate_limiter

log = get_logger(__name__)

BASE_URL = "https://places.googleapis.com/v1"
PAGE_SIZE = 20
MAX_PAGES_PER_QUERY = 3
MAX_ATTEMPTS = 4
DEFAULT_QPS = 5.0
HTTP_TIMEOUT_S = 10.0
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# USD por solicitud Text Search con campos Enterprise (telefono, web, rating). Orientativo:
# verificar en la consola de precios de Google antes de presupuestar (support/09 §5).
COST_USD_PER_SEARCH_REQUEST = 0.035
COST_USD_PER_DETAILS_REQUEST = 0.025

SEARCH_FIELD_MASK = ",".join(
    (
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.rating",
        "places.userRatingCount",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.googleMapsUri",
        "places.businessStatus",
        "places.priceLevel",
        "places.primaryType",
        "nextPageToken",
    )
)
DETAILS_FIELD_MASK = ",".join(
    (
        "id",
        "displayName",
        "nationalPhoneNumber",
        "internationalPhoneNumber",
        "websiteUri",
        "rating",
        "userRatingCount",
        "reviews",
        "regularOpeningHours",
        "googleMapsUri",
    )
)

NICHE_QUERIES: dict[str, tuple[str, ...]] = {
    "dentista": ("dentista", "odontologo", "clinica dental"),
    "clinica_estetica": ("clinica estetica", "medicina estetica", "spa facial"),
    "taller": ("taller mecanico", "taller automotriz", "latoneria y pintura"),
    "restaurante": ("restaurante",),
}

_PRICE_LEVELS = {
    "PRICE_LEVEL_FREE": 0,
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}


class PlacesError(AppError):
    """Error de la API de Places (sin datos sensibles en el mensaje)."""

    def __init__(self, code: str, message: str, status: int = 502) -> None:
        super().__init__(code, message, status)


class PlacesNotConfiguredError(PlacesError):
    def __init__(self) -> None:
        super().__init__(
            "places_not_configured",
            "Falta configurar la clave de Google Places (VAI_GOOGLE_PLACES_API_KEY).",
            503,
        )


class PlacesBudgetExceededError(PlacesError):
    def __init__(self, scope: str = "diario") -> None:
        super().__init__(
            "places_budget_exceeded", f"Presupuesto {scope} de Google Places agotado.", 429
        )


@dataclass(frozen=True, slots=True)
class PlaceSummary:
    place_id: str
    name: str
    address: str = ""
    phone_raw: str | None = None
    website: str | None = None
    rating: float | None = None
    review_count: int | None = None
    maps_url: str | None = None
    business_status: str | None = None
    primary_type: str | None = None
    price_level: int | None = None


@dataclass(frozen=True, slots=True)
class PlacesPage:
    places: list[PlaceSummary]
    next_page_token: str | None = None


@dataclass(frozen=True, slots=True)
class PlaceReview:
    rating: int | None
    text: str
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class PlaceDetails:
    place_id: str
    name: str
    phone_raw: str | None
    website: str | None
    rating: float | None
    review_count: int | None
    maps_url: str | None
    weekday_hours: list[str]
    reviews: list[PlaceReview]


@dataclass(slots=True)
class SearchStats:
    requests: int = 0
    found: int = 0
    duplicates: int = 0
    rejected: int = 0
    queries: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CostEstimate:
    variants: int
    areas: int
    max_requests: int
    max_cost_usd: float
    queries: list[str]


def build_queries(niche: str, city: str, neighborhoods: Sequence[str] = ()) -> list[str]:
    """Combina variantes del nicho x barrios ("dentista en Granada, Cali")."""
    variants = NICHE_QUERIES.get(niche, (niche.replace("_", " "),))
    areas = [n.strip() for n in neighborhoods if n.strip()] or [""]
    out: list[str] = []
    for variant in variants:
        for area in areas:
            where = f"{area}, {city}" if area else city
            out.append(f"{variant} en {where}, Colombia")
    return out


def estimate_search(
    niche: str, city: str, neighborhoods: Sequence[str] = (), max_results: int = 60
) -> CostEstimate:
    """Cota superior de solicitudes y costo (no hace llamadas)."""
    queries = build_queries(niche, city, neighborhoods)
    pages = min(MAX_PAGES_PER_QUERY, max(1, math.ceil(max_results / PAGE_SIZE)))
    requests = len(queries) * pages
    variants = len(NICHE_QUERIES.get(niche, (niche,)))
    return CostEstimate(
        variants=variants,
        areas=len(queries) // variants,
        max_requests=requests,
        max_cost_usd=round(requests * COST_USD_PER_SEARCH_REQUEST, 4),
        queries=queries,
    )


class QpsLimiter:
    """Separa las solicitudes al menos ``1/qps`` segundos (seguro entre tareas)."""

    def __init__(
        self,
        qps: float = DEFAULT_QPS,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / qps if qps > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self._interval == 0:
            return
        async with self._lock:
            now = self._clock()
            delay = self._next - now
            if delay > 0:
                await self._sleep(delay)
                now = self._clock()
            self._next = max(now, self._next) + self._interval


class PlacesBudget:
    """Tope de solicitudes pagadas por dia y por mes (contadores del rate limiter compartido)."""

    def __init__(
        self,
        *,
        daily_usd: float,
        monthly_usd: float,
        cost_per_request_usd: float = COST_USD_PER_SEARCH_REQUEST,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._daily = max(0, int(daily_usd / cost_per_request_usd))
        self._monthly = max(0, int(monthly_usd / cost_per_request_usd))
        self._now = now

    @classmethod
    def from_settings(cls) -> PlacesBudget:
        monthly = get_settings().google_places_budget_usd_month
        return cls(daily_usd=monthly / 30 * 2, monthly_usd=monthly)

    async def consume(self, cost_units: int = 1) -> None:
        limiter = get_rate_limiter()
        today = self._now().strftime("%Y%m%d")
        month = today[:6]
        for _ in range(cost_units):
            day = await limiter.hit(f"places:budget:d:{today}", limit=self._daily, window_s=86_400)
            if not day.allowed:
                raise PlacesBudgetExceededError("diario")
            mon = await limiter.hit(
                f"places:budget:m:{month}", limit=self._monthly, window_s=31 * 86_400
            )
            if not mon.allowed:
                raise PlacesBudgetExceededError("mensual")


def _text(node: Any) -> str:
    if isinstance(node, dict):
        return str(node.get("text") or "")
    return str(node or "")


def parse_place(raw: dict[str, Any]) -> PlaceSummary | None:
    place_id = raw.get("id")
    if not isinstance(place_id, str) or not place_id:
        return None
    rating = raw.get("rating")
    count = raw.get("userRatingCount")
    return PlaceSummary(
        place_id=place_id,
        name=_text(raw.get("displayName")).strip(),
        address=str(raw.get("formattedAddress") or ""),
        phone_raw=raw.get("internationalPhoneNumber") or raw.get("nationalPhoneNumber"),
        website=raw.get("websiteUri"),
        rating=float(rating) if isinstance(rating, (int, float)) else None,
        review_count=int(count) if isinstance(count, (int, float)) else None,
        maps_url=raw.get("googleMapsUri"),
        business_status=raw.get("businessStatus"),
        primary_type=raw.get("primaryType"),
        price_level=_PRICE_LEVELS.get(str(raw.get("priceLevel") or "")),
    )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class PlacesClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
        qps: float = DEFAULT_QPS,
        budget: PlacesBudget | None = None,
        limiter: QpsLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        key = api_key if api_key is not None else get_settings().google_places_api_key
        self._key = key if isinstance(key, str) else key.get_secret_value()
        if not self._key:
            raise PlacesNotConfiguredError()
        self._http = http
        self._base = base_url.rstrip("/")
        self._budget = budget or PlacesBudget.from_settings()
        self._limiter = limiter or QpsLimiter(qps, sleep=sleep)
        self._sleep = sleep
        self.requests_made = 0

    def __repr__(self) -> str:  # nunca exponer la key
        return "PlacesClient(api_key=***)"

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def __aenter__(self) -> PlacesClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
        return self._http

    async def _request(
        self,
        method: str,
        path: str,
        *,
        field_mask: str,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        cost_units: int = 1,
    ) -> dict[str, Any]:
        await self._budget.consume(cost_units)  # una vez por llamada logica (no por reintento)
        headers = {"X-Goog-Api-Key": self._key, "X-Goog-FieldMask": field_mask}
        resp: httpx.Response | None = None
        for attempt in range(MAX_ATTEMPTS):
            await self._limiter.wait()
            try:
                resp = await self._client().request(
                    method,
                    f"{self._base}/{path}",
                    headers=headers,
                    json=json,
                    params=params,
                    timeout=HTTP_TIMEOUT_S,
                )
            except httpx.HTTPError as exc:
                raise PlacesError(
                    "places_network", f"No se pudo contactar Google Places ({type(exc).__name__})."
                ) from None
            self.requests_made += 1
            if resp.status_code not in RETRYABLE_STATUS:
                break
            if attempt < MAX_ATTEMPTS - 1:
                delay = min(8.0, 0.5 * 2**attempt) * (0.5 + random.random() / 2)  # noqa: S311
                log.warning("places_retry", status=resp.status_code, attempt=attempt + 1)
                await self._sleep(delay)
        assert resp is not None
        if resp.status_code >= 400:
            raise self._error(resp)
        try:
            data = resp.json()
        except ValueError:
            raise PlacesError(
                "places_bad_response", "Respuesta invalida de Google Places."
            ) from None
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _error(resp: httpx.Response) -> PlacesError:
        status = resp.status_code
        reason = ""
        with contextlib.suppress(ValueError, AttributeError):
            reason = str(resp.json().get("error", {}).get("status") or "")
        log.error("places_error", status=status, reason=reason)
        if status == 429:
            return PlacesError("places_rate_limited", "Google Places limito las solicitudes.", 429)
        if status in (401, 403):
            return PlacesError(
                "places_forbidden",
                "Google Places rechazo la clave (revisa que la API este habilitada).",
                502,
            )
        if status == 404:
            return PlacesError("places_not_found", "Lugar no encontrado en Google Places.", 404)
        if status >= 500:
            return PlacesError("places_unavailable", "Google Places no esta disponible.", 502)
        return PlacesError("places_bad_request", "Google Places rechazo la solicitud.", 502)

    async def text_search(
        self,
        query: str,
        *,
        location_bias: dict[str, Any] | None = None,
        page_token: str | None = None,
        page_size: int = PAGE_SIZE,
    ) -> PlacesPage:
        body: dict[str, Any] = {
            "textQuery": query,
            "languageCode": "es",
            "regionCode": "CO",
            "pageSize": max(1, min(PAGE_SIZE, page_size)),
        }
        if location_bias:
            body["locationBias"] = location_bias
        if page_token:
            body["pageToken"] = page_token
        data = await self._request(
            "POST", "places:searchText", field_mask=SEARCH_FIELD_MASK, json=body
        )
        places = [p for raw in data.get("places") or [] if (p := parse_place(raw)) is not None]
        token = data.get("nextPageToken")
        return PlacesPage(places, token if isinstance(token, str) and token else None)

    async def get_details(self, place_id: str) -> PlaceDetails:
        if not place_id or "/" in place_id or len(place_id) > 200:
            raise PlacesError("places_bad_id", "Identificador de lugar invalido.", 422)
        data = await self._request(
            "GET",
            f"places/{place_id}",
            field_mask=DETAILS_FIELD_MASK,
            params={"languageCode": "es", "regionCode": "CO"},
        )
        summary = parse_place({**data, "id": data.get("id", place_id)})
        reviews = [
            PlaceReview(
                rating=r.get("rating") if isinstance(r.get("rating"), int) else None,
                text=_text(r.get("text")),
                published_at=_parse_time(r.get("publishTime")),
            )
            for r in data.get("reviews") or []
            if isinstance(r, dict)
        ]
        hours = (data.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []
        return PlaceDetails(
            place_id=place_id,
            name=summary.name if summary else "",
            phone_raw=summary.phone_raw if summary else None,
            website=summary.website if summary else None,
            rating=summary.rating if summary else None,
            review_count=summary.review_count if summary else None,
            maps_url=summary.maps_url if summary else None,
            weekday_hours=[str(h) for h in hours],
            reviews=reviews,
        )

    async def search_all(
        self,
        niche: str,
        city: str,
        *,
        neighborhoods: Sequence[str] = (),
        max_results: int = 60,
        min_reviews: int = 0,
        stats: SearchStats | None = None,
    ) -> AsyncIterator[PlaceSummary]:
        """Itera variantes de consulta x barrios, pagina y deduplica por ``place.id``.

        Descarta lugares cerrados y con menos de ``min_reviews`` resenas (cuentan en ``stats``).
        Se detiene al alcanzar ``max_results`` lugares aceptados.
        """
        stats = stats if stats is not None else SearchStats()
        seen: set[str] = set()
        accepted = 0
        for query in build_queries(niche, city, neighborhoods):
            stats.queries.append(query)
            token: str | None = None
            for _ in range(MAX_PAGES_PER_QUERY):
                if accepted >= max_results:
                    return
                stats.requests += 1
                page = await self.text_search(query, page_token=token)
                for place in page.places:
                    if place.place_id in seen:
                        stats.duplicates += 1
                        continue
                    seen.add(place.place_id)
                    if (
                        place.business_status not in (None, "OPERATIONAL")
                        or not place.name
                        or (place.review_count or 0) < min_reviews
                    ):
                        stats.rejected += 1
                        continue
                    accepted += 1
                    stats.found += 1
                    yield place
                    if accepted >= max_results:
                        return
                token = page.next_page_token
                if not token:
                    break
