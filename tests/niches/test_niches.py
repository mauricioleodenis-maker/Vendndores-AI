from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models.niches import NicheTemplate as NicheRow
from app.niches import loader
from app.niches.schema import NicheTemplate, render_placeholders

NICHES = ["dentista", "clinica_estetica", "taller", "restaurante"]


def _raw(niche: str) -> dict:
    return yaml.safe_load((loader.TEMPLATES_DIR / f"{niche}.yaml").read_text(encoding="utf-8"))


def test_list_niches() -> None:
    assert loader.list_niches() == NICHES


@pytest.mark.parametrize("niche", NICHES)
def test_each_template_validates(niche: str) -> None:
    tpl = loader.get_niche_template(niche)
    assert tpl.niche == niche
    assert tpl.typical_services and len(tpl.faq_seeds) >= 3
    assert tpl.escalation_triggers and tpl.forbidden_topics
    assert {"saludo", "confirmacion", "recordatorio_24h", "recordatorio_2h"} <= set(
        tpl.message_templates
    )
    assert tpl.currency == "COP" and tpl.timezone == "America/Bogota"


@pytest.mark.parametrize("niche", NICHES)
def test_placeholders_all_defined(niche: str) -> None:
    tpl = loader.get_niche_template(niche)
    assert "negocio" in tpl.required_variables
    unknown = tpl.placeholders() - set(tpl.required_variables) - {
        "nombre", "servicio", "fecha", "hora", "fecha_hora", "personas", "numero", "total",
        "domicilio", "tiempo",
    }  # fmt: skip
    assert not unknown


def test_alias_and_unknown() -> None:
    assert loader.get_niche_template("taller_mecanico").niche == "taller"
    with pytest.raises(AppError) as exc:
        loader.get_niche_template("otro")
    assert exc.value.status == 404


def test_returns_independent_copy() -> None:
    a = loader.get_niche_template("dentista")
    a.typical_services.clear()
    assert loader.get_niche_template("dentista").typical_services


def test_dentista_content_matches_source() -> None:
    tpl = loader.get_niche_template("dentista")
    limpieza = tpl.service("limpieza")
    assert limpieza is not None
    assert (limpieza.price_min_cop, limpieza.price_max_cop) == (80_000, 150_000)
    implante = tpl.service("implante")
    assert implante is not None and implante.requires_assessment and implante.requires_deposit
    assert any(r.level == 1 for r in tpl.escalation_triggers)


def test_taller_has_safety_escalation_and_vehicle_fields() -> None:
    tpl = loader.get_niche_template("taller")
    level1 = [r for r in tpl.escalation_triggers if r.level == 1]
    assert level1 and "grua" in level1[0].action
    assert "marca" in tpl.booking_rules.required_fields


def test_schema_rejects_extra_and_bad_data() -> None:
    raw = _raw("dentista")
    with pytest.raises(ValidationError):
        NicheTemplate.model_validate({**raw, "campo_raro": 1})
    with pytest.raises(ValidationError):
        NicheTemplate.model_validate({**raw, "niche": "veterinaria"})


def test_schema_rejects_unknown_placeholder() -> None:
    raw = _raw("dentista")
    raw["message_templates"]["saludo"] = "Hola {{inventada}}"
    with pytest.raises(ValidationError, match="inventada"):
        NicheTemplate.model_validate(raw)


def test_schema_rejects_missing_required_message() -> None:
    raw = _raw("taller")
    del raw["message_templates"]["recordatorio_2h"]
    with pytest.raises(ValidationError, match="recordatorio_2h"):
        NicheTemplate.model_validate(raw)


def test_schema_rejects_duplicate_and_dangling_services() -> None:
    raw = _raw("taller")
    raw["typical_services"].append(dict(raw["typical_services"][0]))
    with pytest.raises(ValidationError, match="duplicados"):
        NicheTemplate.model_validate(raw)
    raw = _raw("taller")
    raw["booking_rules"]["deposit_services"] = ["no_existe"]
    with pytest.raises(ValidationError, match="no_existe"):
        NicheTemplate.model_validate(raw)


def test_schema_rejects_inverted_price_range() -> None:
    raw = _raw("dentista")
    raw["typical_services"][0]["price_min_cop"] = 900_000
    raw["typical_services"][0]["price_max_cop"] = 1_000
    with pytest.raises(ValidationError, match="price_min_cop"):
        NicheTemplate.model_validate(raw)


def test_render_placeholders() -> None:
    out = render_placeholders(
        "Hola {{ nombre }} en {{negocio}} {{falta}}", {"nombre": "Ana", "negocio": "X"}
    )
    assert out == "Hola Ana en X {{falta}}"


def test_files_exist_for_every_niche() -> None:
    for niche in NICHES:
        assert Path(loader.TEMPLATES_DIR / f"{niche}.yaml").is_file()


async def test_seed_is_idempotent(session: AsyncSession) -> None:
    assert await loader.seed(session) == 4
    assert await loader.seed(session) == 0
    rows = (await session.execute(select(NicheRow))).scalars().all()
    assert {r.niche for r in rows} == set(NICHES)
    rows[0].payload = {}
    await session.flush()
    assert await loader.seed(session) == 1
