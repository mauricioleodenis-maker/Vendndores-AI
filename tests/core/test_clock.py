from datetime import UTC, datetime

from freezegun import freeze_time

from app.core.clock import ensure_utc, to_bogota, utcnow


@freeze_time("2026-01-15 15:00:00")
def test_utcnow_and_bogota():
    assert utcnow() == datetime(2026, 1, 15, 15, tzinfo=UTC)
    assert to_bogota(utcnow()).hour == 10
    assert to_bogota(datetime(2026, 1, 15, 15)).hour == 10


def test_ensure_utc():
    assert ensure_utc(datetime(2026, 1, 1)).tzinfo is UTC
