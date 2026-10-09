from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select

from app.channels.sender import sign_twilio
from app.core.crypto import phone_hash
from app.db.models.leads import Lead
from app.db.models.outreach import CampaignTarget, OutreachMessage
from app.db.models.privacy import SuppressionEntry

from .conftest import PLATFORM_TOKEN, TUESDAY_10AM

INBOUND = "http://test/webhooks/twilio/outreach-inbound"
STATUS = "http://test/webhooks/twilio/outreach-status"
GENERIC_STATUS = "http://test/webhooks/twilio/status"


async def _post(
    client: httpx.AsyncClient, url: str, params: dict[str, str], *, sig: str | None = None
) -> httpx.Response:
    sig = sig if sig is not None else sign_twilio(url, params, PLATFORM_TOKEN)
    path = url.removeprefix("http://test")
    return await client.post(
        path, data=params, headers={"X-Twilio-Signature": sig, "X-CSRF-Token": ""}
    )


async def _sent_message(session, camp, lead, *, sid="SM1", status="sent"):  # type: ignore[no-untyped-def]
    msg = OutreachMessage(
        campaign_id=camp.id,
        lead_id=lead.id,
        step=1,
        status=status,
        provider_sid=sid,
        sent_at=TUESDAY_10AM,
        idempotency_key=f"{camp.id}:{lead.id}:1",
    )
    session.add(msg)
    await session.commit()
    return msg


async def test_bad_signature_forbidden(client) -> None:  # type: ignore[no-untyped-def]
    params = {"From": "whatsapp:+573001230001", "Body": "STOP", "MessageSid": "SMx"}
    r = await _post(client, INBOUND, params, sig="bogus")
    assert r.status_code == 403
    r = await client.post("/webhooks/twilio/outreach-status", data=params)
    assert r.status_code == 403


async def test_stop_creates_suppression_and_cancels_queue(
    client, session, pitch, make_lead, make_campaign
) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead(phone_e164="+573001230001")
    other = await make_lead()
    camp = await make_campaign(pitch, [lead, other])
    session.add(
        OutreachMessage(
            campaign_id=camp.id, lead_id=lead.id, step=1, status="queued", idempotency_key="qq"
        )
    )
    await session.commit()
    r = await _post(
        client, INBOUND, {"From": "whatsapp:+573001230001", "Body": " Stop ", "MessageSid": "SM9"}
    )
    assert r.status_code == 200 and "<Message>" in r.text
    entry = (await session.execute(select(SuppressionEntry))).scalar_one()
    assert entry.phone_hash == phone_hash("+573001230001") and entry.scope == "global"
    refreshed = await session.get(Lead, lead.id, populate_existing=True)
    assert refreshed.disposition == "no_contactar"
    msg = (await session.execute(select(OutreachMessage))).scalar_one()
    assert msg.status == "skipped"
    targets = {
        t.lead_id: t.status for t in (await session.execute(select(CampaignTarget))).scalars()
    }
    assert targets[lead.id] == "cancelled" and targets[other.id] == "pending"
    # reintento de Twilio: idempotente, sin segunda respuesta
    r2 = await _post(
        client, INBOUND, {"From": "whatsapp:+573001230001", "Body": " Stop ", "MessageSid": "SM9"}
    )
    assert r2.status_code == 200 and "<Message>" not in r2.text


async def test_stop_from_unknown_number_still_suppressed(client, session) -> None:  # type: ignore[no-untyped-def]
    r = await _post(
        client, INBOUND, {"From": "whatsapp:+573119998877", "Body": "BAJA", "MessageSid": "SM10"}
    )
    assert r.status_code == 200
    assert (await session.execute(select(SuppressionEntry))).scalar_one().phone_hash == phone_hash(
        "+573119998877"
    )


async def test_reply_marks_replied_and_promotes_stage(
    client, session, pitch, make_lead, make_campaign
) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead(phone_e164="+573001230002")
    camp = await make_campaign(pitch, [lead])
    msg = await _sent_message(session, camp, lead)
    target = (await session.execute(select(CampaignTarget))).scalar_one()
    target.status = "sent"
    await session.commit()
    r = await _post(
        client,
        INBOUND,
        {"From": "whatsapp:+573001230002", "Body": "Si, me interesa", "MessageSid": "SM11"},
    )
    assert r.status_code == 200 and "<Message>" not in r.text
    assert (await session.get(OutreachMessage, msg.id, populate_existing=True)).status == "replied"
    assert (
        await session.get(OutreachMessage, msg.id, populate_existing=True)
    ).replied_at is not None
    assert (await session.get(Lead, lead.id, populate_existing=True)).stage == "contactado"
    assert (await session.get(CampaignTarget, target.id, populate_existing=True)).status == "done"
    assert (await session.execute(select(SuppressionEntry))).first() is None


async def test_reply_from_unrelated_number_ignored(client, session) -> None:  # type: ignore[no-untyped-def]
    r = await _post(
        client, INBOUND, {"From": "whatsapp:+573005550000", "Body": "hola", "MessageSid": "SM12"}
    )
    assert r.status_code == 200


async def test_status_progression_is_monotonic(
    client, session, pitch, make_lead, make_campaign
) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead])
    msg = await _sent_message(session, camp, lead, sid="SMabc")

    async def status(value: str, **extra: str) -> None:
        r = await _post(client, STATUS, {"MessageSid": "SMabc", "MessageStatus": value, **extra})
        assert r.status_code == 200

    await status("delivered")
    assert (
        await session.get(OutreachMessage, msg.id, populate_existing=True)
    ).status == "delivered"
    await status("sent")  # retroceso ignorado
    assert (
        await session.get(OutreachMessage, msg.id, populate_existing=True)
    ).status == "delivered"
    await status("read")
    assert (await session.get(OutreachMessage, msg.id, populate_existing=True)).status == "read"


async def test_status_failed_with_code(client, session, pitch, make_lead, make_campaign) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead])
    msg = await _sent_message(session, camp, lead, sid="SMfail")
    await _post(
        client,
        STATUS,
        {"MessageSid": "SMfail", "MessageStatus": "undelivered", "ErrorCode": "63024"},
    )
    got = await session.get(OutreachMessage, msg.id, populate_existing=True)
    assert got.status == "failed" and got.delivery_error_code == "63024"


async def test_status_optout_error_suppresses(
    client, session, pitch, make_lead, make_campaign
) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead(phone_e164="+573001230077")
    camp = await make_campaign(pitch, [lead])
    await _sent_message(session, camp, lead, sid="SMopt")
    await _post(
        client, STATUS, {"MessageSid": "SMopt", "MessageStatus": "failed", "ErrorCode": "63032"}
    )
    assert (await session.get(Lead, lead.id, populate_existing=True)).disposition == "no_contactar"
    assert (await session.execute(select(SuppressionEntry))).scalar_one()


async def test_generic_status_callback_reaches_outreach(
    client, session, pitch, make_lead, make_campaign
) -> None:  # type: ignore[no-untyped-def]
    lead = await make_lead()
    camp = await make_campaign(pitch, [lead])
    msg = await _sent_message(session, camp, lead, sid="SMgen")
    r = await _post(
        client,
        GENERIC_STATUS,
        {"MessageSid": "SMgen", "MessageStatus": "delivered", "From": "whatsapp:+15550001111"},
    )
    assert r.status_code == 200
    assert (
        await session.get(OutreachMessage, msg.id, populate_existing=True)
    ).status == "delivered"


async def test_unknown_sid_and_status_are_noops(client) -> None:  # type: ignore[no-untyped-def]
    r = await _post(client, STATUS, {"MessageSid": "SMnone", "MessageStatus": "delivered"})
    assert r.status_code == 200
    r = await _post(client, STATUS, {"MessageSid": "SMnone", "MessageStatus": "weird"})
    assert r.status_code == 200


def _unused(_: Any, __: timedelta) -> None: ...
