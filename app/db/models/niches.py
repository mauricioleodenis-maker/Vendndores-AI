"""Plantillas de nicho (el YAML es la fuente; la tabla es opcional/cache)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, TimestampMixin, uuid_pk


class NicheTemplate(TimestampMixin, Base):
    __tablename__ = "niche_templates"

    id: Mapped[uuid.UUID] = uuid_pk()
    niche: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
