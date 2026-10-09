from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models.leads import Lead, LeadEvent, SecretShopTest
from app.leads import secret_shop as ss
from tests.leads.test_sales_pipeline import make_lead

NOW = datetime(2026, 3, 10, 17, 0, tzinfo=UTC)  # martes 12:00 Bogota


def test_suggested_script() -> None:
    assert "limpieza dental" in ss.suggested_script("precio", "dentista")
    assert "sus servicios" in ss.suggested_script("cita", "otro")
    with pytest.raises(AppError):
        ss.suggested_script("nada", "dentista")


def test_after_hours_default() -> None:
    assert not ss.is_after_hours(datetime(2026, 3, 10, 17, 0, tzinfo=UTC))  # 12:00 martes
    assert ss.is_after_hours(datetime(2026, 3, 11, 2, 0, tzinfo=UTC))  # 21:00
    assert ss.is_after_hours(datetime(2026, 3, 15, 17, 0, tzinfo=UTC))  # domingo
    assert not ss.is_after_hours(datetime(2026, 3, 14, 17, 0, tzinfo=UTC))  # sabado


def test_after_hours_places_periods() -> None:
    hours = {
        "periods": [
            {"open": {"day": 2, "hour": 9}, "close": {"day": 2, "hour": 12}},
            {"open": {"day": 4}, "close": {"day": 5}, "bad": 1},
            {"broken": True},
        ]
    }
    assert ss.is_after_hours(datetime(2026, 3, 10, 17, 0, tzinfo=UTC), hours)  # 12:00 martes
    assert not ss.is_after_hours(datetime(2026, 3, 10, 15, 0, tzinfo=UTC), hours)  # 10:00
    assert ss.is_after_hours(datetime(2026, 3, 11, 17, 0, tzinfo=UTC), hours)  # miercoles
    always = {"periods": [{"open": {"day": 0, "hour": 0}}]}  # Places: abierto 24/7 sin close
    assert not ss.is_after_hours(datetime(2026, 3, 11, 5, 0, tzinfo=UTC), always)
    assert ss.is_after_hours(datetime(2026, 3, 9, 17, 0, tzinfo=UTC), {"periods": [{"x": 1}]})


def test_classify_outcome() -> None:
    assert ss.classify_outcome(None) == "sin_respuesta"
    assert ss.classify_outcome(60) == "respuesta_ok"
    assert ss.classify_outcome(60, closed_booking=True) == "respuesta_con_cierre"
    assert ss.classify_outcome(16 * 60) == "respuesta_lenta"


@freeze_time(NOW)
async def test_create_test_moves_stage(session: AsyncSession, owner_user: Any) -> None:
    lead = await make_lead(session)
    test = await ss.create_test(session, lead.id, actor=owner_user, scenario="cita")
    assert lead.stage == "prueba_secreta"
    assert test.message_text and not test.after_hours
    kinds = {e.kind for e in (await session.execute(select(LeadEvent))).scalars()}
    assert {"secret_shop_sent", "stage_changed"} <= kinds
    with pytest.raises(ConflictError):
        await ss.create_test(session, lead.id, actor=owner_user)


@freeze_time(NOW)
async def test_create_test_validations(session: AsyncSession, owner_user: Any) -> None:
    lead = await make_lead(session)
    with pytest.raises(AppError):
        await ss.create_test(session, lead.id, actor=owner_user, scenario="x")
    with pytest.raises(AppError):
        await ss.create_test(session, lead.id, actor=owner_user, channel="x")
    with pytest.raises(AppError):
        await ss.create_test(session, lead.id, actor=owner_user, sent_at=NOW + timedelta(hours=1))
    blocked = await make_lead(session, name="Otro", disposition="no_contactar")
    with pytest.raises(ConflictError):
        await ss.create_test(session, blocked.id, actor=owner_user)


@freeze_time(NOW)
async def test_record_reply_scores_and_outcome(session: AsyncSession, owner_user: Any) -> None:
    lead = await make_lead(session, phone_e164="+573001112233", phone_type="mobile")
    test = await ss.create_test(
        session, lead.id, actor=owner_user, sent_at=NOW - timedelta(hours=1)
    )
    done = await ss.record_reply(
        session,
        test.id,
        first_reply_at=NOW - timedelta(minutes=10),
        excerpt="Hola, escríbame a correo@x.com " + "a" * 400,
        actor=owner_user,
    )
    assert done.response_seconds == 50 * 60
    assert done.outcome == "respuesta_lenta"
    assert len(done.first_reply_excerpt) <= 280 and "correo@x.com" not in done.first_reply_excerpt
    assert lead.score >= 25  # senal fuerte de prueba lenta
    assert "prueba_secreta" in lead.score_breakdown
    with pytest.raises(ConflictError):
        await ss.record_reply(session, test.id, first_reply_at=NOW, actor=owner_user)


@freeze_time(NOW)
async def test_record_reply_validations(session: AsyncSession, owner_user: Any) -> None:
    lead = await make_lead(session)
    test = await ss.create_test(
        session, lead.id, actor=owner_user, sent_at=NOW - timedelta(hours=1)
    )
    with pytest.raises(AppError):
        await ss.record_reply(
            session, test.id, first_reply_at=NOW - timedelta(hours=2), actor=owner_user
        )
    with pytest.raises(AppError):
        await ss.record_reply(
            session, test.id, first_reply_at=NOW + timedelta(hours=2), actor=owner_user
        )
    import uuid

    with pytest.raises(NotFoundError):
        await ss.record_reply(session, uuid.uuid4(), first_reply_at=NOW, actor=owner_user)


async def test_timeout_job(session: AsyncSession, owner_user: Any, engine: Any) -> None:
    lead = await make_lead(session, phone_e164="+573001112233", phone_type="mobile")
    with freeze_time(NOW - timedelta(hours=25)):
        old = await ss.create_test(
            session, lead.id, actor=owner_user, sent_at=NOW - timedelta(hours=25)
        )
    other = await make_lead(session, name="Reciente")
    with freeze_time(NOW):
        fresh = await ss.create_test(session, other.id, actor=owner_user, sent_at=NOW)
        assert [
            t.id for t in await ss.pending_reviews(session, now=NOW + timedelta(minutes=20))
        ] == [
            old.id,
            fresh.id,
        ]
        assert await ss.expire_unanswered(session, now=NOW) == 1
        assert old.outcome == "sin_respuesta" and fresh.outcome is None
        assert "prueba_secreta" in lead.score_breakdown
        assert await ss.expire_unanswered(session, now=NOW) == 0


async def test_timeout_job_entrypoint(session: AsyncSession, owner_user: Any, engine: Any) -> None:
    from app.db.session import configure_database

    configure_database(engine)
    lead = await make_lead(session)
    await ss.create_test(
        session, lead.id, actor=owner_user, sent_at=datetime.now(UTC) - timedelta(hours=30)
    )
    await session.commit()
    assert await ss.secret_shop_timeout_job({}) == 1
    await session.refresh(lead)
    row = (await session.execute(select(SecretShopTest))).scalar_one()
    assert row.outcome == "sin_respuesta"


@freeze_time(NOW)
async def test_pitch_evidence(session: AsyncSession, owner_user: Any) -> None:
    lead = await make_lead(session)
    assert await ss.pitch_evidence(session, lead.id) == {"available": False}
    test = await ss.create_test(
        session, lead.id, actor=owner_user, sent_at=NOW - timedelta(hours=3)
    )
    ev = await ss.pitch_evidence(session, lead.id)
    assert ev["available"] and not ev["answered"] and ev["response_minutes"] is None
    assert ev["sent_at_local"] == "9:00 am"
    await ss.record_reply(
        session, test.id, first_reply_at=NOW - timedelta(hours=1), actor=owner_user
    )
    ev = await ss.pitch_evidence(session, lead.id)
    assert ev["response_minutes"] == 120 and ev["outcome"] == "respuesta_lenta"


async def test_lead_unused_import_guard() -> None:
    assert Lead.__tablename__ == "leads"
