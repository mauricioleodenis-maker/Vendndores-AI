import hashlib

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.payments import WompiClient, WompiError, WompiSettings

BASE = "https://sandbox.wompi.co/v1"


def make(**kw):
    return WompiClient(
        "pub_test_x",
        SecretStr("prv_secret"),
        SecretStr("ev"),
        SecretStr("integ"),
        BASE,
        retry_backoff_s=0,
        **kw,
    )


def test_integrity_signature():
    exp = hashlib.sha256(b"REF1150000COPinteg").hexdigest()
    assert make().integrity_signature("REF1", 150000) == exp


def test_repr_hides_keys():
    assert "prv_secret" not in repr(make())


@respx.mock
async def test_create_link():
    route = respx.post(f"{BASE}/payment_links").mock(
        return_value=httpx.Response(201, json={"data": {"id": "abc"}})
    )
    link = await make().create_payment_link("R1", 5000, "Setup", "https://x/ok", "a@b.co")
    assert link.id == "abc" and link.url.endswith("/l/abc")
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer prv_secret"
    assert b'"amount_in_cents":500000' in req.content.replace(b" ", b"")


@respx.mock
async def test_get_transaction():
    respx.get(f"{BASE}/transactions/t1").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "id": "t1",
                    "reference": "R1",
                    "status": "APPROVED",
                    "amount_in_cents": 500000,
                    "payment_method_type": "NEQUI",
                    "extra": 1,
                }
            },
        )
    )
    tx = await make().get_transaction("t1")
    assert tx.status == "APPROVED" and tx.payment_method_type == "NEQUI"


@respx.mock
async def test_retry_on_5xx_then_ok():
    route = respx.get(f"{BASE}/transactions/t1").mock(
        side_effect=[
            httpx.Response(503),
            httpx.ConnectTimeout("t"),
            httpx.Response(
                200,
                json={
                    "data": {
                        "id": "t1",
                        "reference": "R",
                        "status": "PENDING",
                        "amount_in_cents": 1,
                    }
                },
            ),
        ]
    )
    tx = await make().get_transaction("t1")
    assert tx.status == "PENDING" and route.call_count == 3


@respx.mock
async def test_no_retry_on_4xx():
    route = respx.get(f"{BASE}/transactions/t1").mock(return_value=httpx.Response(404))
    with pytest.raises(WompiError):
        await make().get_transaction("t1")
    assert route.call_count == 1


@respx.mock
async def test_gives_up_after_retries_without_leaking_key():
    respx.get(f"{BASE}/transactions/t1").mock(return_value=httpx.Response(500))
    with pytest.raises(WompiError) as ei:
        await make().get_transaction("t1")
    assert "prv_secret" not in str(ei.value)


def test_settings_env(monkeypatch):
    monkeypatch.setenv("VAI_WOMPI_PUBLIC_KEY", "pk")
    monkeypatch.setenv("VAI_WOMPI_PRIVATE_KEY", "sk")
    s = WompiSettings()
    assert s.enabled and s.effective_base_url.startswith("https://sandbox")
    monkeypatch.setenv("VAI_WOMPI_SANDBOX", "false")
    assert "production" in WompiSettings().effective_base_url
