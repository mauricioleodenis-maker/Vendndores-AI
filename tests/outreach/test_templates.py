from __future__ import annotations

import pytest

from app.db.models.outreach import MessageTemplate
from app.outreach import templates as t
from app.outreach.templates import SEED_TEMPLATES, TemplateRenderError


@pytest.mark.parametrize("seed", SEED_TEMPLATES, ids=lambda s: s.name)
def test_seed_templates_are_valid(seed: t.SeedTemplate) -> None:
    t.validate_template(seed.body, list(seed.variables))
    assert t.placeholder_count(seed.body) == len(seed.variables)


def test_validate_rejects_bad_templates() -> None:
    with pytest.raises(TemplateRenderError):
        t.validate_template("Hola {{1}} y {{3}} gracias", ["contact_name", "agency"])
    with pytest.raises(TemplateRenderError):
        t.validate_template("{{1}} hola de {{2}} gracias", ["contact_name", "agency"])
    with pytest.raises(TemplateRenderError):
        t.validate_template("Hola {{1}} gracias", ["inventada"])


async def test_seed_is_idempotent_and_unapproved(session, templates) -> None:  # type: ignore[no-untyped-def]
    assert await t.seed_templates(session) == 0
    assert len(templates) == len(SEED_TEMPLATES)
    assert all(not t.is_sendable(tpl) for tpl in templates.values())


async def test_build_and_render_with_defaults(templates, make_lead) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead(name="Sonrisa Dental", niche="dentista")
    tpl = templates["lead_contacto_inicial"]
    variables = t.build_variables(tpl, lead)
    assert variables == {"1": "hola", "2": "Vendedores AI", "3": "tu clinica"}
    body = t.render_body(tpl.body, variables)
    assert "{{" not in body and body.startswith("Hola hola")


async def test_secret_shop_evidence_variables(templates, make_lead) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead(name="Sonrisa\nDental")
    tpl = templates["lead_prueba_secreta"]
    ev = {"available": True, "scenario": "precio", "response_minutes": 47}
    variables = t.build_variables(tpl, lead, ev)
    assert variables["4"] == "Sonrisa Dental"  # sin saltos de linea
    assert variables["5"] == "47"
    assert "47 minutos" in t.render_body(tpl.body, variables)


async def test_evidence_required_raises(templates, make_lead) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    with pytest.raises(TemplateRenderError):
        t.build_variables(templates["lead_prueba_secreta"], lead, {"available": False})


def test_render_body_missing_value() -> None:
    with pytest.raises(TemplateRenderError):
        t.render_body("Hola {{1}} de {{2}}", {"1": "x"})


def test_is_sendable_requires_all(templates: dict[str, MessageTemplate]) -> None:
    tpl = templates["lead_contacto_inicial"]
    tpl.approved = True
    tpl.approval_status = "approved"
    assert not t.is_sendable(tpl)  # sin ContentSid
    tpl.twilio_content_sid = "HX" + "c" * 32
    assert t.is_sendable(tpl)
