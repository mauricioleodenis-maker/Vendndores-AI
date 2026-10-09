from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import unquote

from app.core.clock import utcnow
from app.db.models.leads import Lead
from app.db.models.users import User
from app.leads import messages
from tests.leads.test_router import make_lead  # noqa: F401, F811  (fixture)


def _lead(**kw: Any) -> Lead:
    base = {
        "name": "Clínica Sonrisa",
        "niche": "dentista",
        "city": "Cali",
        "phone_e164": "+573001112233",
    }
    base.update(kw)
    return Lead(**base)


def _user(name: str = "Jeronimo Barney") -> User:
    return User(email="j@x.co", full_name=name, role="owner")


def test_steps_are_filled_and_opener_does_not_sell() -> None:
    steps = {s.key: s for s in messages.build_steps(_lead(rating=4.6, review_count=80), _user())}
    assert list(steps) == list(messages.STEP_KEYS)
    opener = steps["apertura"].text
    assert "Clínica Sonrisa" in opener and "Jeronimo" in opener
    for word in ("IA", "recepcionista", "demo", "plan", "precio"):
        assert word not in opener
    assert "[" not in opener
    assert "recepcionista con IA" in steps["propuesta"].text
    assert len(steps["prueba_secreta"].alternatives) == 2
    assert messages.OPT_OUT.strip() in opener
    url = steps["apertura"].wa_url or ""
    assert (
        url.startswith("https://wa.me/573001112233?text=")
        and unquote(url.split("=", 1)[1]) == opener
    )


def test_without_phone_or_name() -> None:
    steps = messages.build_steps(_lead(phone_e164=None), _user(""))
    assert all(s.wa_url is None for s in steps)
    assert "[TU_NOMBRE]" in steps[1].text


def test_evidence_and_demo_link() -> None:
    ev = {
        "available": True,
        "answered": True,
        "response_minutes": 42,
        "after_hours": True,
        "outcome": "lenta",
    }
    steps = {
        s.key: s
        for s in messages.build_steps(_lead(), _user(), evidence=ev, demo_link="https://x/demo/t")
    }
    assert "42 minutos" in steps["propuesta"].text and "fuera de horario" in steps["propuesta"].text
    assert "https://x/demo/t" in steps["demo"].text and steps["demo"].wa_url
    pending = {"available": True, "answered": False, "outcome": None}
    steps = {s.key: s for s in messages.build_steps(_lead(), _user(), evidence=pending)}
    assert "como un cliente" not in steps["propuesta"].text
    assert steps["demo"].wa_url is None


def test_follow_ups_become_due() -> None:
    sent = {"apertura": utcnow() - timedelta(days=3, hours=1)}
    steps = {s.key: s for s in messages.build_steps(_lead(), _user(), sent=sent)}
    assert steps["seguimiento_1"].due and steps["seguimiento_3"].due
    assert not steps["seguimiento_7"].due and steps["apertura"].sent_at


async def test_pages_and_mark_sent_flow(authenticated_client, make_lead):  # noqa: F811
    c = authenticated_client
    lead = await make_lead(name="Taller Rayo", niche="taller", phone_e164="+573009998877", score=80)
    queue = (await c.get("/admin/mensajes")).text
    assert "Taller Rayo" in queue and "Pruebas secretas" in queue

    page = await c.get(f"/admin/leads/{lead.id}/mensajes")
    assert page.status_code == 200 and "Taller Rayo" in page.text and "Copiar" in page.text

    r = await c.post(
        f"/admin/leads/{lead.id}/mensajes/prueba_secreta/enviado",
        data={"volver": "cola"},
        headers={"X-CSRF-Token": c.csrf_token},
    )
    assert r.status_code == 303 and r.headers["location"] == "/admin/mensajes"
    queue = (await c.get("/admin/mensajes")).text
    assert queue.index("Taller Rayo") < queue.index("Pruebas secretas")  # ahora en aperturas

    await c.post(
        f"/admin/leads/{lead.id}/mensajes/apertura/enviado", headers={"X-CSRF-Token": c.csrf_token}
    )
    detail = (await c.get(f"/admin/leads/{lead.id}")).text
    assert "Contactado" in detail or "contactado" in detail

    bad = await c.post(
        f"/admin/leads/{lead.id}/mensajes/zzz/enviado", headers={"X-CSRF-Token": c.csrf_token}
    )
    assert bad.status_code == 422


async def test_inactive_lead_cannot_be_marked(authenticated_client, make_lead):  # noqa: F811
    c = authenticated_client
    lead = await make_lead(
        name="No molestar", disposition="no_contactar", phone_e164="+573001234567"
    )
    assert "No molestar" not in (await c.get("/admin/mensajes")).text
    r = await c.post(
        f"/admin/leads/{lead.id}/mensajes/apertura/enviado", headers={"X-CSRF-Token": c.csrf_token}
    )
    assert r.status_code == 409


def test_niche_otro_uses_generic_wording() -> None:
    steps = {s.key: s for s in messages.build_steps(_lead(niche="otro"), _user())}
    assert "paciente" not in steps["apertura"].text and "consultorio" not in steps["apertura"].text
    assert "limpieza dental" not in steps["prueba_secreta"].text


def test_follow_ups_hide_sale_until_pitch() -> None:
    before = {s.key: s for s in messages.build_steps(_lead(), _user(), sent={"apertura": utcnow()})}
    assert "IA" not in before["seguimiento_1"].text
    after = {
        s.key: s
        for s in messages.build_steps(
            _lead(), _user(), sent={"propuesta": utcnow()}, demo_link="https://x/demo/t"
        )
    }
    assert "https://x/demo/t" in after["seguimiento_1"].text


def test_short_reply_time_is_not_used_as_evidence() -> None:
    ev = {"available": True, "answered": True, "response_minutes": 10, "outcome": "ok"}
    steps = {s.key: s for s in messages.build_steps(_lead(), _user(), evidence=ev)}
    assert "tardó" not in steps["propuesta"].text


def test_price_objection_uses_catalog() -> None:
    answers = dict(messages.objection_replies(_lead(), _user()))
    assert "$800.000" in answers["¿Cuánto cuesta?"] and "Fundador" in answers["¿Cuánto cuesta?"]
    assert len(answers) >= 12


async def test_mark_sent_twice_and_from_contactado(session, owner_user, make_lead):  # noqa: F811
    lead = await make_lead(stage="contactado", phone_e164="+573001112299")
    await messages.mark_sent(session, lead, "prueba_secreta", owner_user)
    await messages.mark_sent(session, lead, "apertura", owner_user)
    first = (await messages.sent_steps(session, lead.id))["apertura"]
    await messages.mark_sent(session, lead, "apertura", owner_user)
    assert lead.stage == "contactado"
    assert (await messages.sent_steps(session, lead.id))["apertura"] == first


async def test_csrf_required(authenticated_client, make_lead):  # noqa: F811
    lead = await make_lead(phone_e164="+573001112200")
    for url in (
        f"/admin/leads/{lead.id}/mensajes/apertura/enviado",
        f"/admin/leads/{lead.id}/no-contactar",
        f"/admin/leads/{lead.id}/instrucciones",
    ):
        r = await authenticated_client.post(url, data={"text": "x"}, headers={"X-CSRF-Token": ""})
        assert r.status_code == 403


async def test_do_not_contact_blocks_and_hides_buttons(authenticated_client, make_lead):  # noqa: F811
    c = authenticated_client
    lead = await make_lead(name="Dijo No", phone_e164="+573001112201")
    r = await c.post(f"/admin/leads/{lead.id}/no-contactar", headers={"X-CSRF-Token": c.csrf_token})
    assert r.status_code == 303
    page = (await c.get(f"/admin/leads/{lead.id}/mensajes")).text
    assert "No lo contactes" in page and "Marcar enviado" not in page and "wa.me" not in page
    assert "Dijo No" not in (await c.get("/admin/mensajes")).text


async def test_replied_moves_to_respondieron_and_stops_follow_ups(authenticated_client, make_lead):  # noqa: F811
    c = authenticated_client
    h = {"X-CSRF-Token": c.csrf_token}
    lead = await make_lead(name="Contestaron SAS", phone_e164="+573001112202", score=99)
    await c.post(f"/admin/leads/{lead.id}/mensajes/apertura/enviado", headers=h)
    r = await c.post(f"/admin/leads/{lead.id}/respondio", data={"volver": "cola"}, headers=h)
    assert r.status_code == 303 and r.headers["location"] == "/admin/mensajes"
    queue = (await c.get("/admin/mensajes")).text
    start = queue.index("Respondieron")
    assert start < queue.index("Contestaron SAS") < queue.index("Seguimientos que tocan hoy")
    page = (await c.get(f"/admin/leads/{lead.id}/mensajes")).text
    assert "Última respuesta registrada" in page and "Toca hoy" not in page


async def test_pending_secret_shop_shows_review_note(session, authenticated_client, make_lead):  # noqa: F811
    from app.db.models.leads import SecretShopTest

    lead = await make_lead(
        name="Prueba Pendiente", phone_e164="+573001112203", stage="prueba_secreta"
    )
    session.add(SecretShopTest(lead_id=lead.id, sent_at=utcnow() - timedelta(minutes=70)))
    await session.commit()
    queue = (await authenticated_client.get("/admin/mensajes")).text
    assert (
        "Prueba Pendiente" in queue and "hace 70 min" in queue and "revisión de los 60 min" in queue
    )
