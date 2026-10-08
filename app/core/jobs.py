"""Registro y encolado de jobs (arq + Redis en prod; fallback en proceso en dev/test).

Una funcion de job es ``async def fn(ctx: dict, *args, **kwargs)`` y se puede llamar directo en
tests. En ``env=test`` los jobs NO se ejecutan: se acumulan en ``ENQUEUED`` y se corren con
``await run_enqueued()``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

JobFn = Callable[..., Awaitable[Any]]
JOB_REGISTRY: dict[str, JobFn] = {}
ENQUEUED: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
_tasks: set[asyncio.Task[None]] = set()
_arq_pool: Any = None


def register_job(name: str) -> Callable[[JobFn], JobFn]:
    def deco(fn: JobFn) -> JobFn:
        JOB_REGISTRY[name] = fn
        return fn

    return deco


async def _run_local(
    job_name: str, args: tuple[Any, ...], kwargs: dict[str, Any], delay_s: float
) -> None:
    if delay_s > 0:
        await asyncio.sleep(delay_s)
    fn = JOB_REGISTRY.get(job_name)
    if fn is None:
        log.error("job_not_registered", job=job_name)
        return
    try:
        await fn({}, *args, **kwargs)
    except Exception as exc:
        log.error("job_failed", job=job_name, error_type=type(exc).__name__)


async def run_enqueued() -> int:
    """Ejecuta (y vacia) los jobs acumulados en modo test. Devuelve cuantos corrieron."""
    pending = list(ENQUEUED)
    ENQUEUED.clear()
    for name, args, kwargs in pending:
        await _run_local(name, args, kwargs, 0)
    return len(pending)


async def _get_arq_pool() -> Any:
    global _arq_pool
    if _arq_pool is None:
        from arq import create_pool
        from arq.connections import RedisSettings

        _arq_pool = await create_pool(RedisSettings.from_dsn(str(get_settings().redis_url)))
    return _arq_pool


async def enqueue(
    job_name: str,
    *args: Any,
    _defer_by: timedelta | None = None,
    _job_id: str | None = None,
    **kwargs: Any,
) -> None:
    settings = get_settings()
    if settings.redis_url:
        pool = await _get_arq_pool()
        await pool.enqueue_job(job_name, *args, _defer_by=_defer_by, _job_id=_job_id, **kwargs)
        return
    if settings.env == "test":
        ENQUEUED.append((job_name, args, kwargs))
        return
    delay = _defer_by.total_seconds() if _defer_by else 0.0
    task = asyncio.create_task(_run_local(job_name, args, kwargs, delay))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
