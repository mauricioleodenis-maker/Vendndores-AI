from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.core.clock import to_bogota
from app.voice import booking_flow as bf

TODAY = date(2026, 10, 9)  # viernes


def slot(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    # hora local Bogota (UTC-5)
    return datetime(2026, month, day, hour + 5, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("sí", "yes"),
        ("Sí, claro", "yes"),
        ("dale, perfecto", "yes"),
        ("de una", "yes"),
        ("no hay problema", "yes"),
        ("no", "no"),
        ("mejor no", "no"),
        ("prefiero otra hora", "no"),
        ("repite por favor", "repeat"),
        ("no sé qué decir", "no"),
        ("el lunes", None),
        ("", None),
    ],
)
def test_parse_confirmation(text: str, want: str | None) -> None:
    assert bf.parse_confirmation(text) == want


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("las tres de la tarde", (15, 0)),
        ("a las 10", (10, 0)),
        ("tres y media", (15, 30)),
        ("las tres y cuarto de la tarde", (15, 15)),
        ("cuatro menos cuarto", (15, 45)),
        ("las diez de la mañana", (10, 0)),
        ("10:30", (10, 30)),
        ("3 pm", (15, 0)),
        ("mediodía", (12, 0)),
        ("las nueve y veinte", (9, 20)),
    ],
)
def test_parse_spoken_time(text: str, want: tuple[int, int]) -> None:
    t = bf.parse_spoken_time(text)
    assert t is not None
    r = t.resolve()
    assert (r.hour, r.minute) == want


def test_parse_spoken_time_none() -> None:
    assert bf.parse_spoken_time("quiero una cita") is None


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("hoy", date(2026, 10, 9)),
        ("mañana", date(2026, 10, 10)),
        ("pasado mañana", date(2026, 10, 11)),
        ("el lunes", date(2026, 10, 12)),
        ("el viernes", date(2026, 10, 16)),
        ("el 15 de noviembre", date(2026, 11, 15)),
        ("quince de noviembre", date(2026, 11, 15)),
        ("primero de diciembre", date(2026, 12, 1)),
        ("el treinta y uno de octubre", date(2026, 10, 31)),
        ("el 20", date(2026, 10, 20)),
        ("el quince", date(2026, 10, 15)),
        ("5 de enero", date(2027, 1, 5)),
        ("12/11", date(2026, 11, 12)),
        ("en tres días", date(2026, 10, 12)),
        ("el 31 de febrero", None),
        ("a las tres de la mañana", None),
        ("hola", None),
    ],
)
def test_parse_spoken_date(text: str, want: date | None) -> None:
    assert bf.parse_spoken_date(text, TODAY) == want


def test_morning_period_is_not_tomorrow() -> None:
    assert bf.parse_spoken_date("a las diez de la mañana", TODAY) is None


SLOTS = [slot(12, 9), slot(12, 15), slot(13, 10, 30)]


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("el primero", 0),
        ("la segunda", 1),
        ("el tercero", 2),
        ("el último", 2),
        ("opción dos", 1),
        ("el de las tres", 1),
        ("el de las nueve", 0),
        ("las diez y media", 2),
        ("el de la tarde", 1),
        ("dos", 1),
        ("no sé", None),
        ("el de las siete", None),
    ],
)
def test_parse_slot_choice(text: str, want: int | None) -> None:
    assert bf.parse_slot_choice(text, SLOTS) == want


def test_choice_ambiguous_time() -> None:
    slots = [slot(12, 15), slot(13, 15)]
    assert bf.parse_slot_choice("el de las tres", slots) is None


def test_spoken_time() -> None:
    assert bf.spoken_time(slot(12, 15)) == "a las tres de la tarde"
    assert bf.spoken_time(slot(12, 13, 30)) == "a la una y media de la tarde"
    assert bf.spoken_time(slot(12, 9, 45)) == "a las diez menos cuarto de la mañana"
    assert bf.spoken_time(slot(12, 12)) == "al mediodía"


def test_spoken_date() -> None:
    assert bf.spoken_date(slot(12, 9)) == "el lunes doce de octubre"
    assert bf.spoken_date(slot(10, 9), today=TODAY) == "mañana"
    assert bf.spoken_date(slot(9, 9), today=TODAY) == "hoy"
    assert bf.spoken_date(slot(1, 9, month=12)) == "el martes primero de diciembre"


def test_offer_max_three_and_natural() -> None:
    many = [slot(12, h) for h in (8, 9, 10, 11, 14, 15, 16)]
    text = bf.offer_slots_text(many, today=TODAY)
    picked = bf.select_slots_to_offer(many)
    assert len(picked) == 3
    assert text.count(" o ") == 1 and text.endswith("¿Cuál prefieres?")
    assert text.startswith("El lunes doce de octubre tengo")
    # variado: incluye manana y tarde
    assert any(to_bogota(p).hour >= 12 for p in picked) and any(
        to_bogota(p).hour < 12 for p in picked
    )


def test_offer_multi_day_and_empty_and_single() -> None:
    text = bf.offer_slots_text([slot(12, 9), slot(13, 15)], today=TODAY)
    assert "lunes doce de octubre a las nueve" in text and "martes trece" in text
    assert "otro día" in bf.offer_slots_text([])
    assert bf.offer_slots_text([slot(10, 9)], today=TODAY).startswith("Tengo mañana")


def test_confirm_text_repeats_date_and_time() -> None:
    out = bf.confirm_text("limpieza dental", slot(12, 15), today=TODAY)
    assert (
        out
        == "Te agendo limpieza dental el lunes doce de octubre a las tres de la tarde. ¿Lo confirmo?"
    )


# ------------------------------------------------------------------ /admin/voz
from sqlalchemy import select  # noqa: E402

from app.db.models.audit import AuditLog  # noqa: E402
from app.db.models.tenants import ChannelAccount, TenantProfile  # noqa: E402
from app.db.models.voice import CallSession  # noqa: E402
from app.voice.admin import admin_router  # noqa: E402


@pytest.fixture
def voz_app(app):
    app.include_router(admin_router)
    return app


async def test_voz_requires_auth(voz_app, client):
    assert (await client.get("/admin/voz")).status_code == 401


async def test_voz_page_lists_calls_and_settings(
    voz_app, authenticated_client, session, make_tenant
):
    t = await make_tenant()
    session.add(ChannelAccount(tenant_id=t.id, channel="voice", phone_e164="+576015550123"))
    session.add(
        CallSession(tenant_id=t.id, call_sid="CAabcdef123456", turns=3, status="transferred")
    )
    await session.commit()
    r = await authenticated_client.get("/admin/voz")
    assert r.status_code == 200
    assert "+576015550123" in r.text and "Transferida" in r.text and "123456" in r.text
    assert "CAabcdef123456" not in r.text
    r = await authenticated_client.get(f"/admin/voz?negocio={t.id}&estado=completed")
    assert r.status_code == 200 and "Sin llamadas" in r.text


async def test_voz_forbidden_for_operator(voz_app, client, login, make_user):
    await login(client, await make_user("operator"))
    assert (await client.get("/admin/voz")).status_code == 403


async def test_voz_save_settings(voz_app, authenticated_client, session, make_tenant):
    t = await make_tenant()
    tid = t.id
    session.add(
        ChannelAccount(
            tenant_id=t.id, channel="voice", phone_e164="+576015550124", voice_enabled=True
        )
    )
    await session.commit()
    r = await authenticated_client.post(
        f"/admin/voz/{t.id}/ajustes", data={"handoff_phone": "+57 300 123 4567"}
    )
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    session.expire_all()
    prof = await session.get(TenantProfile, tid)
    assert prof is not None and prof.handoff_phone == "+573001234567"
    acc = (await session.execute(select(ChannelAccount))).scalars().one()
    assert acc.voice_enabled is False  # checkbox ausente
    assert (
        (await session.execute(select(AuditLog).where(AuditLog.action == "voice.settings_updated")))
        .scalars()
        .first()
    )


async def test_voz_rejects_bad_phone_and_csrf(voz_app, authenticated_client, make_tenant):
    t = await make_tenant()
    r = await authenticated_client.post(f"/admin/voz/{t.id}/ajustes", data={"handoff_phone": "abc"})
    assert r.status_code == 303 and "error=" in r.headers["location"]
    r = await authenticated_client.post(
        f"/admin/voz/{t.id}/ajustes", data={}, headers={"X-CSRF-Token": ""}
    )
    assert r.status_code == 403
