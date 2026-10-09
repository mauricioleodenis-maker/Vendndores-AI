from __future__ import annotations

from sqlalchemy import select

from app.db.models.outreach import Campaign

from .conftest import CONTENT_SID


async def _tpl_id(client, name: str) -> str:  # type: ignore[no-untyped-def]
    r = await client.get("/api/outreach/templates")
    assert r.status_code == 200
    return next(t["id"] for t in r.json() if t["name"] == name)


async def test_requires_auth(client) -> None:  # type: ignore[no-untyped-def]
    assert (await client.get("/api/outreach/templates")).status_code in (401, 403)
    assert (await client.get("/admin/campanas", follow_redirects=False)).status_code in (
        302,
        303,
        401,
        403,
    )


async def test_templates_api_and_approval(authenticated_client) -> None:  # type: ignore[no-untyped-def]
    c = authenticated_client
    tid = await _tpl_id(c, "lead_contacto_inicial")
    r = await c.post(f"/api/outreach/templates/{tid}/approval", json={"content_sid": "nope"})
    assert r.status_code == 422
    r = await c.post(f"/api/outreach/templates/{tid}/approval", json={"content_sid": CONTENT_SID})
    assert r.status_code == 200 and r.json()["approved"] is True
    r = await c.post(
        "/api/outreach/templates",
        json={
            "name": "mi_plantilla",
            "kind": "pitch",
            "body": "Hola {{1}} somos {{2}} gracias",
            "variables": ["contact_name", "agency"],
        },
    )
    assert r.status_code == 201 and r.json()["version"] == 1 and r.json()["approved"] is False
    r = await c.post(
        "/api/outreach/templates",
        json={
            "name": "mala",
            "kind": "pitch",
            "body": "{{1}} hola de {{2}}",
            "variables": ["contact_name", "agency"],
        },
    )
    assert r.status_code == 422


async def test_campaign_flow_api(authenticated_client, make_lead) -> None:  # type: ignore[no-untyped-def]
    c = authenticated_client
    await make_lead(niche="dentista")
    tid = await _tpl_id(c, "lead_contacto_inicial")
    r = await c.post(
        "/api/campaigns",
        json={"name": "Piloto", "template_id": tid, "audience": {"niche": "dentista"}},
    )
    assert r.status_code == 201
    cid = r.json()["id"]
    pv = (await c.post(f"/api/campaigns/{cid}/preview")).json()
    assert pv["audience_count"] == 1 and pv["template_approved"] is False
    # sin aprobar -> 409; sin confirmar -> 422
    assert (await c.post(f"/api/campaigns/{cid}/start", json={"confirm": True})).status_code == 409
    await c.post(f"/api/outreach/templates/{tid}/approval", json={"content_sid": CONTENT_SID})
    assert (await c.post(f"/api/campaigns/{cid}/start", json={"confirm": False})).status_code == 422
    r = await c.post(f"/api/campaigns/{cid}/start", json={"confirm": True})
    assert r.status_code == 200 and r.json()["status"] == "running"
    got = (await c.get(f"/api/campaigns/{cid}")).json()
    assert got["targets"] == {"pending": 1}
    assert (await c.post(f"/api/campaigns/{cid}/pause")).json()["status"] == "paused"
    assert (await c.post(f"/api/campaigns/{cid}/resume")).json()["status"] == "running"
    assert (await c.post(f"/api/campaigns/{cid}/bogus")).status_code == 404
    assert (await c.post(f"/api/campaigns/{cid}/cancel")).json()["status"] == "cancelled"
    assert (await c.get(f"/api/campaigns/{cid}/messages")).json() == []


async def test_operator_cannot_start(
    client, login, make_user, session, pitch, make_lead, make_campaign
) -> None:  # type: ignore[no-untyped-def]
    op = await make_user("operator")
    await login(client, op)
    await make_lead()
    r = await client.post("/api/campaigns", json={"name": "Borrador", "template_id": str(pitch.id)})
    assert r.status_code == 201
    cid = r.json()["id"]
    assert (
        await client.post(f"/api/campaigns/{cid}/start", json={"confirm": True})
    ).status_code == 403
    assert (await client.post(f"/api/campaigns/{cid}/cancel")).status_code == 403
    assert (
        await client.post(
            "/api/outreach/templates",
            json={
                "name": "abc",
                "kind": "pitch",
                "body": "Hola {{1}} gracias",
                "variables": ["contact_name"],
            },
        )
    ).status_code == 403
    assert (await client.get(f"/api/campaigns/{cid}")).status_code == 200


async def test_csrf_required(authenticated_client, pitch) -> None:  # type: ignore[no-untyped-def]
    r = await authenticated_client.post(
        "/api/campaigns",
        json={"name": "Sin csrf", "template_id": str(pitch.id)},
        headers={"X-CSRF-Token": ""},
    )
    assert r.status_code == 403


async def test_ui_pages(authenticated_client, session, pitch, make_lead) -> None:  # type: ignore[no-untyped-def]
    c = authenticated_client
    await make_lead()
    for path in ("/admin/campanas", "/admin/campanas/nueva", "/admin/campanas/plantillas"):
        r = await c.get(path)
        assert r.status_code == 200, path
    assert "Campañas" in (await c.get("/admin/campanas")).text
    r = await c.post(
        "/admin/campanas/nueva",
        data={
            "name": "Desde UI",
            "template_id": str(pitch.id),
            "daily_limit": "10",
            "csrf_token": c.csrf_token,
        },  # type: ignore[attr-defined]
        follow_redirects=False,
    )
    assert r.status_code == 303
    camp = (await session.execute(select(Campaign))).scalar_one()
    detail = await c.get(r.headers["location"])
    assert (
        detail.status_code == 200
        and "Vista previa" in detail.text
        and "Iniciar campaña" in detail.text
    )
    r = await c.post(
        f"/admin/campanas/{camp.id}/iniciar",
        data={"confirm": "si", "csrf_token": c.csrf_token},
        follow_redirects=False,
    )  # type: ignore[attr-defined]
    assert r.status_code == 303
    assert "Pausar" in (await c.get(f"/admin/campanas/{camp.id}")).text
    r = await c.post(
        f"/admin/campanas/{camp.id}/pause",
        data={"csrf_token": c.csrf_token},
        follow_redirects=False,
    )  # type: ignore[attr-defined]
    assert r.status_code == 303
    assert "Reanudar" in (await c.get(f"/admin/campanas/{camp.id}")).text
