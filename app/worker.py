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

# Modulos que registran jobs fuera de ``jobs.py`` (se importan para poblar JOB_REGISTRY).
EXTRA_JOB_MODULES: tuple[str, ...] = (
    "app.tenants.service",
    "app.conversation.memory",
)

# Cron esperados por el plan (recordatorios, retencion, prueba secreta, campanas, facturacion).
# Cada duenio los declara en su ``jobs.py``; aqui solo se avisa si falta alguno.
EXPECTED_CRON_NAMES: tuple[str, ...] = (
    "reminders.enqueue_due",
    "privacy.purge_retention",
    "leads.secret_shop_timeout",
    "outreach.dispatch",
    "billing.generate_monthly_records",
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
    for modname in EXTRA_JOB_MODULES:
        try:
            importlib.import_module(modname)
        except ModuleNotFoundError as exc:
            if not modname.startswith(exc.name or "\0"):
                raise
    return _dedupe_crons(crons)


def _dedupe_crons(crons: list[CronJob]) -> list[CronJob]:
    seen: dict[str, CronJob] = {}
    for job in crons:
        seen.setdefault(job.name, job)
    return list(seen.values())


def missing_expected_crons(crons: list[CronJob]) -> list[str]:
    names = {c.name for c in crons}
    return [n for n in EXPECTED_CRON_NAMES if n not in names]


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    configure_database(make_engine())
    missing = missing_expected_crons(_crons)
    if missing and get_settings().env == "prod":
        raise RuntimeError(f"Faltan crons esperados en el worker: {', '.join(missing)}")
    log.info(
        "worker_started",
        jobs=sorted(JOB_REGISTRY),
        crons=sorted(c.name for c in _crons),
        missing_crons=missing,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()


_crons = load_job_modules()


def resolve_redis_dsn() -> str:
    settings = get_settings()
    if settings.redis_url:
        return settings.redis_url
    if settings.env == "prod":
        raise RuntimeError("REDIS_URL es obligatorio para el worker en produccion")
    return "redis://localhost:6379"


class WorkerSettings:
    functions = [func(fn, name=name) for name, fn in JOB_REGISTRY.items()]
    cron_jobs = _crons
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(resolve_redis_dsn())
    max_jobs = 10
    job_timeout = 300
    keep_result = 3600
