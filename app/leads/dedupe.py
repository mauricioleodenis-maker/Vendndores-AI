"""Deteccion de duplicados y fusion de leads.

Prioridad: (1) place_id, (2) phone_hash, (3) website_domain no generico,
(4) name_key similar (trigramas >= 0.85) en la misma ciudad -> solo sugerencia, no fusiona.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.leads import Lead, LeadEvent, SecretShopTest
from app.db.models.outreach import CampaignTarget, OutreachMessage
from app.leads.normalize import GENERIC_DOMAINS

NAME_SIMILARITY_THRESHOLD = 0.85
MATCH_PLACE_ID = "place_id"
MATCH_PHONE = "phone_hash"
MATCH_DOMAIN = "website_domain"
MATCH_NAME = "name_similar"
PRIORITY = (MATCH_PLACE_ID, MATCH_PHONE, MATCH_DOMAIN, MATCH_NAME)


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    lead: Lead
    kind: str

    @property
    def auto_merge(self) -> bool:
        """Solo name_similar requiere revision manual."""
        return self.kind != MATCH_NAME


def trigrams(text: str) -> set[str]:
    padded = f"  {text} "
    return {padded[i : i + 3] for i in range(len(padded) - 2)} if text else set()


def trigram_similarity(a: str, b: str) -> float:
    """Jaccard de trigramas en [0, 1]."""
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class NameIndex:
    """Indice en memoria de ``(id, name_key, trigramas)`` por ciudad para dedupe difuso en lote.

    Evita recargar todos los leads de la ciudad por cada fila (O(N^2) de consultas) y permite
    ver los leads creados antes en la misma importacion via ``add``.
    """

    def __init__(self) -> None:
        self._by_city: dict[str, list[tuple[UUID, str, frozenset[str]]]] = {}

    async def _load(
        self, session: AsyncSession, city: str
    ) -> list[tuple[UUID, str, frozenset[str]]]:
        if city not in self._by_city:
            rows = (
                await session.execute(
                    select(Lead.id, Lead.name_key).where(
                        Lead.city == city, Lead.disposition != "perdido", Lead.name_key != ""
                    )
                )
            ).all()
            self._by_city[city] = [(i, k, frozenset(trigrams(k))) for i, k in rows]
        return self._by_city[city]

    async def best(
        self, session: AsyncSession, city: str, name_key: str, exclude_id: UUID | None
    ) -> tuple[float, UUID] | None:
        mine = frozenset(trigrams(name_key))
        if not mine:
            return None
        best: tuple[float, UUID] | None = None
        for lead_id, _key, tg in await self._load(session, city):
            if lead_id == exclude_id or not tg:
                continue
            # Cota superior de Jaccard: min/max de tamanos; descarta sin intersectar.
            if min(len(mine), len(tg)) / max(len(mine), len(tg)) < NAME_SIMILARITY_THRESHOLD:
                continue
            sim = len(mine & tg) / len(mine | tg)
            if sim >= NAME_SIMILARITY_THRESHOLD and (best is None or sim > best[0]):
                best = (sim, lead_id)
        return best

    def add(self, city: str, lead_id: UUID, name_key: str) -> None:
        if city in self._by_city and name_key:
            self._by_city[city].append((lead_id, name_key, frozenset(trigrams(name_key))))


async def find_duplicate(
    session: AsyncSession,
    *,
    place_id: str | None = None,
    phone_hash: str | None = None,
    website_domain: str | None = None,
    name_key: str = "",
    city: str = "",
    exclude_id: UUID | None = None,
    name_index: NameIndex | None = None,
) -> DuplicateMatch | None:
    """Primer duplicado segun la prioridad; ignora leads ``perdido`` salvo por place_id."""

    async def first(*conds: Any) -> Lead | None:
        stmt = select(Lead).where(*conds).limit(1)
        if exclude_id is not None:
            stmt = stmt.where(Lead.id != exclude_id)
        return (await session.execute(stmt)).scalars().first()

    if place_id:
        hit = await first(Lead.place_id == place_id)
        if hit:
            return DuplicateMatch(hit, MATCH_PLACE_ID)
    if phone_hash:
        hit = await first(Lead.phone_hash == phone_hash, Lead.disposition != "perdido")
        if hit:
            return DuplicateMatch(hit, MATCH_PHONE)
    if website_domain and website_domain not in GENERIC_DOMAINS:
        hit = await first(Lead.website_domain == website_domain, Lead.disposition != "perdido")
        if hit:
            return DuplicateMatch(hit, MATCH_DOMAIN)
    if name_key and city:
        found = await (name_index or NameIndex()).best(session, city, name_key, exclude_id)
        if found:
            cand = await session.get(Lead, found[1])
            if cand is not None:
                return DuplicateMatch(cand, MATCH_NAME)
    return None


_MERGE_FIELDS = (
    "place_id",
    "maps_url",
    "address",
    "city",
    "phone_e164",
    "phone_hash",
    "phone_enc",
    "phone_type",
    "website",
    "website_domain",
    "instagram",
    "instagram_handle",
    "rating",
    "review_count",
    "business_status",
    "price_level",
    "hours",
)


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == {} or value == []


async def merge_leads(session: AsyncSession, winner: Lead, loser: Lead) -> Lead:
    """Fusiona ``loser`` en ``winner``: conserva el valor mas completo y reasigna historial.

    Si alguno es ``no_contactar`` el resultado hereda ``no_contactar``. ``loser`` queda ``perdido``
    con tag ``merged``.
    """
    if winner.id == loser.id:
        return winner
    # Los campos unicos (place_id, phone_hash) se liberan en loser antes de copiarlos a winner.
    values = {f: getattr(loser, f) for f in _MERGE_FIELDS}
    blocked = "no_contactar" in (winner.disposition, loser.disposition)
    loser_tags = list(loser.tags or [])
    loser.place_id = None
    loser.phone_hash = None
    loser.disposition = "perdido"
    loser.tags = sorted({*(loser.tags or []), "merged"})
    await session.flush()
    for f, v in values.items():
        if _empty(getattr(winner, f)) and not _empty(v):
            setattr(winner, f, v)
    if (loser.review_count or 0) > (winner.review_count or 0):
        winner.review_count, winner.rating = loser.review_count, loser.rating or winner.rating
    if not winner.name_key and loser.name_key:
        winner.name_key = loser.name_key
    if loser.notes and loser.notes not in (winner.notes or ""):
        winner.notes = f"{winner.notes}\n{loser.notes}".strip()
    winner.tags = sorted({*(winner.tags or []), *loser_tags})
    signals = {**(loser.signals or {}), **(winner.signals or {})}
    winner.signals = signals
    if blocked:
        winner.disposition = "no_contactar"

    await session.flush()

    for model in (LeadEvent, OutreachMessage, SecretShopTest):
        await session.execute(
            update(model).where(model.lead_id == loser.id).values(lead_id=winner.id)
        )
    existing = set(
        (
            await session.execute(
                select(CampaignTarget.campaign_id).where(CampaignTarget.lead_id == winner.id)
            )
        ).scalars()
    )
    targets = (
        (await session.execute(select(CampaignTarget).where(CampaignTarget.lead_id == loser.id)))
        .scalars()
        .all()
    )
    for t in targets:
        if t.campaign_id in existing:
            await session.delete(t)
        else:
            t.lead_id = winner.id
    await session.flush()
    return winner
