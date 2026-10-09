import re
from datetime import UTC, datetime, timedelta

import pytest

from app.web import filters
from app.web.nav import NAV_ITEMS, PLACEHOLDER_PATHS, nav_for_role


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1200000, "$1.200.000"), (250000.0, "$250.000"), (0, "$0"), (None, "-"), (-50000, "$-50.000")],
)
def test_cop(value, expected):
    assert filters.cop(value) == expected


def test_fechas_in_bogota():
    dt = datetime(2026, 10, 8, 20, 5, tzinfo=UTC)  # 15:05 Bogota
    assert filters.fecha(dt) == "08 oct 2026"
    assert filters.fecha_hora(dt) == "jue 08 oct 2026, 3:05 p. m."
    assert filters.fecha_hora(None) == filters.fecha("") == "-"
    assert filters.fecha_hora(datetime(2026, 1, 1, 5, 0, tzinfo=UTC)).endswith("12:00 a. m.")


def test_hace():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert filters.hace(now - timedelta(seconds=5), now) == "hace un momento"
    assert filters.hace(now - timedelta(minutes=5), now) == "hace 5 min"
    assert filters.hace(now - timedelta(hours=3), now) == "hace 3 h"
    assert filters.hace(now - timedelta(days=2), now) == "hace 2 d"
    assert filters.hace(None) == "-"


def test_tel_mask_hides_number():
    assert filters.tel_mask("+573001112233") == "+57 ••• ••• 2233"
    assert "300111" not in filters.tel_mask("+573001112233")
    assert filters.tel_mask(None) == "-" and filters.tel_mask("123") == "•••"


def test_estado_and_nicho():
    assert filters.estado_es("needs_review") == "Falta confirmar"
    assert filters.estado_clase("active") == "ok" and filters.estado_clase("zzz") == "neutral"
    assert filters.estado_es("raro") == "raro" and filters.estado_es(None) == "-"
    assert filters.nicho_es("clinica_estetica") == "Clinica estetica"
    assert filters.porcentaje(0.256, 1) == "25,6 %" and filters.porcentaje(None) == "-"


def test_nav_has_every_maestro_page_and_role_filtering():
    hrefs = {i.href for i in NAV_ITEMS} | {c.href for i in NAV_ITEMS for c in i.children}
    for needed in (
        "/admin/inicio", "/admin/negocios", "/admin/negocios/nuevo", "/admin/conversaciones",
        "/admin/citas", "/admin/leads", "/admin/campanas", "/admin/planes", "/admin/facturacion",
        "/admin/kit-ventas", "/admin/auditoria", "/admin/ajustes",
    ):  # fmt: skip
        assert needed in hrefs
    owner = {i["href"] for i in nav_for_role("owner")}
    admin = {i["href"] for i in nav_for_role("admin")}
    operator = {i["href"] for i in nav_for_role("operator")}
    assert "/admin/auditoria" in owner and "/admin/auditoria" not in admin
    assert "/admin/planes" in admin and "/admin/planes" not in operator
    assert "/admin/leads" in operator and "/admin/ajustes" in operator
    assert nav_for_role(None) != nav_for_role("owner")


async def test_base_template_has_full_spanish_nav_and_no_inline_or_cdn(authenticated_client):
    r = await authenticated_client.get("/admin/ajustes")
    html = r.text
    for label in ("Inicio", "Empresas", "Conversaciones", "Citas", "Leads", "Campañas", "Planes",
                  "Facturación", "Kit de ventas", "Auditoría", "Ajustes", "Cerrar sesión"):  # fmt: skip
        assert label in html
    assert 'name="csrf-token"' in html
    assert "/static/htmx.min.js" in html and "/static/app.js" in html
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)  # sin scripts inline (CSP)
    assert not re.search(r"https?://(?!test)", html.replace("http://www.w3.org", ""))  # sin CDN


async def test_top_level_pages_render(authenticated_client):
    for path in PLACEHOLDER_PATHS:
        r = await authenticated_client.get(path)
        assert r.status_code == 200, path


async def test_placeholders_require_auth(client):
    r = await client.get("/admin/leads", headers={"accept": "text/html"})
    assert r.status_code == 303


async def test_static_assets_served(client):
    htmx = await client.get("/static/htmx.min.js")
    assert htmx.status_code == 200 and len(htmx.content) > 10_000 and b"htmx" in htmx.content
    js = await client.get("/static/app.js")
    assert "X-CSRF-Token" in js.text
    css = await client.get("/static/app.css")
    assert css.status_code == 200 and "prefers-color-scheme: dark" in css.text
    assert (await client.get("/static/../main.py")).status_code == 404
