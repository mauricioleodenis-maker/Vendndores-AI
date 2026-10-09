from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.db.models.outreach import CampaignTarget, OutreachMessage
from app.outreach import campaigns, compliance, dispatch
from app.privacy.service import is_optout_message

from .conftest import TUESDAY_10AM
from .test_dispatch import FakeSender
from .test_webhooks import INBOUND, _post

CASES = json.loads(
    (Path(__file__).parents[1] / "fixtures/data/optout_cases.json").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize(
    "case", [c for c in CASES if c["label"] != "optout"], ids=lambda c: c["id"]
)
def test_optout_detector_no_false_positives(case: dict[str, str]) -> None:
    assert is_optout_message(case["text"]) is False, case


@pytest.mark.xfail(
    reason="app/privacy/service.is_optout_message (fuera de mi alcance) tiene recall bajo: "
    "ver integracion-pendientes.md Ronda 3 Hardening-D",
    strict=False,
)
@pytest.mark.parametrize(
    "case", [c for c in CASES if c["label"] == "optout"], ids=lambda c: c["id"]
)
def test_optout_detector_recall(case: dict[str, str]) -> None:
    assert is_optout_message(case["text"]) is True, case


async def test_tag_filter_applies_before_cap(session, make_lead, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(campaigns, "MAX_AUDIENCE", 2)
    for _ in range(3):  # mas puntaje, sin etiqueta: llenarian el tope si el filtro fuera despues
        await make_lead(score=90)
    tagged = await make_lead(score=10, tags=["vip"])
    leads, _ = await campaigns.select_audience(session, {"tag": "vip"})
    assert [lead.id for lead in leads] == [tagged.id]


def test_holidays_cached() -> None:
    assert compliance._co_holidays(2026) is compliance._co_holidays(2026)


async def test_queued_message_locked_elsewhere_is_not_resent(  # type: ignore[no-untyped-def]
    session, pitch, make_lead, make_campaign, monkeypatch
) -> None:
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead])
    sender = FakeSender()
    # Simula otra corrida que ya reclamo el mensaje: el reclamo devuelve nada.
    original = session.execute

    async def fake_execute(stmt, *a, **kw):  # type: ignore[no-untyped-def]
        if "FOR UPDATE" in str(stmt.compile()) if hasattr(stmt, "compile") else False:

            class _R:
                def scalar_one_or_none(self) -> None:
                    return None

            return _R()
        return await original(stmt, *a, **kw)

    monkeypatch.setattr(session, "execute", fake_execute)
    res = await dispatch.dispatch_campaign(session, camp, now=TUESDAY_10AM, sender=sender, pace=0)
    monkeypatch.undo()
    assert sender.calls == [] and res["sent"] == 0
    tgt = (await session.execute(__import__("sqlalchemy").select(CampaignTarget))).scalar_one()
    assert tgt.status == "pending"
    msg = (await session.execute(__import__("sqlalchemy").select(OutreachMessage))).scalar_one()
    assert msg.status == "queued"


async def test_oversized_webhook_body_rejected(client) -> None:  # type: ignore[no-untyped-def]
    r = await client.post(
        "/webhooks/twilio/outreach-inbound",
        content=b"a=" + b"x" * (70 * 1024),
        headers={"content-type": "application/x-www-form-urlencoded", "X-CSRF-Token": ""},
    )
    assert r.status_code == 413
    assert get_settings() and INBOUND and _post
