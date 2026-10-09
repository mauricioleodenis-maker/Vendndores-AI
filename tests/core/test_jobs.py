import asyncio
from datetime import timedelta

from app.core import jobs


async def test_register_and_enqueue_in_test_mode_defers_until_run():
    calls = []

    @jobs.register_job("t.job")
    async def job(ctx, a, b=0):
        calls.append((a, b))

    assert jobs.JOB_REGISTRY["t.job"] is job
    await jobs.enqueue("t.job", 1, b=2, _defer_by=timedelta(seconds=5), _job_id="x")
    assert calls == [] and len(jobs.ENQUEUED) == 1
    assert await jobs.run_enqueued() == 1
    assert calls == [(1, 2)] and jobs.ENQUEUED == []


async def test_failing_job_does_not_raise():
    @jobs.register_job("t.fail")
    async def boom(ctx):
        raise RuntimeError("x")

    await jobs.enqueue("t.fail")
    await jobs.run_enqueued()


async def test_unregistered_job_is_logged_not_raised():
    await jobs.enqueue("t.missing")
    await jobs.run_enqueued()


async def test_dev_fallback_runs_in_background(monkeypatch):
    monkeypatch.setenv("VAI_ENV", "dev")
    from app.core.config import get_settings

    get_settings.cache_clear()
    done = asyncio.Event()

    @jobs.register_job("t.bg")
    async def bg(ctx):
        done.set()

    await jobs.enqueue("t.bg")
    await asyncio.wait_for(done.wait(), 1)


async def test_redis_path_uses_arq_pool(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setenv("VAI_REDIS_URL", "redis://localhost:6379/0")
    from app.core.config import get_settings

    get_settings.cache_clear()
    pool = AsyncMock()
    monkeypatch.setattr(jobs, "_arq_pool", pool)
    await jobs.enqueue("t.x", 1, _job_id="j1")
    pool.enqueue_job.assert_awaited_once_with("t.x", 1, _defer_by=None, _job_id="j1")
