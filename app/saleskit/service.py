"""Carga de los documentos de ``docs/ventas`` (solo lectura, lista blanca de nombres)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup

from app.core.logging import get_logger
from app.saleskit.markdown import render_markdown

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs" / "ventas"
log = get_logger(__name__)
_SLUG = re.compile(r"^\d{2}-[a-z0-9-]+$")


@dataclass(frozen=True)
class KitDoc:
    slug: str
    title: str
    path: Path


def _title(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("# "):
                    return line[2:].strip()
    except (OSError, UnicodeDecodeError):
        log.warning("saleskit.title_unreadable", doc=path.stem)
    return path.stem


def _dir_stamp(root: Path) -> tuple[int, ...]:
    """Huella del directorio: mtime de cada .md valido (detecta altas, bajas y ediciones)."""
    stamp: list[int] = []
    for p in sorted(root.glob("*.md")):
        if not _SLUG.match(p.stem):
            continue
        try:
            stamp.append(p.stat().st_mtime_ns)
        except OSError:
            continue
    return tuple(stamp)


@lru_cache(maxsize=8)
def _list_cached(root: str, names: tuple[str, ...], stamp: tuple[int, ...]) -> tuple[KitDoc, ...]:
    base = Path(root)
    return tuple(
        KitDoc(slug=n, title=_title(base / f"{n}.md"), path=base / f"{n}.md") for n in names
    )


def list_docs(base: Path | None = None) -> list[KitDoc]:
    root = base or DOCS_DIR
    if not root.is_dir():
        return []
    names = tuple(p.stem for p in sorted(root.glob("*.md")) if _SLUG.match(p.stem))
    return list(_list_cached(str(root), names, _dir_stamp(root)))


@lru_cache(maxsize=64)
def _render_cached(path: str, mtime_ns: int) -> Markup:
    return render_markdown(Path(path).read_text(encoding="utf-8"))


def get_doc(slug: str, base: Path | None = None) -> tuple[KitDoc, Markup] | None:
    """Devuelve el documento solo si ``slug`` esta en la lista blanca (sin rutas del usuario)."""
    for doc in list_docs(base):
        if doc.slug == slug:
            try:
                return doc, _render_cached(str(doc.path), doc.path.stat().st_mtime_ns)
            except (OSError, UnicodeDecodeError):
                log.warning("saleskit.doc_unreadable", doc=slug)
                return None
    return None
