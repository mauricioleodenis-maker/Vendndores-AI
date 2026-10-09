from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.saleskit import service

EXPECTED = 9


def test_docs_exist_and_render() -> None:
    docs = service.list_docs()
    assert len(docs) >= EXPECTED
    for d in docs:
        found = service.get_doc(d.slug)
        assert found is not None
        assert "<h2>" in str(found[1]) or "<p>" in str(found[1])


def test_list_docs_missing_dir(tmp_path: Path) -> None:
    assert service.list_docs(tmp_path / "nope") == []


def test_title_fallback(tmp_path: Path) -> None:
    (tmp_path / "01-sin-titulo.md").write_text("texto", encoding="utf-8")
    (tmp_path / "otro.md").write_text("# no", encoding="utf-8")
    docs = service.list_docs(tmp_path)
    assert [d.title for d in docs] == ["01-sin-titulo"]


def test_get_doc_rejects_traversal() -> None:
    assert service.get_doc("../../README") is None
    assert service.get_doc("..%2f..%2fetc") is None


async def test_requires_login(client: httpx.AsyncClient) -> None:
    r = await client.get("/admin/kit-ventas", follow_redirects=False)
    assert r.status_code in (302, 303, 401)


async def test_index(authenticated_client: httpx.AsyncClient) -> None:
    r = await authenticated_client.get("/admin/kit-ventas")
    assert r.status_code == 200
    assert "Kit de ventas" in r.text
    assert "/admin/kit-ventas/01-guion-primer-mensaje" in r.text


@pytest.mark.parametrize("slug", [d.slug for d in service.list_docs()])
async def test_each_doc(authenticated_client: httpx.AsyncClient, slug: str) -> None:
    r = await authenticated_client.get(f"/admin/kit-ventas/{slug}")
    assert r.status_code == 200
    assert "<script>" not in r.text.split("</head>")[-1].replace("<script src=", "")


async def test_copy_buttons_on_scripts(authenticated_client: httpx.AsyncClient) -> None:
    r = await authenticated_client.get("/admin/kit-ventas/01-guion-primer-mensaje")
    assert "data-kit-copy" in r.text
    assert "/static/saleskit.js" in r.text


async def test_unknown_doc_404(authenticated_client: httpx.AsyncClient) -> None:
    r = await authenticated_client.get("/admin/kit-ventas/no-existe")
    assert r.status_code == 404
    r = await authenticated_client.get("/admin/kit-ventas/..%2f..%2fREADME")
    assert r.status_code in (404, 422)


async def test_static_assets(client: httpx.AsyncClient) -> None:
    assert (await client.get("/static/saleskit.js")).status_code == 200
    assert (await client.get("/static/saleskit.css")).status_code == 200


def test_deleted_or_unreadable_doc_does_not_crash(tmp_path: Path) -> None:
    good = tmp_path / "01-ok.md"
    bad = tmp_path / "02-mal.md"
    good.write_text("# Bueno\n\ntexto", encoding="utf-8")
    bad.write_bytes(b"\xff\xfe\x00bad")
    assert [d.title for d in service.list_docs(tmp_path)] == ["Bueno", "02-mal"]
    assert service.get_doc("02-mal", tmp_path) is None
    stale = service.list_docs(tmp_path)  # cache
    good.unlink()
    assert [d.slug for d in service.list_docs(tmp_path)] == ["02-mal"]
    assert stale  # el listado previo no se rompe
