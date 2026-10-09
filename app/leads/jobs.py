"""Jobs del modulo de leads (el worker descubre ``app.<pkg>.jobs``)."""

from __future__ import annotations

from arq.cron import cron

import app.leads.search  # noqa: F401  (registra leads.search_places)
from app.leads.secret_shop import secret_shop_timeout_job

CRON_JOBS = [
    cron(secret_shop_timeout_job, minute={0, 15, 30, 45}, name="leads.secret_shop_timeout"),
]
