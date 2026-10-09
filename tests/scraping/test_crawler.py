from __future__ import annotations

from collections.abc import Callable

import pytest

from app.core.http import FetchError, UnsafeURLError
from app.scraping import crawl_business_site, crawler
from app.scraping.crawler import normalize_start_url, registrable_domain, score_link
from app.scraping.robots import RobotsPolicy

BODY = "Contenido suficiente de la pagina del negocio para pasar el minimo."


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("www.example.com", "example.com"),
        ("a.b.example.com.co", "example.com.co"),
        ("example.co", "example.co"),
        ("localhost", "localhost"),
        ("evil.com", "evil.com"),
    ],
)
def test_registrable_domain(host: str, expected: str) -> None:
    assert registrable_domain(host) == expected


def test_helpers() -> None:
    assert normalize_start_url(" negocio.co/#x ") == "https://negocio.co/"
    assert score_link("https://a.co/precios", "") > score_link("https://a.co/blog", "")
    assert score_link("https://a.co/x", "Contacto") > 0


def test_robots_policy() -> None:
    p = RobotsPolicy("User-agent: *\nDisallow: /privado")
    assert not p.allowed("https://a.co/privado/x") and p.allowed("https://a.co/")
    assert RobotsPolicy(None).allowed("https://a.co/privado")


async def test_crawl_prioritizes_and_stays_in_domain(web, page_html: Callable[..., str]) -> None:
    web.add(
        "https://negocio.co",
        page_html(
            "Inicio",
            BODY,
            (
                "/blog|Blog",
                "/contacto|Contacto",
                "/precios|Precios",
                "https://otro.com/precios|Externo",
                "https://sub.negocio.co/servicios|Servicios",
                "/foto.jpg|img",
                "/precios#top|dup",
            ),
        ),
    )
    for path in ("blog", "contacto", "precios"):
        web.add(f"https://negocio.co/{path}", page_html(path.title(), f"{BODY} {path}"))
    web.add("https://sub.negocio.co/servicios", page_html("Servicios", f"{BODY} svc"))
    pages = await crawl_business_site("negocio.co", max_pages=4)
    urls = [p.url for p in pages]
    assert urls[0] == "https://negocio.co"
    assert "https://negocio.co/blog" not in urls  # el limite deja fuera lo de menor prioridad
    assert "https://otro.com/precios" not in web.calls
    assert set(urls[1:]) == {
        "https://negocio.co/precios",
        "https://negocio.co/contacto",
        "https://sub.negocio.co/servicios",
    }
    assert all(not u.endswith(".jpg") for u in web.calls)
    assert len(set(web.calls)) == len(web.calls)


async def test_crawl_respects_robots(web, page_html: Callable[..., str]) -> None:
    web.add(
        "https://negocio.co/robots.txt", "User-agent: *\nDisallow: /precios", ctype="text/plain"
    )
    web.add("https://negocio.co", page_html("Home", BODY, ("/precios|Precios",)))
    web.add("https://negocio.co/precios", page_html("P", BODY))
    pages = await crawl_business_site("https://negocio.co")
    assert [p.url for p in pages] == ["https://negocio.co"]
    assert "https://negocio.co/precios" not in web.calls


async def test_robots_forbidden_blocks_everything(web, page_html: Callable[..., str]) -> None:
    web.add("https://negocio.co/robots.txt", "no", status=403, ctype="text/plain")
    web.add("https://negocio.co", page_html("Home", BODY))
    assert await crawl_business_site("https://negocio.co") == []


async def test_crawl_tolerates_errors_and_non_html(web, page_html: Callable[..., str]) -> None:
    web.add("https://negocio.co/robots.txt", FetchError("x"))
    web.add(
        "https://negocio.co",
        page_html("Home", BODY, ("/a|Precios", "/b|Servicios", "/c|Contacto", "/d|Horario")),
    )
    web.add("https://negocio.co/a", UnsafeURLError("privada"))
    web.add("https://negocio.co/b", "{}", ctype="application/json")
    web.add("https://negocio.co/c", "oops", status=500)
    web.add("https://negocio.co/d", page_html("D", "corto"))  # texto bajo el minimo
    pages = await crawl_business_site("https://negocio.co")
    assert [p.url for p in pages] == ["https://negocio.co"]


async def test_offsite_redirect_discarded(web) -> None:
    web.add("https://negocio.co", (301, "text/html", ("https://evil.com/", "<p>" + BODY + "</p>")))
    assert await crawl_business_site("https://negocio.co") == []


async def test_www_redirect_allowed(web, page_html: Callable[..., str]) -> None:
    web.add(
        "https://negocio.co", (200, "text/html", ("https://www.negocio.co/", page_html("H", BODY)))
    )
    pages = await crawl_business_site("https://negocio.co")
    assert pages[0].url == "https://www.negocio.co/"


@pytest.mark.parametrize("bad", ["", "http://[::1", "https://"])
async def test_bad_start_urls(web, bad: str) -> None:
    assert await crawl_business_site(bad) == []


async def test_max_pages_clamped(web, page_html: Callable[..., str]) -> None:
    links = tuple(f"/p{i}|Pagina {i}" for i in range(40))
    web.add("https://negocio.co", page_html("Home", BODY, links))
    for i in range(40):
        web.add(f"https://negocio.co/p{i}", page_html(f"P{i}", f"{BODY} {i}"))
    assert len(await crawl_business_site("https://negocio.co", max_pages=999)) == 20
    assert len(await crawl_business_site("https://negocio.co", max_pages=0)) == 1


def test_registrable_domain_ip_y_hosting_compartido() -> None:
    assert crawler.registrable_domain("8.8.3.4") != crawler.registrable_domain("9.9.3.4")
    assert crawler.registrable_domain("a.github.io") != crawler.registrable_domain("b.github.io")
    assert crawler.registrable_domain("www.negocio.com") == "negocio.com"


async def test_no_sale_a_subdominio_de_hosting_compartido(web, page_html) -> None:
    web.add("https://a.github.io", page_html("A", "x" * 40, ("https://b.github.io/p|Contacto",)))
    web.add("https://b.github.io/p", page_html("B", "y" * 40))
    pages = await crawler.crawl_business_site("https://a.github.io")
    assert [p.title for p in pages] == ["A"]


async def test_limita_cola_y_respeta_deadline(web, page_html, monkeypatch) -> None:
    links = tuple(f"/p{i}|Pagina {i}" for i in range(400))
    web.add("https://x.com", page_html("Home", "z" * 40, links))
    monkeypatch.setattr(crawler, "CRAWL_DEADLINE_SECONDS", -1)
    pages = await crawler.crawl_business_site("https://x.com", max_pages=5)
    assert len(pages) <= 1


async def test_html_anidado_no_rompe_el_crawl(web) -> None:
    web.add("https://x.com", "<div>" * 20000 + "texto suficiente para pasar el minimo")
    assert isinstance(await crawler.crawl_business_site("https://x.com"), list)
