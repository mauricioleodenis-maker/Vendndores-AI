from unittest.mock import AsyncMock

import pytest

from app.core.errors import AppError
from app.core.rate_limit import (
    MemoryRateLimiter,
    RedisRateLimiter,
    enforce,
    get_rate_limiter,
    set_rate_limiter,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def test_memory_sliding_window():
    clock = Clock()
    rl = MemoryRateLimiter(clock)
    for i in range(3):
        assert (await rl.hit("k", limit=3, window_s=60)).allowed
    blocked = await rl.hit("k", limit=3, window_s=60)
    assert not blocked.allowed and blocked.retry_after > 0 and blocked.remaining == 0
    clock.t += 61
    assert (await rl.hit("k", limit=3, window_s=60)).allowed


async def test_peek_does_not_consume_and_reset():
    rl = MemoryRateLimiter(Clock())
    await rl.hit("k", limit=1, window_s=60)
    assert not (await rl.peek("k", limit=1, window_s=60)).allowed
    await rl.reset("k")
    assert (await rl.peek("k", limit=1, window_s=60)).allowed
    assert (await rl.peek("other", limit=1, window_s=60)).remaining == 1


async def test_keys_isolated():
    rl = MemoryRateLimiter(Clock())
    await rl.hit("a", limit=1, window_s=60)
    assert (await rl.hit("b", limit=1, window_s=60)).allowed


async def test_enforce_raises_429():
    set_rate_limiter(MemoryRateLimiter(Clock()))
    await enforce("x", limit=1, window_s=10)
    with pytest.raises(AppError) as exc:
        await enforce("x", limit=1, window_s=10)
    assert exc.value.status == 429


def test_default_backend_is_memory():
    assert isinstance(get_rate_limiter(), MemoryRateLimiter)


async def test_redis_backend_with_fake_client():
    r = AsyncMock()
    r.incr.side_effect = [1, 2, 3]
    r.ttl.return_value = 30
    rl = RedisRateLimiter(r)
    assert (await rl.hit("k", limit=2, window_s=60)).allowed
    r.expire.assert_awaited_once_with("vai:rl:k", 60)
    assert (await rl.hit("k", limit=2, window_s=60)).allowed
    res = await rl.hit("k", limit=2, window_s=60)
    assert not res.allowed and res.retry_after == 30
    r.get.return_value = "5"
    assert not (await rl.peek("k", limit=2, window_s=60)).allowed
    await rl.reset("k")
    r.delete.assert_awaited_with("vai:rl:k")


async def test_memory_sweep_drops_expired_keys():
    now = [0.0]
    lim = MemoryRateLimiter(clock=lambda: now[0])
    for i in range(50):
        await lim.hit(f"k{i}", limit=1, window_s=10)
    now[0] = 100.0
    lim._sweep()
    assert not lim._hits


async def test_enforce_falls_back_to_memory_when_redis_fails():
    import app.core.rate_limit as rl

    class Broken:
        async def incr(self, *_a):
            raise ConnectionError("down")

    rl.set_rate_limiter(rl.RedisRateLimiter(Broken()))
    rl._fallback = None
    try:
        await rl.enforce("fb", limit=1, window_s=60)
        with pytest.raises(rl.AppError):
            await rl.enforce("fb", limit=1, window_s=60)
    finally:
        rl.set_rate_limiter(None)
        rl._fallback = None
