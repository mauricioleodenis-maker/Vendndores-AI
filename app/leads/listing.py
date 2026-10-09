"""Consultas de la UI de leads: filtros, kanban, timeline, notas, cambio de etapa y subidas CSV."""

from __future__ import annotations

import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.core.errors import AppError, NotFoundError
from app.db.models.leads import LEAD_DISPOSITIONS, LEAD_STAGES, Lead, LeadEvent
from app.db.models.users import User
from app.leads.scoring import DEFAULT

PAGE_SIZE = 25
KANBAN_COLUMN_LIMIT = 40
BANDS = ("caliente", "tibio", "frio")
MAX_NOTE_CHARS = 2000
UPLOAD_TTL_S = 3600
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class LeadFilters:
    q: str = ""
    niche: str = ""
    city: str = ""
    stage: str = ""
    band: str = ""
    disposition: str = ""
    min_score: int = 0
    page: int = 1

    def query_string(self) -> str:
        pairs = [
            (k, v)
            for k, v in (
                ("q", self.q),
                ("niche", self.niche),
                ("city", self.city),
                ("stage", self.stage),
                ("band", self.band),
                ("disposition", self.disposition),
                ("min_score", self.min_score or ""),
            )
            if v not in ("", None)
        ]
        from urllib.parse import urlencode

        return urlencode(pairs)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _conditions(f: LeadFilters) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = []
    if f.q:
        like = f"%{_escape_like(f.q.strip().lower())}%"
        conds.append(
            func.lower(Lead.name).like(like, escape="\\")
            | func.lower(Lead.address).like(like, escape="\\")
            | func.lower(Lead.website_domain).like(like, escape="\\")
        )
    if f.niche:
        conds.append(Lead.niche == f.niche)
    if f.city:
        conds.append(func.lower(Lead.city) == f.city.lower())
    if f.stage in LEAD_STAGES:
        conds.append(Lead.stage == f.stage)
    if f.disposition in LEAD_DISPOSITIONS:
        conds.append(Lead.disposition == f.disposition)
    if f.band == "caliente":
        conds.append(Lead.score >= DEFAULT.hot)
    elif f.band == "tibio":
        conds.append((Lead.score >= DEFAULT.warm) & (Lead.score < DEFAULT.hot))
    elif f.band == "frio":
        conds.append(Lead.score < DEFAULT.warm)
    if f.min_score > 0:
        conds.append(Lead.score >= f.min_score)
    return conds


async def query_leads(
    session: AsyncSession, f: LeadFilters, *, page_size: int = PAGE_SIZE
) -> tuple[list[Lead], bool]:
    """Pagina ordenada por score desc; devuelve (filas, hay_mas)."""
    page = max(1, f.page)
    stmt = (
        select(Lead)
        .where(*_conditions(f))
        .order_by(Lead.score.desc(), Lead.created_at.desc(), Lead.id)
        .offset((page - 1) * page_size)
        .limit(page_size + 1)
    )
    rows = list((await session.execute(stmt)).scalars())
    return rows[:page_size], len(rows) > page_size


async def all_matching(session: AsyncSession, f: LeadFilters, *, limit: int = 5000) -> list[Lead]:
    stmt = (
        select(Lead)
        .where(*_conditions(f))
        .order_by(Lead.score.desc(), Lead.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


async def distinct_values(session: AsyncSession) -> dict[str, list[str]]:
    niches = (await session.execute(select(Lead.niche).distinct().order_by(Lead.niche))).scalars()
    cities = (
        await session.execute(
            select(Lead.city).where(Lead.city != "").distinct().order_by(Lead.city)
        )
    ).scalars()
    return {"niches": list(niches), "cities": list(cities)}


async def kanban_columns(session: AsyncSession, f: LeadFilters) -> dict[str, dict[str, Any]]:
    """Leads activos por etapa (max ``KANBAN_COLUMN_LIMIT`` por columna) y total real."""
    base = [c for c in _conditions(f)] + [Lead.disposition == "activo"]
    rows = await session.execute(select(Lead.stage, func.count()).where(*base).group_by(Lead.stage))
    totals = {stage: int(n) for stage, n in rows.all()}
    out: dict[str, dict[str, Any]] = {}
    for stage in LEAD_STAGES:
        stmt = (
            select(Lead)
            .where(*base, Lead.stage == stage)
            .order_by(Lead.score.desc(), Lead.created_at.desc())
            .limit(KANBAN_COLUMN_LIMIT)
        )
        out[stage] = {
            "leads": list((await session.execute(stmt)).scalars()),
            "total": int(totals.get(stage, 0)),
        }
    return out


async def get_lead(session: AsyncSession, lead_id: uuid.UUID) -> Lead:
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise NotFoundError("Lead no encontrado")
    return lead


async def timeline(
    session: AsyncSession, lead_id: uuid.UUID, *, limit: int = 200
) -> list[LeadEvent]:
    stmt = (
        select(LeadEvent)
        .where(LeadEvent.lead_id == lead_id)
        .order_by(LeadEvent.ts.desc(), LeadEvent.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


async def move_stage(session: AsyncSession, lead: Lead, stage: str, actor: User) -> bool:
    """Cambia la etapa y deja evento + auditoria. Devuelve False si no habia cambio."""
    if stage not in LEAD_STAGES:
        raise AppError("invalid_stage", "Etapa invalida", 422)
    if lead.stage == stage:
        return False
    previous = lead.stage
    lead.stage = stage
    session.add(
        LeadEvent(
            lead_id=lead.id,
            actor_user_id=actor.id,
            kind="stage_changed",
            data={"from": previous, "to": stage},
        )
    )
    await audit.log_event(
        session,
        actor=actor,
        action="lead.stage_changed",
        entity_type="lead",
        entity_id=lead.id,
        diff={"stage": [previous, stage]},
    )
    await session.flush()
    return True


async def add_note(session: AsyncSession, lead: Lead, text: str, actor: User) -> LeadEvent:
    text = text.strip()
    if not text:
        raise AppError("empty_note", "Escribe una nota", 422)
    event = LeadEvent(
        lead_id=lead.id,
        actor_user_id=actor.id,
        kind="note",
        data={"text": text[:MAX_NOTE_CHARS]},
    )
    session.add(event)
    await session.flush()
    return event


# --------------------------------------------------------------------------- subidas CSV (preview)
def upload_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "vai-csv-import"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _purge_old_uploads(directory: Path) -> None:
    cutoff = time.time() - UPLOAD_TTL_S
    for item in directory.glob("*.csv"):
        try:
            if item.stat().st_mtime < cutoff:
                item.unlink()
        except OSError:
            continue


def stash_upload(data: bytes, sha256: str) -> None:
    """Guarda el CSV de la vista previa para el commit (nombre = sha256 validado, modo 0600)."""
    if not _SHA_RE.match(sha256):
        raise AppError("invalid_upload", "Archivo invalido", 422)
    directory = upload_dir()
    _purge_old_uploads(directory)
    target = directory / f"{sha256}.csv"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def load_upload(sha256: str) -> bytes:
    from app.leads.csv_import import file_sha256

    if not _SHA_RE.match(sha256):
        raise AppError("invalid_upload", "Archivo invalido", 422)
    target = upload_dir() / f"{sha256}.csv"
    try:
        data = target.read_bytes()
    except OSError:
        raise AppError(
            "upload_expired", "La vista previa expiro. Sube el archivo de nuevo.", 410
        ) from None
    if file_sha256(data) != sha256:
        raise AppError("invalid_upload", "Archivo alterado", 422)
    return data


def discard_upload(sha256: str) -> None:
    if _SHA_RE.match(sha256):
        (upload_dir() / f"{sha256}.csv").unlink(missing_ok=True)
