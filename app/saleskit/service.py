"""Carga de los documentos de ``docs/ventas`` (solo lectura, lista blanca de nombres)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup

from app.saleskit.markdown import render_markdown

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs" / "ventas"
_SLUG = re.compile(r"^\d{2}-[a-z0-9-]+$")


@dataclass(frozen=True)
class KitDoc:
    slug: str
    title: str
    path: Path


def _title(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def list_docs(base: Path | None = None) -> list[KitDoc]:
    root = base or DOCS_DIR
    if not root.is_dir():
        return []
    return [
        KitDoc(slug=p.stem, title=_title(p), path=p)
        for p in sorted(root.glob("*.md"))
        if _SLUG.match(p.stem)
    ]


@lru_cache(maxsize=64)
def _render_cached(path: str, mtime_ns: int) -> Markup:
    return render_markdown(Path(path).read_text(encoding="utf-8"))


def get_doc(slug: str, base: Path | None = None) -> tuple[KitDoc, Markup] | None:
    """Devuelve el documento solo si ``slug`` esta en la lista blanca (sin rutas del usuario)."""
    for doc in list_docs(base):
        if doc.slug == slug:
            return doc, _render_cached(str(doc.path), doc.path.stat().st_mtime_ns)
    return None
