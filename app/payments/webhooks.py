"""Webhook de eventos Wompi: ``POST /webhooks/wompi/events``."""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.billing import service as billing_service
from app.core.deps import get_session
from app.db.base import utcnow
from app.db.models.payments import PaymentIntent
from app.db.models.plans import BillingRecord
from app.payments.settings import WompiSettings, get_wompi_settings

log = logging.getLogger(__name__)

webhooks_router = APIRouter(prefix="/webhooks/wompi", tags=["wompi"])

MAX_BODY = 64 * 1024
MAX_AGE = timedelta(hours=24)  # Wompi reintenta hasta 24 h
MAX_FUTURE = timedelta(minutes=5)
_STATUS_MAP = {
    "APPROVED": "approved",
    "DECLINED": "declined",
    "VOIDED": "voided",
    "ERROR": "error",
    "PENDING": "pending",
}
_METHOD_MAP = {"PSE": "pse", "NEQUI": "nequi", "DAVIPLATA": "daviplata"}


def _lookup(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def compute_checksum(event: dict[str, Any], events_secret: str) -> str | None:
    sig = event.get("signature")
    props = sig.get("properties") if isinstance(sig, dict) else None
    ts = event.get("timestamp")
    if not isinstance(props, list) or not props or isinstance(ts, bool):
        return None
    if not isinstance(ts, int) or not all(isinstance(p, str) for p in props):
        return None
    data = event.get("data")
    parts: list[str] = []
    for p in props:
        v = _lookup(data, p)
        if v is None or isinstance(v, (dict, list)):
            return None
        parts.append(str(v))
    raw = "".join(parts) + str(ts) + events_secret
    return hashlib.sha256(raw.encode()).hexdigest()


def verify_event(event: dict[str, Any], events_secret: str) -> bool:
    if not events_secret:
        return False
    expected = compute_checksum(event, events_secret)
    got = (event.get("signature") or {}).get("checksum")
    if expected is None or not isinstance(got, str):
        return False
    return hmac.compare_digest(expected.lower().encode(), got.lower().encode())


def _reply(status: str, code: int = 200) -> JSONResponse:
    return JSONResponse({"status": status}, status_code=code)


@webhooks_router.post("/events")
async def wompi_events(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: WompiSettings = Depends(get_wompi_settings),
) -> JSONResponse:
    secret = settings.events_secret.get_secret_value()
    if not secret:
        return _reply("not_configured", 503)
    body = await request.body()
    if len(body) > MAX_BODY:
        return _reply("too_large", 413)
    try:
        event = await request.json()
    except ValueError:
        return _reply("invalid", 400)
    if not isinstance(event, dict) or not verify_event(event, secret):
        return _reply("invalid_signature", 401)

    now = utcnow()
    ts = event["timestamp"]
    try:
        age = now.timestamp() - ts
    except (OverflowError, OSError):  # pragma: no cover
        return _reply("invalid", 400)
    if age > MAX_AGE.total_seconds() or age < -MAX_FUTURE.total_seconds():
        return _reply("stale", 400)

    if event.get("event") != "transaction.updated":
        return _reply("ignored")
    tx = _lookup(event, "data.transaction")
    if not isinstance(tx, dict):
        return _reply("invalid", 400)
    reference, tx_id = tx.get("reference"), tx.get("id")
    status = _STATUS_MAP.get(str(tx.get("status")))
    if not isinstance(reference, str) or not isinstance(tx_id, str) or status is None:
        return _reply("invalid", 400)

    intent = (
        await session.execute(
            select(PaymentIntent).where(PaymentIntent.reference == reference).with_for_update()
        )
    ).scalar_one_or_none()
    if intent is None:
        return _reply("unknown_reference")

    event_key = f"{tx_id}:{tx.get('status')}:{ts}"
    last = intent.raw_last_event or {}
    if last.get("_key") == event_key:
        return _reply("duplicate")
    if intent.status == "approved":  # terminal: no se degrada
        return _reply("ignored")

    amount_ok = tx.get("amount_in_cents") == intent.amount_cop * 100
    intent.raw_last_event = {
        "_key": event_key,
        "event": event.get("event"),
        "timestamp": ts,
        "transaction": {k: tx.get(k) for k in ("id", "reference", "status", "amount_in_cents")},
    }
    intent.provider_tx_id = tx_id[:100]
    billing = await session.get(BillingRecord, intent.billing_record_id)
    tenant_id = billing.tenant_id if billing is not None else None

    if status == "approved" and not amount_ok:
        intent.status = "error"
        await log_event(
            session,
            actor="webhook",
            action="payments.wompi_amount_mismatch",
            entity_type="payment_intent",
            entity_id=intent.id,
            tenant_id=tenant_id,
            diff={"tx": tx_id},
        )
        log.warning("wompi amount mismatch ref=%s", reference)
        return _reply("amount_mismatch")

    intent.status = status
    await log_event(
        session,
        actor="webhook",
        action="payments.wompi_event",
        entity_type="payment_intent",
        entity_id=intent.id,
        tenant_id=tenant_id,
        diff={"status": status, "tx": tx_id},
    )
    if status == "approved":
        method = _METHOD_MAP.get(str(tx.get("payment_method_type")).upper(), "otro")
        await billing_service.mark_paid(
            session,
            intent.billing_record_id,
            actor=None,
            method=method,
            reference=f"wompi:{tx_id}",
        )
    return _reply("processed")
