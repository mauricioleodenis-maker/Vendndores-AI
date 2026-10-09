from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import NotFoundError
from app.db.base import Base
from app.db.models.plans import Plan
from app.db.session import make_engine
from app.plans import catalog
from app.plans.offers import (
    OfferUnavailableError,
    get_offer,
    is_applicable,
    list_offers,
    redeem_offer,
    release_offer,
    remaining_slots,
)


async def test_seed_matches_maestro_and_is_idempotent(session: AsyncSession) -> None:
    assert await catalog.seed(session) == {"plans": 3, "offers": 1}
    assert await catalog.seed(session) == {"plans": 0, "offers": 0}
    plans = {p.code: p for p in (await session.execute(select(Plan))).scalars()}
    assert [plans[c].setup_fee_cop for c in ("basico", "pro", "premium")] == [
        800_000,
        1_200_000,
        2_000_000,
    ]
    assert [plans[c].monthly_fee_cop for c in ("basico", "pro", "premium")] == [
        250_000,
        390_000,
        600_000,
    ]
    assert [plans[c].limits["max_conversations_month"] for c in ("basico", "pro", "premium")] == [
        500,
        1500,
        4000,
    ]
    assert plans["premium"].limits["max_services"] is None
    assert plans["premium"].limits["voice_enabled"] is True
    assert plans["basico"].limits["followups_enabled"] is False


async def test_seed_keeps_redeemed(session: AsyncSession, seeded: None) -> None:
    await redeem_offer(session, "fundador", "pro")
    await catalog.seed(session)
    assert (await get_offer(session, "fundador")).redeemed == 1


def test_default_limits_fallback() -> None:
    assert catalog.default_limits("nope") == catalog.default_limits("basico")


async def test_founder_offer_defaults(session: AsyncSession, seeded: None) -> None:
    offer = await get_offer(session, "fundador")
    assert (offer.discount_type, offer.value, offer.max_redemptions) == ("setup_pct", 100, 5)
    assert remaining_slots(offer) == 5
    assert [o.code for o in await list_offers(session, only_active=True)] == ["fundador"]


async def test_missing_offer(session: AsyncSession, seeded: None) -> None:
    with pytest.raises(NotFoundError):
        await get_offer(session, "nada")


async def test_slots_run_out(session: AsyncSession, seeded: None) -> None:
    for _ in range(5):
        await redeem_offer(session, "fundador", "basico")
    with pytest.raises(OfferUnavailableError):
        await redeem_offer(session, "fundador", "basico")
    offer = await get_offer(session, "fundador")
    assert offer.redeemed == 5 and remaining_slots(offer) == 0


async def test_release_returns_slot(session: AsyncSession, seeded: None) -> None:
    await redeem_offer(session, "fundador", "basico")
    await release_offer(session, "fundador")
    await release_offer(session, "fundador")  # no baja de cero
    await session.refresh(await get_offer(session, "fundador"))
    assert (await get_offer(session, "fundador")).redeemed == 0


async def test_applicability_rules(session: AsyncSession, seeded: None) -> None:
    offer = await get_offer(session, "fundador")
    assert is_applicable(offer, "pro") is None
    assert is_applicable(offer, "otro_plan") == "La oferta no aplica a este plan"
    offer.valid_until = date.today() - timedelta(days=2)
    assert is_applicable(offer, "pro") == "La oferta ya venció"
    offer.valid_until = None
    offer.is_active = False
    assert is_applicable(offer, "pro") == "La oferta no está activa"
    with pytest.raises(OfferUnavailableError):
        await redeem_offer(session, "fundador", "pro")


async def test_unlimited_offer(session: AsyncSession, seeded: None) -> None:
    offer = await get_offer(session, "fundador")
    offer.max_redemptions = None
    await session.flush()
    assert remaining_slots(offer) is None
    for _ in range(7):
        await redeem_offer(session, "fundador", "pro")


async def test_concurrent_redemption_never_oversells(tmp_path: Path) -> None:
    """Con conexiones reales distintas (archivo SQLite) nunca se vende de mas."""
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            await catalog.seed(s)
            (await get_offer(s, "fundador")).max_redemptions = 3
            await s.commit()

        async def attempt() -> bool:
            async with maker() as s:
                try:
                    await redeem_offer(s, "fundador", "pro")
                    await s.commit()
                except OfferUnavailableError:
                    await s.rollback()
                    return False
                return True

        results = await asyncio.gather(*(attempt() for _ in range(8)))
        assert sum(results) == 3
        async with maker() as s:
            assert (await get_offer(s, "fundador")).redeemed == 3
    finally:
        await engine.dispose()
