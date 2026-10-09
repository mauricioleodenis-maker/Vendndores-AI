import httpx
import pytest
from fastapi import APIRouter

from app import main as main_module
from app.main import ROUTER_PACKAGES, create_app, discover_routers


async def test_health_and_ready(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    r = await client.get("/readyz")
    assert r.status_code == 200 and r.json()["checks"]["db"] == "ok"


async def test_readyz_fails_when_db_down(client, monkeypatch):
    class Broken:
        def connect(self):
            raise RuntimeError("down")

    monkeypatch.setattr(main_module, "get_engine", lambda: Broken())
    r = await client.get("/readyz")
    assert r.status_code == 503 and r.json()["status"] == "error"


async def test_readyz_redis_down(client, monkeypatch):
    monkeypatch.setenv("VAI_REDIS_URL", "redis://127.0.0.1:1/0")
    from app.core.config import get_settings

    get_settings.cache_clear()
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["redis"] == "error"


async def test_root_redirects_to_admin(client):
    r = await client.get("/")
    assert r.status_code == 307 and r.headers["location"] == "/admin"
    r = await client.get("/admin")
    assert r.status_code == 307 and r.headers["location"] == "/admin/inicio"


async def test_docs_hidden_in_prod(monkeypatch):
    from app.core.config import Settings

    s = Settings(env="prod", secret_key="x" * 40, master_keys='{"v1":"a"}', phone_hash_key="k",
                 cookie_secure=True, public_base_url="https://a.example.com",
                 allowed_hosts=["a.example.com"], _env_file=None)  # fmt: skip
    app = create_app(s)
    assert app.docs_url is None and app.openapi_url is None


def test_discovers_auth_and_audit_routers():
    paths = {
        p for router in discover_routers() for r in router.routes if (p := getattr(r, "path", None))
    }
    assert "/login" in paths and "/admin/auditoria" in paths
    assert "auth" in ROUTER_PACKAGES and "dashboard" in ROUTER_PACKAGES


def test_discovery_skips_missing_but_raises_real_import_errors(monkeypatch, tmp_path):
    import importlib

    real = importlib.import_module

    def fake(name, *a, **k):
        if name == "app.tenants.router":
            raise ImportError("roto")  # error real en el modulo: no se oculta
        return real(name, *a, **k)

    monkeypatch.setattr(main_module.importlib, "import_module", fake)
    with pytest.raises(ImportError):
        discover_routers()


def test_module_router_with_extra_routers_is_registered(monkeypatch):
    import sys
    import types

    mod = types.ModuleType("app.tenants.router")
    mod.router = APIRouter()
    mod.routers = [APIRouter(), APIRouter()]
    monkeypatch.setitem(sys.modules, "app.tenants.router", mod)
    assert len(discover_routers()) >= 5  # auth(1) + audit(1) + 3 de tenants


def test_lazy_app_attribute():
    assert main_module.app is main_module.app
