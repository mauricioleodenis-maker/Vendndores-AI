"""Redaccion de PII y datos de salud para logs y analitica."""

from __future__ import annotations

import re

from app.core.logging import redact_text

_ID_DOC = re.compile(
    r"(?i)\b(c\.?\s?c\.?|cedula|cédula|nit|ti|pasaporte)\b[\s:.#]*[\d][\d.\- ]{4,}\d"
)
_HEALTH = re.compile(
    r"(?i)\b(sufro de|padezco|me diagnosticaron|diagnostico de|alergia a|estoy embarazada)\b"
    r"[^.,;\n]{0,80}"
)
_LONG_DIGITS = re.compile(r"(?<!\w)\d{9,}(?!\w)")


def redact_pii(text: str) -> str:
    """Enmascara correos, telefonos, llaves, documentos de identidad y datos de salud."""
    if not text:
        return text
    out = redact_text(text)
    out = _ID_DOC.sub("[documento]", out)
    out = _HEALTH.sub(lambda m: f"{m.group(1)} [salud]", out)
    return _LONG_DIGITS.sub("[numero]", out)
