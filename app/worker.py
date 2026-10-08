"""Worker arq. Descubre jobs de ``app.<pkg>.jobs`` (funciones con ``@register_job``).

Cada modulo con jobs define ``app/<pkg>/jobs.py``; opcionalmente expone ``CRON_JOBS: list[CronJob]``
(``arq.cron.cron(...)``). Este archivo no necesita editarse para agregar jobs.
"""

from __future__ import annotations

import importlib
from typing import Any

from arq.connections import RedisSettings
from arq.cron import CronJob
from arq.worker import func

from app.core.config import get_settings
from app.core.jobs import JOB_REGISTRY
from app.core.logging import configure_logging, get_logger
from app.db.session import configure_database, dispose_engine, make_engine

JOB_PACKAGES: tuple[str, ...] = (
    "tenants",
    "plans",
    "billing",
    "scraping",
    "factory",
    "conversation",
    "privacy",
    "channels",
    "booking",
    "reminders",
    "leads",
    "outreach",
    "dashboard",
    "audit",
)

log = get_logger(__name__)


def load_job_modules() -> list[CronJob]:
    crons: list[CronJob] = []
    for pkg in JOB_PACKAGES:
        modname = f"app.{pkg}.jobs"
        try:
            module = importlib.import_module(modname)
        except ModuleNotFoundError as exc:
            if exc.name in {modname, f"app.{pkg}"}:
                continue
            raise
        crons.extend(getattr(module, "CRON_JOBS", []))
    return crons


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    configure_database(make_engine())
    log.info("worker_started", jobs=sorted(JOB_REGISTRY))


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()


_crons = load_job_modules()


class WorkerSettings:
    functions = [func(fn, name=name) for name, fn in JOB_REGISTRY.items()]
    cron_jobs = _crons
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url or "redis://localhost:6379")
    max_jobs = 10
    job_timeout = 300
    keep_result = 3600
