import ipaddress

import httpx
import pytest
import respx

from app.core.http import (
    FetchError,
    UnsafeURLError,
    is_blocked_ip,
    safe_fetch,
    validate_url_syntax,
)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "2002:7f00:1::",
        "64:ff9b::7f00:1",
        "2001::1",
    ],
)
def test_blocked_ips(ip):
    assert is_blocked_ip(ipaddress.ip_address(ip))


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_ips_allowed(ip):
    assert not is_blocked_ip(ipaddress.ip_address(ip))


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://user:pw@example.com/",
        "http://example.com:8080/",
        "http://example.com:22/",
        "http:///nohost",
        "",
        "http://exa mple.com/",
    ],
)
def test_bad_syntax(url):
    with pytest.raises(UnsafeURLError):
        validate_url_syntax(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/",
        "http://10.0.0.5/",
        "http://app.localhost/",
        "http://0x7f000001/",
    ],
)
async def test_blocked_targets(url):
    with pytest.raises(FetchError):
        await safe_fetch(url)


async def test_dns_resolving_to_private_is_blocked(fake_dns):
    fake_dns["evil.example.com"] = ["93.184.216.34", "10.0.0.1"]  # CUALQUIER ip privada bloquea
    with pytest.raises(UnsafeURLError):
        await safe_fetch("http://evil.example.com/")


async def test_dns_failure_is_fetch_error(monkeypatch):
    from app.core import http as h

    async def boom(host, port):
        raise OSError("nxdomain")

    monkeypatch.setattr(h, "_resolve", boom)
    with pytest.raises(FetchError):
        await safe_fetch("http://nope.example.com/")


@respx.mock
async def test_ok_fetch():
    respx.get("http://ok.example.com/").mock(
        return_value=httpx.Response(
            200, text="<html>hola</html>", headers={"content-type": "text/html; charset=utf-8"}
        )
    )
    res = await safe_fetch("http://ok.example.com/")
    assert res.status == 200 and "hola" in res.text and res.content_type == "text/html"


@respx.mock
async def test_redirect_followed_and_revalidated(fake_dns):
    respx.get("http://a.example.com/").mock(
        return_value=httpx.Response(302, headers={"location": "http://b.example.com/x"})
    )
    respx.get("http://b.example.com/x").mock(
        return_value=httpx.Response(200, text="final", headers={"content-type": "text/plain"})
    )
    res = await safe_fetch("http://a.example.com/")
    assert res.text == "final" and res.url == "http://b.example.com/x"

    fake_dns["b.example.com"] = ["192.168.0.9"]
    with pytest.raises(UnsafeURLError):
        await safe_fetch("http://a.example.com/")


@respx.mock
async def test_redirect_to_metadata_blocked():
    respx.get("http://a.example.com/").mock(
        return_value=httpx.Response(301, headers={"location": "http://169.254.169.254/"})
    )
    with pytest.raises(UnsafeURLError):
        await safe_fetch("http://a.example.com/")


@respx.mock
async def test_too_many_redirects():
    respx.get("http://loop.example.com/").mock(
        return_value=httpx.Response(302, headers={"location": "http://loop.example.com/"})
    )
    with pytest.raises(FetchError, match="redirecciones"):
        await safe_fetch("http://loop.example.com/", max_redirects=2)


@respx.mock
async def test_redirect_without_location():
    respx.get("http://a.example.com/").mock(return_value=httpx.Response(302))
    with pytest.raises(FetchError):
        await safe_fetch("http://a.example.com/")


@respx.mock
async def test_content_type_rejected():
    respx.get("http://img.example.com/").mock(
        return_value=httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
    )
    with pytest.raises(FetchError, match="Tipo de contenido"):
        await safe_fetch("http://img.example.com/")


@respx.mock
async def test_body_truncated_to_max_bytes():
    respx.get("http://big.example.com/").mock(
        return_value=httpx.Response(
            200, content=b"a" * 10_000, headers={"content-type": "text/html"}
        )
    )
    res = await safe_fetch("http://big.example.com/", max_bytes=1000)
    assert len(res.text) == 1000


@respx.mock
async def test_timeout_maps_to_fetch_error():
    respx.get("http://slow.example.com/").mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(FetchError, match="Tiempo"):
        await safe_fetch("http://slow.example.com/")


@respx.mock
async def test_network_error_maps_to_fetch_error():
    respx.get("http://down.example.com/").mock(side_effect=httpx.ConnectError("x"))
    with pytest.raises(FetchError):
        await safe_fetch("http://down.example.com/")


@respx.mock
async def test_non_2xx_returned_with_status():
    respx.get("http://nf.example.com/").mock(
        return_value=httpx.Response(404, text="no", headers={"content-type": "text/html"})
    )
    assert (await safe_fetch("http://nf.example.com/")).status == 404
