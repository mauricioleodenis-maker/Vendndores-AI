"""Mundo agencia: fuentes, leads, senales de resenas, eventos y prueba secreta."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, TimestampMixin, UTCDateTime, enum_check, utcnow, uuid_pk

LEAD_STAGES = ("nuevo", "prueba_secreta", "contactado", "demo", "cerrado")
LEAD_DISPOSITIONS = ("activo", "perdido", "no_contactar")
LEAD_EVENT_KINDS = (
    "created",
    "imported",
    "scored",
    "stage_changed",
    "note",
    "outreach_sent",
    "reply_received",
    "opt_out",
    "secret_shop_sent",
    "secret_shop_replied",
    "demo_created",
    "converted",
)
SHOP_OUTCOMES = ("sin_respuesta", "respuesta_lenta", "respuesta_ok", "respuesta_con_cierre")


class LeadSource(TimestampMixin, Base):
    __tablename__ = "lead_sources"
    __table_args__ = (enum_check("kind", ("places_api", "csv_import", "manual")),)

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    params: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    stats: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class Lead(TimestampMixin, Base):
    __tablename__ = "leads"
    __table_args__ = (
        enum_check("stage", LEAD_STAGES),
        enum_check("disposition", LEAD_DISPOSITIONS),
        enum_check("phone_type", ("mobile", "landline", "unknown"), nullable=True),
        Index(
            "uq_leads_phone_hash_active",
            "phone_hash",
            unique=True,
            sqlite_where=text("phone_hash IS NOT NULL AND disposition <> 'perdido'"),
            postgresql_where=text("phone_hash IS NOT NULL AND disposition <> 'perdido'"),
        ),
        Index("ix_leads_stage_score", "stage", "score"),
        Index("ix_leads_niche_city", "niche", "city"),
        Index("ix_leads_name_key", "name_key"),
        Index("ix_leads_website_domain", "website_domain"),
        Index("ix_leads_owner_user_id", "owner_user_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("lead_sources.id", ondelete="SET NULL"), nullable=True
    )
    place_id: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    maps_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    name_key: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    niche: Mapped[str] = mapped_column(String(30), nullable=False, default="otro")
    city: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    address: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    phone_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    website_domain: Mapped[str | None] = mapped_column(String(200), nullable=True)
    instagram: Mapped[str | None] = mapped_column(String(300), nullable=True)
    instagram_handle: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(2, 1), nullable=True)
    review_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    business_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    price_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    hours: Mapped[Any] = mapped_column(JSONType, nullable=True)
    signals: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    score_breakdown: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    score_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tags: Mapped[Any] = mapped_column(JSONType, nullable=False, default=list)
    stage: Mapped[str] = mapped_column(String(20), nullable=False, default="nuevo")
    disposition: Mapped[str] = mapped_column(String(20), nullable=False, default="activo")
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    converted_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True
    )
    demo_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # instrucciones del operador para el bot demo de este lead (tono, que resaltar, que evitar)
    demo_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_contacted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    next_action_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ReviewSignal(TimestampMixin, Base):
    __tablename__ = "review_signals"
    __table_args__ = (enum_check("source", ("places", "csv")),)

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="places")
    review_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    rating: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    text_excerpt: Mapped[str] = mapped_column(String(280), nullable=False, default="")
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    matched_patterns: Mapped[Any] = mapped_column(JSONType, nullable=False, default=list)


class LeadEvent(Base):
    """Timeline append-only del lead."""

    __tablename__ = "lead_events"
    __table_args__ = (
        enum_check("kind", LEAD_EVENT_KINDS),
        Index("ix_lead_events_lead_ts", "lead_id", "ts"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    data: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)


class SecretShopTest(TimestampMixin, Base):
    __tablename__ = "secret_shop_tests"
    __table_args__ = (
        enum_check("channel", ("whatsapp", "instagram", "llamada", "web_form")),
        enum_check("scenario", ("precio", "cita", "horario", "urgencia")),
        enum_check("outcome", SHOP_OUTCOMES, nullable=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="whatsapp")
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    scenario: Mapped[str] = mapped_column(String(20), nullable=False, default="precio")
    message_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    first_reply_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    first_reply_excerpt: Mapped[str] = mapped_column(String(280), nullable=False, default="")
    response_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    after_hours: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


class DemoWhatsappSession(TimestampMixin, Base):
    """Quien escribio ``DEMO-<slug>`` al numero de demo de la agencia: a que demo habla."""

    __tablename__ = "demo_whatsapp_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    history: Mapped[Any] = mapped_column(JSONType, nullable=False, default=list)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
