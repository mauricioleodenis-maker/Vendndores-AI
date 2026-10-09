import uuid

from sqlalchemy import select, text

from app.audit import log_event, verify_chain
from app.audit.service import sanitize_diff
from app.db.models.audit import AuditLog


async def test_chain_links_and_verifies(session):
    e1 = await log_event(session, actor="system", action="a.one", entity_type="x", entity_id=1)
    e2 = await log_event(session, actor="webhook", action="a.two")
    e3 = await log_event(session, actor=uuid.uuid4(), action="a.three", tenant_id=uuid.uuid4())
    assert e1.prev_hash == "" and e2.prev_hash == e1.hash and e3.prev_hash == e2.hash
    assert len({e1.hash, e2.hash, e3.hash}) == 3
    assert e3.actor_type == "user" and e2.actor_type == "webhook" and e1.entity_id == "1"
    await session.commit()
    assert await verify_chain(session) == (True, None)


async def test_user_actor_and_default_system(session, owner_user):
    e = await log_event(session, actor=owner_user, action="t.x")
    assert e.actor_user_id == owner_user.id and e.actor_type == "user"
    assert (await log_event(session, actor=None, action="t.y")).actor_type == "system"


async def test_tampering_detected(session):
    for i in range(3):
        await log_event(session, actor="system", action=f"a.{i}", diff={"campos": ["x"]})
    await session.commit()
    await session.execute(text("UPDATE audit_log SET action='falsificado' WHERE id=2"))
    await session.commit()
    ok, bad = await verify_chain(session)
    assert ok is False and bad == 2


async def test_deletion_detected(session):
    for i in range(3):
        await log_event(session, actor="system", action=f"a.{i}")
    await session.commit()
    await session.execute(text("DELETE FROM audit_log WHERE id=2"))
    await session.commit()
    ok, bad = await verify_chain(session)
    assert ok is False and bad == 3


async def test_diff_never_stores_pii_or_secrets(session):
    e = await log_event(
        session,
        actor="system",
        action="secret.update",
        diff={
            "password": "hunter2",
            "api_key": "sk-123",
            "phone": "+573001112233",
            "fields": ["nombre", "telefono"],
            "note": "contacto juan@correo.com y 300 111 2233",
            "nested": {"token": "abc", "ok": 3},
            "role": "admin",
        },
    )
    d = e.diff
    assert d["password"] == d["api_key"] == d["phone"] == "[redacted]"
    assert d["nested"] == {"token": "[redacted]", "ok": 3}
    assert d["fields"] == ["nombre", "telefono"] and d["role"] == "admin"
    assert "juan@" not in d["note"] and "300 111" not in d["note"]
    assert sanitize_diff(5) == 5 and sanitize_diff(["a@b.co"]) == ["[email]"]


async def test_empty_chain_is_valid(session):
    assert await verify_chain(session) == (True, None)


async def test_ids_increment_and_order(session):
    a = await log_event(session, actor="system", action="a")
    b = await log_event(session, actor="system", action="b")
    assert b.id > a.id
    rows = (await session.execute(select(AuditLog.action).order_by(AuditLog.id))).scalars().all()
    assert rows == ["a", "b"]
