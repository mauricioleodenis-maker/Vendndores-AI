"""Cliente asincrono de Wompi (httpx). Nunca registra llaves."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import httpx
from pydantic import SecretStr

from app.payments.schemas import PaymentLink, Transaction
from app.payments.settings import PRODUCTION_URL, SANDBOX_URL, WompiSettings

log = logging.getLogger(__name__)

CHECKOUT_LINK_BASE = "https://checkout.wompi.co/l/"


class WompiError(Exception):
    """Error del gateway (mensaje sin secretos)."""


class WompiClient:
    def __init__(
        self,
        public_key: str,
        private_key: SecretStr,
        events_secret: SecretStr,
        integrity_secret: SecretStr,
        base_url: str | None = None,
        *,
        sandbox: bool = True,
        timeout_s: float = 10.0,
        max_retries: int = 2,
        retry_backoff_s: float = 0.2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.public_key = public_key
        self._private_key = private_key
        self.events_secret = events_secret
        self._integrity_secret = integrity_secret
        self.base_url = (base_url or (SANDBOX_URL if sandbox else PRODUCTION_URL)).rstrip("/")
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._backoff = retry_backoff_s
        self._transport = transport

    @classmethod
    def from_settings(cls, s: WompiSettings) -> WompiClient:
        return cls(
            s.public_key,
            s.private_key,
            s.events_secret,
            s.integrity_secret,
            s.effective_base_url,
            timeout_s=s.timeout_s,
        )

    def __repr__(self) -> str:
        return f"WompiClient(base_url={self.base_url!r})"

    def integrity_signature(
        self, reference: str, amount_in_cents: int, currency: str = "COP"
    ) -> str:
        raw = f"{reference}{amount_in_cents}{currency}{self._integrity_secret.get_secret_value()}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._private_key.get_secret_value()}"}
        last: str = "error"
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout, transport=self._transport
                ) as http:
                    r = await http.request(method, f"{self.base_url}{path}", headers=headers, **kw)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = type(exc).__name__
            else:
                if r.status_code < 500:
                    if r.status_code >= 400:
                        raise WompiError(f"Wompi respondio {r.status_code}")
                    try:
                        return r.json()
                    except ValueError as exc:
                        raise WompiError("Respuesta de Wompi no es JSON") from exc
                last = f"HTTP {r.status_code}"
            if attempt < self._max_retries:
                await asyncio.sleep(self._backoff * (2**attempt))
        log.warning("wompi %s %s fallo tras reintentos: %s", method, path, last)
        raise WompiError(f"Wompi no disponible ({last})")

    async def create_payment_link(
        self,
        reference: str,
        amount_cop: int,
        description: str,
        redirect_url: str,
        customer_email: str | None = None,
    ) -> PaymentLink:
        body: dict[str, Any] = {
            "name": description[:60],
            "description": description,
            "single_use": True,
            "collect_shipping": False,
            "currency": "COP",
            "amount_in_cents": amount_cop * 100,
            "sku": reference,
            "redirect_url": redirect_url,
        }
        if customer_email:
            body["customer_data"] = {"customer_references": [], "email": customer_email}
        data = (await self._request("POST", "/payment_links", json=body)).get("data") or {}
        if "id" not in data:
            raise WompiError("Respuesta de Wompi sin id de link")
        return PaymentLink(id=data["id"], url=f"{CHECKOUT_LINK_BASE}{data['id']}")

    async def get_transaction(self, transaction_id: str) -> Transaction:
        data = (await self._request("GET", f"/transactions/{transaction_id}")).get("data")
        if not data:
            raise WompiError("Respuesta de Wompi sin transaccion")
        try:
            return Transaction.model_validate({k: data.get(k) for k in Transaction.model_fields})
        except ValueError as exc:
            raise WompiError("Transaccion Wompi invalida") from exc
