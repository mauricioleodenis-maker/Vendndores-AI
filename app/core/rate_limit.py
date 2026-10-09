"""Rate limiting con backend en memoria (dev/test) y Redis (prod), misma interfaz."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app.core.config import get_settings
from app.core.errors import AppError


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: int  # segundos


class RateLimiter(Protocol):
    async def hit(self, key: str, *, limit: int, window_s: int) -> RateLimitResult: ...

    async def peek(self, key: str, *, limit: int, window_s: int) -> RateLimitResult: ...

    async def reset(self, key: str) -> None: ...


class MemoryRateLimiter:
    """Ventana deslizante en proceso."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, window_s: int) -> deque[float]:
        q = self._hits[key]
        cutoff = self._clock() - window_s
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def _result(self, q: deque[float], limit: int, window_s: int, allowed: bool) -> RateLimitResult:
        retry = 0 if allowed or not q else max(1, int(q[0] + window_s - self._clock()) + 1)
        return RateLimitResult(allowed, max(0, limit - len(q)), retry)

    async def hit(self, key: str, *, limit: int, window_s: int) -> RateLimitResult:
        q = self._prune(key, window_s)
        if len(q) >= limit:
            return self._result(q, limit, window_s, False)
        q.append(self._clock())
        return self._result(q, limit, window_s, True)

    async def peek(self, key: str, *, limit: int, window_s: int) -> RateLimitResult:
        q = self._prune(key, window_s)
        return self._result(q, limit, window_s, len(q) < limit)

    async def reset(self, key: str) -> None:
        self._hits.pop(key, None)


class RedisRateLimiter:
    """Ventana fija con INCR + EXPIRE."""

    def __init__(self, client: object) -> None:
        self._r = client

    def _k(self, key: str) -> str:
        return f"vai:rl:{key}"

    async def hit(self, key: str, *, limit: int, window_s: int) -> RateLimitResult:
        r = self._r
        k = self._k(key)
        count = await r.incr(k)  # type: ignore[attr-defined]
        if count == 1:
            await r.expire(k, window_s)  # type: ignore[attr-defined]
        ttl = await r.ttl(k)  # type: ignore[attr-defined]
        if ttl is None or ttl < 0:
            await r.expire(k, window_s)  # type: ignore[attr-defined]
            ttl = window_s
        allowed = count <= limit
        return RateLimitResult(allowed, max(0, limit - count), 0 if allowed else int(ttl))

    async def peek(self, key: str, *, limit: int, window_s: int) -> RateLimitResult:
        r = self._r
        raw = await r.get(self._k(key))  # type: ignore[attr-defined]
        count = int(raw or 0)
        ttl = await r.ttl(self._k(key)) if count else 0  # type: ignore[attr-defined]
        allowed = count < limit
        return RateLimitResult(allowed, max(0, limit - count), 0 if allowed else max(int(ttl), 1))

    async def reset(self, key: str) -> None:
        await self._r.delete(self._k(key))  # type: ignore[attr-defined]


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        url = get_settings().redis_url
        if url:
            import redis.asyncio as aioredis

            _limiter = RedisRateLimiter(aioredis.from_url(url, decode_responses=True))  # type: ignore[no-untyped-call]
        else:
            _limiter = MemoryRateLimiter()
    return _limiter


def set_rate_limiter(limiter: RateLimiter | None) -> None:
    """Reemplaza el limitador global (tests / arranque)."""
    global _limiter
    _limiter = limiter


async def enforce(key: str, *, limit: int, window_s: int) -> None:
    """Consume un intento o lanza 429."""
    res = await get_rate_limiter().hit(key, limit=limit, window_s=window_s)
    if not res.allowed:
        raise AppError("rate_limited", "Demasiadas solicitudes. Intenta mas tarde.", 429)
