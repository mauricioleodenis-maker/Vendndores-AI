"""GoogleCalendarProvider con respx (sin red)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking import google_calendar as gc
from app.core.config import get_settings
from app.core.crypto import get_crypto, make_aad, pack_blob
from app.db.models.booking import Appointment, CalendarConnection
from app.db.models.catalog import Service
from app.db.models.contacts import Contact
from app.db.models.tenants import Tenant

CAL = "primary"
EVENTS = f"{gc.API_BASE}/calendars/primary/events"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAI_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("VAI_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    gc.reset_caches()


@pytest_asyncio.fixture
async def conn(session: AsyncSession, tenant: Tenant) -> CalendarConnection:
    await gc.save_refresh_token(session, tenant.id, "refresh-token-1234")
    c = CalendarConnection(
        tenant_id=tenant.id, provider="google", status="active", google_calendar_id=CAL
    )
    session.add(c)
    await session.commit()
    return c


@pytest_asyncio.fixture
async def appt(session: AsyncSession, tenant: Tenant) -> Appointment:
    svc = Service(tenant_id=tenant.id, name="Limpieza dental", duration_min=30)
    contact = Contact(
        tenant_id=tenant.id,
        phone_hash="h" * 64,
        phone_enc=b"x",
        display_name_enc=pack_blob(
            get_crypto().encrypt_str(
                "Juan Perez", aad=make_aad("contacts", tenant.id, "display_name_enc")
            )
        ),
    )
    session.add_all([svc, contact])
    await session.flush()
    start = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
    a = Appointment(
        tenant_id=tenant.id,
        contact_id=contact.id,
        service_id=svc.id,
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        idempotency_key="k1",
    )
    session.add(a)
    await session.commit()
    return a


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def provider(sleeps: list[float]) -> gc.GoogleCalendarProvider:
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    return gc.GoogleCalendarProvider(sleep=fake_sleep)


@pytest_asyncio.fixture
async def mock() -> AsyncIterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.post(gc.TOKEN_URL).respond(200, json={"access_token": "at-1", "expires_in": 3600})
        yield router


def test_event_id_is_deterministic_and_valid() -> None:
    t, a = uuid.uuid4(), uuid.uuid4()
    eid = gc.event_id_for(t, a)
    assert eid == gc.event_id_for(t, a) != gc.event_id_for(t, uuid.uuid4())
    assert 5 <= len(eid) <= 1024 and set(eid) <= set("0123456789abcdefghijklmnopqrstuv")


async def test_busy_intervals_and_cache(
    session: AsyncSession,
    tenant: Tenant,
    conn: CalendarConnection,
    provider: gc.GoogleCalendarProvider,
    mock: respx.MockRouter,
) -> None:
    route = mock.post(f"{gc.API_BASE}/freeBusy").respond(
        200,
        json={
            "calendars": {
                CAL: {
                    "busy": [
                        {"start": "2026-10-12T15:00:00Z", "end": "2026-10-12T16:00:00Z"},
                        {"start": "2026-10-12T13:00:00Z", "end": "2026-10-12T13:30:00Z"},
                    ]
                }
            }
        },
    )
    s, e = datetime(2026, 10, 12, 13, tzinfo=UTC), datetime(2026, 10, 12, 23, tzinfo=UTC)
    out = await provider.busy_intervals(session, tenant.id, s, e)
    assert out[0][0] == datetime(2026, 10, 12, 13, tzinfo=UTC) and len(out) == 2
    body = json.loads(route.calls[0].request.content)
    assert body["timeZone"] == "America/Bogota" and body["items"] == [{"id": CAL}]
    assert route.calls[0].request.headers["authorization"] == "Bearer at-1"
    await provider.busy_intervals(session, tenant.id, s, e)
    assert route.call_count == 1  # cache


async def test_busy_errors_raise(
    session: AsyncSession,
    tenant: Tenant,
    conn: CalendarConnection,
    provider: gc.GoogleCalendarProvider,
    mock: respx.MockRouter,
) -> None:
    mock.post(f"{gc.API_BASE}/freeBusy").respond(
        200, json={"calendars": {CAL: {"errors": [{"reason": "notFound"}], "busy": []}}}
    )
    s = datetime(2026, 10, 12, tzinfo=UTC)
    with pytest.raises(gc.CalendarError):
        await provider.busy_intervals(session, tenant.id, s, s + timedelta(days=1))


async def test_no_connection_raises_auth(
    session: AsyncSession, tenant: Tenant, provider: gc.GoogleCalendarProvider
) -> None:
    s = datetime(2026, 10, 12, tzinfo=UTC)
    with pytest.raises(gc.CalendarAuthError):
        await provider.busy_intervals(session, tenant.id, s, s + timedelta(days=1))
    assert await gc.active_provider(session, tenant.id) is None


async def test_create_event_body_and_state(
    session: AsyncSession,
    tenant: Tenant,
    conn: CalendarConnection,
    appt: Appointment,
    provider: gc.GoogleCalendarProvider,
    mock: respx.MockRouter,
) -> None:
    route = mock.post(EVENTS).respond(
        200, json={"id": gc.event_id_for(tenant.id, appt.id), "etag": '"e1"'}
    )
    eid = await provider.create_event(session, tenant.id, appt)
    req = route.calls[0].request
    body = json.loads(req.content)
    assert eid == body["id"] == appt.google_event_id
    assert appt.sync_status == "synced" and appt.google_event_etag == '"e1"'
    assert body["summary"] == "Limpieza dental - Juan Perez"
    assert body["start"] == {"dateTime": "2026-10-12T09:00:00", "timeZone": "America/Bogota"}
    assert "attendees" not in body and req.url.params["sendUpdates"] == "none"
    assert body["extendedProperties"]["private"]["appt_id"] == str(appt.id)
    assert await gc.active_provider(session, tenant.id) is not None


async def test_create_event_409_is_idempotent(
    session: AsyncSession,
    tenant: Tenant,
    conn: CalendarConnection,
    appt: Appointment,
    provider: gc.GoogleCalendarProvider,
    mock: respx.MockRouter,
) -> None:
    eid = gc.event_id_for(tenant.id, appt.id)
    mock.post(EVENTS).respond(409, json={"error": {"code": 409}})
    get = mock.get(f"{EVENTS}/{eid}").respond(200, json={"id": eid, "etag": '"z"'})
    assert await provider.create_event(session, tenant.id, appt) == eid
    assert get.called and appt.google_event_etag == '"z"'


async def test_retries_with_backoff_then_succeeds(
    session: AsyncSession,
    tenant: Tenant,
    conn: CalendarConnection,
    appt: Appointment,
    provider: gc.GoogleCalendarProvider,
    mock: respx.MockRouter,
    sleeps: list[float],
) -> None:
    eid = gc.event_id_for(tenant.id, appt.id)
    mock.post(EVENTS).mock(
        side_effect=[
            httpx.Response(503),
            httpx.ConnectError("boom"),
            httpx.Response(200, json={"id": eid}),
        ]
    )
    assert await provider.create_event(session, tenant.id, appt) == eid
    assert sleeps == [0.5, 1.0]


async def test_gives_up_after_retries(
    session: AsyncSession,
    tenant: Tenant,
    conn: CalendarConnection,
    appt: Appointment,
    provider: gc.GoogleCalendarProvider,
    mock: respx.MockRouter,
    sleeps: list[float],
) -> None:
    mock.post(EVENTS).respond(500)
    with pytest.raises(gc.CalendarError):
        await provider.create_event(session, tenant.id, appt)
    assert sleeps == [0.5, 1.0, 2.0]


async def test_non_retryable_status_raises(
    session: AsyncSession,
    tenant: Tenant,
    conn: CalendarConnection,
    appt: Appointment,
    provider: gc.GoogleCalendarProvider,
    mock: respx.MockRouter,
    sleeps: list[float],
) -> None:
    mock.post(EVENTS).respond(403)
    with pytest.raises(gc.CalendarError):
        await provider.create_event(session, tenant.id, appt)
    assert sleeps == []


async def test_401_triggers_one_refresh(
    session: AsyncSession,
    tenant: Tenant,
    conn: CalendarConnection,
    appt: Appointment,
    provider: gc.GoogleCalendarProvider,
    mock: respx.MockRouter,
) -> None:
    eid = gc.event_id_for(tenant.id, appt.id)
    mock.post(EVENTS).mock(side_effect=[httpx.Response(401), httpx.Response(200, json={"id": eid})])
    assert await provider.create_event(session, tenant.id, appt) == eid
    assert mock.routes[0].call_count == 2  # dos tokens pedidos


async def test_update_and_delete(
    session: AsyncSession,
    tenant: Tenant,
    conn: CalendarConnection,
    appt: Appointment,
    provider: gc.GoogleCalendarProvider,
    mock: respx.MockRouter,
) -> None:
    await provider.update_event(session, tenant.id, appt)  # sin id: no-op
    await provider.delete_event(session, tenant.id, appt)
    appt.google_event_id = "abc123"
    patch = mock.patch(f"{EVENTS}/abc123").respond(200, json={"id": "abc123", "etag": '"p"'})
    await provider.update_event(session, tenant.id, appt)
    assert patch.called and appt.google_event_etag == '"p"'
    dele = mock.delete(f"{EVENTS}/abc123").respond(204)
    await provider.delete_event(session, tenant.id, appt)
    assert dele.called and appt.google_event_id is None


async def test_delete_already_gone_ok_and_patch_404_recreates(
    session: AsyncSession,
    tenant: Tenant,
    conn: CalendarConnection,
    appt: Appointment,
    provider: gc.GoogleCalendarProvider,
    mock: respx.MockRouter,
) -> None:
    appt.google_event_id = "gone"
    mock.delete(f"{EVENTS}/gone").respond(410)
    await provider.delete_event(session, tenant.id, appt)
    assert appt.google_event_id is None

    appt.google_event_id = "gone2"
    mock.patch(f"{EVENTS}/gone2").respond(404)
    eid = gc.event_id_for(tenant.id, appt.id)
    mock.post(EVENTS).respond(200, json={"id": eid})
    await provider.update_event(session, tenant.id, appt)
    assert appt.google_event_id == eid


async def test_invalid_grant_marks_needs_reauth(
    session: AsyncSession,
    tenant: Tenant,
    conn: CalendarConnection,
    provider: gc.GoogleCalendarProvider,
) -> None:
    with respx.mock() as router:
        router.post(gc.TOKEN_URL).respond(400, json={"error": "invalid_grant"})
        s = datetime(2026, 10, 12, tzinfo=UTC)
        with pytest.raises(gc.CalendarAuthError):
            await provider.busy_intervals(session, tenant.id, s, s + timedelta(days=1))
    await session.refresh(conn)
    assert conn.status == "needs_reauth" and conn.last_error == "invalid_grant"
    assert await gc.active_provider(session, tenant.id) is None


async def test_refresh_failures(
    session: AsyncSession,
    tenant: Tenant,
    conn: CalendarConnection,
    provider: gc.GoogleCalendarProvider,
) -> None:
    s = datetime(2026, 10, 12, tzinfo=UTC)
    with respx.mock() as router:
        router.post(gc.TOKEN_URL).respond(500)
        with pytest.raises(gc.CalendarError):
            await provider.access_token(session, tenant.id)
        router.post(gc.TOKEN_URL).mock(side_effect=httpx.ConnectError("x"))
        with pytest.raises(gc.CalendarError):
            await provider.access_token(session, tenant.id)
    assert s  # sin uso adicional


async def test_missing_refresh_token(
    session: AsyncSession, tenant: Tenant, provider: gc.GoogleCalendarProvider
) -> None:
    c = CalendarConnection(tenant_id=tenant.id, provider="google", status="active")
    session.add(c)
    await session.commit()
    with pytest.raises(gc.CalendarAuthError):
        await provider.access_token(session, tenant.id)
    assert c.status == "needs_reauth"


async def test_token_cached_and_refreshed_when_near_expiry(
    session: AsyncSession,
    tenant: Tenant,
    conn: CalendarConnection,
    provider: gc.GoogleCalendarProvider,
    mock: respx.MockRouter,
) -> None:
    assert await provider.access_token(session, tenant.id) == "at-1"
    await provider.access_token(session, tenant.id)
    assert mock.routes[0].call_count == 1
    gc._TOKENS[tenant.id].expires_at = gc.utcnow() + timedelta(seconds=30)
    await provider.access_token(session, tenant.id)
    assert mock.routes[0].call_count == 2


async def test_secret_roundtrip_and_encrypted(
    session: AsyncSession, tenant: Tenant, make_tenant: Callable[..., Any]
) -> None:
    await gc.save_refresh_token(session, tenant.id, "tok-abcd")
    await gc.save_refresh_token(session, tenant.id, "tok-wxyz")  # rota
    assert await gc.load_refresh_token(session, tenant.id) == "tok-wxyz"
    other = await make_tenant()
    assert await gc.load_refresh_token(session, other.id) is None
    from sqlalchemy import select

    from app.db.models.tenants import TenantSecret

    row = (await session.execute(select(TenantSecret))).scalar_one()
    assert b"tok-wxyz" not in row.ciphertext and row.last4 == "wxyz"
    await gc.delete_refresh_token(session, tenant.id)
    assert await gc.load_refresh_token(session, tenant.id) is None


def test_email_crypto(tenant: Tenant) -> None:
    enc = gc.encrypt_email(tenant.id, "a@b.co")
    assert gc.decrypt_email(tenant.id, enc) == "a@b.co"
    assert gc.decrypt_email(uuid.uuid4(), enc) is None and gc.decrypt_email(tenant.id, None) is None


async def test_revoke_calls_google_best_effort(
    session: AsyncSession,
    tenant: Tenant,
    conn: CalendarConnection,
    provider: gc.GoogleCalendarProvider,
) -> None:
    with respx.mock() as router:
        rev = router.post(gc.REVOKE_URL).respond(200)
        await provider.revoke(session, tenant.id)
        assert rev.called
        router.post(gc.REVOKE_URL).mock(side_effect=httpx.ConnectError("x"))
        await provider.revoke(session, tenant.id)  # no lanza
