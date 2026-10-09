"""Busqueda de leads con Google Places: parametros, estimado de costo y job que guarda los leads.

Los leads se guardan con las funciones de B11 (``import_csv``): dedupe por place_id/telefono/dominio,
cifrado del telefono, scoring y evento ``imported``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFoundError
from app.core.jobs import register_job
from app.core.logging import get_logger
from app.db.models.leads import LeadSource
from app.db.session import get_sessionmaker
from app.leads.csv_import import LeadCandidate
from app.leads.csv_import import import_csv as _import_candidates
from app.leads.normalize import normalize_website, to_e164_co, website_domain
from app.leads.places import (
    COST_USD_PER_SEARCH_REQUEST,
    NICHE_QUERIES,
    CostEstimate,
    PlaceSummary,
    PlacesBudgetExceededError,
    PlacesClient,
    PlacesError,
    SearchStats,
    estimate_search,
)

log = get_logger(__name__)

JOB_NAME = "leads.search_places"
MAX_NEIGHBORHOODS = 20
DONE_STATES = frozenset({"done", "partial", "failed"})

Neighborhood = Annotated[str, Field(min_length=1, max_length=80)]


class SearchParams(BaseModel):
    niche: str
    city: str = Field(default="Cali", min_length=2, max_length=100)
    neighborhoods: list[Neighborhood] = Field(default_factory=list, max_length=MAX_NEIGHBORHOODS)
    max_results: int = Field(default=60, ge=1, le=100)
    min_reviews: int = Field(default=0, ge=0, le=100_000)
    dry_run: bool = False

    @field_validator("niche")
    @classmethod
    def _niche(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in NICHE_QUERIES:
            raise ValueError(f"Nicho no soportado: {', '.join(NICHE_QUERIES)}")
        return value

    @field_validator("city")
    @classmethod
    def _city(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("neighborhoods")
    @classmethod
    def _hoods(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(v.split()) for v in value]
        return list(dict.fromkeys(v for v in cleaned if v))


def parse_neighborhoods(raw: str) -> list[str]:
    """'Granada, Ciudad Jardin\\nSan Fernando' -> lista limpia y sin duplicados."""
    parts = [p.strip() for chunk in raw.splitlines() for p in chunk.split(",")]
    return list(dict.fromkeys(p for p in parts if p))[:MAX_NEIGHBORHOODS]


def plan_search(params: SearchParams) -> CostEstimate:
    return estimate_search(params.niche, params.city, params.neighborhoods, params.max_results)


async def create_search_source(
    session: AsyncSession, params: SearchParams, *, user_id: uuid.UUID | None
) -> LeadSource:
    estimate = plan_search(params)
    source = LeadSource(
        kind="places_api",
        label=f"{params.niche} en {params.city}"[:200],
        params=params.model_dump(exclude={"dry_run"}),
        stats={
            "status": "queued",
            "max_requests": estimate.max_requests,
            "estimated_cost_usd": estimate.max_cost_usd,
        },
        created_by=user_id,
    )
    session.add(source)
    await session.flush()
    return source


def place_to_candidate(place: PlaceSummary, *, row: int, niche: str, city: str) -> LeadCandidate:
    e164, ptype = to_e164_co(place.phone_raw)
    site = normalize_website(place.website)
    return LeadCandidate(
        row=row,
        name=" ".join(place.name.split())[:300],
        phone_raw=(place.phone_raw or "")[:40],
        phone_e164=e164,
        phone_type=ptype,
        address=place.address[:300],
        city=city[:100],
        niche=niche,
        website=site or None,
        website_domain=website_domain(site),
        rating=Decimal(str(place.rating)) if place.rating is not None else None,
        review_count=place.review_count,
        maps_url=(place.maps_url or None) and place.maps_url[:500],  # type: ignore[index]
        place_id=place.place_id[:200],
        category=(place.primary_type or "")[:100],
        business_status=place.business_status or "OPERATIONAL",
    )


def _status_of(source: LeadSource) -> str:
    return str((source.stats or {}).get("status") or "queued")


async def run_search(
    session: AsyncSession, source: LeadSource, client: PlacesClient
) -> dict[str, Any]:
    """Ejecuta la busqueda de ``source`` y guarda los leads. No hace commit."""
    p = SearchParams.model_validate({**(source.params or {}), "dry_run": False})
    stats = SearchStats()
    places: list[PlaceSummary] = []
    status, error = "done", ""
    try:
        async for place in client.search_all(
            p.niche,
            p.city,
            neighborhoods=p.neighborhoods,
            max_results=p.max_results,
            min_reviews=p.min_reviews,
            stats=stats,
        ):
            places.append(place)
    except PlacesBudgetExceededError as exc:
        status, error = "partial", exc.message
    except PlacesError as exc:
        status, error = ("partial" if places else "failed"), exc.message

    candidates = [
        place_to_candidate(pl, row=i, niche=p.niche, city=p.city)
        for i, pl in enumerate(places, start=1)
    ]
    report = await _import_candidates(session, source, candidates, dry_run=False)
    source.stats = {
        **report.summary(),
        "status": status,
        "error": error,
        "requests": stats.requests,
        "api_duplicates": stats.duplicates,
        "api_rejected": stats.rejected,
        "found": stats.found,
        "cost_usd": round(stats.requests * COST_USD_PER_SEARCH_REQUEST, 4),
        "queries": stats.queries,
    }
    await session.flush()
    log.info("places_search_done", source_id=str(source.id), status=status, found=stats.found)
    return dict(source.stats)


@register_job(JOB_NAME)
async def search_places_job(ctx: dict[str, Any], source_id: str) -> dict[str, Any] | None:
    """Job arq: ``ctx['places_client']`` permite inyectar un cliente (tests)."""
    async with get_sessionmaker()() as session:
        source = await session.get(LeadSource, uuid.UUID(source_id))
        if source is None or source.kind != "places_api" or _status_of(source) != "queued":
            return None
        source.stats = {**(source.stats or {}), "status": "running"}
        await session.commit()

        client: PlacesClient | None = ctx.get("places_client")
        owns_client = client is None
        try:
            client = client or PlacesClient()
            result = await run_search(session, source, client)
        except AppError as exc:
            await session.rollback()
            source = await session.get(LeadSource, uuid.UUID(source_id))
            assert source is not None
            source.stats = {**(source.stats or {}), "status": "failed", "error": exc.message}
            result = dict(source.stats)
        except Exception:
            await session.rollback()
            source = await session.get(LeadSource, uuid.UUID(source_id))
            assert source is not None
            source.stats = {**(source.stats or {}), "status": "failed", "error": "Error interno"}
            await session.commit()
            log.exception("places_search_crashed", source_id=source_id)
            raise
        finally:
            if owns_client and client is not None:
                await client.aclose()
        await session.commit()
        return result


async def get_source(session: AsyncSession, source_id: uuid.UUID) -> LeadSource:
    source = await session.get(LeadSource, source_id)
    if source is None:
        raise NotFoundError("Busqueda no encontrada")
    return source


def source_status(source: LeadSource) -> dict[str, Any]:
    stats = dict(source.stats or {})
    status = _status_of(source)
    stats["status"] = status
    stats["finished"] = status in DONE_STATES
    stats["source_id"] = str(source.id)
    stats["kind"] = source.kind
    return stats
