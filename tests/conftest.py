"""Fixtures compartidas: SQLite en memoria, app + cliente HTTP, usuario autenticado, FakeLLM,
bloqueo de red y DNS falso. Ningun test debe tocar la red."""

from __future__ import annotations

import os

# El entorno se fija ANTES de importar la app para que Settings lo lea.
os.environ.update(
    {
        "VAI_ENV": "test",
        "VAI_COOKIE_SECURE": "false",
        "VAI_DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "VAI_SECRET_KEY": "test-secret-key-0123456789-0123456789-abcdef",
        "VAI_TWILIO_DRY_RUN": "true",
        "VAI_OUTREACH_ENABLED": "false",
        "VAI_PUBLIC_BASE_URL": "http://test",
    }
)
os.environ.pop("VAI_REDIS_URL", None)

import socket  # noqa: E402
from collections.abc import AsyncIterator, Callable, Iterator  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Any  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession  # noqa: E402

import app.db.models  # noqa: E402,F401
from app.ai.client import FakeLLM, get_llm  # noqa: E402
from app.auth import service as auth_service  # noqa: E402
from app.core import http as core_http  # noqa: E402
from app.core import jobs as core_jobs  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.rate_limit import set_rate_limiter  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models.tenants import Tenant  # noqa: E402
from app.db.models.users import User, UserSession  # noqa: E402
from app.db.session import configure_database, make_engine  # noqa: E402
from app.main import create_app  # noqa: E402

TEST_PASSWORD = "Clave-de-prueba-12345"
PUBLIC_IP = "93.184.216.34"


# --------------------------------------------------------------------------- red
@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Falla cualquier conexion TCP/UDP real o resolucion DNS real."""
    real_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError(f"Red bloqueada en tests: {address!r}")
        return real_connect(self, address)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"DNS bloqueado en tests: {host!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """``safe_fetch`` resuelve cualquier dominio a una IP publica. Sobrescribe entradas con
    ``fake_dns["host"] = ["10.0.0.1"]`` para probar SSRF."""
    table: dict[str, list[str]] = {}

    async def _resolve(host: str, port: int) -> list[str]:
        return table.get(host, [PUBLIC_IP])

    monkeypatch.setattr(core_http, "_resolve", _resolve)
    return table


@pytest.fixture(autouse=True)
def _reset_globals() -> Iterator[None]:
    get_settings.cache_clear()
    set_rate_limiter(None)
    core_jobs.ENQUEUED.clear()
    yield
    set_rate_limiter(None)
    core_jobs.ENQUEUED.clear()


# --------------------------------------------------------------------------- base de datos
@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    configure_database(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as s:
        yield s


# --------------------------------------------------------------------------- app y cliente
@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest_asyncio.fixture
async def app(engine: AsyncEngine, fake_llm: FakeLLM) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_llm] = lambda: fake_llm
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --------------------------------------------------------------------------- usuarios
@pytest_asyncio.fixture
async def make_user(session: AsyncSession) -> Callable[..., Any]:
    counter = {"n": 0}

    async def _make(role: str = "operator", email: str | None = None, **kw: Any) -> User:
        counter["n"] += 1
        user = await auth_service.create_user(
            session,
            email=email or f"{role}{counter['n']}@example.com",
            password=kw.pop("password", TEST_PASSWORD),
            full_name=kw.pop("full_name", f"Usuario {role}"),
            role=role,
        )
        await session.commit()
        return user

    return _make


@pytest_asyncio.fixture
async def owner_user(make_user: Callable[..., Any]) -> User:
    return await make_user("owner", email="owner@example.com")  # type: ignore[no-any-return]


@dataclass
class LoggedIn:
    user: User
    token: str
    csrf: str
    session_row: UserSession


@pytest_asyncio.fixture
async def login(session: AsyncSession) -> Callable[..., Any]:
    """``await login(client, user)`` fija la cookie de sesion y el header CSRF por defecto."""

    async def _login(client: httpx.AsyncClient, user: User) -> LoggedIn:
        token, row = await auth_service.create_session(session, user)
        await session.commit()
        from app.core.deps import session_cookie_name

        client.cookies.set(session_cookie_name(), token)
        client.headers["X-CSRF-Token"] = row.csrf_token
        return LoggedIn(user, token, row.csrf_token, row)

    return _login


@pytest_asyncio.fixture
async def authenticated_client(
    client: httpx.AsyncClient, owner_user: User, login: Callable[..., Any]
) -> httpx.AsyncClient:
    """Cliente con sesion de owner y ``X-CSRF-Token`` ya configurado.

    Atributos: ``.user`` y ``.csrf_token``. Para probar CSRF ausente: ``headers={"X-CSRF-Token": ""}``.
    """
    logged = await login(client, owner_user)
    client.user = owner_user  # type: ignore[attr-defined]
    client.csrf_token = logged.csrf  # type: ignore[attr-defined]
    return client


@pytest_asyncio.fixture
async def tenant(session: AsyncSession) -> Tenant:
    t = Tenant(slug="clinica-demo", name="Clinica Demo", niche="dentista", status="active")
    session.add(t)
    await session.commit()
    return t


@pytest_asyncio.fixture
async def make_tenant(session: AsyncSession) -> Callable[..., Any]:
    counter = {"n": 0}

    async def _make(name: str | None = None, niche: str = "dentista", **kw: Any) -> Tenant:
        counter["n"] += 1
        n = counter["n"]
        t = Tenant(
            slug=kw.pop("slug", f"tenant-{n}"),
            name=name or f"Negocio {n}",
            niche=niche,
            status=kw.pop("status", "active"),
            **kw,
        )
        session.add(t)
        await session.commit()
        return t

    return _make
