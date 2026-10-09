"""Servicio de usuarios y sesiones (cookie con token aleatorio; en DB solo su sha256)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_event
from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.rate_limit import get_rate_limiter
from app.core.security import (
    hash_password,
    hash_token,
    new_csrf_token,
    new_token,
    password_needs_rehash,
    verify_password,
)
from app.db.models.users import USER_ROLES, User, UserSession

GENERIC_LOGIN_ERROR = "Correo o contrasena incorrectos"
TOUCH_INTERVAL = timedelta(seconds=60)


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    stmt = select(User).where(User.email == normalize_email(email))
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    full_name: str = "",
    role: str = "operator",
    actor: User | None = None,
) -> User:
    if role not in USER_ROLES:
        raise AppError("invalid_role", "Rol invalido", 422)
    email = normalize_email(email)
    if "@" not in email or len(email) > 320:
        raise AppError("invalid_email", "Correo invalido", 422)
    if await get_user_by_email(session, email):
        raise AppError("email_taken", "Ya existe un usuario con ese correo", 409)
    try:
        pw_hash = await asyncio.to_thread(hash_password, password)
    except ValueError as exc:
        raise AppError("weak_password", str(exc), 422) from exc
    user = User(email=email, password_hash=pw_hash, full_name=full_name.strip(), role=role)
    session.add(user)
    await session.flush()
    await log_event(
        session,
        actor=actor,
        action="user.create",
        entity_type="user",
        entity_id=str(user.id),
        diff={"fields": ["email", "role"], "role": role},
    )
    return user


async def authenticate(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[User, str, UserSession]:
    """Valida credenciales. Devuelve (usuario, token_cookie, sesion). Mensaje de error generico."""
    settings = get_settings()
    email = normalize_email(email)
    limiter = get_rate_limiter()
    window_s = settings.login_window_min * 60
    keys = [f"login:ip:{ip or '-'}", f"login:email:{email}"]
    limits = [settings.login_max_attempts * 4, settings.login_max_attempts]
    for key, limit in zip(keys, limits, strict=True):
        res = await limiter.peek(key, limit=limit, window_s=window_s)
        if not res.allowed:
            await log_event(
                session, actor=None, action="auth.login_rate_limited", ip=ip, diff={"key": key[:8]}
            )
            await session.commit()
            raise AppError("rate_limited", "Demasiados intentos. Intenta mas tarde.", 429)

    user = await get_user_by_email(session, email)
    now = utcnow()
    locked = user is not None and user.locked_until is not None and user.locked_until > now
    ok = await asyncio.to_thread(verify_password, user.password_hash if user else None, password)
    if user is None or locked or not ok or not user.is_active:
        for key, limit in zip(keys, limits, strict=True):
            await limiter.hit(key, limit=limit, window_s=window_s)
        if user is not None and not locked:
            user.failed_logins += 1
            if user.failed_logins >= settings.login_max_attempts:
                user.locked_until = now + timedelta(minutes=settings.lockout_min)
                user.failed_logins = 0
                await log_event(
                    session,
                    actor=None,
                    action="auth.lockout",
                    entity_type="user",
                    entity_id=str(user.id),
                    ip=ip,
                )
        await log_event(
            session,
            actor=None,
            action="auth.login_failed",
            entity_type="user",
            entity_id=str(user.id) if user else "",
            ip=ip,
            diff={"locked": bool(locked)},
        )
        await session.commit()  # persistir contador/auditoria aunque se lance el error
        raise AppError("invalid_credentials", GENERIC_LOGIN_ERROR, 401)

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    if password_needs_rehash(user.password_hash):
        user.password_hash = await asyncio.to_thread(hash_password, password)
    for key in keys:
        await limiter.reset(key)
    token, user_session = await create_session(session, user, ip=ip, user_agent=user_agent)
    await log_event(
        session, actor=user, action="auth.login", entity_type="user", entity_id=str(user.id), ip=ip
    )
    return user, token, user_session


async def create_session(
    session: AsyncSession, user: User, *, ip: str | None = None, user_agent: str | None = None
) -> tuple[str, UserSession]:
    settings = get_settings()
    now = utcnow()
    token = new_token(32)
    user_session = UserSession(
        id=hash_token(token),
        user_id=user.id,
        csrf_token=new_csrf_token(),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(minutes=settings.session_ttl_min),
        absolute_expires_at=now + timedelta(days=settings.session_absolute_ttl_days),
        ip=ip,
        user_agent=(user_agent or "")[:300] or None,
    )
    session.add(user_session)
    await session.flush()
    return token, user_session


async def resolve_session(session: AsyncSession, token: str) -> tuple[User, UserSession] | None:
    """Sesion valida (no revocada, no vencida, usuario activo) o None. TTL deslizante."""
    if not token or len(token) > 200:
        return None
    stmt = (
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.id == hash_token(token))
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    user_session, user = row
    now = utcnow()
    if (
        user_session.revoked_at is not None
        or user_session.expires_at <= now
        or user_session.absolute_expires_at <= now
        or not user.is_active
    ):
        return None
    if now - user_session.last_seen_at > TOUCH_INTERVAL:
        settings = get_settings()
        user_session.last_seen_at = now
        user_session.expires_at = min(
            now + timedelta(minutes=settings.session_ttl_min), user_session.absolute_expires_at
        )
    return user, user_session


async def revoke_session(session: AsyncSession, token: str | None) -> None:
    if not token:
        return
    await session.execute(
        update(UserSession)
        .where(UserSession.id == hash_token(token), UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


async def revoke_user_sessions(
    session: AsyncSession, user_id: uuid.UUID, *, except_id: str | None = None
) -> None:
    stmt = update(UserSession).where(
        UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
    )
    if except_id:
        stmt = stmt.where(UserSession.id != except_id)
    await session.execute(stmt.values(revoked_at=utcnow()))


async def change_password(
    session: AsyncSession,
    user: User,
    *,
    current_password: str,
    new_password: str,
    keep_session_id: str | None = None,
) -> None:
    if not await asyncio.to_thread(verify_password, user.password_hash, current_password):
        raise AppError("invalid_credentials", "La contrasena actual no es correcta", 400)
    try:
        user.password_hash = await asyncio.to_thread(hash_password, new_password)
    except ValueError as exc:
        raise AppError("weak_password", str(exc), 422) from exc
    await revoke_user_sessions(session, user.id, except_id=keep_session_id)
    await log_event(
        session,
        actor=user,
        action="user.password_change",
        entity_type="user",
        entity_id=str(user.id),
    )


async def set_user_active(
    session: AsyncSession, actor: User, user_id: uuid.UUID, *, active: bool
) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError("not_found", "Usuario no encontrado", 404)
    if user.id == actor.id and not active:
        raise AppError("self_deactivate", "No puedes desactivarte a ti mismo", 400)
    user.is_active = active
    if not active:
        await revoke_user_sessions(session, user.id)
    await log_event(
        session,
        actor=actor,
        action="user.activate" if active else "user.deactivate",
        entity_type="user",
        entity_id=str(user.id),
    )
    return user


async def list_users(session: AsyncSession) -> list[User]:
    return list((await session.execute(select(User).order_by(User.created_at))).scalars().all())
