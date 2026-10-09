"""Crawler del sitio del negocio (B3). SSRF-safe: todo pasa por ``app.core.http.safe_fetch``.

Politicas: robots.txt, solo el mismo dominio registrable, tope de paginas, prioridad a paginas
de precios/servicios/contacto/horarios, pausa entre peticiones. El contenido es NO CONFIABLE.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from app.core.http import FetchError, safe_fetch
from app.core.logging import get_logger
from app.scraping.extractor import extract_links, extract_title, html_to_text
from app.scraping.robots import RobotsPolicy, load_robots

log = get_logger(__name__)

HARD_MAX_PAGES = 20
CRAWL_DELAY_SECONDS = 1.0
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
    labels = [p for p in host.lower().rstrip(".").split(".") if p]
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


def _candidates(base: str, html: str, domain: str, seen: set[str]) -> list[tuple[int, str]]:
    found: dict[str, int] = {}
    for href, anchor in extract_links(html):
        absolute = urljoin(base, href)
        parts = urlsplit(absolute)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            continue
        if registrable_domain(parts.hostname) != domain or _SKIP_EXT.search(parts.path):
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

    pages: list[ScrapedPage] = []
    seen: set[str] = {_canonical(start)}
    queue: list[tuple[int, int, str]] = [(100, 0, start)]  # (prioridad, orden, url)
    order = 1
    fetched = 0
    while queue and len(pages) < limit and fetched < limit * 2:
        queue.sort(key=lambda it: (-it[0], it[1]))
        _score, _n, current = queue.pop(0)
        if not robots.allowed(current):
            log.info("scrape_robots_disallow")
            continue
        if fetched:
            await asyncio.sleep(CRAWL_DELAY_SECONDS)
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
        if res.status >= 400 or res.content_type not in _HTML_TYPES:
            continue
        text = html_to_text(res.text)
        if len(text) >= MIN_TEXT_CHARS:
            pages.append(ScrapedPage(url=res.url, title=extract_title(res.text), text=text))
        for score, link in _candidates(res.url, res.text, domain, seen):
            seen.add(link)
            queue.append((score, order, link))
            order += 1
    return pages
