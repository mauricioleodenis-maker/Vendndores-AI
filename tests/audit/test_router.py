from app.audit import log_event
from app.audit.service import list_events


async def test_owner_sees_audit_page_with_filters(authenticated_client, session):
    await log_event(
        session, actor="system", action="tenant.create", entity_type="tenant", entity_id="t1"
    )
    await log_event(session, actor="system", action="lead.export")
    await session.commit()
    c = authenticated_client
    page = await c.get("/admin/auditoria")
    assert page.status_code == 200 and "tenant.create" in page.text and "lead.export" in page.text
    filtered = await c.get("/admin/auditoria?action=lead.")
    assert "lead.export" in filtered.text and "tenant.create" not in filtered.text
    assert (await c.get("/admin/auditoria?tenant_id=no-es-uuid")).status_code == 200


async def test_verify_chain_endpoint(authenticated_client, session):
    await log_event(session, actor="system", action="a")
    await session.commit()
    r = await authenticated_client.post("/admin/auditoria/verificar")
    assert "Cadena íntegra" in r.text
    from sqlalchemy import text

    await session.execute(text("UPDATE audit_log SET action='x' WHERE id=1"))
    await session.commit()
    r = await authenticated_client.post("/admin/auditoria/verificar")
    assert "alterada" in r.text


async def test_non_owner_forbidden(client, make_user, login):
    admin = await make_user("admin")
    await login(client, admin)
    assert (await client.get("/admin/auditoria")).status_code == 403
    assert (await client.post("/admin/auditoria/verificar")).status_code == 403


async def test_pagination(authenticated_client, session):
    for i in range(55):
        await log_event(session, actor="system", action=f"bulk.{i:02d}")
    await session.commit()
    p1 = await authenticated_client.get("/admin/auditoria")
    assert "Siguiente" in p1.text
    p2 = await authenticated_client.get("/admin/auditoria?page=2")
    assert "Anterior" in p2.text
    events = await list_events(session, limit=100)
    assert len(events) >= 55


async def test_invalid_tenant_filter_shows_notice_and_no_rows(authenticated_client, session):
    await log_event(session, actor="system", action="visible.event")
    await session.commit()
    page = await authenticated_client.get("/admin/auditoria?tenant_id=no-es-uuid")
    assert "ID de empresa no válido" in page.text and "visible.event" not in page.text
