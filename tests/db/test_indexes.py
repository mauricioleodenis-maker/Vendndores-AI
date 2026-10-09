"""Las FK consultadas deben tener indice propio (joins y borrados en cascada)."""

import app.db.models  # noqa: F401
from app.db.base import Base


def _leading(table: str) -> set[str]:
    t = Base.metadata.tables[table]
    return {list(i.columns)[0].name for i in t.indexes}


def test_fk_indexes_present():
    expected = {
        "appointments": "tenant_id",
        "handoffs": "conversation_id",
        "scheduled_jobs": "appointment_id",
        "campaign_targets": "lead_id",
        "leads": "owner_user_id",
    }
    for table, col in expected.items():
        assert col in _leading(table), (table, col)
