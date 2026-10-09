"""Ofertas con cupos. El bloqueo de cupo es un UPDATE condicional atomico (SQLite y Postgres)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import to_bogota, utcnow
from app.core.errors import AppError, NotFoundError
from app.db.models.plans import Offer


class OfferUnavailableError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__("offer_unavailable", message, 409)


def today_bogota() -> date:
    return to_bogota(utcnow()).date()


def remaining_slots(offer: Offer) -> int | None:
    """Cupos restantes; ``None`` si la oferta no tiene limite."""
    if offer.max_redemptions is None:
        return None
    return max(offer.max_redemptions - offer.redeemed, 0)


def is_applicable(offer: Offer, plan_code: str, *, today: date | None = None) -> str | None:
    """Devuelve el motivo en espanol por el que NO aplica, o ``None`` si aplica."""
    day = today or today_bogota()
    if not offer.is_active:
        return "La oferta no está activa"
    if offer.valid_until is not None and offer.valid_until < day:
        return "La oferta ya venció"
    codes = list(offer.applies_to_plan_codes or [])
    if codes and plan_code not in codes:
        return "La oferta no aplica a este plan"
    if offer.max_redemptions is not None and offer.redeemed >= offer.max_redemptions:
        return "Se agotaron los cupos de la oferta"
    return None


async def get_offer(session: AsyncSession, code: str) -> Offer:
    offer = (await session.execute(select(Offer).where(Offer.code == code))).scalar_one_or_none()
    if offer is None:
        raise NotFoundError("Oferta no encontrada")
    return offer


async def list_offers(session: AsyncSession, *, only_active: bool = False) -> list[Offer]:
    stmt = select(Offer).order_by(Offer.code)
    if only_active:
        stmt = stmt.where(Offer.is_active.is_(True))
    return list((await session.execute(stmt)).scalars())


async def redeem_offer(
    session: AsyncSession, code: str, plan_code: str, *, today: date | None = None
) -> Offer:
    """Reclama un cupo de forma atomica. Falla con 409 si no hay cupo o no aplica.

    El UPDATE condicional serializa a los competidores: solo uno gana el ultimo cupo.
    """
    day = today or today_bogota()
    offer = await get_offer(session, code)
    reason = is_applicable(offer, plan_code, today=day)
    if reason is not None:
        raise OfferUnavailableError(reason)
    result = await session.execute(
        update(Offer)
        .where(
            Offer.id == offer.id,
            Offer.is_active.is_(True),
            or_(Offer.max_redemptions.is_(None), Offer.redeemed < Offer.max_redemptions),
            or_(Offer.valid_until.is_(None), Offer.valid_until >= day),
        )
        .values(redeemed=Offer.redeemed + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise OfferUnavailableError("Se agotaron los cupos de la oferta")
    await session.refresh(offer)
    return offer


async def release_offer(session: AsyncSession, code: str) -> None:
    """Devuelve un cupo (p. ej. si la suscripcion se anula antes de cobrar)."""
    await session.execute(
        update(Offer)
        .where(Offer.code == code, Offer.redeemed > 0)
        .values(redeemed=Offer.redeemed - 1)
        .execution_options(synchronize_session=False)
    )
