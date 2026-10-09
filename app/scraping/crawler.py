"""Crawler del sitio del negocio (B3). SSRF-safe: todo pasa por ``app.core.http.safe_fetch``.

Politicas: robots.txt, solo el mismo dominio registrable, tope de paginas, prioridad a paginas
de precios/servicios/contacto/horarios, pausa entre peticiones. El contenido es NO CONFIABLE.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import time
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from app.core.http import FetchError, safe_fetch
from app.core.logging import get_logger
from app.scraping.extractor import parse_page
from app.scraping.robots import RobotsPolicy, load_robots

log = get_logger(__name__)

HARD_MAX_PAGES = 20
CRAWL_DELAY_SECONDS = 1.0
MAX_ROBOTS_DELAY = 5.0
CRAWL_DEADLINE_SECONDS = 90.0
MAX_QUEUE = 500
MAX_HTML_CHARS = 1_500_000
# Hosting compartido: cada subdominio es un tercero distinto, no el mismo negocio.
_SHARED_SUFFIXES = frozenset({
    "github.io", "gitlab.io", "vercel.app", "netlify.app", "herokuapp.com", "pages.dev",
    "workers.dev", "web.app", "firebaseapp.com", "blogspot.com", "wixsite.com",
    "myshopify.com", "wordpress.com", "weebly.com", "square.site", "godaddysites.com",
    "webflow.io", "carrd.co", "linktr.ee", "onrender.com", "fly.dev", "azurewebsites.net",
})  # fmt: skip
MIN_TEXT_CHARS = 20
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_SKIP_EXT = re.compile(
    r"\.(?:jpe?g|png|gif|webp|svg|ico|pdf|zip|rar|mp[34]|avi|mov|docx?|xlsx?|pptx?|css|js|xml|json)$",
    re.I,
)
_SECOND_LEVEL = frozenset({"com", "net", "org", "gov", "edu", "co", "mil"})
# (patron, puntos): se suma por coincidencia en ruta o texto del enlace.
_PRIORITY: tuple[tuple[re.Pattern[str], int], ...] = tuple(
    (re.compile(p, re.I), w)
    for p, w in (
        (r"precio|tarifa|costo|valor|plan", 10),
        (r"servicio|tratamiento|menu|menú|carta|catalogo|catálogo|producto", 9),
        (r"contact|ubicaci|direcci|donde|dónde|llega", 8),
        (r"horario|agend|cita|reserv", 8),
        (r"nosotros|about|quienes|quiénes|equipo|preguntas|faq", 5),
    )
)


@dataclass(frozen=True, slots=True)
class ScrapedPage:
    url: str
    title: str
    text: str


def registrable_domain(host: str) -> str:
    """Dominio registrable aproximado (sin lista PSL): ``a.b.x.com.co`` -> ``x.com.co``."""
    host = host.lower().rstrip(".").strip("[]")
    try:
        ipaddress.ip_address(host)
        return host  # una IP solo coincide consigo misma
    except ValueError:
        pass
    labels = [p for p in host.split(".") if p]
    if len(labels) > 2 and ".".join(labels[-2:]) in _SHARED_SUFFIXES:
        return ".".join(labels[-3:])
    if len(labels) <= 2:
        return ".".join(labels)
    keep = 3 if labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2 else 2
    return ".".join(labels[-keep:])


def normalize_start_url(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = f"https://{url}"
    return urldefrag(url)[0]


def _canonical(url: str) -> str:
    parts = urlsplit(urldefrag(url)[0])
    path = parts.path or "/"
    return urlunsplit(
        (parts.scheme, parts.netloc.lower(), path.rstrip("/") or "/", parts.query, "")
    )


def score_link(url: str, anchor: str) -> int:
    target = f"{urlsplit(url).path} {anchor}"
    return sum(w for rx, w in _PRIORITY if rx.search(target))


def _candidates(
    base: str, links: list[tuple[str, str]], domain: str, seen: set[str]
) -> list[tuple[int, str]]:
    found: dict[str, int] = {}
    for href, anchor in links:
        try:
            absolute = urljoin(base, href)
            parts = urlsplit(absolute)
            hostname = parts.hostname
        except ValueError:
            continue
        if parts.scheme not in {"http", "https"} or not hostname:
            continue
        if registrable_domain(hostname) != domain or _SKIP_EXT.search(parts.path):
            continue
        canon = _canonical(absolute)
        if canon in seen:
            continue
        found[canon] = max(found.get(canon, 0), score_link(absolute, anchor))
    return [(s, u) for u, s in found.items()]


async def crawl_business_site(url: str, *, max_pages: int = 10) -> list[ScrapedPage]:
    """Rastrea el sitio (home + paginas utiles del mismo dominio). Nunca lanza por fallos de red."""
    limit = max(1, min(max_pages, HARD_MAX_PAGES))
    try:
        start = normalize_start_url(url)
        host = urlsplit(start).hostname
    except ValueError:
        return []
    if not host:
        return []
    domain = registrable_domain(host)
    origin = f"{urlsplit(start).scheme}://{urlsplit(start).netloc}"
    robots: RobotsPolicy = await load_robots(origin)
    delay = CRAWL_DELAY_SECONDS
    if CRAWL_DELAY_SECONDS > 0 and (rd := robots.crawl_delay()) is not None:
        delay = max(delay, min(rd, MAX_ROBOTS_DELAY))
    deadline = time.monotonic() + CRAWL_DEADLINE_SECONDS

    pages: list[ScrapedPage] = []
    seen: set[str] = {_canonical(start)}
    queue: list[tuple[int, int, str]] = [(100, 0, start)]  # (prioridad, orden, url)
    order = 1
    fetched = 0
    while queue and len(pages) < limit and fetched < limit * 2:
        if time.monotonic() > deadline:
            log.info("scrape_deadline")
            break
        queue.sort(key=lambda it: (-it[0], it[1]))
        _score, _n, current = queue.pop(0)
        if not robots.allowed(current):
            log.info("scrape_robots_disallow")
            continue
        if fetched:
            await asyncio.sleep(delay)
        fetched += 1
        try:
            res = await safe_fetch(current)
        except FetchError as exc:
            log.info("scrape_fetch_failed", error=type(exc).__name__)
            continue
        final_host = urlsplit(res.url).hostname or ""
        if registrable_domain(final_host) != domain:
            continue  # redireccion fuera del dominio
        seen.add(_canonical(res.url))
        if res.url != current and not robots.allowed(res.url):
            continue
        if res.status >= 400 or res.content_type not in _HTML_TYPES:
            continue
        try:
            title, text, links = await asyncio.to_thread(parse_page, res.text[:MAX_HTML_CHARS])
        except (RecursionError, ValueError, TypeError) as exc:
            log.warning("scrape_parse_failed", error=type(exc).__name__)
            continue
        if len(text) >= MIN_TEXT_CHARS:
            pages.append(ScrapedPage(url=res.url, title=title, text=text))
        for score, link in _candidates(res.url, links, domain, seen):
            if len(queue) >= MAX_QUEUE:
                break
            seen.add(link)
            queue.append((score, order, link))
            order += 1
    return pages
