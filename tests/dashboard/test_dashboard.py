from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.channels.service import encrypt_phone
from app.conversation.memory import encrypt_body
from app.core.clock import utcnow
from app.core.crypto import phone_hash
from app.dashboard import service
from app.db.models.audit import AuditLog
from app.db.models.booking import Appointment
from app.db.models.contacts import Contact
from app.db.models.conversations import Conversation, Handoff, Message
from app.db.models.leads import Lead
from app.db.models.plans import Plan, Subscription

pytestmark = pytest.mark.asyncio

PHONE = "+573001234567"


async def _conv(
    session, tenant, *, inbound_ago=timedelta(minutes=5), status="open", channel="whatsapp"
):
    contact = (
        await session.execute(select(Contact).where(Contact.tenant_id == tenant.id))
    ).scalar_one_or_none()
    if contact is None:
        contact = Contact(
            tenant_id=tenant.id,
            phone_hash=phone_hash(PHONE),
            phone_enc=encrypt_phone(tenant.id, PHONE),
        )
        session.add(contact)
        await session.flush()
    now = utcnow()
    conv = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        channel=channel,
        status=status,
        last_message_at=now,
        last_inbound_at=now - inbound_ago,
    )
    session.add(conv)
    await session.flush()
    session.add(
        Message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            direction="in",
            role="user",
            body_enc=encrypt_body(tenant.id, "Hola, quiero una cita"),
            body_redacted="Hola, quiero una cita",
            status="received",
        )
    )
    await session.commit()
    return conv


async def test_kpis(session, make_tenant):
    t = await make_tenant()
    await make_tenant(is_demo=True)
    plan = Plan(code="basico", name="B", setup_fee_cop=1, monthly_fee_cop=250_000)
    session.add(plan)
    await session.flush()
    session.add(Subscription(tenant_id=t.id, plan_id=plan.id, status="active"))
    session.add(Lead(name="Hot", score=85))
    session.add(Lead(name="Cold", score=10))
    await _conv(session, t)
    await _conv(session, t, channel="sandbox")
    session.add(
        Appointment(
            tenant_id=t.id,
            starts_at=utcnow() + timedelta(days=1),
            ends_at=utcnow() + timedelta(days=1, hours=1),
            status="confirmed",
            source="bot",
        )
    )
    await session.commit()
    k = await service.compute_kpis(session, include_billing=True)
    assert (k.active_tenants, k.conversations_month, k.bot_appointments_month) == (1, 1, 1)
    assert (k.hot_leads, k.demos, k.mrr_cop) == (1, 1, 250_000)
    assert (await service.compute_kpis(session, include_billing=False)).mrr_cop is None


async def test_list_filters_and_masking(session, tenant):
    conv = await _conv(session, tenant)
    rows, more = await service.list_conversations(session)
    assert len(rows) == 1 and not more and rows[0].phone == PHONE
    assert rows[0].preview.startswith("Hola")
    assert (await service.list_conversations(session, status="closed"))[0] == []
    assert len((await service.list_conversations(session, q="cita"))[0]) == 1
    assert (await service.list_conversations(session, q="zzz"))[0] == []
    assert (await service.list_conversations(session, q="%"))[0] == []
    assert conv.id == rows[0].id


async def test_take_control_reply_return(session, tenant, owner_user):
    conv = await _conv(session, tenant)
    with pytest.raises(Exception, match="control"):
        await service.send_manual_reply(session, conv, "hola", actor=owner_user)
    await service.take_control(session, conv, actor=owner_user)
    assert conv.status == "handoff" and conv.bot_paused_until is None
    handoff = (await session.execute(select(Handoff))).scalar_one()
    assert handoff.status == "claimed" and handoff.claimed_by == owner_user.id
    # el envio esta en dry-run: no toca la red
    msg = await service.send_manual_reply(session, conv, "  Con gusto  ", actor=owner_user)
    assert msg.role == "human_agent"
    thread = await service.load_thread(session, conv, actor=owner_user)
    assert [m.body for m in thread] == ["Hola, quiero una cita", "Con gusto"]
    await service.return_to_bot(session, conv, actor=owner_user)
    assert conv.status == "open"
    await session.refresh(handoff)
    assert handoff.status == "resolved"
    actions = {a for a in (await session.execute(select(AuditLog.action))).scalars()}
    assert {
        "conversation.view",
        "conversation.take_control",
        "conversation.return_to_bot",
    } <= actions


async def test_reply_outside_window(session, tenant, owner_user):
    conv = await _conv(session, tenant, inbound_ago=timedelta(hours=30))
    await service.take_control(session, conv, actor=owner_user)
    assert not service.window_open(conv)
    with pytest.raises(Exception, match="24 h"):
        await service.send_manual_reply(session, conv, "hola", actor=owner_user)
    with pytest.raises(Exception):
        await service.send_manual_reply(session, conv, "x" * 2000, actor=owner_user)


async def test_appointments_overview(session, make_tenant):
    t = await make_tenant(name="Clinica A")
    now = utcnow()
    for days, status in ((1, "confirmed"), (2, "cancelled"), (30, "confirmed")):
        session.add(
            Appointment(
                tenant_id=t.id,
                starts_at=now + timedelta(days=days),
                ends_at=now + timedelta(days=days, hours=1),
                status=status,
                source="manual",
            )
        )
    await session.commit()
    rows, per = await service.appointments_overview(session, days=7)
    assert len(rows) == 1 and per == {"Clinica A": 1}


async def test_pages(authenticated_client, session, tenant):
    conv = await _conv(session, tenant)
    c = authenticated_client
    inicio = await c.get("/admin/inicio")
    assert inicio.status_code == 200 and "Empresas activas" in inicio.text and "MRR" in inicio.text
    assert (await c.get("/admin/inicio/kpis")).status_code == 200
    lst = await c.get("/admin/conversaciones?estado=open&q=cita")
    assert lst.status_code == 200 and "Clinica Demo" in lst.text and "3001234567" not in lst.text
    det = await c.get(f"/admin/conversaciones/{conv.id}")
    assert det.status_code == 200 and "Hola, quiero una cita" in det.text
    r = await c.post(f"/admin/conversaciones/{conv.id}/tomar", follow_redirects=False)
    assert r.status_code == 303
    r = await c.post(
        f"/admin/conversaciones/{conv.id}/responder",
        data={"mensaje": "Listo"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = await c.post(f"/admin/conversaciones/{conv.id}/devolver", follow_redirects=False)
    assert r.status_code == 303
    assert (await c.get("/admin/citas/resumen")).status_code == 200
    import uuid

    assert (await c.get(f"/admin/conversaciones/{uuid.uuid4()}")).status_code == 404


async def test_operator_sees_transcript_not_billing(client, make_user, login, session, tenant):
    conv = await _conv(session, tenant)
    await login(client, await make_user("operator"))
    page = await client.get("/admin/inicio")
    assert page.status_code == 200 and "MRR" not in page.text
    assert (await client.get(f"/admin/conversaciones/{conv.id}")).status_code == 200


async def test_requires_login_and_csrf(client, session, tenant):
    conv = await _conv(session, tenant)
    assert (await client.get("/admin/inicio")).status_code in (401, 303, 307)
    assert (await client.post(f"/admin/conversaciones/{conv.id}/tomar")).status_code in (401, 403)
