"""Rutas de planes: API JSON (/api/...) y UI Jinja+HTMX (/admin/planes)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session, require_role
from app.core.errors import AppError, NotFoundError
from app.db.models.plans import Offer, Plan, Subscription
from app.db.models.users import User
from app.plans import catalog, service
from app.plans import entitlements as ent_mod
from app.plans import offers as offers_mod
from app.plans.quote import Quote
from app.plans.schemas import (
    DiscountOut,
    EntitlementsOut,
    OfferOut,
    PlanOut,
    QuoteIn,
    QuoteOut,
    SubscriptionIn,
    SubscriptionOut,
    SubscriptionPatch,
)
from app.web.templating import render

router = APIRouter(tags=["planes"])
admin_only = require_role("admin")


def quote_out(q: Quote) -> QuoteOut:
    return QuoteOut(
        plan_code=q.plan_code,
        offer_code=q.offer_code,
        setup_cop=q.setup_cop,
        monthly_cop=q.monthly_cop,
        discounts=[DiscountOut(label=d.label, amount_cop=d.amount_cop) for d in q.discounts],
        setup_net_cop=q.setup_net_cop,
        first_month_net_cop=q.first_month_net_cop,
        recurring_monthly_cop=q.recurring_monthly_cop,
        free_months=q.free_months,
        include_iva=q.include_iva,
        iva_cop=q.iva_cop,
        total_first_invoice_cop=q.total_first_invoice_cop,
    )


def offer_out(offer: Offer) -> OfferOut:
    return OfferOut(
        code=offer.code,
        name=offer.name,
        discount_type=offer.discount_type,
        value=offer.value,
        max_redemptions=offer.max_redemptions,
        redeemed=offer.redeemed,
        remaining=offers_mod.remaining_slots(offer),
        valid_until=offer.valid_until,
        applies_to_plan_codes=list(offer.applies_to_plan_codes or []),
        conditions=offer.conditions,
        is_active=offer.is_active,
    )


async def _subscription_out(
    session: AsyncSession, sub: Subscription, q: Quote | None = None
) -> SubscriptionOut:
    plan = await session.get(Plan, sub.plan_id)
    offer = await session.get(Offer, sub.offer_id) if sub.offer_id else None
    return SubscriptionOut(
        id=sub.id,
        tenant_id=sub.tenant_id,
        plan_code=plan.code if plan else "",
        status=sub.status,
        offer_code=offer.code if offer else None,
        started_at=sub.started_at,
        current_period_end=sub.current_period_end,
        custom_monthly_fee_cop=sub.custom_monthly_fee_cop,
        guarantee_until=sub.guarantee_until,
        guarantee_status=sub.guarantee_status,
        quote=quote_out(q) if q else None,
    )


# ------------------------------------------------------------------ API publica / admin
@router.get("/api/plans", response_model=list[PlanOut])
async def api_list_plans(session: AsyncSession = Depends(get_session)) -> list[Plan]:
    """Catalogo publico para landing y cotizador (solo planes ``is_public``)."""
    return await service.list_public_plans(session)


@router.get("/api/offers", response_model=list[OfferOut])
async def api_list_offers(
    _user: User = Depends(admin_only), session: AsyncSession = Depends(get_session)
) -> list[OfferOut]:
    return [offer_out(o) for o in await offers_mod.list_offers(session)]


@router.post("/api/plans/quote", response_model=QuoteOut)
async def api_quote(
    data: QuoteIn, _user: User = Depends(admin_only), session: AsyncSession = Depends(get_session)
) -> QuoteOut:
    q = await service.quote_for(
        session,
        data.plan_code,
        data.offer_code,
        include_iva=data.include_iva,
        custom_monthly_fee_cop=data.custom_monthly_fee_cop,
    )
    return quote_out(q)


@router.get("/api/tenants/{tenant_id}/subscription", response_model=SubscriptionOut)
async def api_get_subscription(
    tenant_id: uuid.UUID,
    _user: User = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> SubscriptionOut:
    sub = await service.get_live_subscription(session, tenant_id)
    if sub is None:
        raise NotFoundError("La empresa no tiene suscripción vigente")
    return await _subscription_out(session, sub)


@router.post(
    "/api/tenants/{tenant_id}/subscription", response_model=SubscriptionOut, status_code=201
)
async def api_create_subscription(
    tenant_id: uuid.UUID,
    data: SubscriptionIn,
    user: User = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> SubscriptionOut:
    outcome = await service.create_subscription(
        session,
        tenant_id,
        data.plan_code,
        offer_code=data.offer_code,
        custom_monthly_fee_cop=data.custom_monthly_fee_cop,
        include_iva=data.include_iva,
        actor=user,
    )
    return await _subscription_out(session, outcome.subscription, outcome.quote)


@router.patch("/api/tenants/{tenant_id}/subscription", response_model=SubscriptionOut)
async def api_patch_subscription(
    tenant_id: uuid.UUID,
    data: SubscriptionPatch,
    user: User = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> SubscriptionOut:
    sub: Subscription | None = None
    if data.plan_code is not None or data.custom_monthly_fee_cop is not None:
        sub = await service.change_plan(
            session,
            tenant_id,
            data.plan_code,
            custom_monthly_fee_cop=data.custom_monthly_fee_cop,
            actor=user,
        )
    if data.status is not None:
        sub = await service.set_status(session, tenant_id, data.status, actor=user)
    if sub is None:
        sub = await service.get_live_subscription(session, tenant_id)
        if sub is None:
            raise NotFoundError("La empresa no tiene suscripción vigente")
    return await _subscription_out(session, sub)


@router.get("/api/tenants/{tenant_id}/entitlements", response_model=EntitlementsOut)
async def api_entitlements(
    tenant_id: uuid.UUID,
    _user: User = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> EntitlementsOut:
    ent = await ent_mod.get_entitlements(session, tenant_id)
    return EntitlementsOut(
        plan_code=ent.plan_code,
        status=ent.status,
        active=ent.active,
        has_subscription=ent.has_subscription,
        period=ent.period,
        limits=ent.limits,
        usage=ent.usage,
        conversations_pct=ent.usage_pct("conversations"),
        near_conversation_limit=ent.near_limit("conversations"),
    )


# ------------------------------------------------------------------ UI
@router.get("/admin/planes", response_class=HTMLResponse)
async def planes_page(
    request: Request,
    _user: User = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    plans = await service.list_public_plans(session)
    offers = await offers_mod.list_offers(session)
    default = plans[0].code if plans else ""
    ctx: dict[str, Any] = {
        "plans": plans,
        "offers": [offer_out(o) for o in offers],
        "quote": await service.quote_for(session, default) if default else None,
        "selected_plan": default,
        "selected_offer": "",
        "include_iva": False,
        "guarantee_days": catalog.GUARANTEE_DAYS,
    }
    return render(request, "plans/planes.html", ctx)


@router.get("/admin/planes/cotizar", response_class=HTMLResponse)
async def cotizar_partial(
    request: Request,
    plan: str = Query("", max_length=40),
    offer: str = Query("", max_length=40),
    iva: str = Query("", max_length=5),
    _user: User = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Parcial HTMX del cotizador (GET puro, sin efectos)."""
    q: Quote | None = None
    error = ""
    try:
        q = await service.quote_for(
            session, plan, offer or None, include_iva=iva.lower() in ("1", "on", "true")
        )
    except AppError as exc:  # mensaje en espanol apto para el operador
        error = exc.message
    return render(request, "plans/_cotizacion.html", {"quote": q, "error": error})
