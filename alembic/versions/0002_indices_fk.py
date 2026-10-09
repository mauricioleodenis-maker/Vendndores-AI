"""indices en llaves foraneas consultadas (joins y borrados en cascada)

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_appointments_tenant_contact", "appointments", ("tenant_id", "contact_id")),
    ("ix_handoffs_conversation_id", "handoffs", ("conversation_id",)),
    ("ix_scheduled_jobs_appointment_id", "scheduled_jobs", ("appointment_id",)),
    ("ix_scheduled_jobs_contact_id", "scheduled_jobs", ("contact_id",)),
    ("ix_campaign_targets_lead_id", "campaign_targets", ("lead_id",)),
    ("ix_leads_owner_user_id", "leads", ("owner_user_id",)),
)


def upgrade() -> None:
    for name, table, cols in _INDEXES:
        op.create_index(name, table, list(cols), unique=False)


def downgrade() -> None:
    for name, table, _cols in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
