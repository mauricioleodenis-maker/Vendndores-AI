from datetime import timedelta

import pytest
from freezegun import freeze_time
from sqlalchemy import select

from app.auth import service
from app.core.clock import utcnow
from app.core.errors import AppError
from app.core.security import hash_token
from app.db.models.audit import AuditLog
from app.db.models.users import UserSession
from tests.conftest import TEST_PASSWORD


async def test_create_user_normalizes_and_validates(session):
    u = await service.create_user(
        session, email="  Ana@Example.COM ", password=TEST_PASSWORD, role="admin"
    )
    assert u.email == "ana@example.com" and u.role == "admin"
    assert u.password_hash.startswith("$argon2id$")
    with pytest.raises(AppError, match="Ya existe"):
        await service.create_user(session, email="ana@example.com", password=TEST_PASSWORD)
    with pytest.raises(AppError) as e:
        await service.create_user(session, email="b@example.com", password="corta")
    assert e.value.code == "weak_password"
    with pytest.raises(AppError):
        await service.create_user(
            session, email="b@example.com", password=TEST_PASSWORD, role="dios"
        )
    with pytest.raises(AppError):
        await service.create_user(session, email="sin-arroba", password=TEST_PASSWORD)


async def test_login_success_creates_hashed_session(session, make_user):
    u = await make_user("owner")
    user, token, us = await service.authenticate(
        session, email=u.email, password=TEST_PASSWORD, ip="1.2.3.4"
    )
    assert us.id == hash_token(token) and us.id != token  # solo se guarda el hash
    assert user.last_login_at is not None
    resolved = await service.resolve_session(session, token)
    assert resolved and resolved[0].id == u.id


async def test_wrong_password_generic_error_and_audit(session, make_user):
    u = await make_user()
    with pytest.raises(AppError) as e1:
        await service.authenticate(session, email=u.email, password="incorrecta-12345")
    with pytest.raises(AppError) as e2:
        await service.authenticate(session, email="nadie@example.com", password="incorrecta-12345")
    assert e1.value.message == e2.value.message == service.GENERIC_LOGIN_ERROR
    assert e1.value.status == 401
    actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert actions.count("auth.login_failed") == 2


async def test_lockout_after_max_attempts(session, make_user):
    u = await make_user()
    for _ in range(5):
        with pytest.raises(AppError):
            await service.authenticate(session, email=u.email, password="incorrecta-12345", ip=None)
    await session.refresh(u)
    assert u.locked_until is not None
    # el rate limit por email (5/15min) ya corta: ni siquiera con la clave correcta entra
    with pytest.raises(AppError) as e:
        await service.authenticate(session, email=u.email, password=TEST_PASSWORD)
    assert e.value.status == 429
    actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert "auth.lockout" in actions and "auth.login_rate_limited" in actions


async def test_locked_user_rejected_even_with_correct_password(session, make_user):
    u = await make_user()
    u.locked_until = utcnow() + timedelta(minutes=10)
    await session.commit()
    with pytest.raises(AppError) as e:
        await service.authenticate(session, email=u.email, password=TEST_PASSWORD)
    assert e.value.status == 401


async def test_lock_expires(session, make_user):
    u = await make_user()
    u.locked_until = utcnow() - timedelta(seconds=1)
    await session.commit()
    await service.authenticate(session, email=u.email, password=TEST_PASSWORD)


async def test_inactive_user_cannot_login(session, make_user):
    u = await make_user()
    u.is_active = False
    await session.commit()
    with pytest.raises(AppError):
        await service.authenticate(session, email=u.email, password=TEST_PASSWORD)


async def test_successful_login_resets_counters(session, make_user):
    u = await make_user()
    for _ in range(2):
        with pytest.raises(AppError):
            await service.authenticate(session, email=u.email, password="incorrecta-12345")
    await service.authenticate(session, email=u.email, password=TEST_PASSWORD)
    await session.refresh(u)
    assert u.failed_logins == 0


async def test_session_expiry_revocation_and_sliding(session, make_user):
    u = await make_user()
    with freeze_time("2026-03-01 10:00:00") as clock:
        token, us = await service.create_session(session, u)
        await session.commit()
        clock.tick(timedelta(minutes=100))
        assert await service.resolve_session(session, token)
        await session.commit()
        await session.refresh(us)
        first_expiry = us.expires_at
        clock.tick(timedelta(minutes=400))  # 500 > ttl 480 desde el inicio, pero se deslizo
        assert await service.resolve_session(session, token)
        await session.commit()
        await session.refresh(us)
        assert us.expires_at > first_expiry
        clock.tick(timedelta(days=8))
        assert await service.resolve_session(session, token) is None  # tope absoluto 7d


async def test_revoked_and_unknown_tokens(session, make_user):
    u = await make_user()
    token, _ = await service.create_session(session, u)
    await service.revoke_session(session, token)
    assert await service.resolve_session(session, token) is None
    assert await service.resolve_session(session, "x" * 10) is None
    assert await service.resolve_session(session, "") is None
    assert await service.resolve_session(session, "y" * 500) is None
    await service.revoke_session(session, None)


async def test_change_password_revokes_other_sessions(session, make_user):
    u = await make_user()
    t1, s1 = await service.create_session(session, u)
    t2, s2 = await service.create_session(session, u)
    await service.change_password(
        session,
        u,
        current_password=TEST_PASSWORD,
        new_password="Nueva-clave-98765",
        keep_session_id=s1.id,
    )
    assert await service.resolve_session(session, t1)
    assert await service.resolve_session(session, t2) is None
    await service.authenticate(session, email=u.email, password="Nueva-clave-98765")
    with pytest.raises(AppError):
        await service.change_password(
            session, u, current_password="mal", new_password="Otra-clave-12345"
        )
    with pytest.raises(AppError) as e:
        await service.change_password(
            session, u, current_password="Nueva-clave-98765", new_password="corta"
        )
    assert e.value.code == "weak_password"


async def test_deactivate_user(session, make_user):
    owner = await make_user("owner")
    other = await make_user("operator")
    token, _ = await service.create_session(session, other)
    await service.set_user_active(session, owner, other.id, active=False)
    assert await service.resolve_session(session, token) is None
    with pytest.raises(AppError):
        await service.set_user_active(session, owner, owner.id, active=False)
    with pytest.raises(AppError):
        await service.set_user_active(session, owner, __import__("uuid").uuid4(), active=True)
    await service.set_user_active(session, owner, other.id, active=True)
    assert len(await service.list_users(session)) == 2


async def test_session_row_has_no_raw_token(session, make_user):
    u = await make_user()
    token, _ = await service.create_session(session, u)
    await session.commit()
    ids = (await session.execute(select(UserSession.id))).scalars().all()
    assert token not in ids
