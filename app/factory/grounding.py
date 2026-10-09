"""Verificacion de fundamento: precios y horarios deben venir del dueno o del sitio scrapeado."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.factory.schemas import GeneratedBotConfig

# Monto = numero con separador de miles, cifra de 4+ digitos, o 1-3 digitos con marca de moneda.
_NUM_RE = re.compile(
    r"(?<![\d.,'])(?P<cur>\$\s*)?(?P<num>\d{1,3}(?:[.,']\d{3})+|\d+)(?![\d])"
    r"(?P<suf>\s*(?:cop\b|pesos\b))?",
    re.I,
)
_MIL_RE = re.compile(r"(\d{1,4})(?:[.,](\d))?\s*(?:mil\b|k\b)", re.I)
_INJECTION_RE = re.compile(
    r"ignor[ae]\s+(?:todas?\s+)?(?:las\s+)?instrucciones|ignore\s+(?:all\s+)?(?:previous|prior)|"
    r"system\s*prompt|eres\s+ahora|you\s+are\s+now|olvida\s+(?:tus|las)\s+reglas|"
    r"act[uú]a\s+como|revela\s+tu\s+prompt",
    re.I,
)


def fold(text: str) -> str:
    """Minusculas sin tildes (para comparar nombres)."""
    nfd = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").strip()


def extract_amounts(text: str) -> set[int]:
    """Todos los montos enteros que aparecen en el texto ($80.000, 80,000, 80 mil, 80k)."""
    found: set[int] = set()
    for m in _NUM_RE.finditer(text):
        digits = re.sub(r"[.,']", "", m.group("num"))
        if not digits.isdigit() or len(digits) > 9:
            continue
        # Cifras cortas (direcciones, telefonos parciales, horas) solo cuentan con marca de moneda.
        if len(digits) <= 3 and not (m.group("cur") or m.group("suf")):
            continue
        found.add(int(digits))
    for m in _MIL_RE.finditer(text):
        base = int(m.group(1)) * 1000 + (int(m.group(2)) * 100 if m.group(2) else 0)
        found.add(base)
    return found


def _time_patterns(hhmm: str) -> list[re.Pattern[str]]:
    h, mm = int(hhmm[:2]), hhmm[3:]
    h12 = h % 12 or 12
    suffix = r"\s*(?:a\.?\s?m\.?|p\.?\s?m\.?|am|pm)"
    pats = [rf"(?<!\d){h}:{mm}(?!\d)", rf"(?<!\d){h:02d}:{mm}(?!\d)"]
    if mm == "00":
        pats += [rf"(?<!\d){h12}{suffix}(?![a-z0-9])", rf"(?<!\d){h}\s?h(?:oras)?\b"]
    else:
        pats.append(rf"(?<!\d){h12}:{mm}{suffix}")
    if mm == "00":
        pats.append(rf"(?<!\d){h12}:00{suffix}")
    return [re.compile(p, re.I) for p in pats]


def time_in_text(hhmm: str, text: str) -> bool:
    return any(p.search(text) for p in _time_patterns(hhmm))


def has_injection(text: str) -> bool:
    return bool(_INJECTION_RE.search(text))


@dataclass(slots=True)
class GroundingReport:
    """Resultado: indices/ids de elementos que el humano debe confirmar."""

    service_ids: set[str] = field(default_factory=set)
    faq_indexes: set[int] = field(default_factory=set)
    hours_ungrounded: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return bool(self.service_ids or self.faq_indexes or self.hours_ungrounded)

    def as_dict(self) -> dict[str, Any]:
        return {
            "service_ids": sorted(self.service_ids),
            "faq_indexes": sorted(self.faq_indexes),
            "hours_ungrounded": self.hours_ungrounded,
            "reasons": self.reasons,
        }


def verify_grounding(
    config: GeneratedBotConfig,
    *,
    owner_text: str,
    owner_prices: dict[str, int | None],
    source_text: str,
) -> GroundingReport:
    """Marca precios/horarios/FAQs con cifras que no estan en las fuentes (dueno o web).

    ``owner_prices``: nombre normalizado (``fold``) -> precio declarado por el dueno.
    """
    report = GroundingReport()
    haystack = f"{owner_text}\n{source_text}"
    amounts = extract_amounts(haystack) | {p for p in owner_prices.values() if p is not None}
    for svc in config.services:
        if svc.price_cop is None:
            continue
        declared = owner_prices.get(fold(svc.name))
        if declared is not None and declared == svc.price_cop:
            continue
        if svc.price_cop in amounts:
            continue
        report.service_ids.add(svc.id)
        report.reasons.append(f"Precio sin fuente: {svc.name}")
    for day, ranges in config.hours.weekly.items():
        for r in ranges:
            if not (time_in_text(r.open, haystack) and time_in_text(r.close, haystack)):
                report.hours_ungrounded.append(day)
                break
    if report.hours_ungrounded:
        report.reasons.append("Horarios sin fuente: " + ", ".join(report.hours_ungrounded))
    for i, faq in enumerate(config.faqs):
        cited = extract_amounts(faq.answer)
        if any(c >= 1000 and not 1900 <= c <= 2100 and c not in amounts for c in cited):
            report.faq_indexes.add(i)
            report.reasons.append(f"FAQ con cifra sin fuente: {faq.question[:60]}")
    return report
