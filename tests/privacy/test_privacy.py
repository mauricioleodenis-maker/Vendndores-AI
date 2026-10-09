from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.core.clock import utcnow
from app.core.crypto import get_crypto, make_aad, pack_blob, phone_hash
from app.core.errors import AppError
from app.core.jobs import JOB_REGISTRY
from app.db.models.contacts import Consent, Contact
from app.db.models.conversations import Conversation, Message
from app.privacy import dsar, retention, service
from app.privacy.jobs import purge_retention_job
from app.privacy.keywords import is_erase_request, is_rights_request
from app.privacy.texts import privacy_notice

PHONE = "+573001234567"


@pytest.mark.parametrize(
    "text",
    [
        "STOP",
        "stop.",
        " Parar ",
        "BAJA",
        "No más",
        "no mas!",
        "SALIR",
        "Cancelar suscripción",
        "dar de baja",
        "Unsubscribe",
    ],
)
def test_optout_detected(text: str) -> None:
    assert service.is_optout_message(text)


@pytest.mark.parametrize(
    "text",
    [
        "cancelar",
        "Cancelar",
        "quiero cancelar mi cita",
        "hola",
        "",
        "no puedo ir",
        "para el martes",
    ],
)
def test_not_optout(text: str) -> None:
    assert not service.is_optout_message(text)


def test_optin_and_requests() -> None:
    assert service.is_optin_message("START") and service.is_optin_message("Sí quiero")
    assert not service.is_optin_message("si")
    assert is_erase_request("Borrar mis datos") and is_rights_request("DERECHOS")


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        ("mi mail a@b.co", "a@b.co"),
        ("llama al +57 300 123 4567", "300"),
        ("cc 1.234.567.890", "1.234"),
        ("sufro de diabetes tipo 2", "diabetes"),
        ("tarjeta 4111111111111111", "4111"),
    ],
)
def test_redact_pii(raw: str, needle: str) -> None:
    assert needle not in service.redact_pii(raw)
    assert service.redact_pii("") == ""
    assert service.redact_pii("Hola, quiero una cita") == "Hola, quiero una cita"


async def _contact(session: Any, tenant: Any, phone: str = PHONE) -> Contact:
    c = Contact(
        tenant_id=tenant.id,
        phone_hash=phone_hash(phone),
        phone_enc=pack_blob(
            get_crypto().encrypt_str(phone, aad=make_aad("contacts", tenant.id, "phone_enc"))
        ),
    )
    session.add(c)
    await session.commit()
    return c


async def test_optout_optin_cycle(session: Any, tenant: Any) -> None:
    c = await _contact(session, tenant)
    await service.record_consent(session, tenant.id, c.id, "recordatorios", "SI")
    assert not await service.is_suppressed(session, PHONE)
    await service.apply_optout(session, phone_e164=PHONE, tenant_id=tenant.id, evidence="STOP")
    await service.apply_optout(session, phone_e164=PHONE, tenant_id=tenant.id, evidence="STOP")
    await session.commit()
    assert await service.is_suppressed(session, PHONE)
    assert await service.is_suppressed(session, PHONE, tenant_id=tenant.id)
    assert not await service.is_suppressed(session, PHONE, tenant_id=uuid.uuid4())
    await session.refresh(c)
    assert c.opted_out and c.opt_out_source == "keyword"
    assert not await service.has_consent(session, tenant.id, c.id, "recordatorios")
    assert await service.apply_optin(
        session, phone_e164=PHONE, tenant_id=tenant.id, evidence="START"
    )
    await session.commit()
    await session.refresh(c)
    assert not c.opted_out
    assert not await service.is_suppressed(session, PHONE)
    assert not await service.apply_optin(
        session, phone_e164=PHONE, tenant_id=tenant.id, evidence="x"
    )


async def test_global_optout_and_complaint_not_lifted(session: Any) -> None:
    await service.apply_optout(session, phone_e164=PHONE, evidence="STOP")
    await session.commit()
    assert await service.is_suppressed(session, PHONE, tenant_id=uuid.uuid4())
    from app.db.models.privacy import SuppressionEntry

    row = (await session.execute(select(SuppressionEntry))).scalar_one()
    row.reason = "complaint"
    await session.commit()
    assert not await service.apply_optin(session, phone_e164=PHONE, evidence="START")
    assert await service.is_suppressed(session, PHONE)


async def test_consents(session: Any, tenant: Any) -> None:
    c = await _contact(session, tenant)
    await service.record_consent(session, tenant.id, c.id, "atencion", "texto exacto")
    await service.record_consent(session, tenant.id, c.id, "atencion", "texto exacto")
    rows = (await session.execute(select(Consent))).scalars().all()
    assert len(rows) == 1 and rows[0].policy_version == "1.0"
    assert await service.revoke_consent(session, tenant.id, c.id, "atencion") == 1
    await service.record_consent(session, tenant.id, c.id, "atencion", "otra vez")
    assert await service.has_consent(session, tenant.id, c.id, "atencion")
    with pytest.raises(AppError):
        await service.record_consent(session, tenant.id, c.id, "spam", "x")


def test_notice_texts() -> None:
    t = privacy_notice("atencion", negocio="Clínica X", agencia="Agencia Y", url="http://u")
    assert "Clínica X" in t and "STOP" in t and "http://u" in t
    assert "Clínica X" in privacy_notice("marketing", negocio="Clínica X")
    assert "recordatorios" in privacy_notice("recordatorios")
    with pytest.raises(ValueError):
        privacy_notice("otro")


async def _conv_with_message(
    session: Any, tenant: Any, c: Contact, text: str = "Mi cédula es 123"
) -> Message:
    conv = Conversation(tenant_id=tenant.id, contact_id=c.id, summary="resumen con PII")
    session.add(conv)
    await session.flush()
    blob = pack_blob(
        get_crypto().encrypt_str(text, aad=make_aad("messages", tenant.id, "body_enc"))
    )
    m = Message(
        tenant_id=tenant.id,
        conversation_id=conv.id,
        direction="in",
        body_enc=blob,
        body_redacted="red",
    )
    session.add(m)
    await session.commit()
    return m


async def test_export_and_erase(session: Any, tenant: Any) -> None:
    c = await _contact(session, tenant)
    m = await _conv_with_message(session, tenant, c)
    await service.record_consent(session, tenant.id, c.id, "atencion", "SI")
    data = await dsar.export_contact_data(session, tenant.id, c.id)
    assert data["contacto"]["telefono"] == PHONE
    assert data["conversaciones"][0]["mensajes"][0]["texto"] == "Mi cédula es 123"
    assert data["consentimientos"][0]["finalidad"] == "atencion"
    await dsar.erase_contact(session, tenant.id, c.id)
    await session.commit()
    await session.refresh(c)
    await session.refresh(m)
    assert c.phone_enc is None and c.erased_at and c.phone_hash.startswith("erased:")
    assert m.body_enc is None and m.body_redacted == dsar.ERASED_BODY
    after = await dsar.export_contact_data(session, tenant.id, c.id)
    assert after["contacto"]["telefono"] is None


async def test_dsar_wrong_tenant(session: Any, tenant: Any, make_tenant: Any) -> None:
    c = await _contact(session, tenant)
    other = await make_tenant()
    with pytest.raises(AppError):
        await dsar.export_contact_data(session, other.id, c.id)
    with pytest.raises(AppError):
        await dsar.erase_contact(session, other.id, uuid.uuid4())


async def test_retention(session: Any, tenant: Any) -> None:
    c = await _contact(session, tenant)
    old = await _conv_with_message(session, tenant, c)
    old.purge_after = (utcnow() - timedelta(days=1)).date()
    fresh = await _conv_with_message(session, tenant, c)
    fresh.purge_after = (utcnow() + timedelta(days=5)).date()
    await session.commit()
    assert await retention.purge_expired_messages(session) == 1
    assert await retention.purge_expired_messages(session) == 0
    await session.commit()
    await session.refresh(old)
    await session.refresh(fresh)
    assert old.body_enc is None and old.body_redacted == retention.PURGED_BODY
    assert fresh.body_enc is not None
    assert retention.default_purge_after() > utcnow().date()


async def test_backfill_and_job(session: Any, tenant: Any, engine: Any) -> None:
    c = await _contact(session, tenant)
    m = await _conv_with_message(session, tenant, c)
    m.created_at = utcnow() - timedelta(days=400)
    await session.commit()
    assert await retention.backfill_purge_after(session) == 1
    await session.commit()
    assert "privacy.purge_retention" in JOB_REGISTRY
    assert await purge_retention_job({}) >= 1
    await session.refresh(m)
    assert m.body_enc is None


async def test_public_pages(client: Any, tenant: Any) -> None:
    r = await client.get("/privacidad")
    assert r.status_code == 200 and "Borrador para revisión legal" in r.text
    assert "Derechos" in r.text or "derechos" in r.text
    r = await client.get(f"/privacidad/{tenant.slug}")
    assert r.status_code == 200 and "Clinica Demo" in r.text
    r = await client.get("/privacidad/no-existe", headers={"accept": "text/html"})
    assert r.status_code == 404


async def test_dsar_api(authenticated_client: Any, session: Any, tenant: Any) -> None:
    c = await _contact(session, tenant)
    base = f"/api/privacidad/{tenant.id}/contactos/{c.id}"
    r = await authenticated_client.get(base + "/exportar")
    assert r.status_code == 200 and r.json()["contacto"]["telefono"] == PHONE
    assert r.headers["cache-control"] == "no-store"
    r = await authenticated_client.post(base + "/borrar")
    assert r.status_code == 200
    r = await authenticated_client.post(base + "/borrar", headers={"X-CSRF-Token": ""})
    assert r.status_code in (400, 403)


async def test_dsar_api_requires_auth(client: Any, tenant: Any) -> None:
    r = await client.get(f"/api/privacidad/{tenant.id}/contactos/{uuid.uuid4()}/exportar")
    assert r.status_code == 401
