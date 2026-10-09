import re

from sqlalchemy import select

from app.core.deps import session_cookie_name
from app.db.models.audit import AuditLog
from tests.conftest import TEST_PASSWORD


async def login_form(client):
    r = await client.get("/login")
    token = re.search(r'name="login_csrf" value="([^"]+)"', r.text).group(1)
    return token


async def do_login(client, email, password, **extra):
    token = await login_form(client)
    return await client.post(
        "/login", data={"email": email, "password": password, "login_csrf": token, **extra}
    )


async def test_login_page_renders_spanish_with_cookie_flags(client):
    r = await client.get("/login")
    assert r.status_code == 200 and "Iniciar sesión" in r.text and "Contraseña" in r.text
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


async def test_login_success_sets_secure_session_cookie(client, owner_user):
    r = await do_login(client, owner_user.email, TEST_PASSWORD)
    assert r.status_code == 303 and r.headers["location"] == "/admin/inicio"
    sc = [h for h in r.headers.get_list("set-cookie") if h.startswith(session_cookie_name())][
        0
    ].lower()
    assert "httponly" in sc and "samesite=lax" in sc and "path=/" in sc
    page = await client.get("/admin/inicio")
    assert page.status_code == 200 and "Cerrar sesión" in page.text


async def test_login_wrong_password_generic_message(client, owner_user):
    r = await do_login(client, owner_user.email, "incorrecta-12345")
    assert r.status_code == 401 and "Correo o contrasena incorrectos" in r.text
    assert session_cookie_name() not in client.cookies


async def test_login_requires_login_csrf(client, owner_user):
    r = await client.post("/login", data={"email": owner_user.email, "password": TEST_PASSWORD})
    assert r.status_code == 403
    await client.get("/login")
    r = await client.post(
        "/login",
        data={"email": owner_user.email, "password": TEST_PASSWORD, "login_csrf": "forjado"},
    )
    assert r.status_code == 403


async def test_open_redirect_blocked(client, owner_user):
    for bad in ("//evil.com", "https://evil.com", "/\\evil.com"):
        r = await do_login(client, owner_user.email, TEST_PASSWORD, next=bad)
        assert r.headers["location"] == "/admin/inicio"
        client.cookies.clear()
    r = await do_login(client, owner_user.email, TEST_PASSWORD, next="/admin/leads")
    assert r.headers["location"] == "/admin/leads"


async def test_protected_pages_redirect_to_login(client):
    r = await client.get("/admin/inicio", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = await client.get("/admin/inicio", headers={"accept": "application/json"})
    assert r.status_code == 401


async def test_login_page_redirect_param_safe(client):
    r = await client.get("/login?next=//evil.com")
    assert "evil.com" not in r.text


async def test_logout_requires_csrf_and_revokes(authenticated_client):
    c = authenticated_client
    r = await c.post("/logout", headers={"X-CSRF-Token": ""})
    assert r.status_code == 403
    r = await c.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    c.cookies.clear()


async def test_logout_revokes_server_side(authenticated_client, client):
    token = client.cookies.get(session_cookie_name())
    await client.post("/logout")
    client.cookies.set(session_cookie_name(), token)
    assert (
        await client.get("/admin/inicio", headers={"accept": "application/json"})
    ).status_code == 401


async def test_csrf_enforced_on_mutating_admin_and_api(authenticated_client, app):
    from fastapi import APIRouter

    r = APIRouter()

    @r.post("/api/_t")
    async def t():
        return {"ok": True}

    @r.post("/webhooks/_t")
    async def w():
        return {"ok": True}

    # el guard global se aplica via include_router(dependencies=[csrf_guard]) en create_app
    from fastapi import Depends

    from app.core.deps import csrf_guard

    app.include_router(r, dependencies=[Depends(csrf_guard)])
    c = authenticated_client
    assert (await c.post("/api/_t", headers={"X-CSRF-Token": ""})).status_code in (401, 403)
    assert (await c.post("/api/_t", headers={"X-CSRF-Token": "mal"})).status_code == 403
    assert (await c.post("/api/_t")).status_code == 200
    assert (await c.post("/api/_t", headers={"Origin": "http://evil.com"})).status_code == 403
    assert (await c.post("/api/_t", headers={"Origin": "http://test"})).status_code == 200
    c.cookies.clear()
    assert (await c.post("/webhooks/_t")).status_code == 200  # webhooks exentos


async def test_csrf_via_form_field(authenticated_client):
    c = authenticated_client
    tok = c.csrf_token
    c.headers.pop("X-CSRF-Token")
    r = await c.post(
        "/admin/ajustes/password",
        data={"current_password": "x", "new_password": "y"},
    )
    assert r.status_code == 403
    r = await c.post(
        "/admin/ajustes/password",
        data={"current_password": "mal", "new_password": "y", "csrf_token": tok},
    )
    assert r.status_code == 303 and "error=" in r.headers["location"]


async def test_settings_page_and_password_change(authenticated_client):
    c = authenticated_client
    page = await c.get("/admin/ajustes")
    assert page.status_code == 200 and "Mi cuenta" in page.text and "Usuarios" in page.text
    r = await c.post(
        "/admin/ajustes/password",
        data={"current_password": TEST_PASSWORD, "new_password": "Otra-clave-98765"},
    )
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    assert "Contraseña actualizada" in (await c.get(r.headers["location"])).text


async def test_owner_manages_users(authenticated_client, session):
    c = authenticated_client
    r = await c.post(
        "/admin/ajustes/usuarios",
        data={
            "email": "nuevo@example.com",
            "password": "Clave-nueva-12345",
            "role": "operator",
            "full_name": "N",
        },
    )
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    dup = await c.post(
        "/admin/ajustes/usuarios",
        data={"email": "nuevo@example.com", "password": "Clave-nueva-12345", "role": "operator"},
    )
    assert "error=" in dup.headers["location"]
    from app.db.models.users import User

    new = (
        await session.execute(select(User).where(User.email == "nuevo@example.com"))
    ).scalar_one()
    r = await c.post(f"/admin/ajustes/usuarios/{new.id}/desactivar")
    assert "ok=" in r.headers["location"]
    r = await c.post(f"/admin/ajustes/usuarios/{new.id}/borrar")
    assert r.status_code == 404
    r = await c.post(f"/admin/ajustes/usuarios/{c.user.id}/desactivar")
    assert "error=" in r.headers["location"]


async def test_operator_cannot_manage_users(client, make_user, login):
    op = await make_user("operator")
    await login(client, op)
    page = await client.get("/admin/ajustes")
    assert "Nuevo usuario" not in page.text
    r = await client.post(
        "/admin/ajustes/usuarios",
        data={"email": "x@example.com", "password": "Clave-nueva-12345"},
    )
    assert r.status_code == 403


async def test_login_events_audited(client, owner_user, session):
    await do_login(client, owner_user.email, TEST_PASSWORD)
    actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert "auth.login" in actions
