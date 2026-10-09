"""Valida normalize/scoring contra los casos etiquetados de tests/fixtures/data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.leads.normalize import to_e164_co
from app.leads.scoring import review_signal_scan

DATA = Path(__file__).resolve().parents[1] / "fixtures" / "data"
PHONES = json.loads((DATA / "phone_cases.json").read_text(encoding="utf-8"))["cases"]
REVIEWS = json.loads((DATA / "review_signal_cases.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", PHONES, ids=lambda c: f"ph{c['id']}")
def test_phone_cases(case: dict) -> None:
    e164, kind = to_e164_co(case["raw"])
    assert e164 == case["expected_e164"], case
    if case["expected_e164"] is not None:
        assert kind == case["phone_type"], case


@pytest.mark.parametrize("case", REVIEWS, ids=lambda c: c["id"])
def test_review_signal_cases(case: dict) -> None:
    detected = bool(review_signal_scan([case["text"]]))
    assert detected == case["pain_signal"], case
