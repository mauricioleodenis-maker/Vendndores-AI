"""Carga y validacion de plantillas de nicho (dueno: B2, firmas fijas MAESTRO §5)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models.niches import NicheTemplate as NicheTemplateRow
from app.niches.schema import NicheTemplate, render_placeholders

__all__ = [
    "NICHES",
    "NicheTemplate",
    "get_niche_template",
    "list_niches",
    "render_placeholders",
    "seed",
]

TEMPLATES_DIR = Path(__file__).parent / "templates"
NICHES: tuple[str, ...] = ("dentista", "clinica_estetica", "taller", "restaurante")
_ALIASES = {"taller_mecanico": "taller"}


class NicheNotFoundError(AppError):
    def __init__(self, niche: str) -> None:
        super().__init__("niche_not_found", f"No existe plantilla para el nicho '{niche}'", 404)


def _read(path: Path) -> NicheTemplate:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    return NicheTemplate.model_validate(raw)


@lru_cache(maxsize=1)
def _load_all() -> dict[str, NicheTemplate]:
    templates: dict[str, NicheTemplate] = {}
    for niche in NICHES:
        tpl = _read(TEMPLATES_DIR / f"{niche}.yaml")
        if tpl.niche != niche:
            raise ValueError(f"{niche}.yaml declara el nicho '{tpl.niche}'")
        templates[niche] = tpl
    return templates


def list_niches() -> list[str]:
    return list(NICHES)


def get_niche_template(niche: str) -> NicheTemplate:
    """Devuelve una copia (el llamador puede modificarla sin afectar el cache)."""
    key = _ALIASES.get(niche, niche)
    template = _load_all().get(key)
    if template is None:
        raise NicheNotFoundError(niche)
    return template.model_copy(deep=True)


async def seed(session: AsyncSession) -> int:
    """Hook de ``app.cli seed``: upsert idempotente en ``niche_templates`` (cache/consulta)."""
    changed = 0
    for niche in NICHES:
        tpl = _load_all()[niche]
        payload = tpl.model_dump(mode="json")
        row = (
            await session.execute(
                select(NicheTemplateRow).where(
                    NicheTemplateRow.niche == niche, NicheTemplateRow.version == tpl.version
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                NicheTemplateRow(
                    niche=niche, name=tpl.display_name_es, version=tpl.version, payload=payload
                )
            )
            changed += 1
        elif row.payload != payload or row.name != tpl.display_name_es:
            row.payload = payload
            row.name = tpl.display_name_es
            changed += 1
    await session.flush()
    return changed
