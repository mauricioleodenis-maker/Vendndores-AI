"""``safe_fetch``: descarga HTTP con proteccion SSRF completa.

Controles: esquema http/https, sin credenciales en la URL, puertos 80/443, resolucion DNS y bloqueo
de IP no globales (privadas, loopback, link-local, metadata, reservadas, IPv6 incluidas), anti
DNS-rebinding (la conexion TCP re-valida y se fija a la IP resuelta), redirecciones manuales
revalidadas, tope de bytes (sobre el cuerpo ya descomprimido), tiempo total y content-type.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

DEFAULT_ALLOWED_TYPES: tuple[str, ...] = (
    "text/html",
    "application/xhtml+xml",
    "application/ld+json",
    "text/plain",
    "application/json",
    "text/xml",
    "application/xml",
)
ALLOWED_PORTS = frozenset({80, 443})
USER_AGENT = "VendedoresAI-Bot/1.0"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_BLOCKED_HOSTNAMES = frozenset(
    {"localhost", "metadata.google.internal", "metadata", "instance-data"}
)
_EXTRA_BLOCKED = tuple(
    ipaddress.ip_network(n)
    for n in ("100.64.0.0/10", "192.0.0.0/24", "198.18.0.0/15", "169.254.0.0/16", "fd00:ec2::/32")
)


class FetchError(Exception):
    """Fallo de descarga (tiempo, contenido, redirecciones, etc.)."""


class UnsafeURLError(FetchError):
    """La URL o su resolucion DNS viola la politica anti-SSRF."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status: int
    content_type: str
    text: str


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True si la IP no es publica/ruteable."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded: ipaddress.IPv4Address | None = ip.ipv4_mapped
        if embedded is None and ip.sixtofour is not None:
            embedded = ip.sixtofour
        if embedded is None and ip.teredo is not None:
            return True  # Teredo se bloquea por completo
        if embedded is not None:
            return is_blocked_ip(embedded)
        # NAT64 (64:ff9b::/96) embebe IPv4: validar la parte IPv4
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            return is_blocked_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    return any(ip in net for net in _EXTRA_BLOCKED if net.version == ip.version)


async def _resolve(host: str, port: int) -> list[str]:
    """Resuelve ``host`` a IPs (reemplazable en tests)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


async def resolve_public_ips(host: str, port: int) -> list[str]:
    """Resuelve y exige que TODAS las IPs sean publicas."""
    lowered = host.lower().rstrip(".")
    if lowered in _BLOCKED_HOSTNAMES or lowered.endswith((".localhost", ".internal", ".local")):
        raise UnsafeURLError("Host no permitido")
    try:
        literal = ipaddress.ip_address(lowered.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        ips = [str(literal)]
    else:
        try:
            ips = await _resolve(lowered, port)
        except (OSError, UnicodeError) as exc:
            raise FetchError("No se pudo resolver el dominio") from exc
    if not ips:
        raise FetchError("El dominio no resuelve")
    for raw in ips:
        # zona de scope IPv6 (fe80::1%eth0) -> invalida para nosotros
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError as exc:
            raise UnsafeURLError("IP invalida") from exc
        if is_blocked_ip(ip) or "%" in raw:
            raise UnsafeURLError("La direccion resuelve a una red no publica")
    return ips


def validate_url_syntax(url: str) -> tuple[str, str, int]:
    """Valida esquema/credenciales/puerto. Devuelve (scheme, host, port)."""
    if not url or len(url) > 2048 or any(c in url for c in "\r\n\t "):
        raise UnsafeURLError("URL invalida")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError("URL invalida") from exc
    if parts.scheme not in {"http", "https"}:
        raise UnsafeURLError("Solo se permiten URLs http/https")
    if parts.username or parts.password:
        raise UnsafeURLError("No se permiten credenciales en la URL")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL sin host")
    effective = port or (443 if parts.scheme == "https" else 80)
    if effective not in ALLOWED_PORTS:
        raise UnsafeURLError("Puerto no permitido")
    return parts.scheme, host, effective


class _ValidatingBackend(httpcore.AsyncNetworkBackend):
    """Backend de red que re-resuelve, valida y conecta a la IP validada (anti-rebinding)."""

    def __init__(self) -> None:
        from httpcore._backends.anyio import AnyIOBackend

        self._inner = AnyIOBackend()

    async def connect_tcp(  # type: ignore[no-untyped-def]
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        ips = await resolve_public_ips(host, port)
        return await self._inner.connect_tcp(
            ips[0], port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise UnsafeURLError("Sockets unix no permitidos")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _build_client(timeout: float) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(retries=0)
    transport._pool = httpcore.AsyncConnectionPool(  # type: ignore[attr-defined]
        network_backend=_ValidatingBackend(), max_connections=4, http2=False
    )
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
    )


def _normalize_type(raw: str) -> str:
    return raw.split(";", 1)[0].strip().lower()


async def _fetch_once(
    client: httpx.AsyncClient, url: str, max_bytes: int, allowed: Iterable[str]
) -> tuple[httpx.Response, str, bytes]:
    async with client.stream("GET", url) as resp:
        if resp.status_code in _REDIRECT_STATUSES:
            return resp, "", b""
        ctype = _normalize_type(resp.headers.get("content-type", ""))
        if ctype not in set(allowed):
            raise FetchError(f"Tipo de contenido no permitido: {ctype or 'desconocido'}")
        declared = resp.headers.get("content-length")
        buf = bytearray()
        if declared and declared.isdigit() and int(declared) > max_bytes * 20:
            raise FetchError("Respuesta demasiado grande")
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) >= max_bytes:
                del buf[max_bytes:]
                break
        return resp, ctype, bytes(buf)


async def safe_fetch(
    url: str,
    *,
    max_bytes: int = 2_000_000,
    timeout: float = 8.0,
    max_redirects: int = 3,
    allowed_content_types: Iterable[str] = DEFAULT_ALLOWED_TYPES,
) -> FetchResult:
    """Descarga ``url`` de forma segura. Lanza ``UnsafeURLError`` o ``FetchError``.

    El cuerpo se trunca a ``max_bytes`` (ya descomprimido) y ``timeout`` aplica al total.
    """
    allowed = tuple(allowed_content_types)

    async def _run() -> FetchResult:
        current = url
        async with _build_client(timeout) as client:
            for _hop in range(max_redirects + 1):
                _scheme, host, port = validate_url_syntax(current)
                await resolve_public_ips(host, port)
                try:
                    resp, ctype, body = await _fetch_once(client, current, max_bytes, allowed)
                except httpx.TimeoutException as exc:
                    raise FetchError("Tiempo de espera agotado") from exc
                except httpx.HTTPError as exc:
                    raise FetchError("Error de red al descargar la pagina") from exc
                if resp.status_code in _REDIRECT_STATUSES:
                    location = resp.headers.get("location")
                    if not location:
                        raise FetchError("Redireccion sin destino")
                    current = urljoin(current, location)
                    continue
                encoding = resp.charset_encoding or "utf-8"
                try:
                    text = body.decode(encoding, errors="replace")
                except LookupError:
                    text = body.decode("utf-8", errors="replace")
                return FetchResult(
                    url=current, status=resp.status_code, content_type=ctype, text=text
                )
        raise FetchError("Demasiadas redirecciones")

    try:
        return await asyncio.wait_for(_run(), timeout=timeout * (max_redirects + 1) + 1)
    except TimeoutError as exc:
        raise FetchError("Tiempo de espera agotado") from exc
